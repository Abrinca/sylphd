// Taxonomic aggregation of /profile rows: a port of src/sylphd/taxonomy.py
// (the lineage rules, aggregate_rows and taxprof_lines), plus per-rank grouping
// for the abundance chart. Pure functions, no DOM; also loadable from node
// (tests/test_web_taxonomy_js.py compares it with the Python module).
//
// Lineage rules (same as the Python module):
//  * taxonomy strings are split on ';', tokens without a known rank prefix are ignored;
//  * trailing empty ranks are dropped (the lineage ends at the last named rank);
//  * an empty rank before a named one becomes "<rank>__unclassified <nearest named ancestor>"
//    ("root" when there is none);
//  * a row without usable taxonomy goes to a single "unclassified" clade.
(function (root) {
  'use strict';

  const RANKS = ['d', 'p', 'c', 'o', 'f', 'g', 's'];
  const RANK_NAMES = { d: 'domain', p: 'phylum', c: 'class', o: 'order', f: 'family', g: 'genus', s: 'species' };
  const UNCLASSIFIED = 'unclassified';
  const TAXONOMY_COLUMNS = { gtdb: 'gtdb_taxonomy', ncbi: 'ncbi_taxonomy' };

  function rankLevels(taxonomy) {
    const levels = {};
    for (let token of String(taxonomy == null ? '' : taxonomy).split(';')) {
      token = token.trim();
      if (!token) continue;
      const at = token.indexOf('__');
      if (at < 0) continue;
      const rank = token.slice(0, at).trim().toLowerCase();
      if (!RANKS.includes(rank)) continue; // t__, b__, k__ ...
      const name = token.slice(at + 2).trim();
      // first mention wins, but a later named token may fill an empty one
      if (!(rank in levels) || (!levels[rank] && name)) levels[rank] = name;
    }
    return levels;
  }

  // 'd__A;p__B;...' -> ['d__A', 'p__B', ...] (cumulative names, gaps filled); [] when no rank is named
  function parseLineage(taxonomy) {
    const levels = rankLevels(taxonomy);
    let lastNamed = -1;
    RANKS.forEach((r, i) => { if (levels[r]) lastNamed = i; });
    if (lastNamed < 0) return [];
    const clades = [];
    let ancestor = 'root';
    for (const rank of RANKS.slice(0, lastNamed + 1)) {
      const name = levels[rank] || '';
      if (name) { clades.push(`${rank}__${name}`); ancestor = name; } else clades.push(`${rank}__unclassified ${ancestor}`);
    }
    return clades;
  }

  function strainLabel(row) {
    const acc = row.accession;
    if (typeof acc === 'string' && acc.trim()) return acc.trim();
    const base = String(row.Genome_file == null ? '' : row.Genome_file).replace(/\\/g, '/').split('/').pop().trim();
    return base || 'unknown';
  }

  function number(v) {
    if (v == null || typeof v === 'boolean') return 0;
    const n = typeof v === 'number' ? v : (typeof v === 'string' && v.trim() !== '' ? Number(v) : NaN);
    return Number.isNaN(n) ? 0 : n;
  }
  function optionalNumber(v) {
    if (v == null || typeof v === 'boolean') return null;
    const n = typeof v === 'number' ? v : (typeof v === 'string' && v.trim() !== '' ? Number(v) : NaN);
    return Number.isNaN(n) ? null : n;
  }

  // Sum rows over every clade of the taxonomy; ordered like sylph-tax taxprof
  // (rank by rank, decreasing relative abundance, first-seen order for ties).
  function aggregateRows(rows, taxonomy = 'gtdb') {
    const column = TAXONOMY_COLUMNS[taxonomy];
    if (!column) throw new Error(`unknown taxonomy ${taxonomy}`);
    const relative = new Map();
    const sequence = new Map();
    const strainStats = new Map();
    for (const row of rows) {
      const lineage = parseLineage(row[column]);
      const strain = `t__${strainLabel(row)}`;
      const parts = lineage.length ? [...lineage, strain] : [UNCLASSIFIED, strain];
      const rel = number(row.Taxonomic_abundance);
      const seq = number(row.Sequence_abundance);
      let clade = '';
      for (const part of parts) {
        clade = clade ? `${clade}|${part}` : part;
        relative.set(clade, (relative.get(clade) || 0) + rel);
        sequence.set(clade, (sequence.get(clade) || 0) + seq);
      }
      let cov = row.Eff_cov;
      if (cov == null) cov = row.True_cov;
      strainStats.set(clade, [optionalNumber(row.Adjusted_ANI), optionalNumber(cov)]);
    }
    const out = [];
    for (const [clade, value] of relative) {
      const stats = strainStats.get(clade) || [null, null];
      const depth = clade.split('|').length;
      out.push({
        clade, relative: value, sequence: sequence.get(clade), ani: stats[0], coverage: stats[1],
        depth, isStrain: clade.slice(clade.lastIndexOf('|') + 1).startsWith('t__'),
      });
    }
    // Strains (t__) are the last rank, however short their lineage was.
    const rank = (c) => (c.isStrain ? RANKS.length + 1 : c.depth);
    out.sort((a, b) => rank(a) - rank(b) || b.relative - a.relative); // stable
    return out;
  }

  // Python's str(float(x)): "100.0", "0.5", "1e-05"
  function pyFloat(x) {
    if (!Number.isFinite(x)) return Number.isNaN(x) ? 'nan' : (x > 0 ? 'inf' : '-inf');
    if (Number.isInteger(x) && Math.abs(x) < 1e16) return x.toFixed(1);
    const a = Math.abs(x);
    if (a < 1e-4 || a >= 1e16) {
      const [m, e] = x.toExponential().split('e');
      const sign = e[0] === '-' ? '-' : '+';
      const digits = e.replace(/^[+-]/, '').padStart(2, '0');
      return `${m}e${sign}${digits}`;
    }
    return String(x);
  }
  const cell = (v) => (v == null ? 'NA' : pyFloat(v));

  const TAXPROF_COLUMNS = ['clade_name', 'relative_abundance', 'sequence_abundance', 'ANI (if strain-level)', 'Coverage (if strain-level)'];

  // One .sylphmpa document, as `sylphd profile --taxprof` / sylph-tax taxprof writes it.
  function taxprofLines(name, taxonomy, clades) {
    const lines = [`#SampleID\t${name}\tTaxonomies_used:['${taxonomy}']`, TAXPROF_COLUMNS.join('\t')];
    for (const c of clades) {
      lines.push(c.isStrain
        ? `${c.clade}\t${cell(c.relative)}\t${cell(c.sequence)}\t${cell(c.ani)}\t${cell(c.coverage)}`
        : `${c.clade}\t${cell(c.relative)}\t${cell(c.sequence)}\tNA\tNA`);
    }
    return lines;
  }

  // Abundance per clade of one rank, for the chart.
  //   metric: 'relative' (Taxonomic_abundance) or 'sequence' (Sequence_abundance)
  // Returns { groups: [{ key, name, value, genomes }] sorted by decreasing value,
  //           none: abundance of rows whose lineage does not reach this rank (or has no taxonomy),
  //           total } . A clade is keyed by its full path, so equal names under different
  // ancestors ("unclassified Bacteria") stay separate.
  function rankGroups(rows, taxonomy, rank, metric = 'relative') {
    const column = TAXONOMY_COLUMNS[taxonomy];
    const depth = RANKS.indexOf(rank) + 1;
    if (!column || !depth) throw new Error('bad taxonomy or rank');
    const groups = new Map();
    let none = 0;
    let total = 0;
    for (const row of rows) {
      const v = number(metric === 'sequence' ? row.Sequence_abundance : row.Taxonomic_abundance);
      total += v;
      const lineage = parseLineage(row[column]);
      if (lineage.length < depth) { none += v; continue; }
      const key = lineage.slice(0, depth).join('|');
      let g = groups.get(key);
      if (!g) {
        const last = lineage[depth - 1];
        g = { key, name: last.slice(last.indexOf('__') + 2), value: 0, genomes: 0 };
        groups.set(key, g);
      }
      g.value += v;
      g.genomes += 1;
    }
    return { groups: [...groups.values()].sort((a, b) => b.value - a.value), none, total };
  }

  const api = { RANKS, RANK_NAMES, TAXONOMY_COLUMNS, UNCLASSIFIED, parseLineage, strainLabel, aggregateRows, taxprofLines, rankGroups, pyFloat };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.SylphdTax = api;
})(typeof self !== 'undefined' ? self : this);
