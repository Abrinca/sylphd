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
  const DEFAULT_LIMIT_MB = 20;
  const COMPRESS_OVER = 1 << 20; // compress pasted text above 1 MiB
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
    statusDetail: $('status-detail'), banner: $('server-banner'), submitHint: $('submit-hint'),
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
  function fmtBytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`;
    return `${(n / 1048576).toFixed(n < 10485760 ? 2 : 1)} MB`;
  }
  function fmtBp(n) {
    if (!Number.isFinite(n)) return '–';
    return n >= 1e6 ? `${(n / 1e6).toFixed(2)} Mb` : n >= 1e3 ? `${(n / 1e3).toFixed(1)} kb` : `${n} bp`;
  }
  const fmtSecs = (ms) => `${(ms / 1000).toFixed(1)} s`;
const fmtDur = (ms) => (ms < 1000 ? `${Math.round(ms)} ms` : fmtSecs(ms));
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const has = (v) => v != null && v !== '' && v !== 'none' && v !== 'na';

  /* -------------------------------------------------------- server info */
  let serverInfo = null;
  let infoTimer = null;

  function setStatus(state, text) {
    ui.status.dataset.state = state;
    ui.statusText.textContent = text;
  }

  // Why the server cannot be used right now: null (usable or not yet known),
  // 'none' | 'invalid' | 'offline' | 'noassembly'. A running re-check keeps the
  // previous value, so the banner never flashes during "Checking…".
  let serverProblem = null;
  let bannerKey = '';
  const PROBLEM_HINT = {
    none: 'Public server offline', invalid: 'Invalid API URL', offline: 'Server offline',
    noassembly: 'Server does not accept assemblies',
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

  function connectForm() {
    const input = el('input', {
      type: 'text', id: 'other-api', placeholder: 'http://127.0.0.1:8765', spellcheck: 'false',
      autocomplete: 'off', autocapitalize: 'off', inputmode: 'url', 'aria-label': 'API URL of another server',
    });
    const err = el('p', { class: 'detail connect-err', role: 'alert', hidden: true });
    const form = el('form', { class: 'connect', novalidate: true },
      el('label', { for: 'other-api', text: 'Use another server' }),
      el('div', { class: 'connect-row' }, input, el('button', { type: 'submit', class: 'btn', text: 'Connect' })),
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
    return form;
  }

  function renderBanner() {
    const key = `${serverProblem}|${API}`;
    if (!serverProblem) {
      ui.banner.hidden = true;
      ui.banner.replaceChildren();
      bannerKey = '';
      return;
    }
    ui.banner.hidden = false;
    if (key === bannerKey) return; // keep a half-typed URL in the connect form
    bannerKey = key;
    let cls = 'error';
    let title;
    let body;
    let retry = false;
    if (serverProblem === 'none') {
      title = 'Public server offline';
      body = [el('p', null, 'The public sylphd server is offline, so this page cannot search anything right now. ',
        'You can use your own server instead (see ', el('a', { href: './docs/guides/public-server/', text: 'running a public server' }), ').')];
    } else if (serverProblem === 'invalid') {
      title = 'Invalid API URL';
      body = [el('p', null, 'The API base URL ', el('code', { text: API || '(empty)' }), ' is not a valid URL.')];
    } else if (serverProblem === 'noassembly') {
      cls = 'warn';
      title = 'This server does not accept assemblies';
      body = [el('p', null, 'The server at ', el('code', { text: API }), ' is online, but its ',
        el('code', { text: '/assembly' }), ' endpoint is disabled (it was started without ', el('code', { text: '--allow-sequences' }), '), so this page cannot submit sequences to it.')];
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
      apiOverride ? el('p', { class: 'detail' }, 'Using ', el('code', { text: API || '(empty)' }), ' from the address. ', resetLink(), ' to go back to the default server.') : null,
      connectForm()));
  }

  function setProblem(kind) {
    serverProblem = kind;
    renderBanner();
    updateSubmit();
  }

  function overrideBits() {
    return apiOverride ? [' · ', el('span', null, 'API: ', el('code', { text: API || '(empty)' }), ' ('), resetLink(), ')'] : [];
  }

  async function loadInfo() {
    clearTimeout(infoTimer);
    if (!API) {
      serverInfo = null;
      setStatus('offline', 'Public server offline');
      ui.statusDetail.replaceChildren('The public server is offline. ',
        'To use your own server, add ', el('code', { text: '?api=https://…' }), ' to the address.');
      setProblem('none');
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
      ui.statusDetail.replaceChildren('The API base URL ', el('code', { text: API || '(empty)' }), ' is not a valid URL.', ...(apiOverride ? [' (', resetLink(), ')'] : []));
      setProblem('invalid');
      return;
    }
    try {
      const res = await fetch(`${API}/info`, { signal: timeoutSignal(8000), cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const info = await res.json();
      serverInfo = info;
      const db = info.database || {};
      const meta = info.metadata || {};
      setStatus('online', 'Server online');
      const lim = info.limits || {};
      const fact = (label, value, title) => el('span', { class: 'fact', title: title || null },
        el('span', { class: 'fact-label', text: label }), el('span', { class: 'fact-value', text: value }));
      const facts = [];
      facts.push(fact('Database', meta.release ? `GTDB r${meta.release}` : (db.name || '?'), db.name || null));
      if (isNum(db.genomes)) facts.push(fact('Genomes', fmtInt(db.genomes)));
      if (isNum(db.c) && isNum(db.k)) facts.push(fact('Sketch', `c=${db.c} · k=${db.k}`, 'sylph subsampling rate c and k-mer size k'));
      if (isNum(lim.max_sequence_mb)) facts.push(fact('Upload', `≤ ${lim.max_sequence_mb} MB`, 'maximum assembly size'));
      if (lim.rate_limit && isNum(lim.rate_limit.count)) {
        const w = Math.round(lim.rate_limit.window_seconds);
        facts.push(fact('Rate', `${lim.rate_limit.count} / ${w === 60 ? 'min' : `${w} s`}`, 'requests per client'));
      }
      if (apiOverride) {
        facts.push(el('span', { class: 'fact' }, el('span', { class: 'fact-label', text: 'API' }),
          el('code', { class: 'fact-value', text: API }), ' ', resetLink()));
      }
      const frag = [el('span', { class: 'facts' }, ...facts)];
      const noAssembly = lim.allow_sequences === false || info.allow_sequences === false;
      if (noAssembly) {
        frag.push(' · ', el('span', { class: 'warn', text: 'this server does not accept assemblies (/assembly disabled)' }));
      }
      ui.statusDetail.replaceChildren(...frag);
      infoTimer = setTimeout(loadInfo, 60000);
      setProblem(noAssembly ? 'noassembly' : null);
      refreshInput();
    } catch (e) {
      serverInfo = null;
      setStatus('offline', 'Server offline');
      ui.statusDetail.replaceChildren('Cannot reach ', el('code', { text: API }), ...(apiOverride ? [' (', resetLink(), ')'] : ['.']));
      setProblem('offline');
      const rb = $('retry-now');
      if (rb) { rb.disabled = false; rb.textContent = 'Retry now'; }
      infoTimer = setTimeout(loadInfo, 20000);
    }
  }

  function timeoutSignal(ms) {
    if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) return AbortSignal.timeout(ms);
    const c = new AbortController();
    setTimeout(() => c.abort(), ms);
    return c.signal;
  }

  const limitMb = () => (serverInfo && serverInfo.limits && serverInfo.limits.max_sequence_mb) || DEFAULT_LIMIT_MB;
  const limitBytes = () => limitMb() * 1024 * 1024;

  /* -------------------------------------------------------------- input */
  const input = { file: null, gz: false, size: 0 };
  let busy = false;
  let cooldownUntil = 0;
  let cooldownTimer = null;

  async function isGzip(file) {
    const b = new Uint8Array(await file.slice(0, 2).arrayBuffer());
    return b.length === 2 && b[0] === 0x1f && b[1] === 0x8b;
  }

  async function setFile(file) {
    input.file = file;
    input.gz = await isGzip(file);
    input.size = file.size;
    ui.fasta.value = '';
    ui.chipName.textContent = file.name;
    ui.chipMeta.textContent = `${fmtBytes(file.size)} · ${input.gz ? 'gzip, sent as-is' : 'plain text'}`;
    ui.chip.hidden = false;
    ui.drop.classList.add('has-file');
    refreshInput();
  }

  function clearInput() {
    input.file = null; input.gz = false; input.size = 0;
    ui.fasta.value = '';
    ui.file.value = '';
    ui.chip.hidden = true;
    ui.drop.classList.remove('has-file');
    refreshInput();
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

  function refreshInput() {
    const size = input.file ? input.size : textBytes();
    input.size = size;
    const over = size > limitBytes();
    ui.size.textContent = size ? `${fmtBytes(size)} of ${limitMb()} MB max` : '';
    ui.size.classList.toggle('over', over);
    if (over) {
      ui.msg.textContent = `Too large: ${fmtBytes(size)} exceeds the ${limitMb()} MB limit. ` +
        'Reduce the assembly (e.g. drop tiny contigs) or upload a gzip-compressed file.';
      ui.msg.hidden = false;
    } else {
      ui.msg.hidden = true;
    }
    updateSubmit();
  }

  function updateSubmit() {
    const now = Date.now();
    const cooling = cooldownUntil > now;
    const ready = input.size > 0 && input.size <= limitBytes() && validateOptions().ok;
    const problem = API ? serverProblem : 'none';
    ui.submit.disabled = busy || cooling || !ready || !!problem;
    ui.submitHint.textContent = problem ? PROBLEM_HINT[problem] : '';
    ui.submitHint.hidden = !problem;
    ui.clear.disabled = busy;
    ui.pick.disabled = busy;
    ui.example.disabled = busy;
    ui.top.disabled = busy;
    ui.minAni.disabled = busy;
    if (cooling && !busy) {
      ui.submit.textContent = `Retry in ${Math.ceil((cooldownUntil - now) / 1000)} s`;
    } else {
      ui.submit.textContent = 'Find closest genomes';
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
    if (input.file) clearInput();
    refreshInput();
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
  let runStart = 0;
  let runTimer = null;

  function setBusy(on, text) {
    busy = on;
    ui.progress.hidden = !on;
    ui.cancel.hidden = !on;
    if (on) {
      runStart = performance.now();
      const tick = () => { ui.progressText.textContent = `${text()} ${fmtSecs(performance.now() - runStart)}`; };
      tick();
      runTimer = setInterval(tick, 100);
    } else {
      clearInterval(runTimer);
    }
    updateSubmit();
  }

  async function buildBody() {
    if (input.file) {
      return { body: input.file, type: input.gz ? 'application/gzip' : 'text/plain', name: input.file.name };
    }
    const blob = new Blob([ui.fasta.value], { type: 'text/plain' });
    if (blob.size > COMPRESS_OVER && typeof CompressionStream !== 'undefined') {
      const gz = await new Response(blob.stream().pipeThrough(new CompressionStream('gzip'))).blob();
      return { body: gz, type: 'application/gzip', name: '' };
    }
    return { body: blob, type: 'text/plain', name: '' };
  }

  ui.form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    if (ui.submit.disabled) return;
    const opts = validateOptions();
    if (!opts.ok) return;
    ui.results.replaceChildren();
    let phase = 'Preparing…';
    controller = new AbortController();
    setBusy(true, () => phase);
    try {
      const { body, type, name } = await buildBody();
      phase = `Uploading ${fmtBytes(body.size)} and searching…`;
      const url = new URL(`${API}/assembly`);
      url.searchParams.set('top', String(opts.top));
      url.searchParams.set('min_ani', String(opts.ani));
      if (name) url.searchParams.set('name', name.slice(0, 200));
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
      renderResults(data, performance.now() - runStart);
    } catch (e) {
      if (e.name === 'AbortError') {
        ui.results.replaceChildren(notice('warn', 'Cancelled', 'The request was cancelled.'));
      } else if (e.network) {
        showNetworkError();
      } else {
        ui.results.replaceChildren(notice('error', 'Something went wrong', e.message || String(e)));
      }
    } finally {
      setBusy(false);
      controller = null;
      if (ui.results.firstChild) {
        ui.results.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
      }
    }
  });
  ui.cancel.addEventListener('click', () => { if (controller) controller.abort(); });

  function notice(kind, title, ...body) {
    return el('div', { class: `notice ${kind}`, role: kind === 'error' ? 'alert' : null },
      el('h3', { text: title }), ...body.map((b) => (b instanceof Node ? b : el('p', { text: b }))));
  }

  function showHttpError(status, serverMsg, retry, raw) {
    let node;
    const msg = serverMsg || (raw && raw.length < 300 && !raw.trim().startsWith('<') ? raw.trim() : '');
    if (status === 413) {
      node = notice('error', 'Upload too large (413)',
        `The server rejected the upload because it is over its size limit (${limitMb()} MB, or five times that once decompressed).`,
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
        'This server does not provide POST /assembly (it must be started with --allow-sequences).',
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

  /* ----------------------------------------------------------- results */
  const RANKS = { d: 'Domain', p: 'Phylum', c: 'Class', o: 'Order', f: 'Family', g: 'Genus', s: 'Species' };

  function parseLineage(str) {
    if (!has(str)) return [];
    return String(str).split(';').map((p) => {
      const m = /^([a-z]+)__(.*)$/.exec(p.trim());
      return m ? { rank: m[1], name: m[2] } : { rank: '', name: p.trim() };
    });
  }
  const lineageName = (lin, r) => { const x = lin.find((e) => e.rank === r); return x && x.name ? x.name : ''; };

  let current = null; // { data, hits, sortKey, sortDir, thr, elapsed }

  // Depth of the shared GTDB rank: drives badge colours and sorting.
  const DEPTH = { species: 0, genus: 1, family: 2, order: 3, class: 4, phylum: 5, domain: 6 };
  const NONE_DEPTH = 7;

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
  const hitKey = (h) => h.accession || h.genome_file || `#${h.rank}`;

  function decorate(h) {
    const lin = parseLineage(h.gtdb_taxonomy);
    return Object.assign({}, h, {
      _lin: lin, _species: lineageName(lin, 's'), _genus: lineageName(lin, 'g'),
      _rel: relClass(h),
      _frac: Array.isArray(h.containment) && h.containment[1] > 0 ? h.containment[0] / h.containment[1] : (h.containment_fraction ?? null),
    });
  }

  function renderResults(data, clientMs) {
    const thr = Object.assign({ species: 95, imprecise_below: 85 }, data.thresholds || {});
    const hits = data.hits.map((h) => decorate(h));
    current = { data, hits, thr, sortKey: 'rank', sortDir: 'asc', clientMs, open: new Set() };
    const out = [];
    out.push(summaryCard(data, clientMs));
    if (!hits.length) {
      out.push(emptyCard(data));
    } else {
      out.push(bestCard(data, hits, thr));
      out.push(tableCard());
    }
    ui.results.replaceChildren(...out);
  }

  function summaryCard(data, clientMs) {
    const q = data.query || {};
    const db = data.database || {};
    const item = (label, value, small) => el('div', null, el('dt', { text: label }), el('dd', null, value, small ? el('small', { text: small }) : null));
    return el('section', { class: 'card', 'aria-label': 'Query summary' },
      el('div', { class: 'card-head' }, el('h2', { text: 'Query' }),
        el('span', { class: 'muted', text: q.name ? `“${q.name}”` : '' })),
      el('dl', { class: 'summary' },
        item('Records', fmtInt(q.records)),
        item('Length', fmtBp(q.length), isNum(q.length) ? `${fmtInt(q.length)} bp` : ''),
        item('GC', isNum(q.gc) ? `${q.gc.toFixed(1)} %` : '–'),
        item('Sketch k-mers', fmtInt(q.sketch_kmers)),
        item('Search time', isNum(data.elapsed_ms) ? `${Math.round(data.elapsed_ms)} ms` : '–', `${fmtDur(clientMs)} incl. upload`),
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
      el('span', { title: RANKS[e.rank] }, e.rank === 'g' ? el('i', { text: e.name }) : e.name)]));
  }

  const LEGEND = [
    ['species', 'same species'], ['genus', 'genus'], ['family', 'family'], ['order', 'order'],
    ['class', 'class'], ['phylum', 'phylum'], ['domain', 'domain'], ['none', 'different domain'],
  ];

  function bestCard(data, hits, thr) {
    const h = hits[0];
    const asg = data.assignment || null;
    const species = !!asg;
    const taxon = asg && asg.taxon;
    let verdict;
    let sub = null;
    if (species) {
      verdict = ['Same species as ', taxon ? el('i', { text: taxon }) : el('span', { class: 'mono', text: asg.accession || 'the best hit' })];
    } else {
      verdict = ['No species-level match'];
      sub = el('p', { class: 'closest' }, 'Closest relative: ', el('i', { text: nameFragment(h) }),
        ` (${h.ani.toFixed(1)} % ANI)`);
    }
    const [a, b] = Array.isArray(h.containment) ? h.containment : [null, null];
    const notes = [];
    if (sub) notes.push(sub);
    notes.push(el('p', null, species ? 'Assigned from ' : 'Best hit ', accessionLinks(h.accession),
      has(h.ncbi_organism_name) ? [' · ', h.ncbi_organism_name] : '',
      has(h.ncbi_strain_identifiers) ? ` (${h.ncbi_strain_identifiers})` : ''));
    const trail = lineageTrail(h._lin);
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
      notes.push(el('p', null,
        `${plural(boundary.length, 'other hit reaches', 'other hits reach')} ${thr.species} % ANI but ${boundary.length === 1 ? 'belongs' : 'belong'} to a different GTDB species (`,
        el('a', { href: 'docs/species-and-ani/#species-boundary-cases', text: 'a species-boundary case' }),
        ').'));
    }
    if (isNum(a) && isNum(b) && b > 0) {
      notes.push(el('p', { title: 'Fraction of the query sketch k-mers that are also present in this genome' },
        `${fmtInt(a)} of ${fmtInt(b)} query k-mers shared (${(100 * a / b).toFixed(1)} %).`));
    }
    const legendHead = species ? 'Relation to the assigned species:' : 'Relation to the closest relative:';
    return el('section', { class: 'card', 'aria-label': 'Result' },
      el('div', { class: 'best' },
        gauge(h.ani, thr, species),
        el('div', null,
          el('p', { class: `verdict ${species ? 'verdict-species' : 'verdict-below'}` }, verdict),
          el('div', { class: 'best-meta' }, notes),
          el('ul', { class: 'legend', 'aria-label': legendHead },
            el('li', { class: 'legend-head', text: legendHead }),
            LEGEND.map(([k, label]) => el('li', null, el('i', { class: `sw rel-${k}` }), label)),
            el('li', null, el('i', { class: 'sw-dash' }), `k-mer ANI < ~${thr.imprecise_below} % is imprecise`)))));
  }

  /* links */
  function accessionLinks(acc) {
    if (!has(acc)) return el('span', { class: 'dash', text: '–' });
    const e = encodeURIComponent(acc);
    return el('span', null,
      el('a', { href: `https://www.ncbi.nlm.nih.gov/datasets/genome/${e}/`, rel: 'noopener', title: 'NCBI Datasets', class: 'mono', text: acc }),
      ' ',
      el('a', { href: `https://gtdb.ecogenomic.org/genome?gid=${e}`, rel: 'noopener', title: 'View in GTDB', class: 'badge badge-plain', text: 'GTDB' }));
  }
  function taxidLink(id) {
    const n = Number(id);
    if (!has(id) || !Number.isInteger(n) || n <= 0) return el('span', { class: 'dash', text: '–' });
    return el('a', { href: `https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?id=${n}`, rel: 'noopener', text: String(n) });
  }

  /* table */
  const COLS = [
    { key: 'rank', label: '#', get: (h) => h.rank, dir: 'asc' },
    { key: 'ani', label: 'ANI %', get: (h) => h.ani, dir: 'desc' },
    { key: 'rel', label: 'Relation', hint: 'relation (closest first)', get: (h) => relDepth(h), dir: 'asc' },
    { key: 'cont', label: 'Containment', get: (h) => h._frac, dir: 'desc' },
    { key: 'species', label: 'GTDB species', get: (h) => h._species, dir: 'asc' },
    { key: 'ncbi', label: 'NCBI organism', get: (h) => h.ncbi_organism_name, dir: 'asc' },
    { key: 'taxid', label: 'TaxID', get: (h) => h.ncbi_taxid, dir: 'asc' },
    { key: 'acc', label: 'Accession', get: (h) => h.accession, dir: 'asc' },
    { key: 'quality', label: 'Quality', hint: 'CheckM2 completeness / contamination (%)', get: (h) => h.checkm2_completeness, dir: 'desc' },
  ];

  function sortedHits() {
    const col = COLS.find((c) => c.key === current.sortKey);
    const sgn = current.sortDir === 'asc' ? 1 : -1;
    return current.hits.slice().sort((x, y) => {
      const a = col.get(x), b = col.get(y);
      const an = a == null || a === '', bn = b == null || b === '';
      if (an || bn) return an === bn ? x.rank - y.rank : an ? 1 : -1; // missing always last
      const c = typeof a === 'number' && typeof b === 'number' ? a - b : String(a).localeCompare(String(b), undefined, { sensitivity: 'base' });
      return c * sgn || x.rank - y.rank;
    });
  }

  function tableCard() {
    const table = el('table', { class: 'hits' });
    const headRow = el('tr');
    for (const c of COLS) {
      const th = el('th', { scope: 'col' }, el('button', { type: 'button', text: c.label, title: `Sort by ${c.hint || c.label}`, onclick: () => {
        if (current.sortKey === c.key) current.sortDir = current.sortDir === 'asc' ? 'desc' : 'asc';
        else { current.sortKey = c.key; current.sortDir = c.dir; }
        fillTable(table);
      } }));
      headRow.append(th);
    }
    table.append(el('thead', null, headRow), el('tbody'));
    fillTable(table);
    const n = current.hits.length;
    return el('section', { class: 'card', 'aria-label': 'Hits' },
      el('div', { class: 'card-head' },
        el('h2', { text: `Closest genomes (${n})` }),
        el('div', { class: 'export' },
          el('button', { type: 'button', class: 'btn', text: 'Download TSV', onclick: downloadTsv }),
          el('button', { type: 'button', class: 'btn', text: 'Download JSON', onclick: downloadJson }))),
      mobileSort(table),
      el('div', { class: 'table-wrap' }, table));
  }

  function mobileSort(table) {
    const sel = el('select', { id: 'sort-select', 'aria-label': 'Sort hits by' },
      COLS.map((c) => el('option', { value: c.key, text: c.key === 'quality' ? 'Quality' : c.label })));
    const dirBtn = el('button', { type: 'button', class: 'btn', 'aria-label': 'Toggle sort direction' });
    const sync = () => {
      sel.value = current.sortKey;
      dirBtn.textContent = current.sortDir === 'asc' ? '▲' : '▼';
    };
    sel.addEventListener('change', () => {
      const c = COLS.find((x) => x.key === sel.value);
      current.sortKey = c.key; current.sortDir = c.dir; fillTable(table);
    });
    dirBtn.addEventListener('click', () => { current.sortDir = current.sortDir === 'asc' ? 'desc' : 'asc'; fillTable(table); });
    table._syncSort = sync;
    sync();
    return el('div', { class: 'mobile-sort' }, el('label', { for: 'sort-select', text: 'Sort by' }), sel, dirBtn);
  }

  function fillTable(table) {
    if (table._syncSort) table._syncSort();

    COLS.forEach((c, i) => {
      const th = table.tHead.rows[0].cells[i];
      if (c.key === current.sortKey) th.setAttribute('aria-sort', current.sortDir === 'asc' ? 'ascending' : 'descending');
      else th.removeAttribute('aria-sort');
    });
    // Remember which lineages are expanded across re-sorts, keyed by accession.
    for (const d of table.tBodies[0].querySelectorAll('details.lineage')) {
      if (d.open) current.open.add(d.dataset.key); else current.open.delete(d.dataset.key);
    }
    table.tBodies[0].replaceChildren(...sortedHits().map((h) => hitRow(h)));
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
    if (!h._lin.length) return dash();
    const label = h._species || '(no species)';
    const list = el('ol', null, h._lin.map((e) => el('li', null,
      el('b', { text: RANKS[e.rank] || e.rank || '' }), el('span', { text: e.name || '–' }))));
    const key = hitKey(h);
    return el('details', { class: 'lineage', 'data-key': key, open: current.open.has(key) ? '' : null }, el('summary', { title: 'Show the full GTDB lineage' }, el('i', { text: label })), list);
  }

  function relationBadge(h) {
    if (!has(h.relation)) return dash();
    const title = h.shared_rank ? `Deepest GTDB rank shared: ${h.shared_rank}` : 'No shared GTDB rank';
    return el('div', { class: 'rel' },
      el('span', { class: `badge badge-rel rel-${h._rel}`, title, text: h.relation }),
      h.ani_species_level && h.shared_rank !== 'species'
        ? el('div', { class: 'sub', title: 'The ANI reaches the species threshold, but GTDB places this genome in a different species', text: `ANI ≥ ${current.thr.species}, other species` })
        : null);
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

  function hitRow(h) {
    const [a, b] = Array.isArray(h.containment) ? h.containment : [null, null];
    return el('tr', { class: h.rank === 1 ? 'top-row' : null },
      cellTd('#', 'num rank', String(h.rank)),
      cellTd('ANI %', null, aniCell(h)),
      cellTd('Relation', null, relationBadge(h)),
      cellTd('Containment', 'num', isNum(a) && isNum(b)
        ? [`${fmtInt(a)} / ${fmtInt(b)}`, el('div', { class: 'sub', text: isNum(h._frac) ? `${(h._frac * 100).toFixed(1)} %` : '' })] : dash()),
      cellTd('GTDB species', null, speciesCell(h)),
      cellTd('NCBI organism', null, ncbiCell(h)),
      cellTd('TaxID', 'num', taxidLink(h.ncbi_taxid)),
      cellTd('Accession', null, accessionLinks(h.accession)),
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
  function baseName() {
    const q = current.data.query && current.data.query.name;
    const safe = String(q || 'assembly').replace(/\.(gz|fasta|fna|fa|fas)/gi, '').replace(/[^\w.-]+/g, '_').slice(0, 60) || 'assembly';
    return `sylphd_${safe}`;
  }
  function downloadJson() {
    download(`${baseName()}.json`, JSON.stringify(current.data, null, 2) + '\n', 'application/json');
  }
  function downloadTsv() {
    const hits = sortedHits();
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
    download(`${baseName()}.tsv`, rows.join('\n') + '\n', 'text/tab-separated-values');
  }

  /* -------------------------------------------------------------- init */
  refreshInput();
  loadInfo();
})();
