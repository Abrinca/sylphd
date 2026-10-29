// The server operator's text (GET /info: about.text) as a safe DocumentFragment.
//
// The page can be pointed at any server with ?api=, so the text is untrusted: marked renders the
// Markdown and DOMPurify keeps only the allowlisted tags and http, https and mailto links. Anything
// else (raw HTML, images, tables) is dropped or kept as text. index.html loads both libraries
// before this file. Exposes window.SylphdAbout = { render }.
(() => {
  'use strict';

  const MAX_CHARS = 16 * 1024;
  const CONFIG = {
    ALLOWED_TAGS: ['p', 'br', 'strong', 'em', 'code', 'pre', 'ul', 'ol', 'li', 'a', 'blockquote',
      'h1', 'h2', 'h3', 'h4', 'h5', 'h6'],
    ALLOWED_ATTR: ['href', 'title'],
    ALLOWED_URI_REGEXP: /^(?:https?:|mailto:)/i,
    RETURN_DOM_FRAGMENT: true,
  };

  window.DOMPurify?.addHook('afterSanitizeAttributes', (node) => {
    if (node.tagName === 'A' && node.hasAttribute('href')) {
      node.setAttribute('target', '_blank');
      node.setAttribute('rel', 'noopener noreferrer nofollow ugc');
    }
  });

  function render(markdown) {
    const src = String(markdown ?? '').slice(0, MAX_CHARS);
    const { marked, DOMPurify } = window;
    if (!marked || !DOMPurify) {
      const frag = document.createDocumentFragment();
      const p = document.createElement('p');
      p.textContent = src;
      frag.append(p);
      return frag;
    }
    return DOMPurify.sanitize(marked.parse(src, { gfm: true, breaks: false }), CONFIG);
  }

  window.SylphdAbout = { render };
})();
