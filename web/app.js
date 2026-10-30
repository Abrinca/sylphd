// sylphd web front end: plain JS, no dependencies.
// All server/user strings are inserted with textContent / text nodes only.
(() => {
  'use strict';

  /* ------------------------------------------------------------ config */
  const cfg = window.SYLPHD_CONFIG || {};
  const params = new URLSearchParams(location.search);
  const apiOverride = params.get('api');
  const pageIsLocal = /^(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])$/.test(location.hostname)
    || location.protocol === 'file:';
  const configured = pageIsLocal ? (cfg.devApiBase || cfg.apiBase) : cfg.apiBase;
  const API = (apiOverride || configured || '').trim().replace(/\/+$/, '');
  const LOCAL_INPUT_CAP = 2e9; // sanity cap on the input of the in-browser sketcher
  const EXAMPLE_URL = 'example/mycoplasma_genitalium_G37.fna.gz';
  const EXAMPLE_NAME = 'mycoplasma_genitalium_G37.fna.gz';

  const $ = (id) => document.getElementById(id);
  const ui = {
    form: $('form'), drop: $('drop'), fasta: $('fasta'), file: $('file'), pick: $('pick'),
    example: $('example'), clear: $('clear'), size: $('size'), msg: $('input-msg'),
    chip: $('file-chip'), chipName: $('file-name'), chipMeta: $('file-meta'), chipRemove: $('file-remove'),
    top: $('top'), minAni: $('min-ani'), submit: $('submit'), cancel: $('cancel'),
    progress: $('progress'), progressText: $('progress-text'), results: $('results'),
    status: $('status'), statusText: document.querySelector('#status .status-text'),
    statusDetail: $('status-detail'), catalog: $('sp-catalog'),
    panel: $('server-panel'), spName: $('sp-name'), spAddr: $('sp-addr'),
    spChange: $('sp-change'), spReset: $('sp-reset'), spConnect: $('sp-connect'), spAbout: $('sp-about'),
    spAboutBody: $('sp-about-body'), spAboutToggle: $('sp-about-toggle'), spAboutNote: $('sp-about-note'), banner: $('server-banner'), submitHint: $('submit-hint'),
  };

  /* ----------------------------------------------------------- helpers */
  function el(tag, props, ...kids) {
    const n = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v == null || v === false) continue;
        if (k === 'class') n.className = v;
        else if (k === 'text') n.textContent = v;
        else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
        else n.setAttribute(k, v === true ? '' : String(v));
      }
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      n.append(kid instanceof Node ? kid : String(kid));
    }
    return n;
  }
  const SVGNS = 'http://www.w3.org/2000/svg';
  function svg(tag, attrs, ...kids) {
    const n = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
    for (const kid of kids) if (kid != null) n.append(kid instanceof Node ? kid : String(kid));
    return n;
  }

  const nf0 = new Intl.NumberFormat('en-US');
  const fmtInt = (n) => (Number.isFinite(n) ? nf0.format(n) : '–');
  const fmtNum = (n, d = 2) => (Number.isFinite(n) ? n.toFixed(d) : '–');
  // Decimal units (1 kB = 1000 B), like file managers.
  function fmtBytes(n) {
    if (n >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
    if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e8 ? 0 : 1)} MB`;
    if (n >= 1e3) return `${(n / 1e3).toFixed(n >= 1e5 ? 0 : 1)} kB`;
    return `${Math.round(n)} B`;
  }
  function fmtBp(n) {
    if (!Number.isFinite(n)) return '–';
    if (n >= 1e9) return `${(n / 1e9).toFixed(2)} Gb`;
    return n >= 1e6 ? `${(n / 1e6).toFixed(2)} Mb` : n >= 1e3 ? `${(n / 1e3).toFixed(1)} kb` : `${n} bp`;
  }
  // A percentage with fixed decimals (ANI, GC, fractions already scaled to 0-100).
  const fmtPct = (v, d = 2) => (Number.isFinite(v) ? `${v.toFixed(d)} %` : '–');
  const fmtSecs = (ms) => `${(ms / 1000).toFixed(1)} s`;
  const fmtDur = (ms) => (ms < 1000 ? `${Math.round(ms)} ms` : ms < 60000 ? fmtSecs(ms) : `${Math.floor(ms / 60000)} min ${Math.round((ms % 60000) / 1000)} s`);
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const has = (v) => v != null && v !== '' && v !== 'none' && v !== 'na';

  // Remembered choices: a value from `allowed`, or null (also without localStorage).
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
  }

  /* -------------------------------------------------------------- files */
  const SEQ_SUFFIX = /\.(fastq|fq|fasta|fna|fa|fas|fsa|sylsp)$/i;
  // The file name without compression and sequence-file extensions.
  const stem = (name) => String(name || '').replace(/\.(gz|bgz|bgzf)$/i, '').replace(SEQ_SUFFIX, '');
  // `stem` made safe for a download name.
  const fileStem = (name, fallback) => stem(name).replace(/[^\w.-]+/g, '_').slice(0, 60) || fallback;
  // sylphd_<stem>_<mode>.<ext>, e.g. sylphd_genome_contigs.tsv
  const exportName = (name, mode, ext) => `sylphd_${fileStem(name, 'sample')}_${mode}.${ext}`;

  /* ------------------------------------------------------------- modes */
  // One page, views selected by the hash: '' (landing), '#assembly', and '#reads'.
  const MODES = { assembly: 'Assembly', reads: 'Reads' };
  const TITLES = {
    landing: 'sylphd – identify assemblies and profile reads against GTDB',
    assembly: 'sylphd – identify an assembly',
    reads: 'sylphd – profile reads',
  };
  let mode = 'landing';
  const modeListeners = [];
  const infoListeners = [];
  const notifyInfo = () => infoListeners.forEach((f) => f(serverInfo));

  function modeFromHash() {
    const h = location.hash.replace(/^#/, '').toLowerCase();
    return h in MODES ? h : 'landing';
  }

  // Show what belongs to the current mode (elements list theirs in data-modes).
  function applyMode() {
    for (const n of document.querySelectorAll('[data-modes]')) {
      n.hidden = !n.dataset.modes.split(' ').includes(mode);
    }
    document.body.dataset.mode = mode;
    for (const a of document.querySelectorAll('.tab')) {
      if (a.dataset.tab === mode) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    }
  }

  function route() {
    const next = modeFromHash();
    const changed = next !== mode;
    mode = next;
    document.title = TITLES[mode];
    applyMode();
    renderBanner();
    updateSubmit();
    if (changed) {
      window.scrollTo(0, 0);
      modeListeners.forEach((f) => f(mode));
    }
  }
  window.addEventListener('hashchange', route);
  $('home-link').addEventListener('click', (ev) => {
    ev.preventDefault();
    if (location.hash) history.pushState(null, '', location.pathname + location.search);
    route();
  });

  /* -------------------------------------------------------- server info */
  let serverInfo = null;
  let infoTimer = null;

  function setStatus(state, text) {
    ui.status.dataset.state = state;
    ui.statusText.textContent = text;
    ui.panel.dataset.state = state;
  }

  // Why the server cannot be used right now: null (usable or not yet known),
  // 'none' | 'invalid' | 'offline'. A running re-check keeps the
  // previous value, so the banner never flashes during "Checking…".
  let serverProblem = null;
  let bannerKey = '';
  const PROBLEM_HINT = {
    none: 'Public server offline', invalid: 'Invalid API URL', offline: 'Server offline',
  };

  // Same page with ?api=<url> (nothing else preserved); no argument removes it.
  function pageUrl(apiUrl) {
    const u = new URL(location.href);
    u.search = '';
    u.hash = '';
    if (apiUrl) u.searchParams.set('api', apiUrl);
    return u.toString();
  }

  function resetLink() {
    return el('a', { href: pageUrl(null), text: 'reset', title: 'Remove ?api= and use the default server' });
  }

  // The one "use another server" form, in the server panel (the offline banner opens it).
  function buildConnectForm() {
    const input = el('input', {
      type: 'text', id: 'other-api', placeholder: 'http://127.0.0.1:8765', spellcheck: 'false',
      autocomplete: 'off', autocapitalize: 'off', inputmode: 'url', 'aria-label': 'API URL of another server',
    });
    const err = el('p', { class: 'detail connect-err', role: 'alert', hidden: true });
    const form = el('form', { class: 'connect', novalidate: true },
      el('label', { for: 'other-api', text: 'Use another server' }),
      el('div', { class: 'connect-row' }, input, el('button', { type: 'submit', class: 'btn', text: 'Connect' }),
        el('button', { type: 'button', class: 'btn btn-ghost', text: 'Cancel', onclick: () => closeConnect() })),
      err,
      el('p', { class: 'detail' },
        'The server must allow this page\u2019s origin (start it with ', el('code', { text: `--cors-origin ${location.origin}` }),
        ' or ', el('code', { text: "--cors-origin '*'" }), '). For a server on localhost, your browser may ask for permission to access devices on your local network.'));
    form.addEventListener('submit', (ev) => {
      ev.preventDefault();
      const v = input.value.trim().replace(/\/+$/, '');
      let ok = false;
      try { ok = /^https?:$/.test(new URL(v).protocol); } catch { /* invalid */ }
      if (!ok) {
        err.textContent = 'Enter a full http:// or https:// URL, e.g. http://127.0.0.1:8765';
        err.hidden = false;
        input.focus();
        return;
      }
      location.assign(pageUrl(v));
    });
    form.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') closeConnect(); });
    return form;
  }
  const connectFormEl = buildConnectForm();
  ui.spConnect.append(connectFormEl);
  function openConnect() {
    ui.spConnect.hidden = false;
    ui.spChange.setAttribute('aria-expanded', 'true');
    ui.panel.scrollIntoView({ block: 'nearest' });
    $('other-api').focus();
  }
  function closeConnect() {
    ui.spConnect.hidden = true;
    ui.spChange.setAttribute('aria-expanded', 'false');
    ui.spChange.focus();
  }
  ui.spChange.addEventListener('click', () => (ui.spConnect.hidden ? openConnect() : closeConnect()));

  const shownProblem = () => serverProblem;

  function renderBanner() {
    const key = `${shownProblem()}|${API}`;
    if (!shownProblem()) {
      ui.banner.hidden = true;
      ui.banner.replaceChildren();
      bannerKey = '';
      return;
    }
    ui.banner.hidden = false;
    if (key === bannerKey) return; // keep a half-typed URL in the connect form
    bannerKey = key;
    const problem = shownProblem();
    let cls = 'error';
    let title;
    let body;
    let retry = false;
    if (problem === 'none') {
      title = 'Public server offline';
      body = [el('p', null, 'The public sylphd server is offline, so this page cannot search anything right now. ',
        'You can use your own server instead (see ', el('a', { href: './docs/guides/public-server/', text: 'running a public server' }), ').')];
    } else if (problem === 'invalid') {
      title = 'Invalid API URL';
      body = [el('p', null, 'The API base URL ', el('code', { text: API || '(empty)' }), ' is not a valid URL.')];
    } else {
      title = 'Could not reach the server';
      const items = [
        'the server is offline or restarting,',
        `the server does not allow this page's origin ${location.origin} (CORS; start it with --cors-origin ${location.origin}, or --cors-origin '*'), or`,
        'a network, firewall or proxy problem is blocking the connection.',
      ];
      if (location.protocol === 'https:' && API.startsWith('http:')) {
        items.push('this page is served over HTTPS but the API URL is plain HTTP (blocked as mixed content).');
      }
      retry = true;
      body = [el('p', null, 'Requests to ', el('code', { text: API }), ' failed before getting a response. Possible reasons:'),
        el('ul', null, items.map((t) => el('li', { text: t })))];
    }
    const foot = retry
      ? el('p', { class: 'detail retry' }, 'Retrying automatically every few seconds. ',
        el('button', { type: 'button', class: 'btn btn-small', id: 'retry-now', text: 'Retry now', onclick: loadInfo }))
      : null;
    ui.banner.replaceChildren(el('div', { class: `notice ${cls}`, role: cls === 'error' ? 'alert' : 'status' },
      el('h3', { text: title }), ...body, foot,
      el('p', { class: 'detail' }, el('button', { type: 'button', class: 'btn btn-small', text: 'Use another server', onclick: openConnect }),
        apiOverride ? [' or ', resetLink(), ' to go back to the default server.'] : null)));
  }

  function setProblem(kind) {
    serverProblem = kind;
    renderBanner();
    updateSubmit();
  }

  /* ------------------------------------------------------ server panel */
  const defaultApi = (configured || '').trim().replace(/\/+$/, '');

  // Name, address and reset link: from /info when known, else from the address alone.
  function renderPanelHead() {
    const name = serverInfo && typeof serverInfo.name === 'string' && serverInfo.name.trim() ? serverInfo.name.trim() : '';
    // without a name the address alone identifies the server
    ui.spName.textContent = name;
    ui.spName.hidden = !name;
    ui.spAddr.textContent = API || '(no server configured)';
    ui.spAddr.hidden = false;
    ui.spAddr.classList.toggle('sp-addr-main', !name);
    ui.spReset.hidden = !apiOverride;
    ui.spReset.href = pageUrl(null);
  }

  // The operator's Markdown text, collapsed to a few lines when long. Rebuilt only when the text changes.
  let aboutShown = null;
  function renderAbout() {
    const about = serverInfo && serverInfo.about;
    const md = about && about.format === 'markdown' && typeof about.text === 'string' ? about.text : '';
    if (!md || !window.SylphdAbout) {
      aboutShown = null;
      ui.spAbout.hidden = true;
      ui.spAboutBody.replaceChildren();
      return;
    }
    ui.spAbout.hidden = false;
    ui.spAboutNote.hidden = !API || API === defaultApi;
    if (md === aboutShown) return;
    aboutShown = md;
    ui.spAboutBody.replaceChildren(window.SylphdAbout.render(md));
    setAboutCollapsed(true);
    requestAnimationFrame(measureAbout);
  }
  function setAboutCollapsed(collapsed) {
    ui.spAboutBody.classList.toggle('collapsed', collapsed);
    ui.spAboutToggle.textContent = collapsed ? 'Show more' : 'Show less';
    ui.spAboutToggle.setAttribute('aria-expanded', String(!collapsed));
  }
  // Show the toggle only when the text is longer than the collapsed height.
  function measureAbout() {
    if (ui.spAbout.hidden) return;
    const collapsed = ui.spAboutBody.classList.contains('collapsed');
    ui.spAboutBody.classList.add('collapsed');
    const long = ui.spAboutBody.scrollHeight > ui.spAboutBody.clientHeight + 2;
    if (!collapsed) ui.spAboutBody.classList.remove('collapsed');
    ui.spAboutToggle.hidden = !long && collapsed;
    if (!long) ui.spAboutBody.classList.remove('collapsed');
  }
  ui.spAboutToggle.addEventListener('click', () => setAboutCollapsed(!ui.spAboutBody.classList.contains('collapsed')));
  window.addEventListener('resize', () => { if (!ui.spAbout.hidden) measureAbout(); });

  async function loadInfo() {
    clearTimeout(infoTimer);
    if (!API) {
      serverInfo = null;
      setStatus('offline', 'Public server offline');
      ui.statusDetail.replaceChildren();
      renderPanelHead();
      renderAbout();
      renderCatalog();
      setProblem('none');
      notifyInfo();
      return;
    }
    setStatus('checking', 'Checking server…');
    const retryBtn = $('retry-now');
    if (retryBtn) { retryBtn.disabled = true; retryBtn.textContent = 'Checking…'; }
    try {
      new URL(API);
    } catch {
      serverInfo = null;
      setStatus('offline', 'No valid API URL');
      ui.statusDetail.replaceChildren();
      renderPanelHead();
      renderAbout();
      renderCatalog();
      setProblem('invalid');
      notifyInfo();
      return;
    }
    try {
      const res = await fetch(`${API}/info`, { signal: timeoutSignal(8000), cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const info = await res.json();
      serverInfo = info;
      const db = info.database || {};
      const meta = info.metadata || {};
      const dbs = Array.isArray(info.databases) ? info.databases : [];
      const pooled = dbs.length > 1;
      // Only call the database "GTDB r<release>" when a loaded database really is GTDB's:
      // a fungi-only server can have GTDB metadata for taxonomy lookups without serving it.
      const gtdbServed = dbs.some((d) => d.gtdb === true);
      setStatus('online', 'Server online');
      const lim = info.limits || {};
      const fact = (label, value, title) => el('span', { class: 'fact', title: title || null },
        el('span', { class: 'fact-label', text: label }), el('span', { class: 'fact-value', text: value }));
      const facts = [];
      if (has(info.version)) facts.push(fact('sylphd', `v${info.version}`, 'sylphd version of this server'));
      if (pooled) {
        // several databases are profiled as one pool: /info's `database` is the pooled view
        facts.push(fact('Databases', String(dbs.length), dbs.map((d) => d.name).join(', ')));
        if (isNum(db.genomes)) facts.push(fact('Genomes', fmtInt(db.genomes), `Number of reference genomes in all ${dbs.length} databases`));
      } else {
        facts.push(fact('Database', gtdbServed && meta.release ? `GTDB r${meta.release}` : (db.name || '?'), `Reference database: ${db.name || 'unknown'}`));
        if (isNum(db.genomes)) facts.push(fact('Genomes', fmtInt(db.genomes), 'Number of reference genomes in the database'));
      }
      if (isNum(db.c) && isNum(db.k)) facts.push(fact('Sketch', `c=${db.c} · k=${db.k}`, 'sylph subsampling rate c and k-mer size k; sketches must match (same k, c no larger than this)'));
      if (isNum(lim.max_upload_mb)) {
        facts.push(fact('Upload', `≤ ${lim.max_upload_mb} MB`, 'Maximum size of the sketch you can send, not of your sequence files. Typical sketch sizes are listed in the public-server guide under "Sizing the upload limit" (docs/guides/public-server/).'));
      }
      if (lim.rate_limit && isNum(lim.rate_limit.count)) {
        const w = Math.round(lim.rate_limit.window_seconds);
        facts.push(fact('Rate', `${lim.rate_limit.count} / ${w === 60 ? 'min' : `${w} s`}`, 'Requests allowed per client address'));
      }
      // self-reported by the server, like its about text
      if (usageOn()) {
        facts.push(el('a', {
          class: 'fact fact-link', href: '#usage',
          title: 'this server reports that it keeps usage statistics',
          onclick: (ev) => {
            ev.preventDefault();
            if (window.SylphdUsage) window.SylphdUsage.openModal();
          },
        }, el('span', { class: 'fact-value', text: 'usage statistics on' })));
      }
      const frag = [el('span', { class: 'facts' }, ...facts)];
      ui.statusDetail.replaceChildren(...frag);
      renderPanelHead();
      renderAbout();
      renderCatalog();
      infoTimer = setTimeout(loadInfo, 60000);
      setProblem(null);
      refreshInput();
      notifyInfo();
    } catch (e) {
      serverInfo = null;
      setStatus('offline', 'Server offline');
      ui.statusDetail.replaceChildren();
      renderPanelHead();
      renderAbout();
      renderCatalog();
      setProblem('offline');
      notifyInfo();
      const rb = $('retry-now');
      if (rb) { rb.disabled = false; rb.textContent = 'Retry now'; }
      infoTimer = setTimeout(loadInfo, 20000);
    }
  }

  // Databases (only when the server pools several) and the taxonomy sources of a format 3
  // metadata file. A single-database server without taxonomy sources shows nothing here, so
  // its panel is unchanged.
  function renderCatalog() {
    const info = serverInfo;
    const parts = [];
    const dbs = info && Array.isArray(info.databases) ? info.databases : [];
    if (dbs.length > 1) {
      parts.push(el('div', { class: 'sp-group' },
        el('span', { class: 'sp-group-label', text: 'Databases' }),
        el('ul', { class: 'sp-dbs' }, dbs.map((d) => {
          const twoStage = d.kind === 'two-stage';
          const kind = twoStage ? '.syl2db' : '.syldb';
          return el('li', {
            class: 'sp-db',
            title: `${d.name}${d.file && d.file !== d.name ? ` (${d.file})` : ''}: ${kind === '.syl2db' ? 'two-stage database' : 'plain database'}`,
          },
          el('span', { class: 'sp-db-name', text: d.name || d.file || '?' }),
          el('span', { class: 'sp-db-kind', text: kind }),
          isNum(d.genomes) ? el('span', { class: 'sp-db-num', text: `${fmtInt(d.genomes)} genomes` }) : null,
          isNum(d.c) && isNum(d.k) ? el('span', { class: 'sp-db-num', text: `c=${d.c} · k=${d.k}` }) : null);
        }))));
    }
    const meta = (info && info.metadata) || {};
    const sources = info && info.metadata && Array.isArray(info.metadata.taxonomy_sources)
      ? info.metadata.taxonomy_sources.filter((s) => s && typeof s.name === 'string') : [];
    if (sources.length) {
      const chips = sources.map((s) => el('span', {
        class: 'sp-chip',
        title: [
          s.kind === 'metadata' ? 'the GTDB metadata of this server' : 'imported sylph-tax file',
          s.origin ? `origin: ${s.origin}` : '',
          s.md5 ? `md5: ${s.md5}` : '',
          s.has_host ? 'carries virus hosts' : '',
        ].filter(Boolean).join('\n'),
      },
      el('span', { class: 'sp-chip-name', text: s.name }),
      isNum(s.keys) ? el('span', { class: 'sp-chip-num', text: `${fmtInt(s.keys)} keys` }) : null));
      // The server lists each overlapping pair once, with its shared keys and examples.
      const overlaps = Array.isArray(meta.taxonomy_overlaps)
        ? meta.taxonomy_overlaps.filter((p) => p && Array.isArray(p.sources) && p.sources.length === 2) : [];
      if (overlaps.length) {
        const involved = new Set();
        for (const p of overlaps) for (const name of p.sources) involved.add(name);
        const detail = overlaps.map((p) => {
          const examples = Array.isArray(p.examples) && p.examples.length ? ` (e.g. ${p.examples.slice(0, 5).join(', ')})` : '';
          return `${p.sources[0]} and ${p.sources[1]} share ${fmtInt(p.keys)} keys${examples}`;
        }).join('; ');
        chips.push(el('span', {
          class: 'sp-chip sp-chip-warn',
          title: `${detail}. Where two sources have a key, the source the server imported later wins.`,
        }, `⚠ ${involved.size} sources overlap`));
      }
      parts.push(el('div', { class: 'sp-group' },
        el('span', { class: 'sp-group-label', text: sources.length === 1 ? 'Taxonomy source' : 'Taxonomy sources' }),
        el('div', { class: 'sp-chips' }, chips)));
    }
    ui.catalog.replaceChildren(...parts);
    ui.catalog.hidden = !parts.length;
  }

  function timeoutSignal(ms) {
    if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) return AbortSignal.timeout(ms);
    const c = new AbortController();
    setTimeout(() => c.abort(), ms);
    return c.signal;
  }

  // The server's `usage` object when it keeps usage statistics, else null (self-reported).
  const usageOn = () => (serverInfo && serverInfo.usage && serverInfo.usage.enabled ? serverInfo.usage : null);

  // The sketch is checked against the server's upload limit before it is sent.
  const sketchLimitBytes = () => ((serverInfo && serverInfo.limits && serverInfo.limits.max_upload_mb) || 0) * 1024 * 1024;

  // null when this browser can run the sketcher (assemblies and reads are always sketched in the browser), else why not.
  const sketcherProblem = () => (window.SylphdSketch ? window.SylphdSketch.unsupported() : 'The sketcher script did not load.');

  // Assemblies and reads are both sketched in the browser: without WebAssembly SIMD and workers
  // there is nothing to fall back to. Say so on every page and card; submit buttons stay disabled.
  (() => {
    const why = sketcherProblem();
    if (!why) return;
    $('browser-notice').hidden = false;
    $('browser-notice').replaceChildren(el('div', { class: 'notice warn', role: 'alert' },
      el('h3', { text: 'This browser cannot sketch your sequences' }),
      el('p', null, why, ' Assemblies and reads are sketched in your browser so that they never leave your computer. Use a current Chrome, Edge, Firefox or Safari, or the ',
        el('code', { text: 'sylphd' }), ' command line.')));
    for (const n of document.querySelectorAll('.mode-unsupported')) n.hidden = false;
  })();

  /* -------------------------------------------------------------- input */
  const input = { file: null, gz: false, size: 0, sylsp: false, header: null };
  let busy = false;
  let cooldownUntil = 0;
  let cooldownTimer = null;

  async function isGzip(file) {
    const b = new Uint8Array(await file.slice(0, 2).arrayBuffer());
    return b.length === 2 && b[0] === 0x1f && b[1] === 0x8b;
  }

  async function setFile(file) {
    input.file = file;
    input.sylsp = window.SylphdSketch ? window.SylphdSketch.isSylsp(file) : false;
    input.header = null;
    input.gz = input.sylsp ? false : await isGzip(file);
    input.size = file.size;
    if (input.sylsp && window.SylphdSketch) {
      // a ready-made sketch: read its header (a few KB) to show and check c and k
      window.SylphdSketch.readSylspHeader(file).then((h) => h, (e) => ({ error: e.message || String(e) })).then((h) => {
        if (input.file === file) { input.header = h; refreshInput(); }
      });
    }
    ui.fasta.value = '';
    ui.chipName.textContent = file.name;
    ui.chipMeta.dataset.size = fmtBytes(file.size);
    ui.chip.hidden = false;
    ui.drop.classList.add('has-file');
    refreshInput();
  }

  function clearFile() {
    input.file = null; input.gz = false; input.size = 0; input.sylsp = false; input.header = null;
    ui.file.value = '';
    ui.chip.hidden = true;
    ui.drop.classList.remove('has-file');
    refreshInput();
  }

  function clearInput() {
    ui.fasta.value = '';
    clearFile();
  }

  function textBytes() {
    const t = ui.fasta.value;
    return t ? new Blob([t]).size : 0;
  }

  function validateOptions() {
    const top = ui.top.value.trim() === '' ? NaN : Number(ui.top.value);
    const ani = ui.minAni.value.trim() === '' ? NaN : Number(ui.minAni.value);
    return {
      top, ani,
      ok: Number.isInteger(top) && top >= 1 && top <= 50 && Number.isFinite(ani) && ani >= 70 && ani <= 100,
    };
  }

  // Why an uploaded sketch cannot be sent, or null: unreadable, wrong c/k for the server, too big.
  function sketchProblem() {
    if (!input.sylsp) return null;
    if (!input.header) return 'pending';
    return window.SylphdSketch.sketchProblem(input.file, input.header, serverInfo, 'assembly');
  }

  function refreshInput() {
    const size = input.file ? input.size : textBytes();
    input.size = size;
    const over = !input.sylsp && size > LOCAL_INPUT_CAP;
    const sp = sketchProblem();
    if (input.file) {
      const h = input.header && !input.header.error ? input.header : null;
      ui.chipMeta.textContent = input.sylsp
        ? `${ui.chipMeta.dataset.size || fmtBytes(size)} · ready-made sketch${h ? `, ${fmtInt(h.kmers)} k-mers, c=${h.c}, k=${h.k}` : ''}`
        : `${ui.chipMeta.dataset.size || fmtBytes(size)} · ${input.gz ? 'gzip' : 'plain text'}`;
    }
    ui.size.textContent = !size ? '' : input.sylsp ? `${fmtBytes(size)} · uploaded as it is, nothing is sketched` : `${fmtBytes(size)} · sketched in your browser`;
    ui.size.classList.toggle('over', over || (!!sp && sp !== 'pending'));
    if (over) {
      ui.msg.textContent = `Too large: ${fmtBytes(size)} exceeds the ${fmtBytes(LOCAL_INPUT_CAP)} the browser sketcher accepts. Use the sylphd command line for this one.`;
      ui.msg.hidden = false;
    } else if (sp && sp !== 'pending') {
      ui.msg.textContent = sp;
      ui.msg.hidden = false;
    } else {
      ui.msg.hidden = true;
    }
    updateSubmit();
  }

  const perContig = () => window.SylphdContigs || null;

  function updateSubmit() {
    const now = Date.now();
    const cooling = cooldownUntil > now;
    const pc = perContig();
    const pcOn = !!(pc && pc.active());
    const pcOpts = pcOn ? pc.validate() : null;
    const ready = input.size > 0
      && (input.sylsp ? !pcOn : input.size <= LOCAL_INPUT_CAP)
      && (pcOn || !sketchProblem())
      && (pcOn ? pcOpts.ok : validateOptions().ok);
    const problem = API ? shownProblem() : 'none';
    const noSketcher = !!sketcherProblem();
    ui.submit.disabled = busy || cooling || !ready || !!problem || noSketcher;
    const pcSketch = pcOn && input.sylsp;
    ui.submitHint.textContent = problem ? PROBLEM_HINT[problem]
      : noSketcher ? 'This browser cannot sketch'
        : pcSketch ? 'Per contig needs FASTA or FASTQ, not a .sylsp sketch' : '';
    ui.submitHint.hidden = !problem && !noSketcher && !pcSketch;
    ui.clear.disabled = busy;
    ui.pick.disabled = busy;
    ui.example.disabled = busy;
    ui.top.disabled = busy;
    ui.minAni.disabled = busy;
    const toggle = $('per-contig');
    if (toggle) toggle.disabled = busy || !(pc && pc.enabled());
    for (const node of document.querySelectorAll('#per-contig-options input')) node.disabled = busy;
    if (cooling && !busy) {
      ui.submit.textContent = `Retry in ${Math.ceil((cooldownUntil - now) / 1000)} s`;
    } else {
      ui.submit.textContent = pcOn ? 'Classify contigs' : 'Find closest genomes';
    }
  }

  function startCooldown(seconds) {
    cooldownUntil = Date.now() + seconds * 1000;
    clearInterval(cooldownTimer);
    const tick = () => {
      const left = Math.ceil((cooldownUntil - Date.now()) / 1000);
      for (const s of document.querySelectorAll('[data-countdown]')) {
        s.textContent = left > 0 ? `${left} s` : 'now';
      }
      updateSubmit();
      if (left <= 0) clearInterval(cooldownTimer);
    };
    tick();
    cooldownTimer = setInterval(tick, 250);
  }

  ui.fasta.addEventListener('input', () => {
    if (input.file) clearFile();
    else refreshInput();
  });
  ui.top.addEventListener('input', updateSubmit);
  ui.minAni.addEventListener('input', updateSubmit);
  ui.pick.addEventListener('click', () => ui.file.click());
  ui.file.addEventListener('change', () => { if (ui.file.files[0]) setFile(ui.file.files[0]); });
  ui.clear.addEventListener('click', () => { clearInput(); ui.results.replaceChildren(); });
  ui.chipRemove.addEventListener('click', clearInput);
  ui.status.addEventListener('click', loadInfo);

  ui.example.addEventListener('click', async () => {
    ui.example.disabled = true;
    try {
      const res = await fetch(EXAMPLE_URL);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      await setFile(new File([blob], EXAMPLE_NAME, { type: 'application/gzip' }));
    } catch (e) {
      ui.msg.textContent = `Could not load the example genome: ${e.message}`;
      ui.msg.hidden = false;
    } finally {
      updateSubmit();
    }
  });

  // drag & drop
  let dragDepth = 0;
  const hasFiles = (ev) => ev.dataTransfer && Array.from(ev.dataTransfer.types || []).includes('Files');
  ui.drop.addEventListener('dragenter', (ev) => {
    if (!hasFiles(ev) || busy) return;
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
    if (busy) return;
    const f = ev.dataTransfer.files[0];
    if (f) setFile(f);
  });
  // a stray drop outside the box would make the browser navigate to the file
  for (const t of ['dragover', 'drop']) {
    window.addEventListener(t, (ev) => { if (hasFiles(ev) && !ui.drop.contains(ev.target)) ev.preventDefault(); });
  }

  /* ------------------------------------------------------------- submit */
  let controller = null;
  let sketchHandle = null; // the running in-browser sketch, if any
  let runStart = 0;
  let runTimer = null;

  function setBusy(on, text) {
    busy = on;
    ui.progress.hidden = !on;
    ui.cancel.hidden = !on;
    if (on) {
      runStart = performance.now();
      const tick = () => { ui.progressText.textContent = `${text()} · ${fmtSecs(performance.now() - runStart)}`; };
      tick();
      runTimer = setInterval(tick, 100);
    } else {
      clearInterval(runTimer);
    }
    updateSubmit();
  }

  // Sketch the input in this browser (WebAssembly workers); resolves { sketch, stats, ms }.
  async function sketchLocally(sketchPhase) {
    const info = serverInfo || {};
    const db = info.database || {};
    if (!isNum(db.c) || !isNum(db.k)) throw new Error('The server did not report its sketch parameters (c and k).');
    const file = input.file || new File([ui.fasta.value], 'pasted.fasta', { type: 'text/plain' });
    const total = file.size;
    const t0 = performance.now();
    sketchHandle = window.SylphdSketch.start({
      files: [file], layout: 'single', c: db.c, k: db.k, name: (input.file ? input.file.name : '') || 'assembly',
      onProgress: (p) => {
        const read = p.streams.reduce((n, x) => n + (x.read || 0), 0);
        const sk = p.sketch || {};
        sketchPhase.text = `Sketching in your browser: ${fmtBytes(Math.min(read, total))} of ${fmtBytes(total)}` +
          (sk.reads ? ` · ${fmtInt(sk.reads)} sequences` : '');
      },
    });
    try {
      const done = await sketchHandle.done;
      const st = done.stats;
      return {
        sketch: done.sketch, ms: performance.now() - t0,
        stats: { records: st.reads, length: st.bases, gc: st.bases ? Math.round((10000 * st.gcBases) / st.bases) / 100 : null },
      };
    } finally {
      sketchHandle = null;
    }
  }

  // Per-contig mode owns the sketching, batching and result rendering (contigs.js); the page
  // keeps the shared progress line, Cancel button and busy state.
  async function runPerContig() {
    ui.results.replaceChildren();
    const phase = { text: 'Preparing…' };
    controller = new AbortController();
    setBusy(true, () => phase.text);
    let wholeAssembly = false;
    try {
      const outcome = await window.SylphdContigs.run({ phase, signal: controller.signal });
      wholeAssembly = !!(outcome && outcome.whole);
    } catch (e) {
      ui.results.replaceChildren(notice('error', 'Could not classify the contigs', e.message || String(e)));
    } finally {
      setBusy(false);
      controller = null;
      if (ui.results.firstChild) {
        ui.results.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
      }
    }
    // "Whole assembly instead" in the many-contigs modal: leave per-contig mode and
    // start the ordinary assembly search with the same input.
    if (wholeAssembly && window.SylphdContigs) {
      window.SylphdContigs.deactivate();
      ui.form.requestSubmit();
    }
  }

  ui.form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    if (ui.submit.disabled) return;
    // nothing is sketched or sent until the visitor has agreed (once per server and policy)
    if (!(await window.SylphdUsage.ensureAgreed())) return;
    if (window.SylphdContigs && window.SylphdContigs.active()) {
      await runPerContig();
      return;
    }
    const opts = validateOptions();
    if (!opts.ok) return;
    ui.results.replaceChildren();
    const phase = { text: 'Preparing…' };
    controller = new AbortController();
    setBusy(true, () => phase.text);
    try {
      const url = new URL(`${API}/assembly`);
      url.searchParams.set('top', String(opts.top));
      url.searchParams.set('min_ani', String(opts.ani));
      // a ready-made .sylsp is sent as it is; anything else is sketched here first
      const localRun = input.sylsp ? null : await sketchLocally(phase);
      if (localRun && !localRun.stats.records) throw new Error('No sequences found in the input.');
      const limit = sketchLimitBytes();
      const sketchBytes = localRun ? localRun.sketch.byteLength : input.file.size;
      if (limit && sketchBytes > limit) {
        throw new Error(`The sketch (${fmtBytes(sketchBytes)}) is over the server's upload limit of ${fmtBytes(limit)}. This input is too large or diverse for this server; use the sylphd command line.`);
      }
      const type = 'application/vnd.sylph.sketch';
      const body = localRun ? new Blob([localRun.sketch], { type }) : input.file;
      // the file name is only a label for the results card; it is never sent
      const label = input.file ? input.file.name.replace(/\.sylsp$/i, '') : '';
      if (localRun) {
        url.searchParams.set('records', String(localRun.stats.records));
        url.searchParams.set('length', String(localRun.stats.length));
        if (isNum(localRun.stats.gc)) url.searchParams.set('gc', String(localRun.stats.gc));
      }
      phase.text = `Sending the sketch (${fmtBytes(body.size)}) and searching…`;
      let res;
      try {
        res = await fetch(url, { method: 'POST', body, headers: { 'Content-Type': type }, signal: controller.signal });
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
        showHttpError(res.status, data && data.error, Number.isFinite(retry) ? retry : null, raw);
        return;
      }
      if (!data || !Array.isArray(data.hits)) throw new Error('Unexpected response from the server.');
      // the server never saw the sequence: fill in what this browser counted (unknown for an uploaded sketch)
      const q = data.query = data.query || {};
      if (localRun) for (const k of ['records', 'length', 'gc']) if (q[k] == null) q[k] = localRun.stats[k];
      renderResults(data, performance.now() - runStart, localRun ? { label, sketchBytes: body.size, sketchMs: localRun.ms } : { label, sketchBytes: body.size, uploaded: true });
    } catch (e) {
      if (e.name === 'AbortError') {
        ui.results.replaceChildren(notice('warn', 'Cancelled', 'Sketching or the request was cancelled. Nothing was kept.'));
      } else if (e.network) {
        showNetworkError();
      } else {
        ui.results.replaceChildren(notice('error', 'Could not sketch or search', e.message || String(e)));
      }
    } finally {
      setBusy(false);
      controller = null;
      sketchHandle = null;
      if (ui.results.firstChild) {
        ui.results.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
      }
    }
  });
  ui.cancel.addEventListener('click', () => {
    if (window.SylphdContigs) window.SylphdContigs.cancel();
    if (sketchHandle) sketchHandle.cancel();
    if (controller) controller.abort();
  });

  function notice(kind, title, ...body) {
    return el('div', { class: `notice ${kind}`, role: kind === 'error' ? 'alert' : null },
      el('h3', { text: title }), ...body.map((b) => (b instanceof Node ? b : el('p', { text: b }))));
  }

  function showHttpError(status, serverMsg, retry, raw) {
    let node;
    const msg = serverMsg || (raw && raw.length < 300 && !raw.trim().startsWith('<') ? raw.trim() : '');
    if (status === 413) {
      node = notice('error', 'Upload too large (413)',
        'The server rejected the sketch because it is over its upload limit (or that of its proxy). This input is too large or diverse for this server; use the sylphd command line.',
        msg && el('p', { class: 'detail', text: `Server message: ${msg}` }));
    } else if (status === 429) {
      const secs = retry != null ? retry : 60;
      node = notice('warn', 'Rate limit reached (429)',
        el('p', null, 'Too many requests from your address. You can try again in ', el('strong', { 'data-countdown': true, text: `${secs} s` }), '.'),
        msg && el('p', { class: 'detail', text: `Server message: ${msg}` }));
      startCooldown(secs);
    } else if (status === 503) {
      const secs = retry != null ? retry : 5;
      node = notice('warn', 'Server busy (503)',
        el('p', null, 'The server queue is full. Please retry in ', el('strong', { 'data-countdown': true, text: `${secs} s` }), '.'),
        msg && el('p', { class: 'detail', text: `Server message: ${msg}` }));
      startCooldown(secs);
    } else if (status === 404) {
      node = notice('error', 'Endpoint not available (404)',
        'This server does not provide POST /assembly.',
        msg && el('p', { class: 'detail', text: `Server message: ${msg}` }));
    } else {
      node = notice('error', `Request failed (HTTP ${status})`, msg || 'The server returned an error without a message.');
    }
    ui.results.replaceChildren(node);
  }

  function showNetworkError() {
    ui.results.replaceChildren(el('div', { class: 'notice error', role: 'alert' },
      el('h3', { text: 'Could not reach the server' }),
      el('p', null, 'The request to ', el('code', { text: API }), ' failed before getting a response (a proxy may also have closed the connection on a very large upload). The server status is shown at the top of the page.')));
    loadInfo();
  }

  /* ------------------------------------------------------------- modal */
  const abortError = () => Object.assign(new Error('cancelled'), { name: 'AbortError' });

  // One accessible modal. Resolves the chosen button's value (or `escape` for Esc and a click
  // outside), and rejects with AbortError when `signal` aborts. `paragraphs` are strings or
  // nodes; `buttons` are { label, value, primary?, ghost? }; `note` is a closing detail line.
  function choiceModal({ title, paragraphs, buttons, escape, signal, note }) {
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
        if (!document.querySelector('.pc-modal-backdrop')) document.body.classList.remove('modal-open');
        reject(abortError());
      };
      const finish = (choice) => {
        if (done) return;
        done = true;
        document.removeEventListener('keydown', onKey, true);
        if (signal) signal.removeEventListener('abort', onAbort);
        backdrop.remove();
        if (!document.querySelector('.pc-modal-backdrop')) document.body.classList.remove('modal-open');
        resolve(choice);
      };
      const nodes = buttons.map((button) => el('button', {
        type: 'button',
        class: button.primary ? 'btn btn-primary' : button.ghost ? 'btn btn-ghost' : 'btn',
        text: button.label,
        onclick: () => finish(button.value),
      }));
      const onKey = (event) => {
        if (backdrop !== document.querySelector('.pc-modal-backdrop:last-of-type')) return;
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
        note ? el('p', { class: 'detail', text: note }) : null);
      backdrop.append(dialog);
      backdrop.addEventListener('click', (event) => { if (event.target === backdrop) finish(escape); });
      document.body.classList.add('modal-open');
      document.body.append(backdrop);
      document.addEventListener('keydown', onKey, true);
      if (signal) {
        if (signal.aborted) { onAbort(); return; }
        signal.addEventListener('abort', onAbort, { once: true });
      }
      nodes[0].focus();
    });
  }

  /* ------------------------------------------------- shared result widgets */
  // Download TSV / JSON buttons of a results card.
  const exportButtons = (onTsv, onJson) => el('div', { class: 'export' },
    el('button', { type: 'button', class: 'btn', text: 'Download TSV', onclick: onTsv }),
    el('button', { type: 'button', class: 'btn', text: 'Download JSON', onclick: onJson }));

  // Radio buttons as a segmented control; opts are [value, text, title?].
  function seg(label, name, opts, value, onChange) {
    const wrap = el('div', { class: 'seg', role: 'radiogroup', 'aria-label': label });
    for (const [v, text, title] of opts) {
      const input = el('input', { type: 'radio', name, value: v });
      input.checked = v === value;
      input.addEventListener('change', () => onChange(v));
      wrap.append(el('label', { title: title || null }, input, el('span', { text })));
    }
    return wrap;
  }

  // One row of a legend list with a bar: a colour swatch, a name, a bar of value/max and a text.
  // With `onclick` the row is a toggle button (`active`), else plain; `extra` adds a class.
  function barRow({ swatch, name, value, max, text, tip, extra, onclick, active, italic }) {
    const kids = [
      el('i', { class: `sw ${swatch}`, 'aria-hidden': 'true' }),
      el('span', { class: 'aname', title: typeof name === 'string' ? name : null }, italic ? el('i', { text: name }) : name),
      el('span', { class: 'abar', 'aria-hidden': 'true' }, el('span', { class: swatch, style: `width:${Math.max(0.5, (100 * value) / max).toFixed(2)}%` })),
      el('b', { class: 'apct', text })];
    const cls = `arow${extra ? ` ${extra}` : ''}`;
    return el('li', null, onclick
      ? el('button', { type: 'button', class: `${cls}${active ? ' active' : ''}`, 'aria-pressed': active ? 'true' : 'false', title: tip, onclick }, kids)
      : el('div', { class: cls, title: tip }, kids));
  }

  // A sortable table with the mobile sort control (the thead is hidden on small screens).
  //   cols    [{ key, label (or () => label), hint, get(item), dir }]; dir is the first click's direction
  //   state   { sortKey, sortDir, open? } kept by the caller across re-renders; `open` remembers expanded lineages
  //   items() the rows' data; tiebreak(a, b) orders equal and missing values (missing ones always sort last)
  //   row(item) a <tr> or an array of them; view(sorted) can cut the list
  // Returns { nodes: [mobile sort, table wrap], update(), sorted() }.
  function sortableTable({ cols, state, items, tiebreak, row, view = (list) => list, cls, id, noun }) {
    const labelOf = (c) => (typeof c.label === 'function' ? c.label() : c.label);
    const colOf = (key) => cols.find((c) => c.key === key) || cols[0];
    const flip = () => { state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc'; };
    const choose = (c) => {
      if (state.sortKey === c.key) flip(); else { state.sortKey = c.key; state.sortDir = c.dir; }
      update();
    };
    const table = el('table', { class: cls },
      el('thead', null, el('tr', null, cols.map((c) => el('th', { scope: 'col' },
        el('button', { type: 'button', text: labelOf(c), title: `Sort by ${c.hint || labelOf(c)}`, onclick: () => choose(c) }))))),
      el('tbody'));
    const sel = el('select', { id, 'aria-label': `Sort ${noun} by` }, cols.map((c) => el('option', { value: c.key, text: labelOf(c) })));
    sel.addEventListener('change', () => { const c = colOf(sel.value); state.sortKey = c.key; state.sortDir = c.dir; update(); });
    const dirBtn = el('button', { type: 'button', class: 'btn', 'aria-label': 'Toggle sort direction', onclick: () => { flip(); update(); } });

    function sorted() {
      const col = colOf(state.sortKey);
      const sign = state.sortDir === 'asc' ? 1 : -1;
      return items().slice().sort((x, y) => {
        const a = col.get(x), b = col.get(y);
        const an = a == null || a === '', bn = b == null || b === '';
        if (an || bn) return an === bn ? tiebreak(x, y) : an ? 1 : -1;
        const c = typeof a === 'number' && typeof b === 'number' ? a - b : String(a).localeCompare(String(b), undefined, { sensitivity: 'base' });
        return c * sign || tiebreak(x, y);
      });
    }
    function update() {
      sel.value = state.sortKey;
      dirBtn.textContent = state.sortDir === 'asc' ? '▲' : '▼';
      cols.forEach((c, i) => {
        const th = table.tHead.rows[0].cells[i];
        if (c.key === state.sortKey) th.setAttribute('aria-sort', state.sortDir === 'asc' ? 'ascending' : 'descending');
        else th.removeAttribute('aria-sort');
      });
      // remember which lineages are expanded across re-sorts
      if (state.open) {
        for (const d of table.tBodies[0].querySelectorAll('details.lineage')) {
          if (d.open) state.open.add(d.dataset.key); else state.open.delete(d.dataset.key);
        }
      }
      table.tBodies[0].replaceChildren(...view(sorted()).flatMap(row));
    }
    update();
    return {
      nodes: [el('div', { class: 'mobile-sort' }, el('label', { for: id, text: 'Sort by' }), sel, dirBtn), el('div', { class: 'table-wrap' }, table)],
      update, sorted,
    };
  }

  /* ----------------------------------------------------------- results */
  // GTDB rank prefixes (shallowest first) and their names.
  const RANK_NAMES = { d: 'domain', p: 'phylum', c: 'class', o: 'order', f: 'family', g: 'genus', s: 'species' };
  const rankTitle = (r) => (RANK_NAMES[r] ? RANK_NAMES[r][0].toUpperCase() + RANK_NAMES[r].slice(1) : r);

  // 'd__A;p__B;...' -> [{ rank: 'd', name: 'A' }, ...] for display (aggregation happens on the server)
  function parseLineage(str) {
    if (!has(str)) return [];
    return String(str).split(';').map((p) => {
      const m = /^([a-z]+)__(.*)$/.exec(p.trim());
      return m ? { rank: m[1], name: m[2] } : { rank: '', name: p.trim() };
    });
  }
  const lineageName = (lin, r) => { const x = lin.find((e) => e.rank === r); return x && x.name ? x.name : ''; };

  let current = null; // { data, hits, sortKey, sortDir, thr, elapsed }

  // Depth of the shared GTDB rank (species 0 … domain 6): drives badge colours and sorting.
  const DEPTH = Object.fromEntries(Object.values(RANK_NAMES).reverse().map((name, i) => [name, i]));
  const NONE_DEPTH = Object.keys(DEPTH).length;

  // CSS class suffix for a hit: relation colour by shared rank, 'closest' for the best hit
  // when nothing reaches the species threshold, 'plain' when there is no lineage to go by.
  function relClass(h) {
    if (h.relation === 'closest relative') return 'closest';
    if (h.relation === 'related' || h.relation === 'unknown lineage') return 'plain';
    return h.shared_rank && h.shared_rank in DEPTH ? h.shared_rank : 'none';
  }
  const relDepth = (h) => {
    if (h.relation === 'closest relative') return -1;
    if (h.relation === 'related' || h.relation === 'unknown lineage') return NONE_DEPTH + 1;
    return h.shared_rank in DEPTH ? DEPTH[h.shared_rank] : NONE_DEPTH;
  };
  const hitKey = (h) => h.accession || h.genome_id || h.genome_file || `#${h.rank}`;

  function decorate(h) {
    const lin = parseLineage(h.gtdb_taxonomy);
    return Object.assign({}, h, {
      _lin: lin, _species: lineageName(lin, 's') || genericTaxon(h), _genus: lineageName(lin, 'g'),
      _rel: relClass(h),
      _frac: Array.isArray(h.containment) && h.containment[1] > 0 ? h.containment[0] / h.containment[1] : (h.containment_fraction ?? null),
    });
  }

  // A hit outside GTDB carries the server's generic `taxonomy` object ({rank: taxon},
  // shallowest first); its last value is the finest taxon, e.g. a vOTU or a fungal species.
  function genericTaxon(h) {
    const entries = genericRanks(h);
    return entries.length ? entries[entries.length - 1][1] : '';
  }

  // [{ rank, name }, ...] from the generic `taxonomy` object, shallowest first.
  function genericRanks(h) {
    const tax = h.taxonomy;
    if (!tax || typeof tax !== 'object' || Array.isArray(tax)) return [];
    return Object.entries(tax).filter(([, name]) => typeof name === 'string' && name.trim());
  }

  // A hit whose lineage comes from a taxonomy source other than GTDB (UHGV, ICTV, fungi):
  // it has `taxonomy_source`/`taxonomy`, but no gtdb_taxonomy.
  const nonGtdb = (h) => !has(h.gtdb_taxonomy) && (has(h.taxonomy_source) || genericRanks(h).length > 0);
  // The taxonomy source to name for a hit/link badge; GTDB when it has a GTDB lineage.
  // /assembly and /contigs hits call the field `taxonomy_source`, /profile rows `tax_source`.
  function hitSource(h) {
    const source = h.taxonomy_source ?? h.tax_source;
    return !has(h.gtdb_taxonomy) && has(source) ? String(source) : 'GTDB';
  }
  // The hit table's taxon column says "GTDB species" only while every hit is GTDB's.
  const speciesColumnLabel = () => (current && current.hits.some(nonGtdb) ? 'Taxon' : 'GTDB species');

  function renderResults(data, clientMs, local) {
    const thr = Object.assign({ species: 95, imprecise_below: 85 }, data.thresholds || {});
    const hits = data.hits.map((h) => decorate(h));
    current = { data, hits, thr, sortKey: 'rank', sortDir: 'asc', clientMs, open: new Set(), local };
    const out = [];
    out.push(summaryCard(data, clientMs, local));
    if (!hits.length) {
      out.push(emptyCard(data));
    } else {
      out.push(bestCard(data, hits, thr));
      out.push(tableCard());
    }
    ui.results.replaceChildren(...out);
  }

  function summaryCard(data, clientMs, local) {
    const q = data.query || {};
    const db = data.database || {};
    const item = (label, value, small) => el('div', null, el('dt', { text: label }), el('dd', null, value, small ? el('small', { text: small }) : null));
    return el('section', { class: 'card', 'aria-label': 'Query summary' },
      el('div', { class: 'card-head' }, el('h2', { text: 'Query' }),
        el('span', { class: 'muted' }, local.label ? `“${local.label}” ` : '',
          local.uploaded
            ? el('span', { class: 'badge badge-plain', title: 'You uploaded a ready-made sketch (k-mer hashes and counts); nothing was sketched here, so records, length and GC are unknown.' }, 'uploaded sketch')
            : el('span', { class: 'badge badge-rel rel-species', title: `The sequence never left your computer: your browser sketched it in ${fmtDur(local.sketchMs)} and only the ${fmtBytes(local.sketchBytes)} sketch (k-mer hashes and counts) was sent.` }, '✓ sketched locally'))),
      el('dl', { class: 'summary' },
        item('Records', fmtInt(q.records)),
        item('Length', fmtBp(q.length), isNum(q.length) ? `${fmtInt(q.length)} bp` : ''),
        item('GC', fmtPct(q.gc, 1)),
        item('Sketch k-mers', fmtInt(q.sketch_kmers), local.uploaded ? `${fmtBytes(local.sketchBytes)} sketch, uploaded as it is` : `${fmtBytes(local.sketchBytes)} sketch, made in ${fmtDur(local.sketchMs)}`),
        item('Search time', isNum(data.elapsed_ms) ? `${Math.round(data.elapsed_ms)} ms` : '–', local.uploaded ? `${fmtDur(clientMs)} incl. upload` : `${fmtDur(clientMs)} incl. sketching`),
        item('Database', fmtInt(db.genomes), 'genomes searched')));
  }

  function emptyCard(data) {
    const p = data.params || {};
    const min = isNum(p.min_ani) ? p.min_ani : ui.minAni.value;
    const q = data.query || {};
    return el('section', { class: 'card empty' },
      el('h2', { text: 'No hits found' }),
      el('p', null, `No database genome reached ${min}% ANI with your sequence. Likely reasons:`),
      el('ul', null,
        el('li', { text: isNum(min) && Number(min) > 70 ? `All genomes are below the minimum ANI. Try lowering it (the lowest allowed value is 70%).` : 'No genome reaches even the lowest allowed ANI (70%).' }),
        el('li', { text: `The sequence is too small or fragmentary${isNum(q.length) ? ` (${fmtBp(q.length)}, ${fmtInt(q.sketch_kmers)} sketch k-mers)` : ''}: a handful of k-mers cannot be matched reliably. A full assembly works best.` }),
        el('li', { text: 'The organism is novel, or not a bacterium or archaeon (GTDB covers only those), so nothing in the database is close.' })));
  }

  /* gauge */
  // Piecewise-linear scale: [GAUGE_MIN, imprecise_below] -> first 25 % of the arc,
  // [imprecise_below, species] -> next 35 %, [species, 100] -> last 40 %.
  const GAUGE_MIN = 75, GAUGE_MAX = 100, GAUGE_SHARE = [0.25, 0.35, 0.40];
  function gauge(ani, thr, species) {
    const W = 360, H = 212, cx = 180, cy = 170, R = 125;
    const imp = thr.imprecise_below, sp = thr.species;
    const min = GAUGE_MIN, max = GAUGE_MAX;
    const stops = [min, imp, sp, max];
    const okScale = imp > min && sp > imp && max > sp;
    // value -> position on the arc, 0 (left end) .. 1 (right end); values outside clamp
    const frac = (v) => {
      v = Math.min(max, Math.max(min, v));
      if (!okScale) return (v - min) / (max - min);
      let acc = 0;
      for (let i = 0; i < 3; i++) {
        if (v <= stops[i + 1]) return acc + GAUGE_SHARE[i] * (v - stops[i]) / (stops[i + 1] - stops[i]);
        acc += GAUGE_SHARE[i];
      }
      return 1;
    };
    const ang = (v) => Math.PI * (1 - frac(v));
    const pt = (v, r) => [cx + r * Math.cos(ang(v)), cy - r * Math.sin(ang(v))];
    const arc = (v1, v2, r) => {
      const [x1, y1] = pt(v1, r), [x2, y2] = pt(v2, r);
      return `M${x1.toFixed(2)} ${y1.toFixed(2)}A${r} ${r} 0 0 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`;
    };
    // colour by the zone the value falls in: red (imprecise), orange (below species), green (species)
    const zone = ani < imp ? 'distant' : (species && ani >= sp) ? 'species' : 'below';
    const zoneColor = `var(--${zone})`;
    const interp = zone === 'species' ? 'species level'
      : zone === 'below' ? `below the species threshold of ${sp} percent`
        : `below ${imp} percent, imprecise, distantly related`;
    const g = svg('svg', {
      class: 'gauge', viewBox: `0 0 ${W} ${H}`, role: 'img',
      'aria-label': `ANI ${ani.toFixed(2)} percent, ${interp}`,
    });
    // neutral track for the full scale
    g.append(svg('path', { d: arc(min, max, R), class: 'track' }));
    // fill from the scale minimum up to the value, coloured per zone segment; the part under imprecise_below is striped
    const v = Math.min(max, Math.max(min, ani));
    const segs = okScale
      ? [[min, imp, 'distant', 'fill imprecise'], [imp, sp, 'below', 'fill'], [sp, max, 'species', 'fill']]
      : [[min, max, zone, 'fill']];
    for (const [a, b, c, cls] of segs) {
      if (v > a) g.append(svg('path', { d: arc(a, Math.min(b, v), R), class: cls, stroke: `var(--${c})` }));
    }
    // thin zone bands inside the arc keep the thresholds visible on the unfilled track
    if (okScale) {
      for (const [a, b, c] of [[min, imp, 'distant'], [imp, sp, 'below'], [sp, max, 'species']]) {
        g.append(svg('path', { d: arc(a, b, R - 17), class: 'band', stroke: `var(--${c})` }));
      }
    }
    // threshold ticks: dashed at imprecise_below, solid at species
    const marks = [];
    if (okScale) marks.push([imp, `${imp}`, true]);
    if (sp > min && sp < max) marks.push([sp, `species ${sp}`, false]);
    for (const [mv, label, soft] of marks) {
      const [x1, y1] = pt(mv, R - 21), [x2, y2] = pt(mv, R + (soft ? 10 : 14)), [lx, ly] = pt(mv, R + 22);
      const cos = Math.cos(ang(mv));
      g.append(svg('line', { x1, y1, x2, y2, class: soft ? 'tick soft' : 'tick' }));
      const labelText = svg('text', {
        x: lx, y: ly + 4, class: soft ? 'thr soft' : 'thr',
        'text-anchor': cos > 0.25 ? 'start' : cos < -0.25 ? 'end' : 'middle',
      }, label);
      g.append(soft ? labelText : svg('a', { href: 'docs/species-and-ani/#how-sylphd-calls-a-species' }, labelText));
    }
    g.append(svg('text', { x: cx - R, y: cy + 36, 'text-anchor': 'middle' }, String(min)));
    g.append(svg('text', { x: cx + R, y: cy + 36, 'text-anchor': 'middle' }, String(max)));
    // knob on the arc marks the value, in the value's zone colour
    const [kx, ky] = pt(v, R);
    g.append(svg('g', { class: 'knob' },
      svg('circle', { cx: kx, cy: ky, r: 16, class: 'knob-outer' }),
      svg('circle', { cx: kx, cy: ky, r: 14.5, class: 'knob-ring' }),
      svg('circle', { cx: kx, cy: ky, r: 10.5, fill: zoneColor })));
    g.append(svg('text', { x: cx, y: cy - 30, 'text-anchor': 'middle', class: 'value' }, ani.toFixed(2)));
    g.append(svg('text', { x: cx, y: cy - 12, 'text-anchor': 'middle', class: 'unit' }, 'k-mer ANI, %'));
    return g;
  }

  function nameFragment(h) {
    return h._species || h.ncbi_organism_name || h.accession || 'the top hit';
  }

  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const article = (word) => (/^[aeiou]/i.test(word) ? 'an' : 'a');

  function lineageTrail(lin) {
    // Phylum down to genus, as a breadcrumb; the domain is rarely informative.
    const parts = lin.filter((e) => e.name && ['p', 'c', 'o', 'f', 'g'].includes(e.rank));
    if (!parts.length) return null;
    return el('p', { class: 'trail', title: 'GTDB lineage' }, parts.flatMap((e, i) => [
      i ? el('span', { class: 'sep', 'aria-hidden': 'true', text: '›' }) : '',
      el('span', { title: rankTitle(e.rank) }, e.rank === 'g' ? el('i', { text: e.name }) : e.name)]));
  }

  // The same breadcrumb for a hit from another taxonomy source (UHGV vOTU, fungal species, ...):
  // the finest ranks of the generic `taxonomy` object, which has no letter prefixes.
  function genericTrail(h) {
    const parts = genericRanks(h).filter(([, name]) => name).slice(-5);
    if (!parts.length) return null;
    return el('p', { class: 'trail', title: `Lineage from ${h.taxonomy_source || 'the taxonomy source'}` },
      parts.flatMap(([rank, name], i) => [
        i ? el('span', { class: 'sep', 'aria-hidden': 'true', text: '›' }) : '',
        el('span', { title: rank }, name)]));
  }

  const LEGEND = [
    ['species', 'same species'], ['genus', 'genus'], ['family', 'family'], ['order', 'order'],
    ['class', 'class'], ['phylum', 'phylum'], ['domain', 'domain'], ['none', 'different domain'],
  ];

  // A non-GTDB source has its own rank family (`level 1` … `level N` for UHGV,
  // `kingdom` … `species` for fungi), so listing GTDB ranks would be wrong.  Show the
  // ranks of the reference lineage, with one neutral colour (the server's relations name
  // the rank itself: "same level 4", "same genus", "different lineage").
  function genericLegend(reference, hits) {
    const ranks = genericRanks(reference).map(([rank]) => rank);
    const items = ranks.slice().reverse().map((rank) => ['none', `same ${rank}`]);
    if (hits.some((x) => String(x.relation || '').startsWith('different source'))) {
      items.push(['none', 'different source']);
    }
    if (!items.some(([, label]) => label === 'different lineage')) {
      items.push(['none', 'different lineage']);
    }
    return items;
  }

  function legendItems(reference, otherSource, hits) {
    return otherSource ? genericLegend(reference, hits) : LEGEND;
  }

  function bestCard(data, hits, thr) {
    const h = hits[0];
    const asg = data.assignment || null;
    const species = !!asg;
    const taxon = asg && asg.taxon;
    const otherSource = !!(asg && nonGtdb(asg));
    let verdict;
    let sub = null;
    if (species) {
      verdict = [otherSource ? 'Same taxon as ' : 'Same species as ',
        taxon
          ? (otherSource ? el('b', { text: taxon }) : el('i', { text: taxon }))
          : el('span', { class: 'mono', text: asg.accession || 'the best hit' })];
    } else {
      verdict = ['No species-level match'];
      sub = el('p', { class: 'closest' }, 'Closest relative: ', el('i', { text: nameFragment(h) }),
        ` (${h.ani.toFixed(1)} % ANI)`);
    }
    const [a, b] = Array.isArray(h.containment) ? h.containment : [null, null];
    const notes = [];
    if (sub) notes.push(sub);
    // A hit without an NCBI accession (UHGV, ICTV, ...) is named by its genome_id: the
    // taxonomy key, or the first contig id, not the shared UHGV `votus_full.fna.gz`.
    const hitName = has(h.accession) ? accessionLinks(h.accession, hitSource(h))
      : has(h.genome_id) ? el('span', { class: 'mono', title: h.genome_file || h.genome_id, text: String(h.genome_id) })
        : has(h.genome_file) ? el('span', { class: 'mono', title: h.genome_file, text: String(h.genome_file).split('/').pop() })
          : el('span', { class: 'dash', text: '–' });
    notes.push(el('p', null, species ? 'Assigned from ' : 'Best hit ', hitName,
      has(h.ncbi_organism_name) ? [' · ', h.ncbi_organism_name] : '',
      has(h.ncbi_strain_identifiers) ? ` (${h.ncbi_strain_identifiers})` : ''));
    if (otherSource) {
      notes.push(el('p', { class: 'detail' }, 'Taxonomy source: ', el('b', { text: h.taxonomy_source })));
    }
    const trail = lineageTrail(h._lin) || genericTrail(h);
    if (trail) notes.push(trail);
    if (!species) {
      const genus = h._genus, family = lineageName(h._lin, 'f');
      let hint = `Below ${thr.species} % ANI the assembly is probably not the same species as any database genome.`;
      if (h.ani >= thr.imprecise_below && genus) {
        hint += ` It is likely a different species (closest genus: ${genus}${family ? `, family ${family}` : ''}) and may belong to a related, possibly novel, genus.`;
      } else if (h.ani < thr.imprecise_below) {
        hint += ` k-mer ANI below ~${thr.imprecise_below} % is imprecise, so read the number as “distantly related” rather than an exact value.`;
      }
      notes.push(el('p', null, hint));
    }
    const boundary = hits.filter((x, i) => i > 0 && x.ani_species_level && x.shared_rank !== 'species');
    if (species && boundary.length) {
      const what = otherSource ? `a different taxon of ${h.taxonomy_source || 'its source'}` : 'a different GTDB species';
      notes.push(el('p', null,
        `${plural(boundary.length, 'other hit reaches', 'other hits reach')} ${thr.species} % ANI but ${boundary.length === 1 ? 'belongs' : 'belong'} to ${what}`,
        otherSource ? '.' : [' (', el('a', { href: 'docs/species-and-ani/#species-boundary-cases', text: 'a species-boundary case' }), ').']));
    }
    if (isNum(a) && isNum(b) && b > 0) {
      notes.push(el('p', { title: 'Fraction of the query sketch k-mers that are also present in this genome' },
        `${fmtInt(a)} of ${fmtInt(b)} query k-mers shared (${(100 * a / b).toFixed(1)} %).`));
    }
    const legendHead = species
      ? (otherSource ? 'Relation to the assigned taxon:' : 'Relation to the assigned species:')
      : 'Relation to the closest relative:';
    // The server explains what 95 % ANI means when the best hit is not from GTDB (viruses,
    // fungi, another source): show it next to the assignment, as the CLI warns on stderr.
    const caveat = (asg && asg.note) || (data.closest && data.closest.note) || '';
    const card = el('section', { class: 'card', 'aria-label': 'Result' });
    if (has(caveat)) {
      card.append(notice('warn', otherSource ? `Species call from ${asg.taxonomy_source || 'another taxonomy source'}` : 'Species call is a heuristic',
        /[.!?]$/.test(caveat) ? caveat : `${caveat}.`));
    }
    card.append(
      el('div', { class: 'best' },
        gauge(h.ani, thr, species),
        el('div', null,
          el('p', { class: `verdict ${species ? 'verdict-species' : 'verdict-below'}` }, verdict),
          el('p', { class: 'species-note' },
            el('a', { href: 'docs/species-and-ani/', text: 'More on species calls →' })),
          el('div', { class: 'best-meta' }, notes),
          el('ul', { class: 'legend', 'aria-label': legendHead },
            el('li', { class: 'legend-head', text: legendHead }),
            legendItems(asg || h, nonGtdb(asg || h), hits).map(([k, label]) => el('li', null, el('i', { class: `sw rel-${k}` }), label)),
            el('li', null, el('i', { class: 'sw-dash' }), `k-mer ANI < ~${thr.imprecise_below} % is imprecise`)))));
    return card;
  }

  /* links */
  // The GTDB genome page only exists for a GTDB hit; a hit from another taxonomy
  // source (FungiRefSeq-latest, UHGV, ...) keeps the NCBI link and names its source.
  function accessionLinks(acc, source) {
    if (!has(acc)) return el('span', { class: 'dash', text: '–' });
    const e = encodeURIComponent(acc);
    const gtdb = !has(source) || String(source) === 'GTDB';
    return el('span', null,
      el('a', { href: `https://www.ncbi.nlm.nih.gov/datasets/genome/${e}/`, rel: 'noopener', title: 'NCBI Datasets', class: 'mono', text: acc }),
      ' ',
      gtdb
        ? el('a', { href: `https://gtdb.ecogenomic.org/genome?gid=${e}`, rel: 'noopener', title: 'View in GTDB', class: 'badge badge-plain', text: 'GTDB' })
        : el('span', { class: 'badge badge-plain', title: `Taxonomy source: ${source}`, text: String(source) }));
  }
  function taxidLink(id) {
    const n = Number(id);
    if (!has(id) || !Number.isInteger(n) || n <= 0) return el('span', { class: 'dash', text: '–' });
    return el('a', { href: `https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?id=${n}`, rel: 'noopener', text: String(n) });
  }

  /* table */
  const accessionColumnLabel = () => (current && current.hits.some((h) => !has(h.accession) && has(h.genome_id)) ? 'Accession / ID' : 'Accession');

  const COLS = [
    { key: 'rank', label: '#', get: (h) => h.rank, dir: 'asc' },
    { key: 'ani', label: 'ANI %', get: (h) => h.ani, dir: 'desc' },
    { key: 'rel', label: 'Relation', hint: 'relation (closest first)', get: (h) => relDepth(h), dir: 'asc' },
    { key: 'cont', label: 'Containment', get: (h) => h._frac, dir: 'desc' },
    { key: 'species', label: speciesColumnLabel, get: (h) => h._species, dir: 'asc' },
    { key: 'ncbi', label: 'NCBI organism', get: (h) => h.ncbi_organism_name, dir: 'asc' },
    { key: 'taxid', label: 'TaxID', get: (h) => h.ncbi_taxid, dir: 'asc' },
    { key: 'acc', label: accessionColumnLabel, get: (h) => h.accession || h.genome_id, dir: 'asc' },
    { key: 'quality', label: 'Quality', hint: 'CheckM2 completeness / contamination (%)', get: (h) => h.checkm2_completeness, dir: 'desc' },
  ];

  function tableCard() {
    current.table = sortableTable({
      cols: COLS, state: current, items: () => current.hits, tiebreak: (x, y) => x.rank - y.rank,
      row: hitRow, cls: 'hits', id: 'sort-select', noun: 'hits',
    });
    return el('section', { class: 'card', 'aria-label': 'Hits' },
      el('div', { class: 'card-head' },
        el('h2', { text: `Closest genomes (${current.hits.length})` }),
        exportButtons(downloadAssemblyTsv, () => downloadJson(exportAssembly('json'), current.data))),
      ...current.table.nodes);
  }

  const dash = () => el('span', { class: 'dash', text: '–' });

  function aniCell(h) {
    const thr = current.thr;
    const pct = Math.max(0, Math.min(100, ((h.ani - 70) / 30) * 100));
    const tick = (v, cls) => el('i', { class: cls, style: `left:${((v - 70) / 30) * 100}%` });
    return el('div', { class: 'anibar' }, el('b', { text: h.ani.toFixed(2) }),
      el('div', { class: `bar rel-${h._rel}`, 'aria-hidden': 'true' },
        el('span', { style: `width:${pct.toFixed(1)}%` }), tick(thr.imprecise_below, 'soft'), tick(thr.species)));
  }

  function speciesCell(h) {
    if (h._lin.length) {
      const label = h._species || '(no species)';
      const list = el('ol', null, h._lin.map((e) => el('li', null,
        el('b', { text: rankTitle(e.rank) }), el('span', { text: e.name || '–' }))));
      const key = hitKey(h);
      return el('details', { class: 'lineage', 'data-key': key, open: current.open.has(key) ? '' : null }, el('summary', { title: 'Show the full GTDB lineage' }, el('i', { text: label })), list);
    }
    // A hit from another taxonomy source (UHGV vOTU, fungal species): its `taxonomy` object
    // is {display rank: taxon}, shallowest first, and the last entry is the finest taxon.
    const ranks = genericRanks(h);
    if (!ranks.length) return dash();
    const source = has(h.taxonomy_source) ? String(h.taxonomy_source) : 'the taxonomy source';
    const list = el('ol', null, ranks.map(([rank, name], i) => el('li', null,
      el('b', { text: rankTitle(rank) || `level ${i + 1}` }), el('span', { text: name || '–' }))));
    const key = hitKey(h);
    return el('details', { class: 'lineage', 'data-key': key, open: current.open.has(key) ? '' : null },
      el('summary', { title: `Show the full lineage from ${source}` }, el('span', { text: h._species || '(no taxon)' })), list);
  }

  function relationBadge(h) {
    if (!has(h.relation)) return dash();
    // A non-GTDB hit has its own rank family (UHGV levels, fungal ranks), so name its
    // source in the tooltip instead of GTDB; GTDB hits keep the exact wording.
    const gtdb = !nonGtdb(h);
    const source = gtdb ? 'GTDB' : (has(h.taxonomy_source) ? String(h.taxonomy_source) : 'this source');
    const title = gtdb
      ? (h.shared_rank ? `Deepest GTDB rank shared: ${h.shared_rank}` : 'No shared GTDB rank')
      : (h.shared_rank ? `Deepest rank shared in ${source}: ${h.shared_rank}` : `No shared rank in ${source}`);
    let sub = null;
    if (h.ani_species_level && h.shared_rank !== 'species') {
      sub = gtdb
        ? el('div', { class: 'sub', title: 'The ANI reaches the species threshold, but GTDB places this genome in a different species', text: `ANI ≥ ${current.thr.species}, other species` })
        : el('div', { class: 'sub', title: `The ANI reaches the species threshold, but ${source} places this genome in a different taxon`, text: `ANI ≥ ${current.thr.species}, other taxon` });
    }
    return el('div', { class: 'rel' },
      el('span', { class: `badge badge-rel rel-${h._rel}`, title, text: h.relation }), sub);
  }

  function ncbiCell(h) {
    if (!has(h.ncbi_organism_name) && !has(h.ncbi_strain_identifiers)) return dash();
    return el('div', null, has(h.ncbi_organism_name) ? h.ncbi_organism_name : dash(),
      has(h.ncbi_strain_identifiers) ? el('div', { class: 'sub', text: `strain ${h.ncbi_strain_identifiers}` }) : null,
      !has(h.ncbi_strain_identifiers) && has(h.ncbi_isolate) ? el('div', { class: 'sub', text: `isolate ${h.ncbi_isolate}` }) : null);
  }

  function qualityCell(h) {
    const tags = [];
    if (has(h.ncbi_genome_category)) tags.push(el('span', { class: 'badge badge-plain', title: `NCBI genome category: ${h.ncbi_genome_category}`, text: String(h.ncbi_genome_category).replace(/^derived from /, '') }));
    if (has(h.ncbi_assembly_level)) tags.push(el('span', { class: 'badge badge-plain', title: `Assembly level: ${h.ncbi_assembly_level}`, text: String(h.ncbi_assembly_level).replace(/ Genome$/, '').toLowerCase() }));
    if (has(h.ncbi_type_material_designation)) tags.push(el('span', { class: 'badge badge-plain', title: String(h.ncbi_type_material_designation), text: 'type' }));
    const hasQ = isNum(h.checkm2_completeness) || isNum(h.checkm2_contamination);
    if (!hasQ && !tags.length) return dash();
    return el('div', null,
      hasQ ? el('span', { title: 'CheckM2 completeness / contamination (%)', class: 'num' },
        `${isNum(h.checkm2_completeness) ? h.checkm2_completeness.toFixed(1) : '–'} / ${isNum(h.checkm2_contamination) ? h.checkm2_contamination.toFixed(1) : '–'}`) : null,
      tags.length ? el('div', { class: 'tags' }, tags) : null);
  }

  function cellTd(label, cls, ...content) {
    return el('td', { class: cls, 'data-label': label }, el('div', { class: 'cv' }, content));
  }

  function accessionCell(h) {
    if (has(h.accession)) return accessionLinks(h.accession, hitSource(h));
    // UHGV/ICTV/genome hits have no NCBI accession: show the server's genome_id
    // (taxonomy key, or the first contig id) instead of an empty dash.
    if (has(h.genome_id)) return el('span', { class: 'mono', title: h.genome_file || h.genome_id, text: String(h.genome_id) });
    return dash();
  }

  function hitRow(h) {
    const [a, b] = Array.isArray(h.containment) ? h.containment : [null, null];
    return el('tr', { class: h.rank === 1 ? 'top-row' : null },
      cellTd('#', 'num rank', String(h.rank)),
      cellTd('ANI %', null, aniCell(h)),
      cellTd('Relation', null, relationBadge(h)),
      cellTd('Containment', 'num', isNum(a) && isNum(b)
        ? [`${fmtInt(a)} / ${fmtInt(b)}`, el('div', { class: 'sub', text: isNum(h._frac) ? `${(h._frac * 100).toFixed(1)} %` : '' })] : dash()),
      cellTd(speciesColumnLabel(), null, speciesCell(h)),
      cellTd('NCBI organism', null, ncbiCell(h)),
      cellTd('TaxID', 'num', taxidLink(h.ncbi_taxid)),
      cellTd('Accession', null, accessionCell(h)),
      cellTd('Quality', null, qualityCell(h)));
  }

  /* downloads */
  function download(filename, text, mime) {
    const url = URL.createObjectURL(new Blob([text], { type: mime }));
    const a = el('a', { href: url, download: filename });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }
  const downloadJson = (name, data) => download(name, `${JSON.stringify(data, null, 2)}\n`, 'application/json');
  const exportAssembly = (ext) => exportName(current.local.label, 'assembly', ext);
  function downloadAssemblyTsv() {
    const hits = current.table.sorted();
    const lead = ['rank', 'ani', 'containment_hits', 'containment_total', 'containment_fraction', 'relation', 'shared_rank', 'ani_species_level'];
    const skip = new Set(['rank', 'ani', 'containment', 'containment_fraction', 'relation', 'shared_rank', 'ani_species_level']);
    const keys = [];
    for (const h of hits) for (const k of Object.keys(h)) if (!k.startsWith('_') && !skip.has(k) && !keys.includes(k)) keys.push(k);
    const cell = (v) => (v == null ? '' : String(v).replace(/[\t\r\n]+/g, ' '));
    const rows = [lead.concat(keys).join('\t')];
    for (const h of hits) {
      const c = Array.isArray(h.containment) ? h.containment : [null, null];
      rows.push([h.rank, h.ani, c[0], c[1], h.containment_fraction, h.relation, h.shared_rank, h.ani_species_level]
        .concat(keys.map((k) => h[k])).map(cell).join('\t'));
    }
    download(exportAssembly('tsv'), `${rows.join('\n')}\n`, 'text/tab-separated-values');
  }

  /* ----------------------------------------------- shared with the other scripts */
  window.Sylphd = {
    el, svg, fmtInt, fmtNum, fmtBytes, fmtBp, fmtPct, fmtSecs, fmtDur, isNum, has, notice,
    readChoice, writeChoice, choiceModal, stem, fileStem, exportName, download, downloadJson,
    RANK_NAMES, DEPTH, rankTitle, parseLineage, lineageName, genericRanks, genericTaxon, hitSource, relClass,
    exportButtons, seg, barRow, sortableTable,
    accessionLinks, taxidLink, resetLink, loadInfo, timeoutSignal, updateSubmit,
    assemblyInput: () => input,
    api: API, apiOverride,
    info: () => serverInfo,
    usage: usageOn,
    problem: () => (API ? serverProblem : 'none'),
    mode: () => mode,
    onInfo: (f) => infoListeners.push(f),
    onMode: (f) => modeListeners.push(f),
  };

  /* -------------------------------------------------------------- init */
  route();
  refreshInput();
  loadInfo();
})();
