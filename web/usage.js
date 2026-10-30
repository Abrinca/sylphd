// Usage statistics: the agreement asked before the first upload to a server that keeps
// them, and the #usage view that shows them. Loaded after app.js; exposes window.SylphdUsage.
// All server strings are inserted with textContent / text nodes only.
(() => {
  'use strict';
  const S = window.Sylphd;
  if (!S) return;
  const { el, svg } = S;

  /* ------------------------------------------------------------ agreement */
  // One remembered agreement per server address; its value is the policy that was agreed to
  // (the server bumps the policy whenever what it tracks changes), so another server or a
  // higher policy asks again.
  const AGREED_PREFIX = 'sylphd.usage.agreed:';
  const agreedKey = (api) => `${AGREED_PREFIX}${api}`;
  const isAgreed = (api, policy) => S.readChoice(agreedKey(api), [String(policy)]) !== null;
  const agree = (api, policy) => { S.writeChoice(agreedKey(api), String(policy)); announceChoices(); };

  function agreementKeys() {
    const keys = [];
    try {
      for (let i = 0; i < localStorage.length; i += 1) {
        const key = localStorage.key(i);
        if (key && key.startsWith(AGREED_PREFIX)) keys.push(key);
      }
    } catch { /* no localStorage: nothing is remembered */ }
    return keys;
  }
  const hasAgreements = () => agreementKeys().length > 0;
  function forgetAgreements() {
    for (const key of agreementKeys()) S.writeChoice(key, null);
    announceChoices();
  }
  // contigs.js shows "Reset remembered choices" while anything is remembered
  function announceChoices() {
    if (typeof document !== 'undefined') document.dispatchEvent(new Event('sylphd:choices'));
  }

  // Page URL of this view on the same server (for links that open in a new tab).
  function usageUrl() {
    const u = new URL(location.href);
    u.hash = 'usage';
    return u.toString();
  }

  // Resolves true when the visitor may send to the server: it keeps no usage statistics, the
  // visitor agreed before (same server, same policy), or agrees now. False after Cancel.
  async function ensureAgreed() {
    const usage = S.usage();
    if (!usage || isAgreed(S.api, usage.policy)) return true;
    const choice = await S.choiceModal({
      title: 'This server keeps usage statistics',
      paragraphs: [
        'For each successful request it counts, per ten-minute window: the endpoint, the kind of client (this web page, the sylphd command line or something else), the number of samples or contigs, the size of the sketch, and how long the job waited and ran.',
        el('p', { class: 'detail' },
          'No IP addresses, file, sample or contig names, results, taxa or settings are stored, and nothing per person. The totals are public: see the ',
          el('a', { href: usageUrl(), target: '_blank', rel: 'noopener', text: 'Usage view' }),
          ' and the ', el('a', { href: 'terms.html', target: '_blank', rel: 'noopener', text: 'terms of use' }), '.'),
      ],
      buttons: [
        { label: 'Agree and continue', value: 'agree', primary: true },
        { label: 'Cancel', value: 'cancel' },
      ],
      escape: 'cancel',
      note: 'Your sequences still stay on your computer. Your agreement is remembered in this browser for this server (and policy version); nothing is sent if you cancel.',
    });
    if (choice !== 'agree') return false;
    agree(S.api, usage.policy);
    return true;
  }

  window.SylphdUsage = { ensureAgreed, isAgreed, agree, hasAgreements, forgetAgreements, agreedKey };

  /* ----------------------------------------------------------------- view */
  const KINDS = [
    ['web', 'Web page', 'seg-cat-1'],
    ['cli', 'sylphd command line', 'seg-cat-2'],
    ['other', 'Other clients', 'seg-cat-other'],
  ];
  const KIND_NAME = Object.fromEntries(KINDS.map(([key, name]) => [key, name]));
  const ENDPOINT_SWATCH = { '/assembly': 'seg-cat-1', '/contigs': 'seg-cat-2', '/profile': 'seg-cat-3', '/query': 'seg-cat-4' };
  const DAYS = 90;
  const CHART_H = 60;

  let host = null;
  let token = 0;

  // A bucket upper bound in seconds, e.g. "≤ 250 ms".
  const bound = (seconds) => (S.isNum(seconds) ? `≤ ${S.fmtDur(seconds * 1000)}` : '–');
  const pair = (distribution) => (distribution && distribution.count ? `${bound(distribution.median)} / ${bound(distribution.p95)}` : '–');
  const isoDay = (date) => date.toISOString().slice(0, 10);

  function card(title, ...body) {
    return el('section', { class: 'card', 'aria-label': title }, el('div', { class: 'card-head' }, el('h2', { text: title })), ...body);
  }
  function message(kind, title, ...body) {
    return el('div', { class: 'usage-view' }, S.notice(kind, title, ...body), backLink());
  }
  const backLink = () => el('p', { class: 'usage-back' }, el('a', { href: '#', text: '← Back to sylphd' }));

  function summaryCard(data) {
    const t = data.totals;
    const item = (label, value, small) => el('div', null, el('dt', { text: label }), el('dd', null, value, small ? el('small', { text: small }) : null));
    return el('section', { class: 'card', 'aria-label': 'Totals' },
      el('div', { class: 'card-head' }, el('h2', { text: 'Usage statistics' }),
        el('span', { class: 'badge badge-plain', title: 'what is tracked changes only with a new policy version' }, `policy ${data.policy}`)),
      el('dl', { class: 'summary usage-summary' },
        item('Requests', S.fmtInt(t.requests), 'successful'),
        item('Samples profiled', S.fmtInt(t.samples)),
        item('Contigs classified', S.fmtInt(t.contigs)),
        item('Bases', S.fmtBp(t.bases), 'as reported by the client'),
        item('Sketches', S.fmtBytes(t.sketch_bytes), 'received'),
        item('Job time', pair(t.job_seconds), 'median / 95th percentile'),
        item('Queue wait', pair(t.queue_wait_seconds), 'median / 95th percentile'),
        item('Peak queue', S.fmtInt(t.peak_queue_depth), 'jobs beyond capacity'),
        item('Since', data.since ? data.since.slice(0, 10) : '–', `updated about every minute · ${data.window_minutes}-minute windows`)));
  }

  // Requests per UTC day over the last DAYS days, split by client kind.
  function dailyCounts(data) {
    const cols = data.windows.columns;
    const at = (name) => cols.indexOf(name);
    const [iStart, iClient, iRequests] = [at('start'), at('client'), at('requests')];
    const end = new Date(`${data.generated_at.slice(0, 10)}T00:00:00Z`);
    const days = [];
    const byDay = new Map();
    for (let i = DAYS - 1; i >= 0; i -= 1) {
      const key = isoDay(new Date(end.getTime() - i * 86400000));
      const counts = { day: key, web: 0, cli: 0, other: 0 };
      days.push(counts);
      byDay.set(key, counts);
    }
    for (const row of data.windows.rows) {
      const counts = byDay.get(String(row[iStart]).slice(0, 10));
      if (counts && counts[row[iClient]] !== undefined) counts[row[iClient]] += row[iRequests];
    }
    return days;
  }

  function chartCard(data) {
    const days = dailyCounts(data);
    const total = (d) => d.web + d.cli + d.other;
    const peak = Math.max(1, ...days.map(total));
    const sum = days.reduce((n, d) => n + total(d), 0);
    const bars = svg('svg', {
      class: 'uchart-svg', viewBox: `0 0 ${DAYS} ${CHART_H}`, preserveAspectRatio: 'none', role: 'img',
      'aria-label': `Requests per day, last ${DAYS} days: ${S.fmtInt(sum)} in total, at most ${S.fmtInt(peak)} on one day`,
    });
    days.forEach((d, i) => {
      const g = svg('g', null, svg('title', null, `${d.day}: ${S.fmtInt(total(d))} requests${total(d) ? ` (web ${d.web}, command line ${d.cli}, other ${d.other})` : ''}`),
        svg('rect', { class: 'uchart-hit', x: i, y: 0, width: 1, height: CHART_H }));
      let y = CHART_H;
      for (const [kind] of KINDS) {
        const h = (CHART_H * d[kind]) / peak;
        if (!h) continue;
        y -= h;
        g.append(svg('rect', { class: `uc-${kind}`, x: i + 0.1, y, width: 0.8, height: h }));
      }
      bars.append(g);
    });
    return card(`Activity, last ${DAYS} days`,
      el('div', { class: 'uchart' },
        el('div', { class: 'uchart-y' }, el('span', { text: `${S.fmtInt(peak)} / day` }), el('span', { text: '0' })),
        el('div', { class: 'uchart-plot' }, bars,
          el('div', { class: 'uchart-x' }, el('span', { text: days[0].day }), el('span', { text: days[days.length - 1].day })))),
      el('ul', { class: 'ulegend' }, KINDS.map(([kind, name, swatch]) => el('li', null, el('i', { class: `sw ${swatch}`, 'aria-hidden': 'true' }), name))),
      el('p', { class: 'detail' }, 'Successful requests per day (UTC), stacked by kind of client.'));
  }

  function breakdownList(label, groups, order, nameOf, swatchOf) {
    const keys = order.filter((key) => groups[key]);
    const max = Math.max(1, ...keys.map((key) => groups[key].requests));
    return el('div', { class: 'usage-breakdown' }, el('h3', { text: label }),
      el('ul', { class: 'alist' }, keys.map((key) => {
        const g = groups[key];
        return S.barRow({
          swatch: swatchOf(key), name: nameOf(key), value: g.requests, max, text: S.fmtInt(g.requests),
          tip: `${nameOf(key)}: ${S.fmtInt(g.requests)} requests · job time ${pair(g.job_seconds)} (median / p95) · queue wait ${pair(g.queue_wait_seconds)}`,
        });
      })));
  }

  function breakdownCard(data) {
    const endpoints = Object.keys(data.by_endpoint).sort((a, b) => data.by_endpoint[b].requests - data.by_endpoint[a].requests);
    return card('Breakdown',
      el('div', { class: 'usage-breakdowns' },
        breakdownList('By endpoint', data.by_endpoint, endpoints, (key) => key, (key) => ENDPOINT_SWATCH[key] || 'seg-cat-other'),
        breakdownList('By client', data.by_client, KINDS.map(([key]) => key), (key) => KIND_NAME[key] || key,
          (key) => KINDS.find(([k]) => k === key)[2])),
      el('p', { class: 'detail' }, 'Job time and queue wait are read from fixed time buckets, so they are shown as upper bounds (“≤”).'));
  }

  function statementCard() {
    return el('section', { class: 'notice info usage-statement', 'aria-label': 'What is recorded' },
      el('h3', { text: 'What is and is not recorded' }),
      el('p', null,
        'For each successful request to /assembly, /contigs, /profile or /query the server adds to counters kept per ten-minute window, endpoint and kind of client (web page, sylphd command line with its version, or other): the number of requests, samples and contigs, the bases and sketch bytes reported, and a coarse histogram of job time and queue wait. ',
        'It never stores IP addresses (or hashes of them), counts of unique visitors, file, sample or contig names, results, taxa or settings, and nothing per user. Failed or rejected requests and the status, health and usage pages are not counted. ',
        'This describes the sylphd software; the server tells the page that it keeps these statistics and is not verified by it. See the ',
        el('a', { href: 'terms.html', text: 'terms of use' }), '.'));
  }

  async function show() {
    if (!host) host = document.getElementById('mode-usage');
    if (!host) return;
    const mine = (token += 1);
    const info = S.info();
    if (!info) {
      const problem = S.problem();
      host.replaceChildren(problem
        ? message('error', 'Server offline', 'The usage statistics come from the server, and it cannot be reached right now. The server status is shown at the top of the page.')
        : message('info', 'Checking the server…', 'The usage statistics will appear when the server answers.'));
      return;
    }
    if (!S.usage()) {
      host.replaceChildren(message('info', 'Usage statistics are off', 'This server does not keep usage statistics: nothing about your requests is counted.'));
      return;
    }
    // the page refreshes /info every minute: keep showing the numbers while they reload
    if (!host.querySelector('.usage-ready')) host.replaceChildren(message('info', 'Loading the usage statistics…'));
    try {
      const res = await fetch(`${S.api}/usage`, { signal: S.timeoutSignal(15000), cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (mine !== token) return;
      if (!data || !data.totals || !data.windows || !Array.isArray(data.windows.rows)) throw new Error('unexpected response');
      host.replaceChildren(el('div', { class: 'usage-view usage-ready' }, summaryCard(data), chartCard(data), breakdownCard(data), statementCard(), backLink()));
    } catch (e) {
      if (mine !== token) return;
      host.replaceChildren(message('error', 'Could not load the usage statistics', `GET ${S.api}/usage failed: ${e.message || e}.`));
    }
  }

  S.onMode((mode) => { if (mode === 'usage') show(); });
  S.onInfo(() => { if (S.mode() === 'usage') show(); });
  if (S.mode() === 'usage') show();
})();
