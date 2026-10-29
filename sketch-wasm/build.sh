#!/usr/bin/env bash
# Builds the wasm module and the JS glue (ES module, --target web).
#
#   ./build.sh              # -> demo/pkg/   (the spike demo)
#   ./build.sh ../web/wasm  # -> any other directory (the web page; this is what
#                           #    scripts/preview-pages.sh and the Pages workflow do)
#
# Needs: rustc with the wasm32-unknown-unknown target, and wasm-bindgen-cli at
# the same version as the `wasm-bindgen` crate (see Cargo.toml):
#   cargo install wasm-bindgen-cli --version 0.2.129
# wasm-bindgen is looked up on PATH, then in ~/.cargo/bin (or set WASM_BINDGEN).
# wasm-opt (binaryen) is used if found on PATH, or set WASM_OPT=/path/to/wasm-opt.
set -euo pipefail
cd "$(dirname "$0")"
OUT_DIR="${1:-demo/pkg}"
WASM_BINDGEN="${WASM_BINDGEN:-$(command -v wasm-bindgen || echo "$HOME/.cargo/bin/wasm-bindgen")}"
cargo build --release --target wasm32-unknown-unknown --locked
"$WASM_BINDGEN" --target web --out-dir "$OUT_DIR" --out-name sylph_sketch \
  target/wasm32-unknown-unknown/release/sylph_sketch_wasm.wasm
WASM_OPT="${WASM_OPT:-$(command -v wasm-opt || true)}"
if [ -n "$WASM_OPT" ]; then
  "$WASM_OPT" -O3 --enable-bulk-memory --enable-nontrapping-float-to-int --enable-sign-ext --enable-mutable-globals \
    "$OUT_DIR/sylph_sketch_bg.wasm" -o "$OUT_DIR/sylph_sketch_bg.wasm"
fi
ls -l "$OUT_DIR/sylph_sketch_bg.wasm"
