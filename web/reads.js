// sylphd "Reads" mode: sketch FASTQ/FASTA reads locally (WebAssembly in Web Workers), upload only the
// sketch to POST /profile and show the taxonomic profile. Plain JS, no dependencies.
// All server/user strings are inserted with textContent / text nodes only.
//
// Pipeline (see sketch-wasm/README.md): one decompress worker per input stream -> sketch worker,
// transferable chunks with credit backpressure; the sketch (.sylsp bytes) comes back to this thread.
(() => {
  'use strict';
  const S = window.Sylphd;
  const T = window.SylphdTax;
  const root = document.getElementById('mode-reads');
  if (!S || !T || !root) return;
  const { el, fmtInt, isNum, has, notice, download } = S;

  const GIB = 1024 ** 3;
  const MEM_WARN = 1.5 * GIB; // wasm memory is capped at 4 GiB per instance
  const DEFAULT_MIN_ANI = 95;
  const DEFAULT_MIN_KMERS = 10;
  const MAX_ROWS_SHOWN = 100;
  const NAME_MAX = 100;

  /* ---------------------------------------------------------- formatting */
  const fmtPct = (v) => {
    if (!isNum(v)) return '–';
    if (v >= 10) return `${v.toFixed(1)} %`;
    if (v >= 0.01) return `${v.toFixed(2)} %`;
    return v > 0 ? '< 0.01 %' : '0 %';
  };
  // decimal units, like file managers (the assembly mode's 'MB' are MiB)
  const fmtBytes = (n) => {
    if (n >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
    if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e8 ? 0 : 1)} MB`;
    if (n >= 1e3) return `${(n / 1e3).toFixed(n >= 1e5 ? 0 : 1)} kB`;
    return `${Math.round(n)} B`;
  };
  const fmtBases = (n) => {
    if (!isNum(n)) return '–';
    if (n >= 1e9) return `${(n / 1e9).toFixed(2)} Gb`;
    if (n >= 1e6) return `${(n / 1e6).toFixed(1)} Mb`;
    if (n >= 1e3) return `${(n / 1e3).toFixed(1)} kb`;
    return `${n} b`;
  };
  const fmtTime = (ms) => {
    const s = ms / 1000;
    if (s < 60) return `${s.toFixed(1)} s`;
    return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
  };
  const fmtMem = (n) => (n >= GIB ? `${(n / GIB).toFixed(1)} GB` : `${Math.round(n / 1048576)} MB`);
  const safeName = (s) => String(s || 'sample').replace(/[^\w.-]+/g, '_').slice(0, 60) || 'sample';

  /* ----------------------------------------------------- support checks */
  const { unsupported, loadModule } = window.SylphdSketch;

  /* --------------------------------------------------------- file names */
  const COMPRESSION = /\.(gz|bgz|bgzf)$/i;
  const READ_SUFFIX = /\.(fastq|fq|fasta|fa|fna|fas|fsa)$/i;
  const MATE_RE = /[_.]R?([12])(?:_001)?$/i; // sample_R1, sample.R2, sample_1, sample_R1_001
  const stem = (name) => name.replace(COMPRESSION, '').replace(READ_SUFFIX, '');
  const mateOf = (name) => { const m = MATE_RE.exec(stem(name)); return m ? Number(m[1]) : 0; };
  const sampleFromName = (name, paired) => {
    let s = stem(name);
    if (paired) s = s.replace(MATE_RE, '');
    return (s || 'sample').slice(0, NAME_MAX);
  };

  /* ---------------------------------------------------------------- state */
  const st = {
    files: [], // File[]; for the paired layout [R1, R2]
    layout: 'single', // 'single' | 'interleaved' | 'paired'
    pairDetected: false,
    nameTouched: false,
    run: null,
    busy: false,
    view: null, // results view state
  };

  /* ------------------------------------------------------------------ DOM */
  const ui = {};
  const fileInput = el('input', {
    type: 'file', multiple: true, class: 'sr-only', tabindex: '-1', id: 'r-file',
    accept: '.fastq,.fq,.fasta,.fa,.fna,.gz,application/gzip',
  });
  ui.drop = el('div', { class: 'rdrop', id: 'r-drop', role: 'button', tabindex: '0', 'aria-label': 'Choose or drop read files' },
    dropIcon(),
    el('div', { class: 'rdrop-text' },
      el('strong', { text: 'Drop FASTQ or FASTA reads here' }),
      el('span', null, 'or ', el('u', { text: 'choose files' }), '. One file (single-end or interleaved) or two files (R1 + R2). ',
        el('code', { text: '.fastq .fq .fasta .fa' }), ', optionally ', el('code', { text: '.gz' }), '.'),
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
  ui.layout = el('div', { class: 'rlayout' });
  ui.fileMsg = el('p', { class: 'input-msg', role: 'alert', hidden: true });
  ui.name = el('input', { type: 'text', id: 'r-name', maxlength: String(NAME_MAX), autocomplete: 'off', spellcheck: 'false', placeholder: 'sample' });
  ui.minAni = el('input', { type: 'number', id: 'r-min-ani', min: '0', max: '100', step: '0.1', value: String(DEFAULT_MIN_ANI), inputmode: 'decimal' });
  ui.minKmers = el('input', { type: 'number', id: 'r-min-kmers', min: '0', step: '1', value: String(DEFAULT_MIN_KMERS), inputmode: 'numeric' });
  ui.unknown = el('input', { type: 'checkbox', id: 'r-unknown' });
  ui.sketchOnly = el('input', { type: 'checkbox', id: 'r-sketch-only' });
  ui.start = el('button', { type: 'submit', class: 'btn btn-primary', id: 'r-start', text: 'Sketch and profile', disabled: true });
  ui.hint = el('span', { class: 'submit-hint', hidden: true });
  ui.support = el('div');
  ui.progress = el('section', { class: 'card rprogress', id: 'r-progress', 'aria-label': 'Progress', hidden: true });
  ui.results = el('section', { class: 'results', id: 'r-results', 'aria-live': 'polite' });

  const form = el('form', { id: 'r-form', novalidate: true },
    fileInput, ui.drop, ui.fileList, ui.layout, ui.fileMsg,
    el('div', { class: 'options' },
      el('label', { class: 'field field-name', for: 'r-name' }, el('span', { text: 'Sample name' }), ui.name)),
    el('details', { class: 'adv' },
      el('summary', { text: 'Options' }),
      el('div', { class: 'options' },
        el('label', { class: 'field', for: 'r-min-ani' }, el('span', { text: 'Minimum ANI (%)' }), ui.minAni),
        el('label', { class: 'field', for: 'r-min-kmers' }, el('span', { text: 'Minimum k-mers per genome' }), ui.minKmers)),
      el('label', { class: 'check' }, ui.unknown,
        el('span', null, el('strong', { text: 'Estimate unknown fraction' }), ' (sylph ', el('code', { text: '--estimate-unknown' }),
          '): also reports how much of the sample the database does not cover.')),
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

  function autoName() {
    if (st.nameTouched || !st.files.length) return;
    ui.name.value = sampleFromName(st.files[0].name, st.layout === 'paired');
  }

  function removeFile(i) {
    st.files.splice(i, 1);
    st.pairDetected = false;
    st.layout = st.files.length === 2 ? 'paired' : 'single';
    autoName();
    renderFiles();
  }

  function renderFiles() {
    const n = st.files.length;
    const total = st.files.reduce((a, f) => a + f.size, 0);
    const roleOf = (i) => (st.layout === 'paired' ? `R${i + 1}` : st.layout === 'interleaved' ? 'interleaved' : n > 1 ? `part ${i + 1}` : 'reads');
    ui.fileList.replaceChildren(...st.files.map((f, i) => el('li', { class: 'rfile' },
      el('span', { class: `badge ${st.layout === 'paired' ? 'badge-rel rel-closest' : 'badge-plain'}`, text: roleOf(i) }),
      el('span', { class: 'rfile-name', title: f.name, text: f.name }),
      el('span', { class: 'rfile-size', text: fmtBytes(f.size) }),
      el('button', { type: 'button', class: 'link-btn', 'aria-label': `Remove ${f.name}`, text: 'remove', onclick: () => removeFile(i), disabled: st.busy || null }))));
    ui.drop.classList.toggle('has-files', n > 0);

    // layout choices
    const kids = [];
    if (n === 1) {
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
    const msgs = [];
    if (n === 2 && st.layout === 'paired' && !st.pairDetected) {
      msgs.push('The file names do not look like a pair (_R1/_R2, _1/_2): check that the order is R1, R2, or choose “Two single-end files”.');
    }
    if (st.files.some((f) => f.size === 0)) msgs.push('A selected file is empty.');
    if (sizeNote) ui.fileList.append(el('li', { class: 'rfile-total', text: sizeNote }));
    ui.fileMsg.textContent = msgs.join(' ');
    ui.fileMsg.hidden = !msgs.length;
    refresh();
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
    return { ani, kmers, ok, unknown: ui.unknown.checked, sketchOnly: ui.sketchOnly.checked };
  }

  function dbParams() {
    const info = S.info();
    const db = info && info.database;
    return db && isNum(db.c) && isNum(db.k) ? { c: db.c, k: db.k } : null;
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
    const ready = st.files.length > 0 && st.files.every((f) => f.size > 0) && !hint;
    ui.start.disabled = st.busy || !ready;
    ui.hint.textContent = st.files.length ? hint : '';
    ui.hint.hidden = !(st.files.length && hint);
    fileInput.disabled = st.busy;
    ui.name.disabled = st.busy;
    for (const i of [ui.minAni, ui.minKmers, ui.unknown, ui.sketchOnly]) i.disabled = st.busy;
    ui.drop.classList.toggle('disabled', st.busy);
    ui.drop.setAttribute('aria-disabled', String(st.busy));
    ui.start.textContent = o.sketchOnly ? 'Sketch' : 'Sketch and profile';
  }
  ui.unknown.addEventListener('change', refresh);
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
    const run = {
      files: st.files.slice(), layout: st.layout, name, c: db.c, k: db.k, opts: o,
      total: st.files.reduce((n, f) => n + f.size, 0), t0: performance.now(), phase: 'sketch', phaseAt: performance.now(),
      prog: { streams: [], sketch: {}, uploaded: 0 }, sketch: null, stats: null, cancelled: false, kill: null, xhr: null, warned: new Set(),
    };
    st.run = run;
    ui.results.replaceChildren();
    setBusy(true);
    ui.progress.hidden = false;
    renderProgress(run);
    run.timer = setInterval(() => renderProgress(run), 200);
    ui.progress.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'nearest' });
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
    if (o.ani !== null) params.minimum_ani = o.ani;
    if (o.kmers !== null) params.min_number_kmers = o.kmers;
    const form = new FormData();
    form.append('sketch', run.sketch, `${safeName(run.name)}.sylsp`);
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
    if (run.phase === 'sketch') {
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
      if (rate > 0 && sketchElapsed > 1500 && read < run.total) eta = ` · about ${fmtTime(((run.total - read) / rate) * 1000)} left`;
      ui.barText.textContent = `${fmtBytes(read)} of ${fmtBytes(run.total)} read (${(frac * 100).toFixed(0)} %)${eta}`;
    } else if (run.phase === 'upload') {
      ui.barText.textContent = `${fmtBytes(p.uploaded)} of ${fmtBytes(run.sketch.size)} sketch uploaded`;
    } else {
      ui.barText.textContent = `Sketch uploaded (${fmtBytes(run.sketch.size)}); the server is profiling it…`;
    }
    ui.stats.replaceChildren(
      stat(run.layout === 'single' ? 'Reads' : 'Read pairs', fmtInt(sk.reads || 0)),
      stat('Bases', fmtBases(sk.bases || 0)),
      stat('Sketch k-mers', fmtInt(sk.kmers || 0)),
      stat('Elapsed', fmtTime(elapsed)),
      stat('Speed', rate ? `${fmtBytes(rate)}/s` : '–', decompressed ? `${fmtBytes(decompressed)} decompressed` : ''));

    // memory / size warnings
    const warns = [];
    const mem = sk.wasmMemory || 0;
    if (mem > MEM_WARN && run.phase === 'sketch') {
      warns.push(notice('warn', 'Large sample', `The sketcher already uses ${fmtMem(mem)} of the 4 GB a browser tab can give it. If it runs out of memory, sketching stops and you need the sylphd command line for this sample.`));
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
      const a = el('a', { href: url, download: `${safeName(run.name)}.sylsp` });
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
    run.result = { data, sample, rows, params };
    const noTax = !rows.length ? false : rows.every((r) => !has(r.gtdb_taxonomy) && !has(r.ncbi_taxonomy));
    const out = [summaryCard(run, run.result)];
    if (!rows.length) out.push(emptyCard(run));
    else {
      if (noTax) {
        out.push(notice('warn', 'No taxonomy available', 'This server has no taxonomy metadata, so genomes cannot be grouped by taxon. The table below lists the genomes only.'));
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
    const items = [
      item('Sample', run.name, run.layout === 'paired' ? 'paired-end' : run.layout === 'interleaved' ? 'interleaved pairs' : 'single-end'),
      item(run.layout === 'single' ? 'Reads' : 'Read pairs', fmtInt(s.reads), fmtBases(s.bases)),
      item('Sketch', `${fmtInt(s.kmers)} k-mers`, `${fmtBytes(run.sketch.size)} · c=${run.c}, k=${run.k}`),
    ];
    const speed = run.sketchMs ? `${fmtBytes(run.total / (run.sketchMs / 1000))}/s` : '';
    items.push(item('Sketching', fmtTime(run.sketchMs || 0), speed));
    if (result) {
      items.splice(3, 0, item('Genomes detected', fmtInt(result.rows.length)));
      items.push(item('Profiling', fmtTime(run.profileMs), 'upload and server'));
      if (result.params.estimate_unknown) {
        items.push(item('Unknown', fmtPct(unknownFraction(result.rows)), 'not covered by the database'));
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
  const TOP_GROUPS = 10;
  const view = { taxonomy: 'gtdb', rank: 's', metric: 'relative', showAll: false };

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

  const PLURAL = { d: ['domain', 'domains'], p: ['phylum', 'phyla'], c: ['class', 'classes'], o: ['order', 'orders'], f: ['family', 'families'], g: ['genus', 'genera'], s: ['species', 'species'] };
  const italicRank = (rank, name) => (rank === 'g' || rank === 's') && !/^unclassified/i.test(name);

  function abundanceCard(run) {
    const { rows, params } = run.result;
    const card = el('section', { class: 'card', 'aria-label': 'Abundance' });
    const body = el('div', { class: 'abund' });
    const dlBtn = el('button', { type: 'button', class: 'btn' });
    const draw = () => {
      body.replaceChildren(...abundanceBody(run).filter(Boolean));
      dlBtn.textContent = `Download sylphmpa (${view.taxonomy === 'gtdb' ? 'GTDB' : 'NCBI'})`;
    };
    dlBtn.addEventListener('click', () => downloadSylphmpa(run));
    const controls = el('div', { class: 'abund-controls' },
      el('div', { class: 'ctl' }, el('span', { class: 'ctl-label', text: 'Taxonomy' }),
        seg('Taxonomy', 'r-tax', [['gtdb', 'GTDB', 'GTDB lineage of the matched genome'], ['ncbi', 'NCBI', 'NCBI lineage of the matched genome']], view.taxonomy, (v) => { view.taxonomy = v; view.showAll = false; draw(); })),
      el('div', { class: 'ctl' }, el('span', { class: 'ctl-label', text: 'Rank' }),
        seg('Rank', 'r-rank', T.RANKS.slice().reverse().map((r) => [r, T.RANK_NAMES[r][0].toUpperCase() + T.RANK_NAMES[r].slice(1)]), view.rank, (v) => { view.rank = v; view.showAll = false; draw(); })),
      el('div', { class: 'ctl' }, el('span', { class: 'ctl-label', text: 'Abundance' }),
        seg('Abundance measure', 'r-metric', [['relative', 'Taxonomic', 'Share of the organisms, corrected for genome size'], ['sequence', 'Sequence', 'Share of the reads (k-mers) assigned to the genome']], view.metric, (v) => { view.metric = v; draw(); })));
    card.append(el('div', { class: 'card-head' }, el('h2', { text: 'Abundance' }), el('div', { class: 'export' }, dlBtn)), controls, body);
    draw();
    if (params.estimate_unknown) {
      card.append(el('p', { class: 'detail abund-note', text: `Estimated unknown fraction: ${fmtPct(unknownFraction(rows))} of the sample is not covered by the database (shown in the sequence abundance view; taxonomic abundance is relative to the genomes found).` }));
    }
    return card;
  }

  function abundanceBody(run) {
    const { rows, params } = run.result;
    const { groups, none, total } = T.rankGroups(rows, view.taxonomy, view.rank, view.metric);
    const rankName = T.RANK_NAMES[view.rank];
    const unknown = params.estimate_unknown && view.metric === 'sequence' ? unknownFraction(rows) : 0;
    const shown = groups.slice(0, TOP_GROUPS);
    const rest = groups.slice(TOP_GROUPS);
    const restValue = rest.reduce((n, g) => n + g.value, 0);
    const segs = shown.map((g, i) => ({ label: g.name, value: g.value, cls: `cat-${(i % 10) + 1}`, rank: view.rank, genomes: g.genomes }));
    if (restValue > 0) segs.push({ label: `Other (${rest.length} ${rest.length === 1 ? 'taxon' : 'taxa'})`, value: restValue, cls: 'cat-other' });
    if (none > 0.0005) segs.push({ label: `Not classified at ${rankName} level`, value: none, cls: 'cat-none', plain: true });
    if (unknown > 0.0005) segs.push({ label: 'Unknown (not in the database)', value: unknown, cls: 'cat-unknown', plain: true });
    const sum = segs.reduce((n, s) => n + s.value, 0) || 1;
    const scale = Math.max(100, sum);

    const bar = el('div', { class: 'stack', role: 'img', 'aria-label': `Abundance by ${rankName}: ${segs.slice(0, 3).map((s) => `${s.label} ${fmtPct(s.value)}`).join(', ')}` },
      segs.map((s) => el('span', { class: `seg-${s.cls}`, style: `width:${((s.value / scale) * 100).toFixed(3)}%`, title: `${s.label}: ${fmtPct(s.value)}` })));

    const maxV = Math.max(...segs.map((s) => s.value), 1e-9);
    const rowsEl = [];
    let listed = [...shown.map((g, i) => ({ g, cls: `cat-${(i % 10) + 1}` })), ...rest.map((g) => ({ g, cls: 'cat-other' }))];
    const limit = view.showAll ? listed.length : 15;
    for (const { g, cls } of listed.slice(0, limit)) rowsEl.push(listRow(g.name, g.value, maxV, cls, italicRank(view.rank, g.name), `${g.genomes} ${g.genomes === 1 ? 'genome' : 'genomes'}`));
    if (none > 0.0005) rowsEl.push(listRow(`Not classified at ${rankName} level`, none, maxV, 'cat-none', false, ''));
    if (unknown > 0.0005) rowsEl.push(listRow('Unknown (not in the database)', unknown, maxV, 'cat-unknown', false, ''));
    const more = listed.length > 15
      ? el('button', { type: 'button', class: 'link-btn', text: view.showAll ? 'show fewer' : `show all ${listed.length}`, onclick: () => {
        view.showAll = !view.showAll;
        const host = bar.parentNode;
        host.replaceChildren(...abundanceBody(run).filter(Boolean));
      } }) : null;
    const head = el('p', { class: 'abund-head muted' },
      `${groups.length} ${(groups.length === 1 ? PLURAL[view.rank][0] : PLURAL[view.rank][1])}`,
      ` · ${view.metric === 'relative' ? 'taxonomic' : 'sequence'} abundance, ${view.taxonomy === 'gtdb' ? 'GTDB' : 'NCBI'}`);
    return [head, bar, el('ul', { class: 'alist' }, rowsEl), more ? el('p', null, more) : null];
  }

  function listRow(name, value, maxV, cls, italic, sub) {
    return el('li', { class: 'arow' },
      el('i', { class: `sw seg-${cls}`, 'aria-hidden': 'true' }),
      el('span', { class: 'aname', title: name }, italic ? el('i', { text: name }) : name, sub ? el('small', { text: sub }) : null),
      el('span', { class: 'abar', 'aria-hidden': 'true' }, el('span', { class: `seg-${cls}`, style: `width:${Math.max(0.5, (value / maxV) * 100).toFixed(2)}%` })),
      el('b', { class: 'apct', text: fmtPct(value) }));
  }

  function downloadSylphmpa(run) {
    const { rows } = run.result;
    const clades = T.aggregateRows(rows, view.taxonomy);
    const text = `${T.taxprofLines(run.name, view.taxonomy, clades).join('\n')}\n`;
    download(`${safeName(run.name)}.${view.taxonomy}.sylphmpa`, text, 'text/tab-separated-values');
  }

  /* ------------------------------------------------------- genome table */
  const lineagePairs = (str) => (!has(str) ? [] : String(str).split(';').map((p) => {
    const m = /^([a-z]+)__(.*)$/.exec(p.trim());
    return m ? { rank: m[1], name: m[2] } : { rank: '', name: p.trim() };
  }));
  const RANK_LABEL = { d: 'Domain', p: 'Phylum', c: 'Class', o: 'Order', f: 'Family', g: 'Genus', s: 'Species' };
  const covOf = (r) => (isNum(r.Eff_cov) ? r.Eff_cov : isNum(r.True_cov) ? r.True_cov : null);

  function tableCard(run) {
    const { rows, params } = run.result;
    const covLabel = params.estimate_unknown ? 'True cov.' : 'Eff. cov.';
    const hits = rows.map((r, i) => {
      const lin = lineagePairs(r.gtdb_taxonomy);
      const sp = lin.find((e) => e.rank === 's');
      return { r, i: i + 1, lin, species: sp ? sp.name : '' };
    });
    const maxTax = Math.max(...hits.map((h) => h.r.Taxonomic_abundance || 0), 1e-9);
    const COLS = [
      { key: 'rank', label: '#', get: (h) => h.i, dir: 'asc' },
      { key: 'species', label: 'GTDB species', get: (h) => h.species, dir: 'asc' },
      { key: 'tax', label: 'Taxonomic %', hint: 'taxonomic abundance', get: (h) => h.r.Taxonomic_abundance, dir: 'desc' },
      { key: 'seq', label: 'Sequence %', hint: 'sequence abundance', get: (h) => h.r.Sequence_abundance, dir: 'desc' },
      { key: 'ani', label: 'ANI %', hint: 'adjusted ANI', get: (h) => h.r.Adjusted_ANI, dir: 'desc' },
      { key: 'cov', label: covLabel, hint: covLabel === 'True cov.' ? 'true (estimated) coverage' : 'effective coverage', get: (h) => covOf(h.r), dir: 'desc' },
      { key: 'ncbi', label: 'NCBI organism', get: (h) => h.r.ncbi_organism_name, dir: 'asc' },
      { key: 'acc', label: 'Accession', get: (h) => h.r.accession, dir: 'asc' },
    ];
    const tv = { sortKey: 'rank', sortDir: 'asc', open: new Set(), all: false };
    const table = el('table', { class: 'hits reads-hits' });
    const headRow = el('tr');
    for (const c of COLS) {
      headRow.append(el('th', { scope: 'col' }, el('button', { type: 'button', text: c.label, title: `Sort by ${c.hint || c.label}`, onclick: () => {
        if (tv.sortKey === c.key) tv.sortDir = tv.sortDir === 'asc' ? 'desc' : 'asc'; else { tv.sortKey = c.key; tv.sortDir = c.dir; }
        fill();
      } })));
    }
    table.append(el('thead', null, headRow), el('tbody'));
    const more = el('p', { class: 'more-rows' });

    const sorted = () => {
      const col = COLS.find((c) => c.key === tv.sortKey);
      const sgn = tv.sortDir === 'asc' ? 1 : -1;
      return hits.slice().sort((x, y) => {
        const a = col.get(x), b = col.get(y);
        const an = a == null || a === '', bn = b == null || b === '';
        if (an || bn) return an === bn ? x.i - y.i : an ? 1 : -1;
        const c = typeof a === 'number' && typeof b === 'number' ? a - b : String(a).localeCompare(String(b), undefined, { sensitivity: 'base' });
        return c * sgn || x.i - y.i;
      });
    };
    const sel = el('select', { id: 'r-sort-select', 'aria-label': 'Sort genomes by' }, COLS.map((c) => el('option', { value: c.key, text: c.label })));
    const dirBtn = el('button', { type: 'button', class: 'btn', 'aria-label': 'Toggle sort direction' });
    sel.addEventListener('change', () => { const c = COLS.find((x) => x.key === sel.value); tv.sortKey = c.key; tv.sortDir = c.dir; fill(); });
    dirBtn.addEventListener('click', () => { tv.sortDir = tv.sortDir === 'asc' ? 'desc' : 'asc'; fill(); });

    function fill() {
      sel.value = tv.sortKey;
      dirBtn.textContent = tv.sortDir === 'asc' ? '▲' : '▼';
      COLS.forEach((c, i) => {
        const th = table.tHead.rows[0].cells[i];
        if (c.key === tv.sortKey) th.setAttribute('aria-sort', tv.sortDir === 'asc' ? 'ascending' : 'descending'); else th.removeAttribute('aria-sort');
      });
      for (const d of table.tBodies[0].querySelectorAll('details.lineage')) { if (d.open) tv.open.add(d.dataset.key); else tv.open.delete(d.dataset.key); }
      const list = sorted();
      const shown = tv.all ? list : list.slice(0, MAX_ROWS_SHOWN);
      table.tBodies[0].replaceChildren(...shown.map((h) => row(h)));
      more.replaceChildren(...(list.length > MAX_ROWS_SHOWN
        ? [el('button', { type: 'button', class: 'btn btn-small', text: tv.all ? `Show only the first ${MAX_ROWS_SHOWN}` : `Show all ${list.length} genomes`, onclick: () => { tv.all = !tv.all; fill(); } })] : []));
    }

    const td = (label, cls, ...content) => el('td', { class: cls, 'data-label': label }, el('div', { class: 'cv' }, content));
    const dash = () => el('span', { class: 'dash', text: '–' });
    function row({ r, i, lin, species }) {
      const key = r.accession || r.Genome_file || `#${i}`;
      const ci = Array.isArray(r['ANI_5-95_percentile']) ? r['ANI_5-95_percentile'] : null;
      const cont = Array.isArray(r.Containment_ind) ? r.Containment_ind : null;
      return el('tr', { class: i === 1 ? 'top-row' : null },
        td('#', 'num rank', String(i)),
        td('GTDB species', null, lin.length ? el('details', { class: 'lineage', 'data-key': key, open: tv.open.has(key) ? '' : null },
          el('summary', { title: 'Show the full GTDB lineage' }, el('i', { text: species || '(no species)' })),
          el('ol', null, lin.map((e) => el('li', null, el('b', { text: RANK_LABEL[e.rank] || e.rank || '' }), el('span', { text: e.name || '–' }))))) : dash()),
        td('Taxonomic %', 'num', el('div', { class: 'anibar' }, el('b', { text: isNum(r.Taxonomic_abundance) ? r.Taxonomic_abundance.toFixed(2) : '–' }),
          el('div', { class: 'bar rel-closest', 'aria-hidden': 'true' }, el('span', { style: `width:${(((r.Taxonomic_abundance || 0) / maxTax) * 100).toFixed(1)}%` })))),
        td('Sequence %', 'num', isNum(r.Sequence_abundance) ? r.Sequence_abundance.toFixed(2) : dash()),
        td('ANI %', 'num', isNum(r.Adjusted_ANI) ? r.Adjusted_ANI.toFixed(2) : dash(),
          ci && ci.every(isNum) ? el('div', { class: 'sub', title: '5–95 % confidence interval of the adjusted ANI', text: `${ci[0].toFixed(1)}–${ci[1].toFixed(1)}` }) : null),
        td(covLabel, 'num', isNum(covOf(r)) ? covOf(r).toFixed(covOf(r) < 10 ? 2 : 1) : dash(),
          cont ? el('div', { class: 'sub', title: 'Contained k-mers / genome k-mers', text: `${fmtInt(cont[0])}/${fmtInt(cont[1])} k-mers` }) : null),
        td('NCBI organism', null, has(r.ncbi_organism_name) || has(r.ncbi_taxid)
          ? el('div', null, has(r.ncbi_organism_name) ? r.ncbi_organism_name : dash(),
            has(r.ncbi_strain_identifiers) ? el('div', { class: 'sub', text: `strain ${r.ncbi_strain_identifiers}` }) : null,
            has(r.ncbi_taxid) ? el('div', { class: 'sub' }, 'taxid ', S.taxidLink(r.ncbi_taxid)) : null) : dash()),
        td('Accession', null, S.accessionLinks(r.accession)));
    }

    const card = el('section', { class: 'card', 'aria-label': 'Detected genomes' },
      el('div', { class: 'card-head' },
        el('h2', { text: `Detected genomes (${hits.length})` }),
        el('div', { class: 'export' },
          el('button', { type: 'button', class: 'btn', text: 'Download TSV', onclick: () => downloadTsv(run) }),
          el('button', { type: 'button', class: 'btn', text: 'Download JSON', onclick: () => download(`${safeName(run.name)}.json`, `${JSON.stringify(run.result.data, null, 2)}\n`, 'application/json') }))),
      el('div', { class: 'mobile-sort' }, el('label', { for: 'r-sort-select', text: 'Sort by' }), sel, dirBtn),
      el('div', { class: 'table-wrap' }, table), more);
    fill();
    return card;
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
    if (params.estimate_unknown) base[base.indexOf('Eff_cov')] = 'True_cov';
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
    download(`${safeName(run.name)}.tsv`, `${lines.join('\n')}\n`, 'text/tab-separated-values');
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
