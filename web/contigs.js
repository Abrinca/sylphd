// sylphd "Per contig" sub-mode of the Assembly tab: sketch every FASTA/FASTQ record on its own
// (WebAssembly, in Web Workers), send the sketches to POST /contigs in batches, and show the
// per-contig classification plus the taxonomic consensus. Plain JS, no dependencies.
//
// The transport mirrors src/sylphd/per_contig_client.py (batching by the server's limits and
// merging the per-request summaries); everything scientific happens on the server. The table and
// JSON writers reproduce the CLI byte for byte (the columns of `sylphd assembly --per-contig`).
//
// All server/user strings are inserted with textContent / text nodes only.
(() => {
  'use strict';

  /* ---------------------------------------------------------------- format */
  // Pure writers and summary merging. Also exported through module.exports so
  // tests/test_web_contigs_download.py can compare them with the Python client.
  const Format = (() => {
    const COLUMNS = [
      'contig', 'contig_length', 'gc', 'status', 'rank', 'ani', 'shared_kmers', 'contig_kmers',
      'explained_fraction', 'reference_ani', 'relation', 'shared_rank', 'ani_species_level',
      'accession', 'genome_id', 'gtdb_species', 'taxon', 'n_close', 'consensus_rank',
      'consensus_taxon',
    ];
    // The query settings sent with every /contigs request (per_contig.QUERY_PARAMS).
    const QUERY_PARAMS = {
      no_adjust: true, minimum_ani: 0.0, screen_ani: 0.0, min_contain: 3, min_number_kmers: 10,
    };
    const DEFAULT_MIN_LENGTH = 20000;
    const DEFAULT_TOP = 1;
    const DEFAULT_MIN_ANI = 80.0;
    const DEFAULT_CONSENSUS_RATIO = 1.5;
    const DEFAULT_CONSENSUS_Z = 2.0;
    const DEFAULT_PARAMS_BUDGET = 900 * 1024;
    //: Most contigs the browser classifies per run (the 100 longest sendable ones).
    const MAX_CLASSIFIED = 100;

    // Python's f"{x:.2f}" / round(x, 2) round the exact binary value, ties to
    // even. toFixed() agrees except on an exact tie, where it rounds up; detect
    // that case from the exact decimal expansion and truncate to the even digit.
    function exactTieDigits(x, digits) {
      const text = Number(x).toFixed(20);
      const dot = text.indexOf('.');
      if (dot < 0 || text.length < dot + digits + 2) return null;
      if (text[dot + 1 + digits] !== '5') return null;
      if (text.slice(dot + 2 + digits).replace(/0+$/, '') !== '') return null;
      return { text, dot };
    }
    function fixed(x, digits) {
      const value = Number(x);
      if (!Number.isFinite(value)) return String(value);
      const tie = exactTieDigits(value, digits);
      if (tie) {
        const last = Number(tie.text[tie.dot + digits]);
        if (last % 2 === 0) return tie.text.slice(0, tie.dot + digits + 1);
      }
      return value.toFixed(digits);
    }
    const roundHalfEven = (x, digits) => Number(fixed(x, digits));

    // str(value) of Python for the metadata columns: None -> "", bool -> True/False.
    function pyStr(value) {
      if (value == null) return '';
      if (typeof value === 'boolean') return value ? 'True' : 'False';
      return String(value);
    }
    // Columns the server stores as REAL: JSON.parse turns 100.0 into 100, while
    // Python's str(100.0) is "100.0".
    const FLOAT_FIELDS = new Set(['checkm2_completeness', 'checkm2_contamination', 'gc_percentage']);
    function fieldStr(column, value) {
      if (value == null) return '';
      if (typeof value === 'number' && FLOAT_FIELDS.has(column) && Number.isInteger(value)) {
        return `${value}.0`;
      }
      return pyStr(value);
    }

    function isLostEvidence(record) {
      if (record.status === 'too_few_kmers') return true;
      return record.status === 'no_hit' && Object.prototype.hasOwnProperty.call(record, 'reason');
    }

    function breakdown(bases, queriedBp, top) {
      const items = [...bases.values()].map((item) => ({
        rank: item.rank,
        taxon: item.taxon,
        bp: item.bp,
        // Python round(bp / queried_bp, 4)
        fraction: queriedBp ? roundHalfEven(item.bp / queriedBp, 4) : 0.0,
      }));
      items.sort((a, b) => {
        if (b.bp !== a.bp) return b.bp - a.bp;
        const at = a.taxon || '', bt = b.taxon || '';
        if (at !== bt) return at < bt ? -1 : 1;
        const ar = a.rank || '', br = b.rank || '';
        if (ar !== br) return ar < br ? -1 : 1;
        return 0;
      });
      return top == null ? items : items.slice(0, top);
    }

    function summarize(records) {
      const totalBp = records.reduce((sum, r) => sum + Number(r.length || 0), 0);
      const lost = records.filter(isLostEvidence);
      const queried = records.filter((r) => (r.status === 'ok' || r.status === 'no_hit') && !isLostEvidence(r));
      const queriedBp = queried.reduce((sum, r) => sum + Number(r.length || 0), 0);
      const bases = new Map();
      for (const record of queried) {
        const consensus = record.consensus || {};
        const taxon = consensus.taxon;
        const rank = taxon ? consensus.rank : null;
        const key = `${rank === null || rank === undefined ? '' : rank}\u0000${taxon || 'unassigned'}`;
        const cur = bases.get(key);
        if (cur) cur.bp += Number(record.length || 0);
        else bases.set(key, { rank: rank == null ? null : rank, taxon: taxon || 'unassigned', bp: Number(record.length || 0) });
      }
      return {
        contigs: records.length,
        bp: totalBp,
        queried_contigs: queried.length,
        queried_bp: queriedBp,
        too_few_kmers_contigs: lost.length,
        too_few_kmers_bp: lost.reduce((sum, r) => sum + Number(r.length || 0), 0),
        no_hit_contigs: queried.filter((r) => !Array.isArray(r.hits) || !r.hits.length).length,
        consensus_bp: breakdown(bases, queriedBp, null),
      };
    }

    const COUNTERS = ['contigs', 'bp', 'queried_contigs', 'queried_bp',
      'too_few_kmers_contigs', 'too_few_kmers_bp', 'no_hit_contigs'];

    function mergeSummaries(parts) {
      const totals = Object.fromEntries(COUNTERS.map((key) => [key, 0]));
      const bases = new Map();
      for (const part of parts) {
        for (const key of COUNTERS) totals[key] += Number(part[key] || 0);
        for (const item of part.consensus_bp || []) {
          const key = `${item.rank == null ? '' : item.rank}\u0000${item.taxon || 'unassigned'}`;
          const cur = bases.get(key);
          if (cur) cur.bp += Number(item.bp || 0);
          else bases.set(key, { rank: item.rank == null ? null : item.rank, taxon: item.taxon || 'unassigned', bp: Number(item.bp || 0) });
        }
      }
      totals.consensus_bp = breakdown(bases, Number(totals.queried_bp), null);
      return totals;
    }

    function finalizeSummary(summary, top = 5) {
      const bases = new Map();
      for (const item of summary.consensus_bp || []) {
        const key = `${item.rank == null ? '' : item.rank}\u0000${item.taxon || 'unassigned'}`;
        const cur = bases.get(key);
        if (cur) cur.bp += Number(item.bp || 0);
        else bases.set(key, { rank: item.rank == null ? null : item.rank, taxon: item.taxon || 'unassigned', bp: Number(item.bp || 0) });
      }
      const result = {};
      for (const key of [...COUNTERS, 'consensus_bp']) result[key] = summary[key];
      result.consensus_bp = breakdown(bases, Number(summary.queried_bp || 0), top);
      return result;
    }

    // The per-request summaries of the sent batches, merged. Records the browser
    // never sent have no record object at all: their counts travel separately.
    function mergeRun({ summaries }) {
      return mergeSummaries(summaries || []);
    }

    // The CLI's JSON summary, with the two skipped buckets as {contigs, bp}
    // objects (records that were skipped are no longer listed anywhere) and the
    // browser-only classification limit, when one applied.
    function summaryFrom({ records, summaries, minLength, totals, skippedShort, skippedFewKmers, notClassified }) {
      const merged = mergeRun({ summaries });
      const seen = totals || {
        contigs: (records || []).length,
        bp: (records || []).reduce((sum, r) => sum + Number(r.length || 0), 0),
      };
      // The server may itself refuse a kept contig for too few k-mers; those are
      // in the per-request summaries and belong to the same bucket.
      const fewKmers = Number((skippedFewKmers && skippedFewKmers.contigs) || 0)
        + Number(merged.too_few_kmers_contigs || 0);
      const fewBp = Number((skippedFewKmers && skippedFewKmers.bp) || 0)
        + Number(merged.too_few_kmers_bp || 0);
      const summary = {
        contigs: Number(seen.contigs || 0),
        bp: Number(seen.bp || 0),
        queried_contigs: merged.queried_contigs,
        queried_bp: merged.queried_bp,
        skipped_short: {
          contigs: Number((skippedShort && skippedShort.contigs) || 0),
          bp: Number((skippedShort && skippedShort.bp) || 0),
        },
        skipped_few_kmers: { contigs: fewKmers, bp: fewBp },
        no_hit_contigs: merged.no_hit_contigs,
        min_contig_length: minLength,
        consensus_bp: finalizeSummary(merged).consensus_bp,
      };
      if (notClassified && notClassified.limit != null) {
        summary.not_classified = {
          contigs: Number(notClassified.contigs || 0),
          bp: Number(notClassified.bp || 0),
          limit: Number(notClassified.limit),
        };
      }
      return summary;
    }

    function caveats(records) {
      const notes = [];
      for (const record of records) {
        const summary = record.assignment || record.closest || {};
        const note = summary.note;
        if (note && !notes.includes(note)) notes.push(note);
      }
      return notes;
    }

    // PerContigRun.write_tsv().
    function tsv(records, fields) {
      const header = [...COLUMNS, ...(fields || [])];
      let out = `${header.join('\t')}\n`;
      const empty = new Array(Math.max(0, header.length - 4)).fill('');
      for (const record of records) {
        const prefix = [
          record.id,
          String(record.length),
          fixed(record.gc, 2),
          record.status,
        ];
        const hits = Array.isArray(record.hits) ? record.hits : [];
        if (!hits.length) {
          out += `${[...prefix, ...empty].join('\t')}\n`;
          continue;
        }
        const consensus = record.consensus || {};
        for (const hit of hits) {
          out += [
            ...prefix,
            pyStr(hit.rank),
            fixed(hit.ani, 2),
            pyStr(hit.shared_kmers),
            pyStr(hit.contig_kmers),
            fixed(hit.explained_fraction, 4),
            fixed(hit.reference_ani, 2),
            pyStr(hit.relation || ''),
            pyStr(hit.shared_rank || ''),
            hit.ani_species_level ? 'true' : 'false',
            pyStr(hit.accession || ''),
            pyStr(hit.genome_id || ''),
            pyStr(hit.gtdb_species || ''),
            pyStr(hit.taxon || ''),
            pyStr(consensus.n_close || 0),
            pyStr(consensus.rank || ''),
            pyStr(consensus.taxon || ''),
            ...(fields || []).map((column) => fieldStr(column, hit[column])),
          ].join('\t') + '\n';
        }
      }
      return out;
    }

    // PerContigRun.document().
    function document({ records, summaries, options, database, file, totals, skippedShort, skippedFewKmers, notClassified }) {
      const db = database || {};
      return {
        query: { file, per_contig: true },
        database: {
          name: db.name == null ? null : db.name,
          genomes: db.genomes == null ? null : db.genomes,
          c: db.c == null ? null : db.c,
          k: db.k == null ? null : db.k,
        },
        params: {
          top: options.top,
          min_ani: options.min_ani,
          min_contig_length: options.minLength,
          consensus_ratio: options.consensus_ratio,
          consensus_z: options.consensus_z,
          ...QUERY_PARAMS,
        },
        thresholds: { species: 95, imprecise_below: 85 },
        summary: summaryFrom({
          records, summaries, minLength: options.minLength,
          totals, skippedShort, skippedFewKmers, notClassified,
        }),
        contigs: records.map((record) => {
          const item = {
            id: record.id,
            index: record.index,
            length: record.length,
            gc: record.gc,
            status: record.status,
            sketch_kmers: record.sketch_kmers,
            contig_kmers: record.contig_kmers,
          };
          if (Object.prototype.hasOwnProperty.call(record, 'reason')) item.reason = record.reason;
          item.assignment = record.assignment == null ? null : record.assignment;
          item.closest = record.closest == null ? null : record.closest;
          item.consensus = record.consensus == null ? null : record.consensus;
          item.hits = record.hits || [];
          return item;
        }),
      };
    }

    // How many contig sketches to send per POST /contigs (chunk_size_for).
    function chunkSizeFor(info) {
      const limits = (info && info.limits) || {};
      let size = Number(limits.max_sketches || 4096);
      const maxJobs = limits.max_jobs;
      const maxQueue = limits.max_queue;
      if (maxJobs && maxQueue != null) size = Math.min(size, Math.max(1, Number(maxJobs) + Number(maxQueue)));
      return Math.max(1, size);
    }

    // How large the JSON params part may grow (params_budget).
    function paramsBudget(info) {
      const limits = (info && info.limits) || {};
      const limit = Number(limits.max_params_bytes || 1024 * 1024);
      return Math.min(DEFAULT_PARAMS_BUDGET, Math.max(1024, Math.floor(limit * 0.9)));
    }

    return {
      COLUMNS, QUERY_PARAMS, DEFAULT_MIN_LENGTH, DEFAULT_TOP, DEFAULT_MIN_ANI,
      DEFAULT_CONSENSUS_RATIO, DEFAULT_CONSENSUS_Z, DEFAULT_PARAMS_BUDGET, MAX_CLASSIFIED,
      fixed, pyStr, summarize, mergeSummaries, finalizeSummary, mergeRun, summaryFrom, caveats,
      tsv, document, chunkSizeFor, paramsBudget,
    };
  })();

  // Node (the parity test) only wants the pure writers.
  if (typeof module === 'object' && module.exports) {
    module.exports = Format;
    return;
  }

  const S = window.Sylphd;
  if (!S || !window.SylphdSketch) return;
  const $ = (id) => document.getElementById(id);

  const bar = $('per-contig-bar');
  const toggle = $('per-contig');
  const note = $('per-contig-note');
  const optionsBox = $('per-contig-options');
  const assemblyOptions = $('assembly-options');
  const inputs = {
    minLength: $('pc-min-length'), top: $('pc-top'), minAni: $('pc-min-ani'),
    ratio: $('pc-ratio'), z: $('pc-z'),
  };
  if (!bar || !toggle || !optionsBox || !assemblyOptions || !S.updateSubmit) return;

  const st = {
    enabled: false, active: false, busy: false, sketchRun: null,
    view: null, sortKey: 'contig', sortDir: 'asc', open: new Set(), filter: null,
  };

  const num = (node) => { const v = node.value.trim(); return v === '' ? NaN : Number(v); };

  function validate() {
    const minLength = num(inputs.minLength);
    const top = num(inputs.top);
    const ani = num(inputs.minAni);
    const ratio = num(inputs.ratio);
    const z = num(inputs.z);
    const ok = Number.isInteger(minLength) && minLength >= 0 && minLength <= 1e13
      && Number.isInteger(top) && top >= 1 && top <= 50
      && Number.isFinite(ani) && ani >= 70 && ani <= 100
      && Number.isFinite(ratio) && ratio >= 1
      && Number.isFinite(z) && z >= 0;
    return {
      ok, minLength, top, ani, ratio, z,
      min_ani: ani, consensus_ratio: ratio, consensus_z: z,
    };
  }

  function setActive(on) {
    const next = !!on && st.enabled;
    if (next === st.active) { toggle.checked = next; return; }
    st.active = next;
    toggle.checked = next;
    assemblyOptions.hidden = next;
    optionsBox.hidden = !next;
    const results = $('results');
    if (results) results.replaceChildren();
    st.view = null;
    S.updateSubmit();
  }

  // The toggle exists when /info lists POST /contigs; without it, say why it is disabled.
  function refreshEnabled() {
    const info = S.info();
    const known = !!info;
    const has = known && Array.isArray(info.endpoints) && info.endpoints.includes('/contigs');
    st.enabled = !!has;
    const inAssembly = S.mode() === 'assembly';
    bar.hidden = !known || !inAssembly;
    if (!known) {
      if (st.active) setActive(false);
      return;
    }
    if (has) {
      note.hidden = true;
      note.textContent = '';
    } else {
      note.hidden = false;
      note.textContent = 'Per-contig mode is not available on this server: it does not offer POST /contigs. Use the single-assembly search, or connect to an updated server.';
      if (st.active) setActive(false);
    }
    toggle.disabled = !has;
    S.updateSubmit();
  }

  toggle.addEventListener('change', () => setActive(toggle.checked));
  for (const node of Object.values(inputs)) {
    node.addEventListener('input', () => S.updateSubmit());
  }
  S.onInfo(refreshEnabled);
  S.onMode(() => refreshEnabled());

  /* --------------------------------------------------------------- results */
  const RANKING = { d: 'Domain', p: 'Phylum', c: 'Class', o: 'Order', f: 'Family', g: 'Genus', s: 'Species' };

  function parseLineage(str) {
    if (!S.has(str)) return [];
    return String(str).split(';').map((part) => {
      const m = /^([a-z]+)__(.*)$/.exec(part.trim());
      return m ? { rank: m[1], name: m[2] } : { rank: '', name: part.trim() };
    });
  }
  const lineageName = (lin, rank) => { const hit = lin.find((e) => e.rank === rank); return hit && hit.name ? hit.name : ''; };

  function genericRanks(hit) {
    const tax = hit && hit.taxonomy;
    if (!tax || typeof tax !== 'object' || Array.isArray(tax)) return [];
    return Object.entries(tax).filter(([, name]) => typeof name === 'string' && name.trim());
  }

  function hitTaxon(hit) {
    if (S.has(hit.taxon)) return String(hit.taxon);
    const lin = parseLineage(hit.gtdb_taxonomy);
    const species = lineageName(lin, 's');
    if (species) return species;
    const ranks = genericRanks(hit);
    return ranks.length ? ranks[ranks.length - 1][1] : '';
  }

  function hitGenome(hit) {
    if (S.has(hit.accession)) return String(hit.accession);
    if (S.has(hit.genome_id)) return String(hit.genome_id);
    if (S.has(hit.genome_file)) return String(hit.genome_file).split('/').pop();
    return '–';
  }

  const consensusText = (c) => (c && S.has(c.taxon) ? String(c.taxon) : null);

  function consensusSub(record) {
    const c = record.consensus || {};
    if (c.mixed_sources) return 'mixed sources: no consensus';
    if (consensusText(c)) return c.rank ? `rank: ${c.rank}` : null;
    if ((record.hits || []).length) return 'no consensus';
    return null;
  }

  function fmtPct(ani) {
    return S.isNum(ani) ? `${Number(ani).toFixed(2)} %` : '–';
  }

  /* ------------------------------------------------------- no-hit wording */
  const NO_HIT_TIP = 'No reference passed the thresholds.';
  const NO_HIT_SCREEN_TIP = "For short or divergent contigs this can be a miss by the database's first-stage screen, not proof that the organism is absent.";
  let helpSeq = 0;
  const helpId = () => `pc-nohit-help-${++helpSeq}`;

  // A plain database has no first-stage screen, so the screen sentence is only
  // true when at least one loaded database is two-stage (/info databases[].kind,
  // with the same fallbacks app.js uses for older servers).
  function hasTwoStageDatabase(info) {
    const dbs = info && Array.isArray(info.databases) ? info.databases : [];
    const entries = dbs.length ? dbs : (info && info.database ? [info.database] : []);
    return entries.some((d) => !!d && (d.kind ? d.kind === 'two-stage'
      : /\.syl2db$/i.test(String(d.file || '')) || d.two_stage === true));
  }

  function noHitHelp(info) {
    return hasTwoStageDatabase(info) ? `${NO_HIT_TIP} ${NO_HIT_SCREEN_TIP}` : NO_HIT_TIP;
  }

  // Both the title and a visually hidden description, so the text is available
  // to screen readers and to keyboard users (the badge and summary box are not
  // focusable, so aria-describedby is the accessible equivalent of hovering).
  function withHelp(node, text, id) {
    node.setAttribute('title', text);
    node.setAttribute('aria-describedby', id);
    node.append(el('span', { class: 'sr-only', id, text: ` ${text}` }));
    return node;
  }

  const statusLabel = (record) => (record.status === 'too_short' ? 'too short'
    : record.status === 'too_few_kmers' ? 'too few k-mers'
      : record.status === 'no_hit' ? 'no hit' : record.status);
  // Cells of a queried contig without hits say so, instead of an empty gap.
  const noHitLabel = (record) => (record.status === 'no_hit' ? 'no hit' : 'not classified');
  const noHitCell = (record) => el('span', { class: 'pc-nohit-text', text: noHitLabel(record) });

  function statusBadge(record) {
    const label = statusLabel(record);
    const node = el('span', { class: `badge badge-plain pc-status pc-status-${record.status}`, text: label });
    if (record.status === 'no_hit') {
      const id = helpId();
      node.classList.add('pc-nohit-badge');
      withHelp(node, noHitHelp(st.view && st.view.info), id);
    } else {
      const title = record.reason ? String(record.reason)
        : record.status === 'too_short' ? `Shorter than the minimum contig length (${S.fmtInt(st.view && st.view.options.minLength)} bp)` : null;
      if (title) node.setAttribute('title', title);
    }
    return node;
  }

  const COLS = [
    { key: 'contig', label: 'Contig', get: (r) => r.id, dir: 'asc' },
    { key: 'length', label: 'Length', get: (r) => r.length, dir: 'desc' },
    { key: 'gc', label: 'GC %', get: (r) => r.gc, dir: 'desc' },
    { key: 'status', label: 'Status', get: (r) => r.status, dir: 'asc' },
    { key: 'consensus', label: 'Consensus taxon', get: (r) => consensusText(r.consensus) || (r.consensus && r.consensus.mixed_sources ? '~' : ''), dir: 'asc' },
    { key: 'rank', label: 'Rank', get: (r) => (r.consensus && r.consensus.rank) || '', dir: 'asc' },
    { key: 'n_close', label: 'n_close', get: (r) => (r.consensus ? r.consensus.n_close : null), dir: 'desc' },
    { key: 'best', label: 'Best ANI %', get: (r) => (r.hits && r.hits[0] ? r.hits[0].ani : null), dir: 'desc' },
  ];

  function el(tag, props, ...kids) { return S.el(tag, props, ...kids); }

  function visibleRecords() {
    const records = st.view.records;
    if (!st.filter) return records;
    return records.filter((r) => {
      const c = r.consensus || {};
      return (S.has(c.taxon) ? String(c.taxon) : null) === st.filter.taxon
        && ((c.rank || null) === st.filter.rank);
    });
  }

  function sortedRecords() {
    const records = visibleRecords();
    const col = COLS.find((c) => c.key === st.sortKey) || COLS[0];
    const sign = st.sortDir === 'asc' ? 1 : -1;
    return records.slice().sort((x, y) => {
      const a = col.get(x), b = col.get(y);
      const an = a == null || a === '', bn = b == null || b === '';
      if (an || bn) return an === bn ? x.index - y.index : an ? 1 : -1;
      const cmp = typeof a === 'number' && typeof b === 'number' ? a - b
        : String(a).localeCompare(String(b), undefined, { sensitivity: 'base' });
      return cmp * sign || x.index - y.index;
    });
  }

  function summaryItem(label, value, small, title) {
    return el('div', { title: title || null }, el('dt', { text: label }),
      el('dd', null, value, small ? el('small', { text: small }) : null));
  }

  // The "No hit" box carries the same explanation as the badge, as a title and
  // as hidden text referenced from its <dd>.
  function noHitSummaryItem(count, info) {
    const text = noHitHelp(info);
    const id = helpId();
    return el('div', null,
      el('dt', { text: 'No hit' }),
      el('dd', { class: 'pc-nohit-summary', title: text, 'aria-describedby': id },
        `${S.fmtInt(count)} contigs`,
        el('span', { class: 'sr-only', id, text: ` ${text}` })));
  }

  function taxonLabel(item) {
    return `${item.taxon}${item.rank ? ` (${item.rank})` : ''}`;
  }

  function summaryCard(view) {
    const s = view.summary;
    const full = view.fullBreakdown || [];
    const queriedBp = Number(s.queried_bp) || 0;
    const bars = full.map((item) => {
      const pct = queriedBp ? (100 * item.bp) / queriedBp : 0;
      const active = st.filter && st.filter.taxon === item.taxon && st.filter.rank === (item.rank || null);
      return el('button', {
        type: 'button', class: `pc-bar${active ? ' active' : ''}`,
        'aria-pressed': active ? 'true' : 'false',
        title: `Show only contigs whose consensus is ${taxonLabel(item)}`,
        onclick: () => setFilter(item),
      },
      el('span', { class: 'pc-bar-label' }, el('b', { text: item.taxon || 'unassigned' }),
        item.rank ? el('span', { class: 'pc-rank', text: item.rank }) : null),
      el('span', { class: 'pc-bar-track', 'aria-hidden': 'true' },
        el('span', { style: `width:${Math.max(0.5, pct).toFixed(1)}%` })),
      el('span', { class: 'pc-bar-meta', text: `${S.fmtInt(item.bp)} bp · ${pct.toFixed(1)} %` }));
    });
    const caveatNodes = view.caveats.length
      ? [el('div', { class: 'pc-caveats' }, view.caveats.map((text) => el('p', { class: 'detail' }, '⚠ ', text)))]
      : [];
    const short = s.skipped_short || { contigs: 0, bp: 0 };
    const few = s.skipped_few_kmers || { contigs: 0, bp: 0 };
    const limit = s.not_classified;
    const limitNode = limit && limit.contigs
      ? S.notice('warn', 'Classification limit reached',
        view.limitedAll
          ? `${S.fmtInt(limit.contigs)} more contigs were not classified (limit: ${S.fmtInt(limit.limit)} longest overall); use the sylphd command line for all contigs.`
          : `${S.fmtInt(limit.contigs)} more contigs ≥ ${S.fmtInt(s.min_contig_length)} bp were not classified (limit: ${S.fmtInt(limit.limit)} longest); use the sylphd command line for all contigs.`)
      : null;
    return el('section', { class: 'card', 'aria-label': 'Per-contig summary' },
      el('div', { class: 'card-head' },
        el('h2', { text: 'Per-contig summary' }),
        el('div', { class: 'export' },
          el('button', { type: 'button', class: 'btn', text: 'Download TSV', onclick: downloadTsv }),
          el('button', { type: 'button', class: 'btn', text: 'Download JSON', onclick: downloadJson }))),
      el('p', { class: 'muted pc-local' },
        el('span', { class: 'badge badge-rel rel-species', title: 'Your sequences never left your computer: the browser sketched every contig and only the sketches (k-mer hashes and counts) and a few numbers per record were sent.' }, '✓ sketched locally'),
        view.label ? ` “${view.label}” ` : '',
        `in ${S.fmtDur(view.sketchMs || 0)}, ${S.fmtInt(s.contigs)} records, ${S.fmtBp(s.bp)}`),
      el('dl', { class: 'summary' },
        summaryItem('Queried', `${S.fmtInt(s.queried_contigs)} contigs`, `${S.fmtInt(s.queried_bp)} bp classified`),
        summaryItem(`Below ${S.fmtInt(s.min_contig_length)} bp`, `${S.fmtInt(short.contigs)} contigs`,
          `${S.fmtBp(short.bp)} · not classified`),
        summaryItem('Too few k-mers', `${S.fmtInt(few.contigs)} contigs`, `${S.fmtBp(few.bp)} · not classified`),
        noHitSummaryItem(s.no_hit_contigs, view.info),
        summaryItem('Search time', S.isNum(view.searchMs) ? `${Math.round(view.searchMs)} ms` : '–',
          `${S.fmtDur(view.totalMs || 0)} end to end`)),
      limitNode,
      el('h3', { class: 'pc-h3', text: 'Bases by consensus taxon' }),
      bars.length ? el('div', { class: 'pc-bars' }, bars) : el('p', { class: 'muted', text: 'No queried bases with a consensus.' }),
      caveatNodes);
  }

  function setFilter(item) {
    const same = st.filter && st.filter.taxon === item.taxon && st.filter.rank === (item.rank || null);
    st.filter = same ? null : { taxon: item.taxon, rank: item.rank || null };
    rerender();
  }

  function bestCell(record) {
    const hit = record.hits && record.hits[0];
    if (!hit) return noHitCell(record);
    const frac = S.isNum(hit.explained_fraction) ? `${(100 * hit.explained_fraction).toFixed(1)} %` : '–';
    return el('div', { class: 'pc-best' },
      el('b', { text: fmtPct(hit.ani) }),
      el('span', { class: 'pc-best-taxon', text: hitTaxon(hit) || '–' }),
      el('span', { class: 'sub mono', title: 'genome_id', text: hitGenome(hit) }),
      el('span', { class: 'sub', text: `explained ${frac} · reference ANI ${fmtPct(hit.reference_ani)}` }));
  }

  // The expanded detail of a contig without hits: a line or two instead of an
  // empty hit table.
  function noHitDetail(record) {
    const head = record.status === 'no_hit' ? 'No hit. ' : `${noHitLabel(record)}. `;
    return el('div', { class: 'pc-nohit-detail' },
      el('p', null, el('strong', { text: head }), noHitHelp(st.view && st.view.info)),
      record.reason ? el('p', { class: 'detail', text: `Reason: ${record.reason}` }) : null);
  }

  function hitsTable(record) {
    const head = ['#', 'ANI %', 'Relation', 'Shared / contig', 'Explained', 'Reference ANI', 'Genome', 'Taxon', 'GTDB species'];
    const table = el('table', { class: 'hits pc-hits' },
      el('thead', null, el('tr', null, head.map((label) => el('th', { scope: 'col', text: label })))),
      el('tbody', null, (record.hits || []).map((hit) => {
        const lin = parseLineage(hit.gtdb_taxonomy);
        const taxon = hitTaxon(hit);
        return el('tr', null,
          el('td', { class: 'num', text: String(hit.rank) }),
          el('td', { class: 'num', text: fmtPct(hit.ani) }),
          el('td', { text: hit.relation || '–' }),
          el('td', { class: 'num', text: `${S.fmtInt(hit.shared_kmers)} / ${S.fmtInt(hit.contig_kmers)}` }),
          el('td', { class: 'num', text: S.isNum(hit.explained_fraction) ? `${(100 * hit.explained_fraction).toFixed(1)} %` : '–' }),
          el('td', { class: 'num', text: fmtPct(hit.reference_ani) }),
          el('td', { class: 'mono', title: hit.genome_file || hitGenome(hit), text: hitGenome(hit) }),
          el('td', null, taxon || '–', lin.length ? el('div', { class: 'sub', text: `${RANKING[lin[0].rank] || lin[0].rank}: ${lin[0].name}` }) : null),
          el('td', { text: S.has(hit.gtdb_species) ? hit.gtdb_species : '–' }));
      })));
    return table;
  }

  function contigRow(record) {
    const open = st.open.has(record.id);
    const hits = record.hits || [];
    const empty = !hits.length;
    const toggleBtn = el('button', {
      type: 'button', class: 'pc-expand', 'aria-expanded': open ? 'true' : 'false',
      'aria-label': hits.length
        ? `${open ? 'Hide' : 'Show'} the ${hits.length} hit${hits.length === 1 ? '' : 's'} of ${record.id}`
        : `${open ? 'Hide' : 'Show'} why ${record.id} has no hit`,
      onclick: () => {
        if (st.open.has(record.id)) st.open.delete(record.id); else st.open.add(record.id);
        rerender();
      },
    }, el('span', { class: 'pc-caret', 'aria-hidden': 'true', text: open ? '▾' : '▸' }), el('span', { class: 'mono', text: record.id }));
    const cells = [
      el('td', { class: 'pc-contig', 'data-label': 'Contig' },
        el('div', { class: 'pc-contig-cell' }, toggleBtn,
          hits.length > 1 ? el('span', { class: 'sub', text: `${hits.length} hits` }) : null)),
      el('td', { class: 'num', 'data-label': 'Length', text: S.fmtInt(record.length) }),
      el('td', { class: 'num', 'data-label': 'GC %', text: S.isNum(record.gc) ? `${Number(record.gc).toFixed(2)} %` : '–' }),
      el('td', { 'data-label': 'Status' }, el('div', { class: 'cv' }, statusBadge(record),
        record.reason ? el('div', { class: 'sub', text: String(record.reason) }) : null)),
      el('td', { 'data-label': 'Consensus taxon' }, el('div', { class: 'cv pc-consensus' },
        empty ? noHitCell(record) : consensusText(record.consensus) || (record.consensus && record.consensus.mixed_sources
          ? el('span', { class: 'badge badge-plain', title: 'The close set spans two taxonomy sources, so their ranks are not comparable', text: 'mixed sources' })
          : '–'),
        !empty && consensusSub(record) ? el('div', { class: 'sub', text: consensusSub(record) }) : null)),
      el('td', { 'data-label': 'Rank' }, empty ? noHitCell(record) : el('span', { text: (record.consensus && record.consensus.rank) || '–' })),
      el('td', { class: 'num', 'data-label': 'n_close' }, empty ? noHitCell(record)
        : el('span', { text: record.consensus && S.isNum(record.consensus.n_close) ? String(record.consensus.n_close) : '–' })),
      el('td', { 'data-label': 'Best ANI %' }, bestCell(record)),
    ];
    const main = el('tr', { class: open ? 'top-row' : null }, cells);
    const detail = el('tr', { class: 'pc-detail', hidden: open ? null : true },
      el('td', { colspan: String(cells.length) }, empty ? noHitDetail(record) : hitsTable(record)));
    return [main, detail];
  }

  function filterChip() {
    if (!st.filter) return null;
    const count = visibleRecords().length;
    const label = `${st.filter.taxon}${st.filter.rank ? ` (${st.filter.rank})` : ''}`;
    return el('div', { class: 'pc-filter' },
      el('span', null, 'Consensus: ', el('strong', { text: label }), ` · showing ${S.fmtInt(count)} of ${S.fmtInt(st.view.records.length)} contigs`),
      el('button', { type: 'button', class: 'btn btn-small btn-ghost', text: 'Show all', onclick: () => { st.filter = null; rerender(); } }));
  }

  function tableCard() {
    const table = el('table', { class: 'hits pc-table' });
    const head = el('tr');
    for (const col of COLS) {
      const th = el('th', { scope: 'col' }, el('button', {
        type: 'button', text: col.label, title: `Sort by ${col.label}`,
        onclick: () => {
          if (st.sortKey === col.key) st.sortDir = st.sortDir === 'asc' ? 'desc' : 'asc';
          else { st.sortKey = col.key; st.sortDir = col.dir; }
          rerender();
        },
      }));
      head.append(th);
    }
    table.append(el('thead', null, head), el('tbody'));
    fillTable(table);
    const total = st.view.records.length;
    const shown = visibleRecords().length;
    return el('section', { class: 'card', 'aria-label': 'Contigs' },
      el('div', { class: 'card-head' },
        el('h2', { text: st.filter ? `Contigs (${S.fmtInt(shown)} of ${S.fmtInt(total)})` : `Contigs (${S.fmtInt(total)})` }),
        el('span', { class: 'muted', text: 'Click a contig to see its hits; click a consensus bar to filter' })),
      filterChip(),
      el('div', { class: 'table-wrap' }, table));
  }

  function fillTable(table) {
    COLS.forEach((col, i) => {
      const th = table.tHead.rows[0].cells[i];
      if (col.key === st.sortKey) th.setAttribute('aria-sort', st.sortDir === 'asc' ? 'ascending' : 'descending');
      else th.removeAttribute('aria-sort');
    });
    table.tBodies[0].replaceChildren(...sortedRecords().flatMap(contigRow));
  }

  function emptyCard() {
    return el('section', { class: 'card empty' },
      el('h2', { text: 'No contig was classified' }),
      el('p', { text: 'No record was long enough and had enough k-mers to query. Lower the minimum contig length, or check that the file is FASTA/FASTQ.' }));
  }

  function render(view) {
    st.view = view;
    const out = [summaryCard(view)];
    out.push(view.records.length ? tableCard() : emptyCard());
    $('results').replaceChildren(...out);
  }

  function rerender() {
    if (st.view) render(st.view);
  }

  /* -------------------------------------------------------------- downloads */
  function fileBase() {
    const name = (st.view && st.view.label) || 'assembly';
    const safe = String(name).replace(/\.(fastq|fasta|fna|fas|fq|fa|gz)/gi, '')
      .replace(/[^\w.-]+/g, '_').slice(0, 60) || 'assembly';
    return `sylphd_${safe}_contigs`;
  }

  function downloadTsv() {
    if (!st.view) return;
    S.download(`${fileBase()}.tsv`, Format.tsv(st.view.records, st.view.fields), 'text/tab-separated-values');
  }

  function downloadJson() {
    if (!st.view) return;
    const doc = Format.document({
      records: st.view.records, summaries: st.view.summaries,
      options: st.view.options, database: st.view.info.database, file: st.view.file,
      totals: st.view.counts.totals,
      skippedShort: st.view.counts.skippedShort,
      skippedFewKmers: st.view.counts.skippedFewKmers,
      notClassified: st.view.counts.notClassified,
    });
    S.download(`${fileBase()}.json`, `${JSON.stringify(doc)}\n`, 'application/json');
  }

  /* ------------------------------------------------------------------- run */
  function sketchKmers(bytes) {
    if (!bytes || bytes.byteLength < 8) return null;
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    return Number(view.getBigUint64(0, true));
  }

  function httpError(status, message, retryAfter, raw) {
    const err = new Error(message || `HTTP ${status}`);
    err.name = 'HttpError';
    err.status = status;
    err.serverMessage = message || (raw && raw.length < 300 && !raw.trim().startsWith('<') ? raw.trim() : '');
    err.retryAfter = retryAfter;
    return err;
  }

  function showHttpError(err) {
    const status = err.status;
    const msg = err.serverMessage ? el('p', { class: 'detail', text: `Server message: ${err.serverMessage}` }) : null;
    let node;
    if (status === 400) {
      node = S.notice('error', 'The server rejected the request (400)',
        'One of the contig sketches or its numbers was not accepted. This is usually a bug in the page; try reloading it, or use the sylphd command line.', msg);
    } else if (status === 413) {
      node = S.notice('error', 'Upload too large (413)',
        'The server rejected a batch of contig sketches because it is over its upload limit. The request is split until it fits; a single contig sketch can still be too large.', msg);
    } else if (status === 429) {
      const secs = err.retryAfter != null ? err.retryAfter : 60;
      node = S.notice('warn', 'Rate limit reached (429)',
        el('p', null, 'Too many requests from your address. Try again in ', el('strong', { text: `${secs} s` }), '.'), msg);
    } else if (status === 503) {
      const secs = err.retryAfter != null ? err.retryAfter : 5;
      node = S.notice('warn', 'Server busy (503)',
        el('p', null, 'The server queue is full. Please retry in ', el('strong', { text: `${secs} s` }), '.'), msg);
    } else if (status === 404) {
      node = S.notice('error', 'Per-contig mode is not available here (404)',
        'This server does not provide POST /contigs. Use the single-assembly search, or connect to an updated server.', msg);
    } else {
      node = S.notice('error', `Request failed (HTTP ${status})`, err.serverMessage || 'The server returned an error without a message.');
    }
    $('results').replaceChildren(node);
  }

  /* ------------------------------------------------ remembered decisions */
  const SMALL_KEY = 'sylphd.perContig.smallContigs';
  const MANY_KEY = 'sylphd.perContig.manyContigs';

  function readChoice(key, allowed) {
    try {
      const value = localStorage.getItem(key);
      return allowed.includes(value) ? value : null;
    } catch { return null; }
  }
  function writeChoice(key, value) {
    try {
      if (value) localStorage.setItem(key, value);
      else localStorage.removeItem(key);
    } catch { /* private mode: the choice simply is not remembered */ }
    refreshResetLink();
  }
  const storedSmallChoice = () => readChoice(SMALL_KEY, ['yes', 'no']);
  const storedManyChoice = () => readChoice(MANY_KEY, ['yes']);
  const storeSmallChoice = (value) => writeChoice(SMALL_KEY, value);
  const storeManyChoice = (value) => writeChoice(MANY_KEY, value);

  function refreshResetLink() {
    const link = $('pc-reset-remembered');
    if (link) link.hidden = !(storedSmallChoice() || storedManyChoice());
  }

  /* ----------------------------------------------------------- modal */
  // One accessible modal per run. Resolves the chosen value (or `escape`), and
  // rejects with AbortError when the run is cancelled from the page.
  function choiceModal({ title, paragraphs, buttons, escape, signal }) {
    return new Promise((resolve, reject) => {
      const titleId = 'pc-modal-title';
      const backdrop = el('div', { class: 'pc-modal-backdrop' });
      const dialog = el('div', {
        class: 'pc-modal', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId,
      });
      let done = false;
      const onAbort = () => {
        if (done) return;
        done = true;
        document.removeEventListener('keydown', onKey, true);
        backdrop.remove();
        const error = new Error('cancelled');
        error.name = 'AbortError';
        reject(error);
      };
      const finish = (choice) => {
        if (done) return;
        done = true;
        document.removeEventListener('keydown', onKey, true);
        if (signal) signal.removeEventListener('abort', onAbort);
        backdrop.remove();
        resolve(choice);
      };
      const nodes = buttons.map((button) => el('button', {
        type: 'button',
        class: button.primary ? 'btn btn-primary' : button.ghost ? 'btn btn-ghost' : 'btn',
        text: button.label,
        onclick: () => finish(button.value),
      }));
      const onKey = (event) => {
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); finish(escape); return; }
        if (event.key !== 'Tab') return;
        const first = nodes[0];
        const last = nodes[nodes.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      };
      dialog.append(
        el('h3', { id: titleId, text: title }),
        ...paragraphs.map((text) => (text instanceof Node ? text : el('p', { class: 'detail', text }))),
        el('div', { class: 'pc-modal-actions' }, nodes),
        el('p', { class: 'detail', text: '“Always” choices are remembered in this browser. Use “Reset remembered choices” next to “Minimum contig length” to forget them.' }));
      backdrop.append(dialog);
      backdrop.addEventListener('click', (event) => { if (event.target === backdrop) finish(escape); });
      document.body.append(backdrop);
      document.addEventListener('keydown', onKey, true);
      if (signal) {
        if (signal.aborted) { onAbort(); return; }
        signal.addEventListener('abort', onAbort, { once: true });
      }
      nodes[0].focus();
    });
  }

  // More contigs at or above the threshold than the browser will classify.
  function askManyContigs({ contigs, threshold, signal }) {
    if (storedManyChoice()) return Promise.resolve('classify');
    const intro = el('p', null,
      `This assembly has ${S.fmtInt(contigs)} contigs of ${S.fmtInt(threshold)} bp or more. The browser classifies the ${S.fmtInt(Format.MAX_CLASSIFIED)} longest; the `,
      el('code', { text: 'sylphd' }), ' command line classifies all of them. Is this one genome or a MAG? For a whole-genome answer use whole-assembly mode.');
    return choiceModal({
      title: 'Many contigs',
      paragraphs: [
        intro,
        'A whole assembly is profiled as one sample, which is the right question for a single genome. Per-contig mode is for finding contamination in a MAG: a contaminant contig then shows its own species instead of being diluted by the rest of the file.',
      ],
      buttons: [
        { label: 'Classify the 100 longest', value: 'classify', primary: true },
        { label: 'Whole assembly instead', value: 'whole' },
        { label: 'Cancel', value: 'cancel' },
        { label: 'Always classify the 100 longest', value: 'classify-always', ghost: true },
      ],
      escape: 'cancel',
      signal,
    }).then((choice) => {
      if (choice === 'classify-always') { storeManyChoice('yes'); return 'classify'; }
      return choice;
    });
  }

  // Fewer than the limit, but some contigs are below the warning threshold.
  function askSmallContigs({ contigs, bp, threshold, signal }) {
    const stored = storedSmallChoice();
    if (stored) return Promise.resolve(stored);
    return choiceModal({
      title: 'Short contigs',
      paragraphs: [
        el('p', null, `${S.fmtInt(contigs)} contigs (${S.fmtBp(bp)}) are shorter than ${S.fmtInt(threshold)} bp. Upload them too?`),
        'Short contigs give a less precise ANI (about ±1 % at 10 kb), and the database’s first-stage screen is more likely to miss them entirely. The consensus widens accordingly, so treat their calls as hints rather than species-level identifications.',
      ],
      buttons: [
        { label: 'Yes, upload them', value: 'yes', primary: true },
        { label: 'No, skip them', value: 'no' },
        { label: 'Always yes', value: 'yes-always', ghost: true },
        { label: 'Always no', value: 'no-always', ghost: true },
      ],
      escape: 'no',
      signal,
    }).then((choice) => {
      if (choice === 'yes-always') { storeSmallChoice('yes'); return 'yes'; }
      if (choice === 'no-always') { storeSmallChoice('no'); return 'no'; }
      return choice;
    });
  }

  const resetLink = $('pc-reset-remembered');
  if (resetLink) {
    resetLink.addEventListener('click', () => {
      storeSmallChoice(null);
      storeManyChoice(null);
      S.updateSubmit();
    });
  }
  refreshResetLink();

  async function run({ phase, signal }) {
    const info = S.info();
    const options = validate();
    const input = S.assemblyInput();
    const results = $('results');
    results.replaceChildren();
    st.busy = true;
    st.open.clear();
    st.filter = null;

    if (!info || !info.database || !S.isNum(info.database.c) || !S.isNum(info.database.k)) {
      results.replaceChildren(S.notice('error', 'The server did not report its sketch parameters',
        'Per-contig mode needs GET /info with database.c and database.k.'));
      st.busy = false;
      return;
    }
    if (!options.ok) {
      results.replaceChildren(S.notice('error', 'Invalid options', 'Check the minimum contig length, top hits, minimum ANI and consensus options.'));
      st.busy = false;
      return;
    }
    if (input.sylsp) {
      results.replaceChildren(S.notice('error', 'Per-contig mode needs FASTA or FASTQ',
        'A .sylsp file is one ready-made sketch and cannot be split into contigs. Upload the assembly or reads, or use the single-assembly search.'));
      st.busy = false;
      return;
    }
    const file = input.file || new File([$('fasta').value], 'pasted.fasta', { type: 'text/plain' });
    if (!file || input.size === 0) {
      results.replaceChildren(S.notice('error', 'No input', 'Paste FASTA/FASTQ or choose a file first.'));
      st.busy = false;
      return;
    }

    const db = info.database;
    const fields = info.metadata && Array.isArray(info.metadata.columns) ? info.metadata.columns.slice() : [];
    const serverOptions = {
      top: options.top, min_ani: options.min_ani,
      consensus_ratio: options.consensus_ratio, consensus_z: options.consensus_z,
    };
    // POST /contigs takes only its own keys; QUERY_PARAMS is the server's, not the client's.
    const paramsBase = { ...serverOptions, fields, tax_sources: null };
    const chunkSize = Format.chunkSizeFor(info);
    const budget = Format.paramsBudget(info);
    const uploadLimit = Number((info.limits && info.limits.max_upload_mb) || 0) * 1024 * 1024;
    const minKmers = Format.QUERY_PARAMS.min_number_kmers;
    const encoder = new TextEncoder();
    const jsonBytes = (value) => encoder.encode(JSON.stringify(value)).length;

    // Records below the hard k-mer floor are never kept as objects: a fragmented
    // assembly can have millions of them. Only counters survive. Sketchable
    // contigs feed two bounded heaps: the MAX_CLASSIFIED longest overall and the
    // MAX_CLASSIFIED longest at or above the warning threshold, so "No" to the
    // short-contig question still has a full set of long contigs to send.
    const totals = { contigs: 0, bp: 0 };
    const sendable = { contigs: 0, bp: 0 };
    const small = { contigs: 0, bp: 0 };
    const large = { contigs: 0, bp: 0 };
    const skippedFewKmers = { contigs: 0, bp: 0 };
    const heapAll = []; // min-heap by length: the longest sketchable contigs
    const heapLarge = []; // min-heap by length: the longest contigs >= threshold
    const summaries = [];
    let sent = 0;
    let classified = 0;
    let serverMs = 0;
    const t0 = performance.now();
    const sketchStart = performance.now();

    const siftUp = (heap, i) => {
      while (i > 0) {
        const parent = (i - 1) >> 1;
        if (heap[parent].record.length <= heap[i].record.length) break;
        [heap[parent], heap[i]] = [heap[i], heap[parent]];
        i = parent;
      }
    };
    const siftDown = (heap, i) => {
      for (;;) {
        const left = 2 * i + 1;
        const right = left + 1;
        let smallest = i;
        if (left < heap.length && heap[left].record.length < heap[smallest].record.length) smallest = left;
        if (right < heap.length && heap[right].record.length < heap[smallest].record.length) smallest = right;
        if (smallest === i) break;
        [heap[smallest], heap[i]] = [heap[i], heap[smallest]];
        i = smallest;
      }
    };
    const addToHeap = (heap, item) => {
      if (heap.length < Format.MAX_CLASSIFIED) {
        heap.push(item);
        siftUp(heap, heap.length - 1);
        return;
      }
      // keep the longest: ties keep the earlier record
      if (item.record.length > heap[0].record.length) {
        heap[0] = item;
        siftDown(heap, 0);
      }
    };

    function progressSketch() {
      phase.text = `Per contig: ${S.fmtInt(totals.contigs)} records (${S.fmtBp(totals.bp)}), `
        + `${S.fmtInt(sendable.contigs)} sketchable (${S.fmtInt(small.contigs)} below ${S.fmtInt(options.minLength)} bp) · `
        + `${S.fmtInt(skippedFewKmers.contigs)} with too few k-mers`;
    }
    function progressSend() {
      phase.text = `Per contig: ${S.fmtInt(sent)} of ${S.fmtInt(toSendTotal)} contigs sent · ${S.fmtInt(classified)} classified ·`;
    }

    function newRecord(contig) {
      // Python's round() is half-even; the wasm reports a toFixed() value, which
      // can differ on an exact .xx5 (and the TSV must match the CLI byte for byte).
      const gc = contig.length ? Number(Format.fixed((100 * contig.gcCount) / contig.length, 2)) : 0;
      return {
        id: contig.name, index: contig.index, length: contig.length, gc,
        status: 'no_hit',
        sketch_kmers: sketchKmers(contig.sketch),
        contig_kmers: contig.contigKmers == null ? null : contig.contigKmers,
        hits: [], assignment: null, closest: null, consensus: null,
      };
    }

    function apply(response, items) {
      const list = response && response.contigs;
      if (!Array.isArray(list) || list.length !== items.length) {
        throw new Error('Unexpected response from /contigs: contig count mismatch.');
      }
      for (let i = 0; i < items.length; i++) {
        const record = items[i].record;
        const result = list[i];
        if (!result || String(result.name) !== record.id) {
          throw new Error('Unexpected response from /contigs: contigs are out of order.');
        }
        record.hits = Array.isArray(result.hits) ? result.hits : [];
        record.assignment = result.assignment == null ? null : result.assignment;
        record.closest = result.closest == null ? null : result.closest;
        record.consensus = result.consensus == null ? null : result.consensus;
        if (result.contig_kmers != null) record.contig_kmers = result.contig_kmers;
        if (result.status === 'ok') record.status = 'ok';
        else if (result.status === 'too_few_kmers') {
          record.status = 'no_hit';
          record.reason = String(result.reason || 'too few k-mers to score the contig');
        } else record.status = 'no_hit';
      }
      if (response.summary && typeof response.summary === 'object') summaries.push(response.summary);
      if (S.isNum(response.elapsed_ms)) serverMs += Number(response.elapsed_ms);
      classified += items.length;
      progressSend();
    }

    async function send(items) {
      const params = { ...paramsBase, contigs: items.map((item) => item.spec) };
      const body = new FormData();
      body.append('params', JSON.stringify(params));
      for (const item of items) {
        body.append('sketch', new Blob([item.payload], { type: 'application/octet-stream' }), `${item.record.id}.sylsp`);
      }
      sent += items.length;
      progressSend();
      let res;
      try {
        res = await fetch(`${S.api}/contigs`, { method: 'POST', body, signal });
      } catch (e) {
        if (e.name === 'AbortError') throw e;
        const err = new Error('network');
        err.network = true;
        throw err;
      }
      const raw = await res.text();
      let data = null;
      try { data = JSON.parse(raw); } catch { /* not JSON */ }
      if (!res.ok) {
        const retry = parseInt(res.headers.get('Retry-After'), 10);
        throw httpError(res.status, data && data.error, Number.isFinite(retry) ? retry : null, raw);
      }
      apply(data, items);
    }

    // Split the kept contigs into requests that fit the server's limits, one
    // request after the other (the CLI's batching, now over the kept longest).
    async function sendAll(candidates) {
      let index = 0;
      while (index < candidates.length) {
        const items = [];
        let paramsBytes = jsonBytes({ ...paramsBase, contigs: [] });
        let payloadBytes = 0;
        while (index < candidates.length && items.length < chunkSize) {
          const item = candidates[index];
          const entryBytes = jsonBytes(item.spec) + (items.length ? 1 : 0);
          const projected = paramsBytes + entryBytes + payloadBytes + item.payload.byteLength
            + (items.length + 1) * (512 + encoder.encode(String(item.spec.name)).length) + 512;
          if (items.length && (paramsBytes + entryBytes > budget || (uploadLimit && projected > uploadLimit))) break;
          if (!items.length && uploadLimit && projected > uploadLimit) {
            throw new Error(`the sketch of contig '${item.record.id}' needs a request larger than the server's ${Math.floor(uploadLimit / 1048576)} MB upload limit`);
          }
          items.push(item);
          paramsBytes += entryBytes;
          payloadBytes += item.payload.byteLength;
          index += 1;
        }
        await send(items);
      }
    }

    const sketchRun = window.SylphdSketch.sketchContigs(
      file,
      // 0: the threshold is only a warning, so every record with a sketch must be
      // available for the "upload short contigs too?" decision. The hard floor is
      // applied here, after the sketch's k-mer count is known.
      { c: db.c, k: db.k, minLength: 0, onProgress: progressSketch },
      (contig) => {
        totals.contigs += 1;
        totals.bp += contig.length;
        const nk = contig.sketch ? sketchKmers(contig.sketch) : 0;
        if (!contig.sketch || nk < minKmers || !contig.contigKmers) {
          // The hard floor: not enough sample or genome-style k-mers to score it.
          skippedFewKmers.contigs += 1;
          skippedFewKmers.bp += contig.length;
          progressSketch();
          return;
        }
        const record = newRecord(contig);
        const item = {
          record,
          spec: { name: contig.name, contig_kmers: contig.contigKmers, length: contig.length, gc: record.gc },
          payload: contig.sketch,
        };
        addToHeap(heapAll, item);
        sendable.contigs += 1;
        sendable.bp += contig.length;
        if (contig.length >= options.minLength) {
          addToHeap(heapLarge, item);
          large.contigs += 1;
          large.bp += contig.length;
        } else {
          small.contigs += 1;
          small.bp += contig.length;
        }
        progressSketch();
      },
    );
    st.sketchRun = sketchRun;

    let stats = null;
    let candidates = [];
    const skippedShort = { contigs: 0, bp: 0 };
    const notClassified = { contigs: 0, bp: 0, limit: Format.MAX_CLASSIFIED };
    let toSendTotal = 0;
    let limitedAll = false;
    try {
      stats = await sketchRun.done;
      let uploadSmall = false;
      if (large.contigs > Format.MAX_CLASSIFIED) {
        // Small contigs cannot reach the top 100 here, so only this modal appears.
        const choice = await askManyContigs({
          contigs: large.contigs, threshold: options.minLength, signal,
        });
        if (choice === 'whole') {
          st.busy = false;
          return { whole: true };
        }
        if (choice === 'cancel') {
          const error = new Error('cancelled');
          error.name = 'AbortError';
          throw error;
        }
      } else if (small.contigs) {
        uploadSmall = (await askSmallContigs({
          contigs: small.contigs, bp: small.bp, threshold: options.minLength, signal,
        })) === 'yes';
      }
      limitedAll = uploadSmall;
      const pool = uploadSmall ? heapAll : heapLarge;
      const poolTotals = uploadSmall ? sendable : large;
      candidates = pool.slice().sort((a, b) => a.record.index - b.record.index);
      toSendTotal = candidates.length;
      if (!uploadSmall) {
        skippedShort.contigs = small.contigs;
        skippedShort.bp = small.bp;
      }
      const keptBp = candidates.reduce((sum, item) => sum + item.record.length, 0);
      notClassified.contigs = Math.max(0, poolTotals.contigs - candidates.length);
      notClassified.bp = Math.max(0, poolTotals.bp - keptBp);
      progressSend();
      await sendAll(candidates);
    } catch (error) {
      st.sketchRun = null;
      st.busy = false;
      if (error && error.name === 'AbortError') {
        results.replaceChildren(S.notice('warn', 'Cancelled', 'Sketching or the request was cancelled. Nothing was kept.'));
      } else if (error && error.name === 'HttpError') {
        showHttpError(error);
      } else if (error && error.network) {
        results.replaceChildren(S.notice('error', 'Could not reach the server',
          el('p', null, 'The request to ', el('code', { text: S.api }), ' failed before getting a response. The server status is shown at the top of the page.')));
        S.loadInfo();
      } else {
        results.replaceChildren(S.notice('error', 'Could not sketch or classify the contigs', (error && error.message) || String(error)));
      }
      return;
    } finally {
      st.sketchRun = null;
    }
    st.busy = false;

    const records = candidates.map((item) => item.record);
    const merged = Format.mergeSummaries(summaries);
    const view = {
      records, summaries, fields, info, limitedAll,
      counts: { totals, skippedShort, skippedFewKmers, notClassified },
      options: {
        top: options.top, min_ani: options.min_ani, minLength: options.minLength,
        consensus_ratio: options.consensus_ratio, consensus_z: options.consensus_z,
      },
      summary: Format.summaryFrom({
        records, summaries, minLength: options.minLength,
        totals, skippedShort, skippedFewKmers, notClassified,
      }),
      fullBreakdown: merged.consensus_bp,
      caveats: Format.caveats(records),
      label: input.file ? input.file.name : 'pasted FASTA',
      file: input.file ? input.file.name : 'pasted.fasta',
      sketchMs: (stats && stats.lifetimeMs) || (performance.now() - sketchStart),
      searchMs: serverMs,
      totalMs: performance.now() - t0,
    };
    st.filter = null;
    render(view);
  }

  function cancel() {
    if (st.sketchRun) st.sketchRun.cancel();
  }

  window.SylphdContigs = {
    active: () => st.active,
    enabled: () => st.enabled,
    validate,
    run,
    cancel,
    deactivate: () => setActive(false),
    Format,
    _setActive: setActive,
  };
  refreshEnabled();
})();
