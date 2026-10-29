// A small, safe Markdown renderer for the text a server operator publishes about their instance
// (GET /info: about.text). No dependencies. The page can be pointed at any server (?api=), so the
// text is untrusted: the output is built with createElement / createTextNode only (never innerHTML),
// HTML in the text is shown as plain text, and only http:, https: and mailto: links become links.
//
// Supported: paragraphs (a single line break becomes <br>), # headings (rendered as h3-h5),
// **bold**, *italic* and _italic_, `code`, [text](url), bare http(s):// links, - * + lists,
// 1. ordered lists, > blockquotes, backslash escapes. Images are shown as text. Everything else is text.
//
// Bounded work: the input is cut at MAX_CHARS, inline nesting and block nesting are limited, link text
// and URLs have a maximum length, and a shared step budget turns the rest of a pathological input into
// plain text instead of letting it run long. Also loadable from node (tests/test_web_markdown_js.py).
(function (root) {
  'use strict';

  const MAX_CHARS = 16 * 1024;
  const MAX_INLINE_DEPTH = 4;
  const MAX_BLOCK_DEPTH = 3;
  const MAX_LABEL = 1000;
  const MAX_URL = 2000;
  const WORK_BUDGET = 400000;
  const LINK_REL = 'noopener noreferrer nofollow ugc';

  // The href for a link target, or null when it is not an http, https or mailto URL. Whitespace and
  // control characters are refused outright (browsers strip some of them, so "java\tscript:" must not pass).
  function safeUrl(raw) {
    const url = String(raw == null ? '' : raw).trim();
    if (!url || url.length > MAX_URL) return null;
    // eslint-disable-next-line no-control-regex
    if (/[\u0000-\u0020\u007f-\u009f\u2028\u2029\\]/.test(url)) return null;
    if (!/^(https?:\/\/|mailto:)/i.test(url)) return null;
    let parsed;
    try { parsed = new URL(url); } catch { return null; }
    if (!['http:', 'https:', 'mailto:'].includes(parsed.protocol)) return null;
    if (parsed.protocol !== 'mailto:' && !parsed.hostname) return null;
    if (parsed.protocol === 'mailto:' && !parsed.pathname) return null;
    return parsed.href;
  }

  const isSpace = (ch) => ch === ' ' || ch === '\t' || ch === '\n' || ch === '\r' || ch === '' || ch === undefined;
  const isAlnum = (ch) => /^[\p{L}\p{N}]$/u.test(ch || '');
  const PUNCT = '!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~';

  function makeCtx(doc) {
    return { doc, work: WORK_BUDGET };
  }
  const spend = (ctx, n) => { ctx.work -= n; return ctx.work >= 0; };

  function el(ctx, tag, children, attrs) {
    const node = ctx.doc.createElement(tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    for (const child of children || []) node.appendChild(child);
    return node;
  }
  const text = (ctx, s) => ctx.doc.createTextNode(s);

  /* ------------------------------------------------------------- inline */

  // Index of the `]` that closes the `[` at `open`, or -1 (bounded, nesting counted).
  function findBracketEnd(ctx, s, open) {
    let depth = 0;
    const stop = Math.min(s.length, open + MAX_LABEL + 2);
    for (let i = open; i < stop; i++) {
      const ch = s[i];
      if (ch === '\\') { i++; continue; }
      if (ch === '[') depth++;
      else if (ch === ']') { depth--; if (depth === 0) return i; }
    }
    spend(ctx, stop - open);
    return -1;
  }

  // `[label](url)` at `open`: { label, url, end } (end is the index after `)`) or null.
  function matchLink(ctx, s, open) {
    const close = findBracketEnd(ctx, s, open);
    if (close < 0 || s[close + 1] !== '(') return null;
    let depth = 1;
    let i = close + 2;
    const stop = Math.min(s.length, i + MAX_URL + 2);
    for (; i < stop; i++) {
      const ch = s[i];
      if (ch === '\\') { i++; continue; }
      if (ch === '(') depth++;
      else if (ch === ')') { depth--; if (depth === 0) break; }
      else if (ch === '\n') return null;
    }
    spend(ctx, i - close);
    if (i >= stop || depth !== 0) return null;
    let target = s.slice(close + 2, i).trim();
    // an optional "title" after the URL is ignored
    const sp = target.search(/\s/);
    if (sp > 0) target = target.slice(0, sp);
    if (target.startsWith('<') && target.endsWith('>')) target = target.slice(1, -1);
    return { label: s.slice(open + 1, close), url: target, end: i + 1 };
  }

  // Closing run of the emphasis delimiter `d` (length 1-3) after `from`: its index or -1.
  function findEmphasisEnd(ctx, s, from, d) {
    const ch = d[0];
    const stop = Math.min(s.length, from + 2000);
    let i = from;
    while (i < stop) {
      if (!spend(ctx, 1)) return -1;
      const c = s[i];
      if (c === '\\') { i += 2; continue; }
      if (c === '`') { // skip code spans
        let run = i;
        while (s[run] === '`') run++;
        const end = findFenceEnd(ctx, s, run, run - i);
        i = end < 0 || end > stop ? run : end + (run - i);
        continue;
      }
      if (c === ch) {
        let run = i;
        while (s[run] === ch) run++;
        const n = run - i;
        const before = s[i - 1];
        const after = s[run];
        const closes = n === d.length && !isSpace(before) && !(ch === '_' && isAlnum(after));
        if (closes && i > from) return i;
        i = run;
        continue;
      }
      i++;
    }
    return -1;
  }

  // Start of the next run of exactly `len` backticks at or after `from`, or -1.
  function findFenceEnd(ctx, s, from, len) {
    let j = s.indexOf('`', from);
    while (j >= 0) {
      let r = j;
      while (s[r] === '`') r++;
      if (r - j === len) return j;
      if (!spend(ctx, r - j + 1)) return -1;
      j = s.indexOf('`', r);
    }
    return -1;
  }

  function parseInline(ctx, s, depth) {
    const out = [];
    let buf = '';
    const flush = () => { if (buf) { out.push(text(ctx, buf)); buf = ''; } };
    if (depth > MAX_INLINE_DEPTH) return [text(ctx, s)];
    const failedCode = new Set(); // fence lengths that have no closing fence further on
    let i = 0;
    while (i < s.length) {
      if (!spend(ctx, 1)) { buf += s.slice(i); break; }
      const ch = s[i];

      if (ch === '\\' && i + 1 < s.length && PUNCT.includes(s[i + 1])) {
        buf += s[i + 1]; i += 2; continue;
      }

      if (ch === '`') {
        let run = i;
        while (s[run] === '`') run++;
        const fence = s.slice(i, run);
        const close = failedCode.has(fence.length) ? -1 : findFenceEnd(ctx, s, run, fence.length);
        if (close < 0) { failedCode.add(fence.length); buf += fence; i = run; continue; }
        flush();
        let code = s.slice(run, close).replace(/\n/g, ' ');
        if (code.length > 2 && code.startsWith(' ') && code.endsWith(' ') && code.trim()) code = code.slice(1, -1);
        out.push(el(ctx, 'code', [text(ctx, code)]));
        i = close + fence.length;
        continue;
      }

      if (ch === '!' && s[i + 1] === '[') {
        // images are not supported: show the source as text
        const m = matchLink(ctx, s, i + 1);
        if (m) { buf += s.slice(i, m.end); i = m.end; } else { buf += '!'; i++; }
        continue;
      }

      if (ch === '[') {
        const m = matchLink(ctx, s, i);
        if (m) {
          const href = safeUrl(m.url);
          if (href) {
            flush();
            out.push(el(ctx, 'a', parseInline(ctx, m.label, depth + 1), { href, target: '_blank', rel: LINK_REL }));
          } else {
            buf += s.slice(i, m.end); // unsafe or relative target: inert text
          }
          i = m.end;
          continue;
        }
        buf += ch; i++; continue;
      }

      if ((ch === '*' || ch === '_') && depth < MAX_INLINE_DEPTH) {
        let run = i;
        while (s[run] === ch) run++;
        const n = Math.min(run - i, 3);
        const before = s[i - 1];
        const after = s[run];
        const opens = !isSpace(after) && !(ch === '_' && isAlnum(before)) && run - i <= 3;
        if (opens) {
          const delim = ch.repeat(n);
          const end = findEmphasisEnd(ctx, s, run, delim);
          if (end >= 0) {
            flush();
            let inner = parseInline(ctx, s.slice(run, end), depth + 1);
            if (n >= 2) inner = [el(ctx, 'strong', n === 3 ? [el(ctx, 'em', inner)] : inner)];
            else inner = [el(ctx, 'em', inner)];
            out.push(...inner);
            i = end + n;
            continue;
          }
        }
        buf += s.slice(i, run); i = run; continue;
      }

      if (ch === 'h' && (s.startsWith('http://', i) || s.startsWith('https://', i)) && !isAlnum(s[i - 1])) {
        let end = i;
        while (end < s.length && !/[\s<>"']/.test(s[end])) end++;
        let url = s.slice(i, end);
        while (/[.,;:!?)\]}*_]$/.test(url) && !(url.endsWith(')') && url.includes('('))) url = url.slice(0, -1);
        const href = safeUrl(url);
        if (href) {
          flush();
          out.push(el(ctx, 'a', [text(ctx, url)], { href, target: '_blank', rel: LINK_REL }));
          i += url.length;
          continue;
        }
      }

      if (ch === '\n') { buf += ''; flush(); out.push(el(ctx, 'br', [])); i++; continue; }
      buf += ch; i++;
    }
    flush();
    return out;
  }

  /* -------------------------------------------------------------- blocks */

  const RE_HEADING = /^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$/;
  const RE_BULLET = /^ {0,3}[-*+][ \t]+(.*)$/;
  const RE_ORDERED = /^ {0,3}(\d{1,9})[.)][ \t]+(.*)$/;
  const RE_QUOTE = /^ {0,3}>[ \t]?(.*)$/;
  const RE_BLANK = /^[ \t]*$/;
  const RE_RULE = /^ {0,3}([-*_])([ \t]*\1){2,}[ \t]*$/;

  function parseBlocks(ctx, lines, depth) {
    const out = [];
    let i = 0;
    while (i < lines.length) {
      if (!spend(ctx, 1)) {
        out.push(el(ctx, 'p', [text(ctx, lines.slice(i).join('\n'))]));
        break;
      }
      const line = lines[i];
      if (RE_BLANK.test(line)) { i++; continue; }

      let m = RE_HEADING.exec(line);
      if (m) {
        const level = m[1].length <= 2 ? 3 : m[1].length <= 4 ? 4 : 5; // #, ## -> h3; ###, #### -> h4; deeper -> h5
        out.push(el(ctx, `h${level}`, parseInline(ctx, m[2], 0)));
        i++;
        continue;
      }
      if (RE_RULE.test(line) && !RE_BULLET.test(line)) { i++; continue; }

      if (RE_QUOTE.test(line)) {
        const inner = [];
        while (i < lines.length && RE_QUOTE.test(lines[i])) { inner.push(RE_QUOTE.exec(lines[i])[1]); i++; }
        out.push(el(ctx, 'blockquote', depth >= MAX_BLOCK_DEPTH
          ? [el(ctx, 'p', [text(ctx, inner.join('\n'))])]
          : parseBlocks(ctx, inner, depth + 1)));
        continue;
      }

      const bullet = RE_BULLET.exec(line);
      const ordered = bullet ? null : RE_ORDERED.exec(line);
      if (bullet || ordered) {
        const re = bullet ? RE_BULLET : RE_ORDERED;
        const items = [];
        while (i < lines.length) {
          const mm = re.exec(lines[i]);
          if (!mm) break;
          const parts = [mm[mm.length - 1]];
          i++;
          // indented lines continue the item
          while (i < lines.length && /^ {2,}\S/.test(lines[i]) && !re.test(lines[i])) { parts.push(lines[i].trim()); i++; }
          items.push(parts.join('\n'));
          while (i < lines.length && RE_BLANK.test(lines[i]) && i + 1 < lines.length && re.test(lines[i + 1])) i++;
        }
        const attrs = ordered && Number(ordered[1]) > 1 && Number(ordered[1]) < 1e6 ? { start: String(Number(ordered[1])) } : null;
        out.push(el(ctx, bullet ? 'ul' : 'ol', items.map((it) => el(ctx, 'li', parseInline(ctx, it, 0))), attrs));
        continue;
      }

      // paragraph: consecutive lines up to a blank line or the start of another block
      const para = [];
      while (i < lines.length && !RE_BLANK.test(lines[i])) {
        if (para.length && (RE_HEADING.test(lines[i]) || RE_QUOTE.test(lines[i]) || RE_BULLET.test(lines[i]) || RE_ORDERED.test(lines[i]))) break;
        para.push(lines[i].replace(/^[ \t]+/, '').replace(/[ \t]+$/, ''));
        i++;
      }
      out.push(el(ctx, 'p', parseInline(ctx, para.join('\n'), 0)));
    }
    return out;
  }

  // Markdown text -> DocumentFragment (built with `doc`, default: the page's document).
  function render(markdown, doc) {
    const d = doc || (typeof document !== 'undefined' ? document : null);
    const ctx = makeCtx(d);
    let src = String(markdown == null ? '' : markdown);
    let cut = false;
    if (src.length > MAX_CHARS) { src = src.slice(0, MAX_CHARS); cut = true; }
    const lines = src.replace(/\r\n?/g, '\n').split('\n');
    const frag = d.createDocumentFragment();
    for (const node of parseBlocks(ctx, lines, 0)) frag.appendChild(node);
    if (cut) frag.appendChild(el(ctx, 'p', [text(ctx, '…')]));
    return frag;
  }

  const api = { render, safeUrl, MAX_CHARS };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.SylphdMarkdown = api;
})(typeof self !== 'undefined' ? self : this);
