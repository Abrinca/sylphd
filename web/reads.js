// sylphd "Reads" mode: sketch FASTQ/FASTA reads locally (WebAssembly in Web Workers), upload only the
// sketch to POST /profile and show the taxonomic profile. Plain JS, no dependencies.
// All server/user strings are inserted with textContent / text nodes only.
//
// Pipeline (see sketch-wasm/README.md): one decompress worker per input stream -> sketch worker,
// transferable chunks with credit backpressure; the sketch (.sylsp bytes) comes back to this thread.
(() => {
  'use strict';
  const S = window.Sylphd;
  const root = document.getElementById('mode-reads');
  if (!S || !root) return;
  const { el, fmtInt, fmtBytes, fmtBp, fmtDur, isNum, has, notice, download, rankTitle } = S;

  const MEM_WARN = 1.5 * 1024 ** 3; // wasm memory is capped at 4 GiB per instance
  const DEFAULT_MIN_ANI = 95;
  const DEFAULT_MIN_KMERS = 10;
  const MAX_ROWS_SHOWN = 100;
  const NAME_MAX = 100;

  // Rank prefixes of GTDB/NCBI lineages. The server aggregates the clades; the page only
  // groups them by rank for the chart.
  const { RANK_NAMES } = S;
  const RANKS = Object.keys(RANK_NAMES);
  const TAXONOMY_LABELS = { gtdb: 'GTDB', ncbi: 'NCBI', tax: 'All sources' };
  const TAXONOMY_TITLES = {
    gtdb: 'GTDB lineage of the matched genome',
    ncbi: 'NCBI lineage of the matched genome',
    tax: 'The server’s sylph-tax profile over all its taxonomy sources (viruses, fungi, …)',
  };

  /* ---------------------------------------------------------- formatting */
  // an abundance (a percentage that can be tiny), unlike S.fmtPct's fixed decimals
  const fmtShare = (v) => {
    if (!isNum(v)) return '–';
    if (v >= 10) return `${v.toFixed(1)} %`;
    if (v >= 0.01) return `${v.toFixed(2)} %`;
    return v > 0 ? '< 0.01 %' : '0 %';
  };
  const exportAs = (run, ext) => S.exportName(run.name, 'reads', ext);
  const sketchName = (run) => `${S.fileStem(run.name, 'sample')}.sylsp`;

  /* ----------------------------------------------------- support checks */
  const { unsupported, loadModule, isSylsp, readSylspHeader, sketchProblem } = window.SylphdSketch;

  /* --------------------------------------------------------- file names */
  const MATE_RE = /[_.]R?([12])(?:_001)?$/i; // sample_R1, sample.R2, sample_1, sample_R1_001
  const mateOf = (name) => { const m = MATE_RE.exec(S.stem(name)); return m ? Number(m[1]) : 0; };
  const sampleFromName = (name, paired) => {
    let s = S.stem(name);
    if (paired) s = s.replace(MATE_RE, '');
    return (s || 'sample').slice(0, NAME_MAX);
  };

  /* ---------------------------------------------------------------- state */
  const st = {
    files: [], // File[]; for the paired layout [R1, R2]
    layout: 'single', // 'single' | 'interleaved' | 'paired'
    pairDetected: false,
    pairMismatch: false, // the first records of R1 and R2 have different names
    nameTouched: false,
    kind: 'reads', // 'reads' | 'sketch' (one .sylsp) | 'multisketch' (several .sylsp) | 'mixed'
    header: null, // parsed header of the .sylsp (kind 'sketch'), or { error }
    run: null,
    busy: false,
    view: null, // results view state
  };

  /* ------------------------------------------------------------------ DOM */
  const ui = {};
  const fileInput = el('input', {
    type: 'file', multiple: true, class: 'sr-only', tabindex: '-1', id: 'r-file',
    accept: '.fastq,.fq,.fasta,.fa,.fna,.gz,.sylsp,application/gzip',
  });
  ui.drop = el('div', { class: 'rdrop', id: 'r-drop', role: 'button', tabindex: '0', 'aria-label': 'Choose or drop read files' },
    dropIcon(),
    el('div', { class: 'rdrop-text' },
      el('strong', { text: 'Drop FASTQ or FASTA reads (or a .sylsp sketch) here' }),
      el('span', null, 'or ', el('u', { text: 'choose files' }), '. One file (single-end or interleaved) or two files (R1 + R2). ',
        el('code', { text: '.fastq .fq .fasta .fa' }), ', optionally ', el('code', { text: '.gz' }), '.'),
      el('span', null, 'Or one ', el('code', { text: '.sylsp' }), ' sketch you made earlier (for example downloaded here): it is uploaded as it is.'),
      el('span', { class: 'rdrop-private' }, 'Your reads are not uploaded: they are sketched on this computer.')),
    el('div', { class: 'drop-overlay', 'aria-hidden': 'true', text: 'Drop files to load' }));

  function dropIcon() {
    const NS = 'http://www.w3.org/2000/svg';
    const s = document.createElementNS(NS, 'svg');
    s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('width', '40'); s.setAttribute('height', '40');
    s.setAttribute('aria-hidden', 'true'); s.setAttribute('class', 'rdrop-icon');
    const u = document.createElementNS(NS, 'use');
    u.setAttribute('href', '#i-reads');
    s.append(u);
    return s;
  }

  ui.fileList = el('ul', { class: 'rfiles', 'aria-label': 'Selected files' });
  ui.sketchInfo = el('p', { class: 'detail sketch-info', hidden: true });
  ui.layout = el('div', { class: 'rlayout' });
  ui.fileMsg = el('p', { class: 'input-msg', role: 'alert', hidden: true });
  ui.name = el('input', { type: 'text', id: 'r-name', maxlength: String(NAME_MAX), autocomplete: 'off', spellcheck: 'false', placeholder: 'sample' });
  ui.minAni = el('input', { type: 'number', id: 'r-min-ani', min: '0', max: '100', step: '0.1', value: String(DEFAULT_MIN_ANI), inputmode: 'decimal' });
  ui.minKmers = el('input', { type: 'number', id: 'r-min-kmers', min: '0', step: '1', value: String(DEFAULT_MIN_KMERS), inputmode: 'numeric' });
  ui.unknown = el('input', { type: 'checkbox', id: 'r-unknown' });
  ui.unknownImplied = el('span', { class: 'implied-note', hidden: true, text: ' implied by “Estimate read counts”' });
  ui.readCounts = el('input', { type: 'checkbox', id: 'r-read-counts' });
  ui.readCountsRow = el('label', { class: 'check' }, ui.readCounts,
    el('span', null, el('strong', { text: 'Estimate read counts' }), ' (sylph ', el('code', { text: '--estimate-read-counts' }),
      ', experimental): report the estimated number of reads of each genome instead of its share of the sample. It implies “Estimate unknown fraction”.'));
  ui.sketchOnly = el('input', { type: 'checkbox', id: 'r-sketch-only' });
  ui.start = el('button', { type: 'submit', class: 'btn btn-primary', id: 'r-start', text: 'Sketch and profile', disabled: true });
  ui.hint = el('span', { class: 'submit-hint', hidden: true });
  ui.support = el('div');
  ui.progress = el('section', { class: 'card rprogress', id: 'r-progress', 'aria-label': 'Progress', hidden: true });
  ui.results = el('section', { class: 'results', id: 'r-results', 'aria-live': 'polite' });

  const form = el('form', { id: 'r-form', novalidate: true },
    fileInput, ui.drop, ui.fileList, ui.sketchInfo, ui.layout, ui.fileMsg,
    el('div', { class: 'options' },
      el('label', { class: 'field field-name', for: 'r-name' }, el('span', { text: 'Sample name' }), ui.name)),
    el('details', { class: 'adv' },
      el('summary', { text: 'Options' }),
      el('div', { class: 'options' },
        el('label', { class: 'field', for: 'r-min-ani' }, el('span', { text: 'Minimum ANI (%)' }), ui.minAni),
        el('label', { class: 'field', for: 'r-min-kmers' }, el('span', { text: 'Minimum k-mers per genome' }), ui.minKmers)),
      el('label', { class: 'check' }, ui.unknown,
        el('span', null, el('strong', { text: 'Estimate unknown fraction' }), ' (sylph ', el('code', { text: '--estimate-unknown' }),
          '): also reports how much of the sample the database does not cover.', ui.unknownImplied)),
      ui.readCountsRow,
      el('label', { class: 'check' }, ui.sketchOnly,
        el('span', null, el('strong', { text: 'Only sketch' }), ': do not upload; keep the sketch to download or to upload later.'))),
    el('div', { class: 'actions' }, ui.start, ui.hint));

  root.append(
    el('section', { class: 'card', 'aria-labelledby': 'r-h' },
      el('div', { class: 'card-head' }, el('h2', { id: 'r-h', text: 'Your reads' }),
        el('span', { class: 'muted', text: 'sketched locally, only the sketch is uploaded' })),
      ui.support, form),
    ui.progress, ui.results);

  /* --------------------------------------------------- file selection UI */
  function setFiles(list) {
    const files = Array.from(list || []).filter((f) => f && f.name);
    if (!files.length) return;
    ui.results.replaceChildren();
    st.files = files;
    st.pairDetected = false;
    classify();
    if (st.kind !== 'reads') {
      st.layout = 'single';
      if (!st.nameTouched || !ui.name.value) { st.nameTouched = false; autoName(); }
      renderFiles();
      return;
    }
    if (files.length === 2) {
      st.layout = 'paired';
      const [m0, m1] = [mateOf(files[0].name), mateOf(files[1].name)];
      if (m0 && m1 && m0 !== m1) {
        st.pairDetected = true;
        if (m0 > m1) st.files = [files[1], files[0]];
      } else if (m0 !== m1 && !(m0 && m1)) {
        // only one name carries a mate number: R1 first, R2 last
        if (m0 === 2 || m1 === 1) st.files = [files[1], files[0]];
      } else {
        st.files = files.slice().sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
      }
    } else {
      st.layout = 'single';
    }
    if (!st.nameTouched || !ui.name.value) { st.nameTouched = false; autoName(); }
    renderFiles();
  }

  // What was selected: reads, one ready-made sketch, or something that cannot be run. A sketch's
  // header is read (a few KB) to check c and k against the server before anything is uploaded.
  function classify() {
    const n = st.files.filter(isSylsp).length;
    st.kind = n === 0 ? 'reads' : n < st.files.length ? 'mixed' : n === 1 ? 'sketch' : 'multisketch';
    st.header = null;
    st.pairMismatch = false;
    if (st.kind === 'reads' && st.files.length === 2) {
      const files = st.files.slice();
      Promise.all(files.map(firstRecordName)).then(([a, b]) => {
        if (st.files.length === 2 && files.every((f) => st.files.includes(f))) { st.pairMismatch = !!a && !!b && a !== b; refresh(); }
      });
    }
    if (st.kind !== 'sketch') return;
    const file = st.files[0];
    readSylspHeader(file).then((h) => h, (e) => ({ error: e.message || String(e) })).then((h) => {
      if (st.files[0] === file && st.kind === 'sketch') { st.header = h; renderFiles(); }
    });
  }

  // Name of the first record (FASTQ/FASTA) without a /1 or /2 mate suffix and without the comment
  // after the first space (Illumina "1:N:0:..."), or '' if it cannot be read. Only the first
  // few KB are read; a gzip file is decompressed as far as that goes.
  async function firstRecordName(file) {
    try {
      const [b0, b1] = new Uint8Array(await file.slice(0, 2).arrayBuffer());
      let stream = file.slice(0, 16384).stream();
      if (b0 === 0x1f && b1 === 0x8b) stream = stream.pipeThrough(new DecompressionStream('gzip'));
      const reader = stream.pipeThrough(new TextDecoderStream()).getReader();
      let text = '';
      try {
        while (!text.includes('\n')) {
          const { done, value } = await reader.read();
          if (done) break;
          text += value;
        }
      } catch { /* a cut-off gzip stream ends in an error; keep what was decoded */ }
      reader.cancel().catch(() => {});
      const line = text.split('\n')[0].trim();
      return /^[@>]/.test(line) ? line.slice(1).split(/\s/)[0].replace(/\/[12]$/, '') : '';
    } catch {
      return '';
    }
  }

  function autoName() {
    if (st.nameTouched || !st.files.length) return;
    ui.name.value = sampleFromName(st.files[0].name, st.layout === 'paired');
  }

  function removeFile(i) {
    st.files.splice(i, 1);
    st.pairDetected = false;
    classify();
    st.layout = st.kind === 'reads' && st.files.length === 2 ? 'paired' : 'single';
    autoName();
    renderFiles();
  }

  function renderFiles() {
    const n = st.files.length;
    const total = st.files.reduce((a, f) => a + f.size, 0);
    const roleOf = (i) => (isSylsp(st.files[i]) ? 'sketch' : st.layout === 'paired' ? `R${i + 1}` : st.layout === 'interleaved' ? 'interleaved' : n > 1 ? `part ${i + 1}` : 'reads');
    ui.fileList.replaceChildren(...st.files.map((f, i) => el('li', { class: 'rfile' },
      el('span', { class: `badge ${st.layout === 'paired' ? 'badge-rel rel-closest' : 'badge-plain'}`, text: roleOf(i) }),
      el('span', { class: 'rfile-name', title: f.name, text: f.name }),
      el('span', { class: 'rfile-size', text: fmtBytes(f.size) }),
      el('button', { type: 'button', class: 'link-btn', 'aria-label': `Remove ${f.name}`, text: 'remove', onclick: () => removeFile(i), disabled: st.busy || null }))));
    ui.drop.classList.toggle('has-files', n > 0);

    // layout choices
    const kids = [];
    if (st.kind !== 'reads') {
      // a sketch needs no layout
    } else if (n === 1) {
      kids.push(layoutChoice('single', 'Single-end reads'), layoutChoice('interleaved', 'Interleaved pairs (R1, R2, R1, R2 …)'));
    } else if (n === 2) {
      kids.push(layoutChoice('paired', 'Paired-end: R1 + R2'), layoutChoice('single', 'Two single-end files, one sample'));
      if (st.layout === 'paired') {
        kids.push(el('button', { type: 'button', class: 'btn btn-small', text: '⇄ Swap R1 / R2', disabled: st.busy || null, onclick: () => {
          st.files.reverse(); autoName(); renderFiles();
        } }));
      }
    } else if (n > 2) {
      kids.push(el('span', { class: 'muted', text: `${n} files are read in order and combined into one single-end sample.` }));
    }
    ui.layout.replaceChildren(...kids);
    ui.layout.hidden = !n;

    const sizeNote = n ? `${n === 1 ? '1 file' : `${n} files`}, ${fmtBytes(total)}` : '';
    if (sizeNote) ui.fileList.append(el('li', { class: 'rfile-total', text: sizeNote }));
    refresh();
  }

  // Problems with the selection: `block` stops the run, `warn` is only a hint.
  function fileProblems() {
    const block = [];
    const warn = [];
    const n = st.files.length;
    if (st.files.some((f) => f.size === 0)) block.push('A selected file is empty.');
    if (st.kind === 'mixed') {
      block.push('Sequencing reads and .sylsp sketches cannot be mixed: choose either the reads, or one sketch.');
    } else if (st.kind === 'multisketch') {
      block.push('Upload one .sylsp sketch at a time: a result shows one sample. Remove the others.');
    } else if (st.kind === 'sketch') {
      const problem = st.header && sketchProblem(st.files[0], st.header, S.info(), 'reads');
      if (problem) block.push(problem);
    } else if (n === 2 && st.layout === 'paired') {
      if (!st.pairDetected) warn.push('The file names do not look like a pair (_R1/_R2, _1/_2): check that the order is R1, R2, or choose “Two single-end files”.');
      if (st.pairMismatch) warn.push('The first read names of R1 and R2 differ: these files may not belong together.');
    }
    return { block, warn };
  }

  function layoutChoice(value, label) {
    const input = el('input', { type: 'radio', name: 'r-layout', value, disabled: st.busy || null });
    input.checked = st.layout === value;
    input.addEventListener('change', () => { st.layout = value; autoName(); renderFiles(); });
    return el('label', { class: 'radio' }, input, el('span', { text: label }));
  }

  function chooseFiles() { if (!st.busy) fileInput.click(); }
  ui.drop.addEventListener('click', chooseFiles);
  ui.drop.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); chooseFiles(); } });
  fileInput.addEventListener('change', () => { setFiles(fileInput.files); fileInput.value = ''; });
  let dragDepth = 0;
  const hasFiles = (ev) => ev.dataTransfer && Array.from(ev.dataTransfer.types || []).includes('Files');
  ui.drop.addEventListener('dragenter', (ev) => {
    if (!hasFiles(ev) || st.busy) return;
    ev.preventDefault(); dragDepth++; ui.drop.classList.add('dragging');
  });
  ui.drop.addEventListener('dragover', (ev) => { if (hasFiles(ev)) ev.preventDefault(); });
  ui.drop.addEventListener('dragleave', () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) ui.drop.classList.remove('dragging');
  });
  ui.drop.addEventListener('drop', (ev) => {
    if (!hasFiles(ev)) return;
    ev.preventDefault(); dragDepth = 0; ui.drop.classList.remove('dragging');
    if (!st.busy) setFiles(ev.dataTransfer.files);
  });
  ui.name.addEventListener('input', () => { st.nameTouched = true; refresh(); });
  for (const i of [ui.minAni, ui.minKmers]) i.addEventListener('input', refresh);

  /* -------------------------------------------------------- readiness UI */
  const PROBLEM_HINT = {
    none: 'Public server offline', invalid: 'Invalid API URL', offline: 'Server offline',
  };

  function options() {
    const aniText = ui.minAni.value.trim();
    const kText = ui.minKmers.value.trim();
    const ani = aniText === '' ? null : Number(aniText);
    const kmers = kText === '' ? null : Number(kText);
    const ok = (ani === null || (Number.isFinite(ani) && ani >= 0 && ani <= 100))
      && (kmers === null || (Number.isInteger(kmers) && kmers >= 0));
    const readCounts = ui.readCounts.checked;
    // sylph's --estimate-read-counts implies --estimate-unknown, so the unknown option is
    // shown checked (and disabled) while it is on.
    return { ani, kmers, ok, readCounts, unknown: ui.unknown.checked || readCounts, sketchOnly: ui.sketchOnly.checked };
  }

  // Keep the implied "Estimate unknown fraction" in step with "Estimate read counts";
  // the box is disabled in refresh() so it cannot be turned off while read counts are on.
  let unknownBeforeImplied = false;
  function syncImpliedOptions() {
    const on = ui.readCounts.checked;
    if (on) {
      unknownBeforeImplied = ui.unknown.checked;
      ui.unknown.checked = true;
    } else {
      ui.unknown.checked = unknownBeforeImplied;
    }
    ui.unknownImplied.hidden = !on;
  }

  function dbParams() {
    const info = S.info();
    const db = info && info.database;
    return db && isNum(db.c) && isNum(db.k) ? { c: db.c, k: db.k } : null;
  }

  // The taxonomies this server can aggregate: the columns its metadata file
  // has, plus its sylph-tax sources.
  function requestedTaxonomies() {
    const info = S.info();
    if (!info) return [];
    const meta = info.metadata;
    if (!meta) return [];
    const columns = Array.isArray(meta.columns) ? meta.columns : [];
    const out = [];
    if (columns.includes('gtdb_taxonomy')) out.push('gtdb');
    if (columns.includes('ncbi_taxonomy')) out.push('ncbi');
    if (Array.isArray(meta.taxonomy_sources) && meta.taxonomy_sources.length) out.push('tax');
    return out;
  }

  function refresh() {
    let hint = '';
    const o = options();
    const info = S.info();
    const problem = S.problem();
    const profileOk = !info || !Array.isArray(info.endpoints) || info.endpoints.includes('/profile');
    if (unsupported()) hint = 'Not supported by this browser';
    else if (problem) hint = PROBLEM_HINT[problem] || 'Server unavailable';
    else if (!info) hint = 'Waiting for the server…';
    else if (!profileOk) hint = 'This server has no /profile endpoint';
    else if (!dbParams()) hint = 'The server did not report its sketch parameters';
    else if (st.files.length && !o.ok) hint = 'Check the options';
    const probs = fileProblems();
    const headerOk = st.kind !== 'sketch' || (st.header && !st.header.error);
    const ready = st.files.length > 0 && !probs.block.length && headerOk && !hint;
    ui.fileMsg.textContent = [...probs.block, ...probs.warn].join(' ');
    ui.fileMsg.hidden = !(probs.block.length || probs.warn.length);
    const h = st.kind === 'sketch' && st.header && !st.header.error ? st.header : null;
    ui.sketchInfo.hidden = !h;
    if (h) ui.sketchInfo.textContent = `Ready-made sketch: ${fmtInt(h.kmers)} k-mers, c=${h.c}, k=${h.k}${h.paired ? ', from paired reads' : ''}. It is uploaded as it is, nothing is sketched.`;
    ui.start.disabled = st.busy || !ready;
    ui.hint.textContent = st.files.length ? hint : '';
    ui.hint.hidden = !(st.files.length && hint);
    fileInput.disabled = st.busy;
    ui.name.disabled = st.busy;
    for (const i of [ui.minAni, ui.minKmers, ui.unknown, ui.readCounts, ui.sketchOnly]) i.disabled = st.busy;
    if (ui.readCounts.checked) ui.unknown.disabled = true; // implied by estimated read counts
    ui.drop.classList.toggle('disabled', st.busy);
    ui.drop.setAttribute('aria-disabled', String(st.busy));
    ui.start.textContent = st.kind === 'sketch' ? 'Upload and profile' : o.sketchOnly ? 'Sketch' : 'Sketch and profile';
  }
  ui.unknown.addEventListener('change', refresh);
  ui.readCounts.addEventListener('change', () => { syncImpliedOptions(); refresh(); });
  ui.sketchOnly.addEventListener('change', refresh);
  S.onInfo(refresh);

  const why = unsupported();
  // the module is fetched when the mode is first opened (not for visitors of the assembly mode)
  S.onMode((m) => { if (m === 'reads' && !why) loadModule().catch((e) => showModuleError(e)); });
  if (S.mode() === 'reads' && !why) loadModule().catch((e) => showModuleError(e));
  function showModuleError(e) {
    ui.support.replaceChildren(notice('error', 'The WebAssembly sketcher could not be loaded', e.message || String(e)));
  }

  /* ---------------------------------------------------------- the run */
  const STEPS = [['read', 'Reading & decompressing'], ['sketch', 'Sketching'], ['upload', 'Uploading'], ['profile', 'Profiling']];
  const tabBusy = document.querySelector('.tab-busy');
  const onBeforeUnload = (ev) => { ev.preventDefault(); ev.returnValue = ''; };

  function setBusy(on) {
    st.busy = on;
    if (tabBusy) tabBusy.hidden = !on;
    if (on) window.addEventListener('beforeunload', onBeforeUnload); else window.removeEventListener('beforeunload', onBeforeUnload);
    renderFiles();
  }

  function abortError() { const e = new Error('cancelled'); e.name = 'AbortError'; return e; }

  function sketchPhase(run) {
    const handle = window.SylphdSketch.start({
      files: run.files, layout: run.layout, c: run.c, k: run.k, name: run.name,
      onProgress: (p) => { run.prog.streams = p.streams; run.prog.sketch = p.sketch; },
    });
    run.kill = handle.cancel;
    return handle.done;
  }

  // POST with upload progress (fetch cannot report it). Resolves {status, headers, text}; rejects {network} or AbortError.
  function postForm(run, url, form) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      run.xhr = xhr;
      xhr.open('POST', url);
      xhr.responseType = 'text';
      xhr.upload.onprogress = (ev) => { if (ev.lengthComputable) run.prog.uploaded = ev.loaded; };
      xhr.upload.onload = () => { run.prog.uploaded = run.sketch.size; setPhase(run, 'profile'); };
      xhr.onload = () => resolve({ status: xhr.status, retryAfter: parseInt(xhr.getResponseHeader('Retry-After'), 10), text: xhr.responseText });
      xhr.onerror = () => { const e = new Error('network'); e.network = true; reject(e); };
      xhr.ontimeout = xhr.onerror;
      xhr.onabort = () => reject(abortError());
      xhr.send(form);
    });
  }

  function setPhase(run, phase) {
    run.phase = phase;
    run.phaseAt = performance.now();
    renderProgress(run);
  }

  async function start() {
    const db = dbParams();
    const o = options();
    if (!db || !st.files.length || !o.ok) return;
    const name = (ui.name.value.trim() || sampleFromName(st.files[0].name, st.layout === 'paired')).slice(0, NAME_MAX);
    const fromSketch = st.kind === 'sketch' && st.header && !st.header.error;
    // "Only sketch" sends nothing; a ready-made sketch is always uploaded. Anything that sends
    // needs the agreement to the server's usage statistics first.
    if ((fromSketch || !o.sketchOnly) && !(await window.SylphdUsage.ensureAgreed())) return;
    const run = {
      fromSketch, files: st.files.slice(), layout: st.layout, name, c: fromSketch ? st.header.c : db.c, k: fromSketch ? st.header.k : db.k, opts: o,
      total: st.files.reduce((n, f) => n + f.size, 0), t0: performance.now(), phase: fromSketch ? 'upload' : 'sketch', phaseAt: performance.now(),
      prog: { streams: [], sketch: {}, uploaded: 0 }, sketch: fromSketch ? st.files[0] : null,
      stats: fromSketch ? { kmers: st.header.kmers } : null, cancelled: false, kill: null, xhr: null, warned: new Set(),
    };
    st.run = run;
    ui.results.replaceChildren();
    setBusy(true);
    ui.progress.hidden = false;
    renderProgress(run);
    run.timer = setInterval(() => renderProgress(run), 200);
    ui.progress.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'nearest' });
    if (fromSketch) {
      // a ready-made sketch: no reading or sketching, straight to the upload
      return profilePhase(run).catch((e) => failRun(run, e));
    }
    try {
      const done = await sketchPhase(run);
      run.sketch = new Blob([done.sketch], { type: 'application/octet-stream' });
      run.stats = done.stats;
      run.sketchMs = performance.now() - run.t0;
      run.prog.sketch = { ...run.prog.sketch, reads: done.stats.reads, bases: done.stats.bases, kmers: done.stats.kmers };
      if (o.sketchOnly) return sketchReady(run);
      await profilePhase(run);
    } catch (e) {
      failRun(run, e);
    }
  }

  async function profilePhase(run) {
    // an earlier "Only sketch" run is uploaded from here; declining leaves its result as it was
    if (!(await window.SylphdUsage.ensureAgreed())) return;
    clearInterval(run.timer);
    run.cancelled = false;
    run.timer = setInterval(() => renderProgress(run), 200);
    setBusy(true);
    ui.progress.hidden = false;
    ui.results.replaceChildren();
    run.prog.uploaded = 0;
    setPhase(run, 'upload');
    const t = performance.now();
    const o = run.opts;
    const params = { names: [run.name], estimate_unknown: o.unknown };
    if (o.readCounts) params.estimate_read_counts = true;
    if (o.ani !== null) params.minimum_ani = o.ani;
    if (o.kmers !== null) params.min_number_kmers = o.kmers;
    const taxprof = requestedTaxonomies();
    if (taxprof.length) params.taxprof = taxprof;
    const form = new FormData();
    form.append('sketch', run.sketch, sketchName(run));
    form.append('params', JSON.stringify(params));
    try {
      const res = await postForm(run, `${S.api}/profile`, form);
      let data = null;
      try { data = JSON.parse(res.text); } catch { /* not JSON */ }
      if (res.status < 200 || res.status >= 300) {
        return uploadProblem(run, httpProblem(run, res.status, data && data.error, Number.isFinite(res.retryAfter) ? res.retryAfter : null, res.text));
      }
      if (!Array.isArray(data) || !data[0] || !Array.isArray(data[0].rows)) throw new Error('Unexpected response from the server.');
      run.profileMs = performance.now() - t;
      finishRun(run, data, params);
    } catch (e) {
      if (e.name === 'AbortError') throw e;
      if (e.network) {
        S.loadInfo();
        return uploadProblem(run, {
          kind: 'error', title: 'Could not reach the server', retry: 5,
          body: [el('p', null, 'The upload to ', el('code', { text: S.api }), ' failed before getting a response. The sketch is kept: you can retry without sketching again.')],
        });
      }
      throw e;
    }
  }

  function httpProblem(run, status, serverMsg, retry, raw) {
    const msg = serverMsg || (raw && raw.length < 300 && !raw.trim().startsWith('<') ? raw.trim() : '');
    const detail = msg ? el('p', { class: 'detail', text: `Server message: ${msg}` }) : null;
    const size = fmtBytes(run.sketch.size);
    if (status === 413) {
      return { kind: 'error', title: 'Sketch too large (413)', retry: null, body: [
        el('p', { text: `The sketch (${size}) is over the upload limit of the server or its proxy. Sketches grow with the number of distinct k-mers, so very diverse or very large samples produce big ones.` }),
        el('p', { text: 'Download the sketch and profile it with the sylphd command line, or sketch a subset of the reads.' }), detail] };
    }
    if (status === 429) {
      return { kind: 'warn', title: 'Rate limit reached (429)', retry: retry != null ? retry : 60,
        body: [el('p', null, 'Too many requests from your address. The sketch is kept; you can upload it again in ', el('strong', { 'data-countdown': true, text: `${retry != null ? retry : 60} s` }), '.'), detail] };
    }
    if (status === 503) {
      return { kind: 'warn', title: 'Server busy (503)', retry: retry != null ? retry : 5,
        body: [el('p', null, 'The server queue is full. The sketch is kept; retry in ', el('strong', { 'data-countdown': true, text: `${retry != null ? retry : 5} s` }), '.'), detail] };
    }
    if (status === 404) {
      return { kind: 'error', title: 'Endpoint not available (404)', retry: null, body: [el('p', { text: 'This server does not provide POST /profile.' }), detail] };
    }
    if (status === 400) {
      return { kind: 'error', title: 'The server rejected the request (400)', retry: null, body: [
        el('p', { text: 'The server did not accept the sketch or the options. A sketch must use the database’s c and k; if the server was updated while you were on this page, reload it.' }), detail] };
    }
    return { kind: 'error', title: `Request failed (HTTP ${status})`, retry: status >= 500 ? 10 : null,
      body: [el('p', { text: msg || 'The server returned an error without a message.' })] };
  }

  /* Panel shown when the sketch exists but the profile does not: retry / download. */
  let countdownTimer = null;
  function uploadProblem(run, p) {
    clearInterval(run.timer);
    setBusy(false);
    ui.progress.hidden = true;
    const retryBtn = el('button', { type: 'button', class: 'btn btn-primary', text: 'Upload again', onclick: () => { clearInterval(countdownTimer); resume(run); } });
    const actions = el('div', { class: 'actions' }, p.retry == null ? null : retryBtn, sketchDownloadButton(run));
    const box = el('div', { class: `notice ${p.kind}`, role: p.kind === 'error' ? 'alert' : 'status' }, el('h3', { text: p.title }), ...p.body, actions);
    ui.results.replaceChildren(summaryCard(run, null), box);
    clearInterval(countdownTimer);
    if (p.retry) {
      const until = Date.now() + p.retry * 1000;
      retryBtn.disabled = true;
      const tick = () => {
        const left = Math.ceil((until - Date.now()) / 1000);
        for (const s of box.querySelectorAll('[data-countdown]')) s.textContent = left > 0 ? `${left} s` : 'now';
        retryBtn.disabled = left > 0;
        retryBtn.textContent = left > 0 ? `Upload again in ${left} s` : 'Upload again';
        if (left <= 0) clearInterval(countdownTimer);
      };
      tick();
      countdownTimer = setInterval(tick, 250);
    }
  }

  function resume(run) {
    profilePhase(run).catch((e) => failRun(run, e));
  }

  function sketchReady(run) {
    clearInterval(run.timer);
    setBusy(false);
    ui.progress.hidden = true;
    const go = el('button', { type: 'button', class: 'btn btn-primary', text: 'Upload and profile', disabled: S.problem() ? true : null, onclick: () => {
      run.opts = { ...run.opts, sketchOnly: false }; resume(run);
    } });
    ui.results.replaceChildren(summaryCard(run, null),
      el('div', { class: 'notice info' }, el('h3', { text: 'Sketch ready' }),
        el('p', { text: `The sketch (${fmtBytes(run.sketch.size)}) exists only in this browser tab. Download it, or upload it to get the taxonomic profile.` }),
        el('div', { class: 'actions' }, sketchDownloadButton(run, true), go)));
  }

  function failRun(run, e) {
    clearInterval(run.timer);
    setBusy(false);
    ui.progress.hidden = true;
    if (e && e.name === 'AbortError') {
      if (run.sketch) {
        ui.results.replaceChildren(summaryCard(run, null),
          notice('warn', 'Cancelled', 'The upload was cancelled; the sketch is still here.'),
          el('div', { class: 'actions' },
            el('button', { type: 'button', class: 'btn btn-primary', text: 'Upload again', onclick: () => resume(run) }), sketchDownloadButton(run)));
      } else {
        ui.results.replaceChildren(notice('warn', 'Cancelled', 'Sketching was cancelled. Nothing was uploaded.'));
      }
      return;
    }
    ui.results.replaceChildren(notice('error', run.sketch ? 'Something went wrong' : 'Could not sketch the reads', e && e.message ? e.message : String(e)));
  }

  /* ----------------------------------------------------------- progress */
  ui.stepEls = {};
  ui.steps = el('ol', { class: 'steps' }, STEPS.map(([key, label], i) => {
    const li = el('li', { 'data-state': 'todo', 'data-step': key }, el('span', { class: 'step-dot', text: String(i + 1) }), el('span', { class: 'step-label', text: label }));
    ui.stepEls[key] = li;
    return li;
  }));
  ui.bar = el('div', { class: 'pbar', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100' }, el('span'));
  ui.barText = el('div', { class: 'pbar-text' });
  ui.stats = el('dl', { class: 'summary pstats' });
  ui.warn = el('div', { class: 'pwarn' });
  ui.cancel = el('button', { type: 'button', class: 'btn', text: 'Cancel', onclick: cancel });
  ui.progress.append(
    el('div', { class: 'card-head' }, el('h2', { id: 'r-prog-h', text: 'Working…' }), ui.cancel),
    ui.steps, ui.bar, ui.barText, ui.stats, ui.warn);

  function cancel() {
    const run = st.run;
    if (!run) return;
    run.cancelled = true;
    if (run.phase === 'sketch' && run.kill) run.kill();
    else if (run.xhr) run.xhr.abort();
  }

  function stat(label, value, small) {
    return el('div', null, el('dt', { text: label }), el('dd', null, value, small ? el('small', { text: small }) : null));
  }

  function renderProgress(run) {
    const now = performance.now();
    const p = run.prog;
    const sk = p.sketch || {};
    const read = (p.streams || []).reduce((n, x) => n + (x.read || 0), 0);
    const decompressed = (p.streams || []).reduce((n, x) => n + (x.decompressed || 0), 0);
    const readDone = (p.streams || []).length > 0 && p.streams.every((x) => x.finished);
    const states = { read: 'todo', sketch: 'todo', upload: 'todo', profile: 'todo' };
    let frac = 0;
    let indeterminate = false;
    if (run.fromSketch) {
      states.read = 'skipped'; states.sketch = 'skipped';
      if (run.phase === 'upload') { states.upload = 'active'; frac = Math.min(1, p.uploaded / run.sketch.size); }
      else { states.upload = 'done'; states.profile = 'active'; indeterminate = true; }
    } else if (run.phase === 'sketch') {
      states.read = readDone ? 'done' : 'active';
      states.sketch = 'active';
      frac = run.total ? Math.min(1, read / run.total) : 0;
    } else {
      states.read = 'done'; states.sketch = 'done';
      if (run.phase === 'upload') { states.upload = 'active'; frac = run.sketch ? Math.min(1, p.uploaded / run.sketch.size) : 0; }
      else { states.upload = 'done'; states.profile = 'active'; indeterminate = true; }
    }
    for (const [k, v] of Object.entries(states)) ui.stepEls[k].dataset.state = v;
    ui.bar.classList.toggle('indeterminate', indeterminate);
    ui.bar.firstChild.style.width = indeterminate ? '' : `${(frac * 100).toFixed(1)}%`;
    ui.bar.setAttribute('aria-valuenow', indeterminate ? '' : String(Math.round(frac * 100)));
    const elapsed = now - run.t0;
    const sketchElapsed = run.phase === 'sketch' ? elapsed : (run.sketchMs || elapsed);
    const rate = sketchElapsed > 500 ? read / (sketchElapsed / 1000) : 0;
    document.getElementById('r-prog-h').textContent = { sketch: 'Sketching your reads…', upload: 'Uploading the sketch…', profile: 'Profiling on the server…' }[run.phase];

    if (run.phase === 'sketch') {
      let eta = '';
      if (rate > 0 && sketchElapsed > 1500 && read < run.total) eta = ` · about ${fmtDur(((run.total - read) / rate) * 1000)} left`;
      ui.barText.textContent = `${fmtBytes(read)} of ${fmtBytes(run.total)} read (${(frac * 100).toFixed(0)} %)${eta}`;
    } else if (run.phase === 'upload') {
      ui.barText.textContent = `${fmtBytes(p.uploaded)} of ${fmtBytes(run.sketch.size)} sketch uploaded`;
    } else {
      ui.barText.textContent = `Sketch uploaded (${fmtBytes(run.sketch.size)}); the server is profiling it…`;
    }
    if (run.fromSketch) {
      ui.stats.replaceChildren(
        stat('Sketch k-mers', fmtInt(run.stats.kmers), `c=${run.c}, k=${run.k}`),
        stat('Sketch size', fmtBytes(run.sketch.size)),
        stat('Elapsed', fmtDur(elapsed)));
      ui.warn.replaceChildren();
      ui.cancel.textContent = 'Cancel upload';
      return;
    }
    ui.stats.replaceChildren(
      stat(run.layout === 'single' ? 'Reads' : 'Read pairs', fmtInt(sk.reads || 0)),
      stat('Bases', fmtBp(sk.bases || 0)),
      stat('Sketch k-mers', fmtInt(sk.kmers || 0)),
      stat('Elapsed', fmtDur(elapsed)),
      stat('Speed', rate ? `${fmtBytes(rate)}/s` : '–', decompressed ? `${fmtBytes(decompressed)} decompressed` : ''));

    // memory / size warnings
    const warns = [];
    const mem = sk.wasmMemory || 0;
    if (mem > MEM_WARN && run.phase === 'sketch') {
      warns.push(notice('warn', 'Large sample', `The sketcher already uses ${fmtBytes(mem)} of the 4 GB a browser tab can give it. If it runs out of memory, sketching stops and you need the sylphd command line for this sample.`));
    }
    const info = S.info();
    const maxMb = info && info.limits && info.limits.max_upload_mb;
    const est = (sk.kmers || 0) * 12;
    if (isNum(maxMb) && est > maxMb * 1048576 && run.phase === 'sketch') {
      warns.push(notice('warn', 'The sketch will be too large to upload', `About ${fmtBytes(est)} (the server accepts ${maxMb} MB). You can still download it when sketching has finished.`));
    }
    ui.warn.replaceChildren(...warns);
    ui.cancel.textContent = run.phase === 'sketch' ? 'Cancel' : 'Cancel upload';
  }

  /* ------------------------------------------------------------ results */
  function sketchDownloadButton(run, primary) {
    return el('button', { type: 'button', class: primary ? 'btn' : 'btn', title: 'The sketch in sylph .sylsp format (k-mer hashes and counts, no sequences)', text: `Download sketch (.sylsp, ${fmtBytes(run.sketch.size)})`, onclick: () => {
      const url = URL.createObjectURL(run.sketch);
      const a = el('a', { href: url, download: sketchName(run) });
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    } });
  }

  function finishRun(run, data, params) {
    clearInterval(run.timer);
    clearInterval(countdownTimer);
    setBusy(false);
    ui.progress.hidden = true;
    const sample = data[0];
    const rows = sample.rows.slice().sort((x, y) => (y.Taxonomic_abundance || 0) - (x.Taxonomic_abundance || 0));
    run.result = { data, sample, rows, params, taxprof: sample.taxprof || {} };
    // Pick a taxonomy with real clades: a GTDB profile of a UHGV-only sample
    // is one "unclassified" tree, so fall through to NCBI and then "tax".
    const withData = taxonomiesWithData(sample);
    if (withData.length && !withData.includes(view.taxonomy)) view.taxonomy = withData[0];
    view.rank = startRank(sample, view.taxonomy);
    view.showAll = false;
    const out = [summaryCard(run, run.result)];
    if (!rows.length) out.push(emptyCard(run));
    else {
      if (!withData.length) {
        const body = Object.keys(run.result.taxprof).length
          ? 'The server’s taxonomic profile has no classified clade for these hits. The table below lists the genomes with their own taxonomy.'
          : 'This server did not return a taxonomic profile (it has no taxonomy metadata). The table below lists the genomes only.';
        out.push(notice('warn', 'No taxonomic profile', body));
      } else {
        out.push(abundanceCard(run));
      }
      out.push(tableCard(run));
    }
    ui.results.replaceChildren(...out);
    ui.results.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
  }

  function unknownFraction(rows) {
    return Math.max(0, 100 - rows.reduce((n, r) => n + (isNum(r.Sequence_abundance) ? r.Sequence_abundance : 0), 0));
  }

  function summaryCard(run, result) {
    const item = (label, value, small) => el('div', null, el('dt', { text: label }), el('dd', null, value, small ? el('small', { text: small }) : null));
    const s = run.stats || {};
    if (run.fromSketch) {
      const items = [
        item('Sample', run.name, 'uploaded sketch'),
        item('Sketch', `${fmtInt(s.kmers)} k-mers`, `${fmtBytes(run.sketch.size)} · c=${run.c}, k=${run.k}`),
      ];
      if (result) {
        items.push(item('Genomes detected', fmtInt(result.rows.length)));
        items.push(item('Profiling', fmtDur(run.profileMs), 'upload and server'));
        // with estimated read counts the sum of Sequence_abundance is reads, not a share
        if (result.params.estimate_unknown && !result.params.estimate_read_counts) {
          items.push(item('Unknown', fmtShare(unknownFraction(result.rows)), 'not covered by the database'));
        }
      }
      return el('section', { class: 'card', 'aria-label': 'Sample summary' },
        el('div', { class: 'card-head' }, el('h2', { text: 'Summary' }),
          el('span', { class: 'badge badge-plain', title: 'Reads were not involved: a ready-made sketch was uploaded as it is.' }, 'uploaded sketch')),
        el('dl', { class: 'summary rsummary' }, items));
    }
    const items = [
      item('Sample', run.name, run.layout === 'paired' ? 'paired-end' : run.layout === 'interleaved' ? 'interleaved pairs' : 'single-end'),
      item(run.layout === 'single' ? 'Reads' : 'Read pairs', fmtInt(s.reads), fmtBp(s.bases)),
      item('Sketch', `${fmtInt(s.kmers)} k-mers`, `${fmtBytes(run.sketch.size)} · c=${run.c}, k=${run.k}`),
    ];
    const speed = run.sketchMs ? `${fmtBytes(run.total / (run.sketchMs / 1000))}/s` : '';
    items.push(item('Sketching', fmtDur(run.sketchMs || 0), speed));
    if (result) {
      items.splice(3, 0, item('Genomes detected', fmtInt(result.rows.length)));
      items.push(item('Profiling', fmtDur(run.profileMs), 'upload and server'));
      if (result.params.estimate_unknown && !result.params.estimate_read_counts) {
        items.push(item('Unknown', fmtShare(unknownFraction(result.rows)), 'not covered by the database'));
      }
    }
    return el('section', { class: 'card', 'aria-label': 'Sample summary' },
      el('div', { class: 'card-head' }, el('h2', { text: 'Summary' }),
        el('div', { class: 'export' }, sketchDownloadButton(run))),
      el('dl', { class: 'summary rsummary' }, items));
  }

  function emptyCard(run) {
    const min = run.opts.ani;
    return el('section', { class: 'card empty' },
      el('h2', { text: 'No genomes detected' }),
      el('p', null, `No database genome passed the thresholds${min != null ? ` (minimum ANI ${min} %)` : ''}. Likely reasons:`),
      el('ul', null,
        el('li', { text: 'The sample contains no bacteria or archaea that are close to a GTDB genome (GTDB covers only those), or they are present at very low coverage.' }),
        el('li', { text: 'The sample is small: sylph needs enough reads to see a genome’s k-mers (about 0.1x coverage or more).' }),
        el('li', { text: 'Try a lower minimum ANI or fewer required k-mers under Options (below the default, results get less reliable).' })));
  }

  /* ---------------------------------------------------- abundance chart */
  // The server does all taxonomic aggregation: `sample.taxprof[taxonomy]`
  // holds the clade table (clade_name, relative/sequence abundance, and the
  // ANI/coverage of strain rows) and the rendered .sylphmpa text. This section
  // only groups those clades by rank/level for the chart.
  const TOP_GROUPS = 10;
  // The chart's taxonomy, rank and measure are remembered in this browser.
  const PREF = { taxonomy: 'sylphd.reads.taxonomy', rank: 'sylphd.reads.rank', metric: 'sylphd.reads.metric' };
  const view = {
    taxonomy: S.readChoice(PREF.taxonomy, Object.keys(TAXONOMY_LABELS)) || 'gtdb',
    rank: 's',
    metric: S.readChoice(PREF.metric, ['relative', 'sequence']) || 'relative',
    showAll: false,
  };
  const choose = (key, value) => { view[key] = value; S.writeChoice(PREF[key], value); };

  const payloadFor = (sample, taxonomy) => (sample.taxprof || {})[taxonomy] || null;
  const cladeDepth = (clade) => String(clade).split('|').length;
  const lastToken = (clade) => String(clade).slice(String(clade).lastIndexOf('|') + 1);
  const tokenName = (token) => {
    const at = token.indexOf('__');
    return at >= 0 ? token.slice(at + 2) : token;
  };

  function hasClassifiedClades(payload) {
    if (!payload || !Array.isArray(payload.clades) || !payload.clades.length) return false;
    return payload.clades.some((c) => !['unclassified', 'NO_TAXONOMY'].includes(String(c.clade_name).split('|')[0]));
  }

  function taxonomiesWithData(sample) {
    return Object.keys(sample.taxprof || {}).filter((taxonomy) => hasClassifiedClades(payloadFor(sample, taxonomy)));
  }

  function defaultRank(sample, taxonomy) {
    if (taxonomy !== 'tax') return 's';
    // The deepest level whose clades are not strain rows (t__), i.e. the
    // species-equivalent level of the source's own scheme.
    let best = 1;
    for (const clade of (payloadFor(sample, taxonomy) || {}).clades || []) {
      const depth = cladeDepth(clade.clade_name);
      if (!lastToken(clade.clade_name).startsWith('t__')) best = Math.max(best, depth);
    }
    return String(best);
  }

  function rankOptions(sample, taxonomy) {
    if (taxonomy === 'tax') {
      const depths = ((payloadFor(sample, taxonomy) || {}).clades || []).map((c) => cladeDepth(c.clade_name));
      const max = Math.max(1, ...depths);
      return Array.from({ length: max }, (_, i) => [String(i + 1), `Level ${i + 1}`]);
    }
    return RANKS.slice().reverse().map((r) => [r, rankTitle(r)]);
  }

  // The remembered rank when this taxonomy has it, else the default one.
  function startRank(sample, taxonomy) {
    return S.readChoice(PREF.rank, rankOptions(sample, taxonomy).map(([value]) => value)) || defaultRank(sample, taxonomy);
  }

  function rankDepth(taxonomy, rank) {
    return taxonomy === 'tax' ? Number(rank) : RANKS.indexOf(rank) + 1;
  }

  function rankLabel(taxonomy, rank) {
    return taxonomy === 'tax' ? `level ${rank}` : RANK_NAMES[rank];
  }

  const PLURAL = { d: ['domain', 'domains'], p: ['phylum', 'phyla'], c: ['class', 'classes'], o: ['order', 'orders'], f: ['family', 'families'], g: ['genus', 'genera'], s: ['species', 'species'] };

  function rankNoun(taxonomy, rank, count) {
    if (taxonomy === 'tax') return count === 1 ? 'level' : 'levels';
    return count === 1 ? PLURAL[rank][0] : PLURAL[rank][1];
  }

  // Clades of exactly this depth, summed per full path (each clade name is
  // one path). `none` is every row that does not reach the level (including
  // the server's "unclassified"/"NO_TAXONOMY" trees), derived from the rows
  // because that share has no clade of its own.
  function cladeGroups(sample, taxonomy, rank, metric) {
    const payload = payloadFor(sample, taxonomy) || {};
    const depth = rankDepth(taxonomy, rank);
    const valueOf = metric === 'sequence' ? 'sequence_abundance' : 'relative_abundance';
    let total = 0;
    for (const row of sample.rows || []) {
      total += Number((metric === 'sequence' ? row.Sequence_abundance : row.Taxonomic_abundance) || 0);
    }
    const groups = [];
    let classified = 0;
    for (const clade of payload.clades || []) {
      if (cladeDepth(clade.clade_name) !== depth) continue;
      if (['unclassified', 'NO_TAXONOMY'].includes(String(clade.clade_name).split('|')[0])) continue;
      const value = Number(clade[valueOf] || 0);
      groups.push({ key: clade.clade_name, name: tokenName(lastToken(clade.clade_name)), value });
      classified += value;
    }
    groups.sort((a, b) => b.value - a.value);
    return { groups, none: Math.max(0, total - classified), total };
  }

  const italicRank = (rank, name) => (rank === 'g' || rank === 's') && !/^unclassified/i.test(name);

  function abundanceCard(run) {
    const { rows, params, sample } = run.result;
    const readCounts = !!params.estimate_read_counts;
    const card = el('section', { class: 'card', 'aria-label': 'Abundance' });
    const body = el('div', { class: 'abund' });
    const dlBtn = el('button', { type: 'button', class: 'btn' });
    const draw = () => {
      body.replaceChildren(...abundanceBody(run).filter(Boolean));
      dlBtn.textContent = `Download sylphmpa (${TAXONOMY_LABELS[view.taxonomy] || view.taxonomy})`;
    };
    dlBtn.addEventListener('click', () => downloadSylphmpa(run));
    const taxonomies = taxonomiesWithData(sample);
    const taxonomyOptions = (taxonomies.length ? taxonomies : [view.taxonomy]).map((t) => [t, TAXONOMY_LABELS[t] || t, TAXONOMY_TITLES[t] || '']);
    const controls = el('div', { class: 'abund-controls' },
      el('div', { class: 'ctl' }, el('span', { class: 'ctl-label', text: 'Taxonomy' }),
        S.seg('Taxonomy', 'r-tax', taxonomyOptions, view.taxonomy, (v) => {
          choose('taxonomy', v);
          view.rank = startRank(sample, v);
          view.showAll = false;
          draw();
        })),
      el('div', { class: 'ctl' }, el('span', { class: 'ctl-label', text: view.taxonomy === 'tax' ? 'Level' : 'Rank' }),
        S.seg(view.taxonomy === 'tax' ? 'Level' : 'Rank', 'r-rank', rankOptions(sample, view.taxonomy), view.rank, (v) => { choose('rank', v); view.showAll = false; draw(); })),
      el('div', { class: 'ctl' }, el('span', { class: 'ctl-label', text: 'Abundance' }),
        S.seg('Abundance measure', 'r-metric', [
          ['relative', 'Taxonomic', 'Share of the organisms, corrected for genome size'],
          ['sequence', readCounts ? 'Reads (estimated)' : 'Sequence', readCounts
            ? 'Estimated number of reads of each genome (sylph --estimate-read-counts)'
            : 'Share of the reads (k-mers) assigned to the genome'],
        ], view.metric, (v) => { choose('metric', v); draw(); })));
    card.append(el('div', { class: 'card-head' }, el('h2', { text: 'Abundance' }), el('div', { class: 'export' }, dlBtn)), controls, body);
    draw();
    // the unknown fraction is a share of the sample, which estimated read counts do not give
    if (params.estimate_unknown && !readCounts) {
      card.append(el('p', { class: 'detail abund-note', text: `Estimated unknown fraction: ${fmtShare(unknownFraction(rows))} of the sample is not covered by the database (shown in the sequence abundance view; taxonomic abundance is relative to the genomes found).` }));
    }
    return card;
  }

  function abundanceBody(run) {
    const { rows, params, sample } = run.result;
    // With estimated read counts the "sequence" measures are reads, not percentages, so
    // they are shown as integers and the unknown fraction (a share of the sample) is off.
    const readCounts = !!params.estimate_read_counts;
    const reads = readCounts && view.metric === 'sequence';
    const fmtVal = reads ? (v) => fmtInt(v) : fmtShare;
    const { groups, none } = cladeGroups(sample, view.taxonomy, view.rank, view.metric);
    const level = rankLabel(view.taxonomy, view.rank);
    const unknown = params.estimate_unknown && !readCounts && view.metric === 'sequence' ? unknownFraction(rows) : 0;
    const shown = groups.slice(0, TOP_GROUPS);
    const rest = groups.slice(TOP_GROUPS);
    const restValue = rest.reduce((n, g) => n + g.value, 0);
    const segs = shown.map((g, i) => ({ label: g.name, value: g.value, cls: `cat-${(i % 10) + 1}`, rank: view.rank }));
    if (restValue > 0) segs.push({ label: `Other (${rest.length} ${rest.length === 1 ? 'taxon' : 'taxa'})`, value: restValue, cls: 'cat-other' });
    if (none > 0.0005) segs.push({ label: `Not classified at ${level} level`, value: none, cls: 'cat-none', plain: true });
    if (unknown > 0.0005) segs.push({ label: 'Unknown (not in the database)', value: unknown, cls: 'cat-unknown', plain: true });
    const sum = segs.reduce((n, s) => n + s.value, 0) || 1;
    const scale = Math.max(100, sum);

    const bar = el('div', { class: 'stack', role: 'img', 'aria-label': `Abundance by ${level}: ${segs.slice(0, 3).map((s) => `${s.label} ${fmtVal(s.value)}`).join(', ')}` },
      segs.map((s) => el('span', { class: `seg-${s.cls}`, style: `width:${((s.value / scale) * 100).toFixed(3)}%`, title: `${s.label}: ${fmtVal(s.value)}` })));

    const maxV = Math.max(...segs.map((s) => s.value), 1e-9);
    const rowsEl = [];
    const listRow = (name, value, cls, italic) => S.barRow({ swatch: `seg-${cls}`, name, value, max: maxV, text: fmtVal(value), italic });
    const listed = [...shown.map((g, i) => ({ g, cls: `cat-${(i % 10) + 1}` })), ...rest.map((g) => ({ g, cls: 'cat-other' }))];
    const limit = view.showAll ? listed.length : 15;
    for (const { g, cls } of listed.slice(0, limit)) rowsEl.push(listRow(g.name, g.value, cls, italicRank(view.rank, g.name)));
    if (none > 0.0005) rowsEl.push(listRow(`Not classified at ${level} level`, none, 'cat-none'));
    if (unknown > 0.0005) rowsEl.push(listRow('Unknown (not in the database)', unknown, 'cat-unknown'));
    const more = listed.length > 15
      ? el('button', { type: 'button', class: 'link-btn', text: view.showAll ? 'show fewer' : `show all ${listed.length}`, onclick: () => {
        view.showAll = !view.showAll;
        const host = bar.parentNode;
        host.replaceChildren(...abundanceBody(run).filter(Boolean));
      } }) : null;
    const head = el('p', { class: 'abund-head muted' },
      `${groups.length} ${rankNoun(view.taxonomy, view.rank, groups.length)}`,
      ` · ${view.metric === 'relative' ? 'taxonomic abundance' : reads ? 'estimated reads' : 'sequence abundance'}, ${TAXONOMY_LABELS[view.taxonomy] || view.taxonomy}`);
    return [head, bar, el('ul', { class: 'alist' }, rowsEl), more ? el('p', null, more) : null];
  }

  function downloadSylphmpa(run) {
    const payload = payloadFor(run.result.sample, view.taxonomy);
    if (!payload || typeof payload.sylphmpa !== 'string') return;
    download(exportAs(run, `${view.taxonomy}.sylphmpa`), payload.sylphmpa, 'text/tab-separated-values');
  }

  /* ------------------------------------------------------- genome table */
  const covOf = (r) => (isNum(r.Eff_cov) ? r.Eff_cov : isNum(r.True_cov) ? r.True_cov : null);

  // A row without GTDB taxonomy may carry tax_lineage/tax_source from a sylph-tax source
  // ('vFAM-..;vOTU-..', 'root;Monodnaviria;..', 'k__Eukaryota;..;s__..'): show its last rank.
  function taxLineage(r) {
    if (!has(r.tax_lineage)) return [];
    return String(r.tax_lineage).split(';').map((t) => t.trim()).filter(Boolean).map((token) => {
      const m = /^([a-z]{1,4})__(.*)$/.exec(token);
      return m ? { rank: m[1], name: m[2] } : { rank: '', name: token };
    });
  }

  function tableCard(run) {
    const { rows, params } = run.result;
    const readCounts = !!params.estimate_read_counts;
    const covLabel = params.estimate_unknown || readCounts ? 'True cov.' : 'Eff. cov.';
    const seqLabel = readCounts ? 'Reads (estimated)' : 'Sequence %';
    const hits = rows.map((r, i) => {
      const lin = S.parseLineage(r.gtdb_taxonomy);
      const sp = lin.find((e) => e.rank === 's');
      const tax = lin.length ? [] : taxLineage(r);
      return { r, i: i + 1, lin, tax, species: sp ? sp.name : (tax.length ? tax[tax.length - 1].name : '') };
    });
    const taxonLabel = hits.some((h) => h.tax.length) ? 'Taxon' : 'GTDB species';
    const maxTax = Math.max(...hits.map((h) => h.r.Taxonomic_abundance || 0), 1e-9);
    const COLS = [
      { key: 'rank', label: '#', get: (h) => h.i, dir: 'asc' },
      { key: 'species', label: taxonLabel, get: (h) => h.species, dir: 'asc' },
      { key: 'tax', label: 'Taxonomic %', hint: 'taxonomic abundance', get: (h) => h.r.Taxonomic_abundance, dir: 'desc' },
      { key: 'seq', label: seqLabel, hint: readCounts ? 'estimated number of reads' : 'sequence abundance', get: (h) => h.r.Sequence_abundance, dir: 'desc' },
      { key: 'ani', label: 'ANI %', hint: 'adjusted ANI', get: (h) => h.r.Adjusted_ANI, dir: 'desc' },
      { key: 'cov', label: covLabel, hint: covLabel === 'True cov.' ? 'true (estimated) coverage' : 'effective coverage', get: (h) => covOf(h.r), dir: 'desc' },
      { key: 'ncbi', label: 'NCBI organism', get: (h) => h.r.ncbi_organism_name, dir: 'asc' },
      { key: 'acc', label: 'Accession', get: (h) => h.r.accession, dir: 'asc' },
    ];
    const tv = { sortKey: 'rank', sortDir: 'asc', open: new Set(), all: false };
    const td = (label, cls, ...content) => el('td', { class: cls, 'data-label': label }, el('div', { class: 'cv' }, content));
    const dash = () => el('span', { class: 'dash', text: '–' });
    function row({ r, i, lin, tax, species }) {
      const key = r.accession || r.Genome_file || `#${i}`;
      const ci = Array.isArray(r['ANI_5-95_percentile']) ? r['ANI_5-95_percentile'] : null;
      const cont = Array.isArray(r.Containment_ind) ? r.Containment_ind : null;
      const lineage = lin.length ? lin : tax;
      const source = S.hitSource(r);
      return el('tr', { class: i === 1 ? 'top-row' : null },
        td('#', 'num rank', String(i)),
        td(taxonLabel, null, lineage.length ? el('details', { class: 'lineage', 'data-key': key, open: tv.open.has(key) ? '' : null },
          el('summary', { title: `Show the full lineage from ${source}` },
            lin.length ? el('i', { text: species || '(no species)' }) : el('span', { text: species || '(no taxon)' })),
          el('ol', null, lineage.map((e, j) => el('li', null,
            el('b', { text: rankTitle(e.rank) || `level ${j + 1}` }), el('span', { text: e.name || '–' }))))) : dash()),
        td('Taxonomic %', 'num', el('div', { class: 'anibar' }, el('b', { text: isNum(r.Taxonomic_abundance) ? r.Taxonomic_abundance.toFixed(2) : '–' }),
          el('div', { class: 'bar rel-closest', 'aria-hidden': 'true' }, el('span', { style: `width:${(((r.Taxonomic_abundance || 0) / maxTax) * 100).toFixed(1)}%` })))),
        td(seqLabel, 'num', isNum(r.Sequence_abundance)
          ? (readCounts
            ? el('span', { title: 'Estimated number of reads of this genome (sylph --estimate-read-counts)', text: fmtInt(r.Sequence_abundance) })
            : r.Sequence_abundance.toFixed(2))
          : dash()),
        td('ANI %', 'num', isNum(r.Adjusted_ANI) ? r.Adjusted_ANI.toFixed(2) : dash(),
          ci && ci.every(isNum) ? el('div', { class: 'sub', title: '5–95 % confidence interval of the adjusted ANI', text: `${ci[0].toFixed(1)}–${ci[1].toFixed(1)}` }) : null),
        td(covLabel, 'num', isNum(covOf(r)) ? covOf(r).toFixed(covOf(r) < 10 ? 2 : 1) : dash(),
          cont ? el('div', { class: 'sub', title: 'Contained k-mers / genome k-mers', text: `${fmtInt(cont[0])}/${fmtInt(cont[1])} k-mers` }) : null),
        td('NCBI organism', null, has(r.ncbi_organism_name) || has(r.ncbi_taxid)
          ? el('div', null, has(r.ncbi_organism_name) ? r.ncbi_organism_name : dash(),
            has(r.ncbi_strain_identifiers) ? el('div', { class: 'sub', text: `strain ${r.ncbi_strain_identifiers}` }) : null,
            has(r.ncbi_taxid) ? el('div', { class: 'sub' }, 'taxid ', S.taxidLink(r.ncbi_taxid)) : null) : dash()),
        td('Accession', null, S.accessionLinks(r.accession, S.hitSource(r))));
    }

    const table = S.sortableTable({
      cols: COLS, state: tv, items: () => hits, tiebreak: (x, y) => x.i - y.i, row,
      view: (list) => (tv.all ? list : list.slice(0, MAX_ROWS_SHOWN)),
      cls: 'hits reads-hits', id: 'r-sort-select', noun: 'genomes',
    });
    const more = el('p', { class: 'more-rows' });
    const renderMore = () => more.replaceChildren(...(hits.length > MAX_ROWS_SHOWN
      ? [el('button', { type: 'button', class: 'btn btn-small', text: tv.all ? `Show only the first ${MAX_ROWS_SHOWN}` : `Show all ${hits.length} genomes`, onclick: () => { tv.all = !tv.all; table.update(); renderMore(); } })] : []));
    renderMore();

    return el('section', { class: 'card', 'aria-label': 'Detected genomes' },
      el('div', { class: 'card-head' },
        el('h2', { text: `Detected genomes (${hits.length})` }),
        S.exportButtons(() => downloadTsv(run), () => S.downloadJson(exportAs(run, 'json'), run.result.data))),
      ...table.nodes, more);
  }

  /* TSV like `sylphd profile`: sylph columns, then accession and the metadata columns. */
  const PROFILE_COLUMNS = ['Sample_file', 'Genome_file', 'Taxonomic_abundance', 'Sequence_abundance', 'Adjusted_ANI', 'Eff_cov', 'ANI_5-95_percentile',
    'Eff_lambda', 'Lambda_5-95_percentile', 'Median_cov', 'Mean_cov_geq1', 'Containment_ind', 'Naive_ANI', 'kmers_reassigned', 'Contig_name'];
  function tsvValue(column, v) {
    if (v == null) return '';
    const fixed = (d) => Number(v).toFixed(d);
    if (['Adjusted_ANI', 'Naive_ANI'].includes(column)) return fixed(2);
    if (['Eff_cov', 'True_cov', 'Mean_cov_geq1', 'Eff_lambda'].includes(column)) return fixed(3);
    if (['ANI_5-95_percentile', 'Lambda_5-95_percentile'].includes(column)) return Array.isArray(v) ? `${Number(v[0]).toFixed(2)}-${Number(v[1]).toFixed(2)}` : String(v);
    if (column === 'Median_cov') return fixed(0);
    if (column === 'Containment_ind') return Array.isArray(v) ? `${v[0]}/${v[1]}` : String(v);
    if (['Taxonomic_abundance', 'Sequence_abundance'].includes(column)) return fixed(4);
    if (column === 'kmers_reassigned') return String(Math.trunc(Number(v)));
    return String(v).replace(/[\t\r\n]+/g, ' ');
  }
  function downloadTsv(run) {
    const { rows, params } = run.result;
    const base = PROFILE_COLUMNS.slice();
    // as in `sylph profile`: True_cov is the header when estimate_unknown (or the
    // estimate_read_counts that implies it) is set
    if (params.estimate_unknown || params.estimate_read_counts) base[base.indexOf('Eff_cov')] = 'True_cov';
    const info = S.info();
    let metaCols = info && info.metadata && Array.isArray(info.metadata.columns) ? info.metadata.columns : null;
    const known = new Set([...PROFILE_COLUMNS, 'True_cov', 'accession', 'Lambda_status']);
    if (!metaCols) {
      metaCols = [];
      for (const r of rows) for (const k of Object.keys(r)) if (!known.has(k) && !metaCols.includes(k)) metaCols.push(k);
    }
    const header = [...base, 'accession', ...metaCols];
    const lines = [header.join('\t')];
    for (const r of rows) {
      lines.push(header.map((c) => tsvValue(c, c === 'True_cov' && r.True_cov == null ? r.Eff_cov : r[c])).join('\t'));
    }
    download(exportAs(run, 'tsv'), `${lines.join('\n')}\n`, 'text/tab-separated-values');
  }

  /* ---------------------------------------------------------------- init */
  form.addEventListener('submit', (ev) => { ev.preventDefault(); if (!ui.start.disabled) start(); });
  // a stray drop outside the box would make the browser open the file
  for (const t of ['dragover', 'drop']) {
    window.addEventListener(t, (ev) => { if (hasFiles(ev) && !ui.drop.contains(ev.target)) ev.preventDefault(); });
  }
  renderFiles();
  window.SylphdReads = { setFiles, start, state: st }; // for tests / debugging
})();
