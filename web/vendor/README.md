# Vendored libraries

Unmodified copies, loaded by `web/index.html` (`defer`, before `web/about.js`) to render the server
operator's Markdown safely. No CDN is used at runtime.

| File | Library | Version | License | Source | SHA-256 | Size |
|---|---|---|---|---|---|---|
| `marked.umd.js` | [marked](https://github.com/markedjs/marked) | 18.1.2 | MIT (`LICENSE-marked`) | https://cdn.jsdelivr.net/npm/marked@18.1.2/lib/marked.umd.js | `9eb730d2362ae1a8c6201698f17009e16671be6946d3d06503ddc0575283db7c` | 49,207 B (14.9 kB gzipped) |
| `purify.min.js` | [DOMPurify](https://github.com/cure53/DOMPurify) | 3.4.17 | Apache-2.0 (`LICENSE-dompurify`; the project is dual-licensed Apache-2.0 / MPL-2.0 and we use it under Apache-2.0) | https://cdn.jsdelivr.net/npm/dompurify@3.4.17/dist/purify.min.js | `cd37898f9782adf54820c413191b5691803f0f137c799d6edcfd38523b4c2c31` | 29,828 B (11.8 kB gzipped) |

License texts: `LICENSE-marked` (https://cdn.jsdelivr.net/npm/marked@18.1.2/LICENSE, sha256
`8e3a3f82f59a60958f56ca08f445647c32a4733dc7ca6c2c46f6eb898471ab9c`) and `LICENSE-dompurify`
(https://cdn.jsdelivr.net/npm/dompurify@3.4.17/LICENSE, sha256
`cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`).

## Updating

```bash
cd web/vendor
V_MARKED=18.1.2; V_PURIFY=3.4.17            # new versions: https://www.npmjs.com/package/marked and /dompurify
curl -fLO https://cdn.jsdelivr.net/npm/marked@$V_MARKED/lib/marked.umd.js
curl -fL  https://cdn.jsdelivr.net/npm/marked@$V_MARKED/LICENSE -o LICENSE-marked
curl -fLO https://cdn.jsdelivr.net/npm/dompurify@$V_PURIFY/dist/purify.min.js
curl -fL  https://cdn.jsdelivr.net/npm/dompurify@$V_PURIFY/LICENSE -o LICENSE-dompurify
sha256sum marked.umd.js purify.min.js LICENSE-*      # update the table above
cd ../.. && .venv/bin/python -m pytest tests/test_web_about_browser.py   # XSS payloads and timing, needs node + Chromium
```

Then also update the versions in `docs/project/license.md` and the CHANGELOG. `web/about.js` relies on
`marked.parse` and on DOMPurify's `ALLOWED_TAGS`/`ALLOWED_ATTR`/`ALLOWED_URI_REGEXP`/
`RETURN_DOM_FRAGMENT` options and its `afterSanitizeAttributes` hook.
