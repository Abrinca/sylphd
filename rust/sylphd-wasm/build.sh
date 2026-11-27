#!/usr/bin/env bash
# Builds the wasm module and the JS glue (ES module, --target web).
#
#   ./build.sh              # -> ../../web/wasm (the web page; scripts/preview-pages.sh and the
#                           #    Pages workflow build into web/wasm)
#   ./build.sh some/dir     # -> any other directory (relative to caller's cwd or absolute)
#
# Needs: rustc with the wasm32-unknown-unknown target, and wasm-bindgen-cli at
# the same version as the `wasm-bindgen` crate (see ../Cargo.toml):
#   cargo install wasm-bindgen-cli --version 0.2.129
# wasm-bindgen is looked up on PATH, then in ~/.cargo/bin (or set WASM_BINDGEN).
# wasm-opt (binaryen) is used if found on PATH, or set WASM_OPT=/path/to/wasm-opt.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ "${1:-}" != "" ]; then
  case "$1" in
    /*) OUT_DIR="$1" ;;
    *) OUT_DIR="$(pwd)/$1" ;;
  esac
else
  OUT_DIR="$SCRIPT_DIR/../../web/wasm"
fi
cd "$SCRIPT_DIR/.."
WASM_BINDGEN="${WASM_BINDGEN:-$(command -v wasm-bindgen || echo "$HOME/.cargo/bin/wasm-bindgen")}"
cargo build --release --target wasm32-unknown-unknown --locked -p sylphd-wasm
"$WASM_BINDGEN" --target web --out-dir "$OUT_DIR" --out-name sylph_sketch \
  target/wasm32-unknown-unknown/release/sylphd_wasm.wasm
WASM_OPT="${WASM_OPT:-$(command -v wasm-opt || true)}"
if [ -n "$WASM_OPT" ]; then
  # The features rustc emits for wasm32 (SIMD from ../.cargo/config.toml); wasm-opt rejects the module without them.
  "$WASM_OPT" -O3 --enable-simd --enable-bulk-memory --enable-nontrapping-float-to-int --enable-sign-ext \
    --enable-mutable-globals --enable-reference-types --enable-multivalue \
    "$OUT_DIR/sylph_sketch_bg.wasm" -o "$OUT_DIR/sylph_sketch_bg.wasm"
fi
ls -l "$OUT_DIR/sylph_sketch_bg.wasm"
