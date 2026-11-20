// The operator's messages (GET /messages) as a compact area under the server status bar.
//
// Each message is {level: 'info'|'warning'|'danger', title, message (Markdown)}. The text is
// untrusted (the page can be pointed at any server with ?api=) and is rendered by SylphdAbout, the
// same sanitising renderer as the about text. info and warning can be dismissed; the dismissal is
// kept in localStorage under a hash of level + title + message, so a changed text shows again.
// danger cannot be dismissed. Exposes window.SylphdMessages = { hash, clean, create }.
(() => {
  'use strict';

  const LEVELS = ['info', 'warning', 'danger'];
  const STORE_KEY = 'sylphd.messages.dismissed';
  const MAX_KEPT = 200; // remembered dismissals; the oldest are forgotten first
  const MAX_SHOWN = 20;

  // cyrb53: a small synchronous 53-bit string hash (crypto.subtle is async and needs https).
  function hash(message) {
    const text = `${message.level}\u0000${message.title}\u0000${message.message}`;
    let h1 = 0xdeadbeef;
    let h2 = 0x41c6ce57;
    for (let i = 0; i < text.length; i++) {
      const ch = text.charCodeAt(i);
      h1 = Math.imul(h1 ^ ch, 2654435761);
      h2 = Math.imul(h2 ^ ch, 1597334677);
    }
    h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
    h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
    return (4294967296 * (2097151 & h2) + (h1 >>> 0)).toString(16);
  }

  // Keep only well-formed messages from a response body; anything else is ignored.
  function clean(body) {
    const list = body && Array.isArray(body.messages) ? body.messages : [];
    return list.filter((m) => m && typeof m === 'object' && LEVELS.includes(m.level)
      && typeof m.title === 'string' && typeof m.message === 'string').slice(0, MAX_SHOWN);
  }

  function create(container, { render, storage } = {}) {
    let memory = [];
    const load = () => {
      try {
        const v = JSON.parse((storage || localStorage).getItem(STORE_KEY) || '[]');
        return Array.isArray(v) ? v.filter((x) => typeof x === 'string') : [];
      } catch { return memory; }
    };
    const save = (list) => {
      memory = list;
      try { (storage || localStorage).setItem(STORE_KEY, JSON.stringify(list)); } catch { /* private mode */ }
    };
    let current = [];

    function dismiss(id) {
      const kept = load().filter((x) => x !== id);
      kept.push(id);
      save(kept.slice(-MAX_KEPT));
      draw();
    }

    function item(m) {
      const id = hash(m);
      const node = document.createElement('div');
      node.className = `msg msg-${m.level}`;
      node.setAttribute('role', m.level === 'danger' ? 'alert' : 'status');
      const text = document.createElement('div');
      text.className = 'msg-text';
      const title = document.createElement('strong');
      title.className = 'msg-title';
      title.textContent = m.title;
      const body = document.createElement('div');
      body.className = 'msg-body';
      if (render) body.append(render(m.message)); else body.textContent = m.message;
      text.append(title, ' ', body);
      node.append(text);
      if (m.level !== 'danger') {
        const x = document.createElement('button');
        x.type = 'button';
        x.className = 'msg-close';
        x.textContent = '×';
        x.setAttribute('aria-label', `Dismiss: ${m.title}`);
        x.title = 'Dismiss';
        x.addEventListener('click', () => dismiss(id));
        node.append(x);
      }
      return node;
    }

    function draw() {
      const gone = new Set(load());
      const shown = current.filter((m) => m.level === 'danger' || !gone.has(hash(m)));
      container.replaceChildren(...shown.map(item));
      container.hidden = shown.length === 0;
    }

    return {
      show(messages) { current = messages; draw(); },
      clear() { current = []; draw(); },
    };
  }

  window.SylphdMessages = { hash, clean, create };
})();
