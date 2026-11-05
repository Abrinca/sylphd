// sylphd: sunburst and treemap views of a taxonomic profile, drawn with Apache ECharts
// (vendored in web/vendor, loaded on first use so the bar chart costs nothing extra).
//
// The tree comes from the server's sylph-tax "All sources" profile (`taxprof.tax`): its clades
// are grouped under one bin per taxonomy source (GTDB_r232, UHGV_default, ...), each subdivided by
// its own hierarchy. The page does no aggregation of its own: every node's value is a server clade.
(() => {
  'use strict';

  const ECHARTS_SRC = 'vendor/echarts.min.js';
  const NO_TAXONOMY = new Set(['NO_TAXONOMY', 'unclassified']);

  let loading = null;
  function loadECharts() {
    if (window.echarts) return Promise.resolve(window.echarts);
    loading = loading || new Promise((resolve, reject) => {
      const script = Object.assign(document.createElement('script'), { src: ECHARTS_SRC, async: true });
      script.onload = () => resolve(window.echarts);
      script.onerror = () => { loading = null; script.remove(); reject(new Error(`Could not load the chart library (${ECHARTS_SRC}).`)); };
      document.head.append(script);
    });
    return loading;
  }

  // `s__Bacteroides uniformis` -> rank s, name "Bacteroides uniformis"; `vOTU-171526` -> rank vOTU.
  function parseToken(token) {
    const prefixed = /^([A-Za-z]+)__(.*)$/.exec(token);
    if (prefixed) return { rank: prefixed[1], name: prefixed[2] || token };
    const numbered = /^([A-Za-z]+)-\d+$/.exec(token);
    return { rank: numbered ? numbered[1] : null, name: token };
  }

  /**
   * The chart tree: one bin per taxonomy source, then the source's own clades, largest first.
   *
   * clades: the server's clade rows; valueKey: 'sequence_abundance' or 'relative_abundance';
   * sourceOfTop(token): the source of a top-level clade (null when unknown, then `fallbackBin`);
   * extras: [{ name, value, kind }] appended as top-level leaves (the unknown share). Strain rows
   * (t__) are left out: species (or the source's deepest rank) are the leaves. A chain of single
   * children is one node, "GTDB_r232 › Bacteria", so a source with one domain does not spend a
   * ring or a treemap header on it.
   */
  function buildTree({ clades, valueKey, sourceOfTop, fallbackBin, extras = [] }) {
    const byPath = new Map();
    let missing = 0;
    for (const clade of clades || []) {
      const path = String(clade.clade_name);
      const tokens = path.split('|');
      const value = Number(clade[valueKey]) || 0;
      if (tokens[tokens.length - 1].startsWith('t__')) continue;
      if (NO_TAXONOMY.has(tokens[0])) {
        if (tokens.length === 1) missing += value;
        continue;
      }
      byPath.set(path, { ...parseToken(tokens[tokens.length - 1]), value, children: [] });
    }
    const bins = new Map();
    for (const [path, node] of byPath) {
      const cut = path.lastIndexOf('|');
      if (cut >= 0) {
        const parent = byPath.get(path.slice(0, cut));
        if (parent) parent.children.push(node);
        continue;
      }
      const source = sourceOfTop(path) || fallbackBin;
      if (!bins.has(source)) bins.set(source, { name: source, rank: 'source', value: 0, children: [] });
      bins.get(source).children.push(node);
      bins.get(source).value += node.value;
    }
    const tidy = (node) => {
      const names = [node.name];
      while (node.children.length === 1) {
        node = node.children[0];
        names.push(node.name);
      }
      const { children, ...out } = { ...node, name: names.join(' › ') };
      return children.length ? { ...out, children: children.sort((a, b) => b.value - a.value).map(tidy) } : out;
    };
    const tree = [...bins.values()].sort((a, b) => b.value - a.value).map((bin) => ({ ...tidy(bin), kind: 'source', source: bin.name }));
    if (missing > 0.0005) tree.push({ name: 'No taxonomy', value: missing, kind: 'none' });
    for (const extra of extras) if (extra.value > 0.0005) tree.push({ ...extra });
    return tree;
  }

  /* ------------------------------------------------------------- colors */
  function themeColors() {
    const css = getComputedStyle(document.documentElement);
    const v = (name) => css.getPropertyValue(name).trim();
    return {
      palette: Array.from({ length: 10 }, (_, i) => v(`--cat-${i + 1}`)),
      sources: [v('--src-1'), v('--src-2'), v('--src-3')],
      fg: v('--fg'), muted: v('--muted'), card: v('--card'), border: v('--border'), soft: v('--soft'),
    };
  }

  // Diagonal lines as a fill pattern, rising for the unknown share and falling for hits without
  // taxonomy, as in the bar chart. (A pattern color works in both charts; ECharts' per-item
  // `decal` is ignored by the sunburst.)
  function hatch(colors, rising) {
    const canvas = Object.assign(document.createElement('canvas'), { width: 6, height: 6 });
    const g = canvas.getContext('2d');
    g.fillStyle = colors.border;
    g.fillRect(0, 0, 6, 6);
    g.strokeStyle = colors.muted;
    g.lineWidth = 1.5;
    for (const shift of [-6, 0, 6]) {
      g.beginPath();
      g.moveTo(shift, rising ? 6 : 0);
      g.lineTo(shift + 6, rising ? 0 : 6);
      g.stroke();
    }
    return { image: canvas, repeat: 'repeat' };
  }

  // A source bin takes a muted source tone; each clade below it takes the page's categorical
  // palette (largest first, cycling), and its descendants keep that color.
  function paint(tree, colors) {
    const tint = (node, color) => ({ ...node, itemStyle: { color }, ...(node.children && { children: node.children.map((c) => tint(c, color)) }) });
    return tree.map((node, i) => {
      if (node.kind !== 'source') return { ...node, itemStyle: { color: hatch(colors, node.kind === 'unknown') } };
      const children = node.children && node.children.map((child, j) => tint(child, colors.palette[j % colors.palette.length]));
      return { ...node, children, itemStyle: { color: colors.sources[i % colors.sources.length] } };
    });
  }

  /* ------------------------------------------------------------ options */
  function tooltip({ fmtVal, rankName, colors }, encode) {
    return {
      confine: true,
      backgroundColor: colors.card,
      borderColor: colors.border,
      textStyle: { color: colors.fg },
      formatter: (p) => {
        const rank = p.data.rank === 'source' ? 'taxonomy source' : p.data.rank ? rankName(p.data.rank) : '';
        // (the rim fillers of a top-level leaf repeat its name: not a lineage)
        const above = (p.treePathInfo || []).slice(1, -1).map((n) => n.name).filter((name) => name !== p.name);
        return `<strong>${encode(p.name)}</strong> <span style="opacity:.7">${encode(rank)}</span>`
          + `<br>${encode(fmtVal(Number(p.value) || 0))}`
          + (above.length ? `<br><span style="opacity:.7">${encode(above.join(' › '))}</span>` : '');
      },
    };
  }

  // A top-level leaf (the unknown share, hits without taxonomy) is carried out to the rim, so
  // its area matches its share instead of filling only the inner ring.
  function fillToRim(tree) {
    const depth = (nodes) => Math.max(0, ...nodes.map((n) => 1 + depth(n.children || [])));
    const rings = depth(tree);
    return tree.map((node) => {
      if (node.children) return node;
      let filler;
      const look = { label: { show: false }, itemStyle: { ...node.itemStyle, borderWidth: 0 } };
      for (let d = rings; d > 1; d--) filler = { ...node, ...look, ...(filler && { children: [filler] }) };
      return filler ? { ...node, children: [filler] } : node;
    });
  }

  // ECharts' own zoom: a click shows that sector and its levels, the centre goes back up. The
  // rings share the radius, so they widen as you zoom in; labels that do not fit are hidden.
  function sunburstOption(tree, ctx, encode) {
    const { colors } = ctx;
    return {
      aria: { enabled: true, label: { description: ctx.description } },
      tooltip: tooltip(ctx, encode),
      series: [{
        type: 'sunburst',
        data: fillToRim(tree),
        radius: ['8%', '96%'],
        sort: null,
        itemStyle: { borderColor: colors.card, borderWidth: 1 },
        label: { rotate: 'radial', color: colors.fg, textBorderColor: colors.card, textBorderWidth: 2, fontSize: 11, minAngle: 4, width: 80, overflow: 'truncate' },
        labelLayout: { hideOverlap: true },
        emphasis: { focus: 'ancestor' },
      }],
    };
  }

  // As ECharts' "treemap-show-parent" example: headers for the source and the two levels below
  // it. Drag to move, wheel to zoom, the breadcrumb to go back up.
  function treemapOption(tree, ctx, encode) {
    const { colors } = ctx;
    const text = { color: colors.fg, textBorderColor: colors.card, textBorderWidth: 2, overflow: 'truncate', lineOverflow: 'truncate' };
    const upper = { ...text, show: true, height: 22 };
    return {
      aria: { enabled: true, label: { description: ctx.description } },
      tooltip: tooltip(ctx, encode),
      series: [{
        type: 'treemap',
        data: tree,
        top: 0, left: 0, right: 0, bottom: 30,
        roam: true,
        scaleLimit: { min: 0.5, max: 60 },
        sort: null,
        visibleMin: 300,
        // ECharts draws a header in its border color: the card color, not the default white
        itemStyle: { borderColor: colors.card, gapWidth: 1 },
        label: { ...text, show: true, fontSize: 12 },
        breadcrumb: { bottom: 2, left: 0, height: 22, itemStyle: { color: colors.soft, borderColor: colors.border, textStyle: { color: colors.fg } } },
        levels: [
          { itemStyle: { borderWidth: 0, gapWidth: 4 } },
          { itemStyle: { borderColor: colors.border, borderWidth: 3, gapWidth: 2 }, upperLabel: { ...upper, fontWeight: 'bold' } },
          { itemStyle: { borderWidth: 2 }, upperLabel: upper },
          { itemStyle: { borderWidth: 2 }, upperLabel: upper },
        ],
      }],
    };
  }

  /**
   * Draw `kind` ('sunburst' or 'treemap') into `host`. Returns a handle with dispose().
   * opts: { tree (from buildTree), fmtVal, rankName, description }
   */
  function render(host, kind, opts) {
    const box = Object.assign(document.createElement('div'), { className: `taxtree taxtree-${kind}` });
    const status = Object.assign(document.createElement('p'), { className: 'taxtree-status muted', textContent: 'Loading the chart…' });
    const reset = Object.assign(document.createElement('button'), { type: 'button', className: 'link-btn taxtree-reset', textContent: 'Reset view' });
    host.replaceChildren(...(kind === 'treemap' ? [reset] : []), box, status);
    let chart = null;
    let disposed = false;
    const observer = new ResizeObserver(() => chart && chart.resize());
    const scheme = matchMedia('(prefers-color-scheme: dark)');
    const draw = () => {
      if (!chart) return;
      const ctx = { ...opts, colors: themeColors() };
      const tree = paint(opts.tree, ctx.colors);
      const encode = window.echarts.format.encodeHTML;
      chart.setOption(kind === 'sunburst' ? sunburstOption(tree, ctx, encode) : treemapOption(tree, ctx, encode), true);
    };
    reset.addEventListener('click', draw);
    loadECharts().then((echarts) => {
      if (disposed) return;
      status.remove();
      chart = echarts.init(box);
      draw();
      observer.observe(box);
      scheme.addEventListener('change', draw);
    }, (error) => {
      status.textContent = error.message;
      status.classList.add('taxtree-error');
    });
    return {
      dispose() {
        disposed = true;
        scheme.removeEventListener('change', draw);
        observer.disconnect();
        if (chart) chart.dispose();
        chart = null;
      },
    };
  }

  window.SylphdTaxTree = { buildTree, render, load: loadECharts };
})();
