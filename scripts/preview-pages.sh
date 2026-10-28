#!/usr/bin/env bash
# Preview the GitHub Pages site locally: the web app at /, the docs at /docs/.
#
# Assembles the site exactly like .github/workflows/pages.yml (web/ at the
# root, the zensical build under docs/) into ./public and serves it.
#
#   scripts/preview-pages.sh            # http://127.0.0.1:8000/
#   scripts/preview-pages.sh 8080       # another port
#
# The "Reads" mode needs the WebAssembly sketcher, built from sketch-wasm/ into
# web/wasm/ (gitignored; needs cargo, the wasm32-unknown-unknown target and
# wasm-bindgen-cli, see sketch-wasm/README.md). SKIP_WASM=1 skips that build;
# without the module the page still works but the Reads mode says so.
#
# The page uses the local API (devApiBase in web/config.js) when opened from
# localhost; start a server with `--cors-origin local`, or add ?api=... .
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
port=${1:-8000}

uvx zensical build --clean --strict
if [ -z "${SKIP_WASM:-}" ]; then
  if command -v cargo >/dev/null; then
    sketch-wasm/build.sh ../web/wasm
  else
    echo "warning: cargo not found, building without the Reads mode's wasm module" >&2
  fi
fi
rm -rf public
mkdir -p public
cp -r web/. public/
cp -r site public/docs

echo "serving http://127.0.0.1:$port/ (docs at /docs/); Ctrl-C to stop"
exec python3 -m http.server "$port" --bind 127.0.0.1 -d public
