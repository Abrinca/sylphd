#!/usr/bin/env bash
# Builds the wasm module and the JS glue into demo/pkg (ES module, --target web).
# Needs: rustc with the wasm32-unknown-unknown target, and wasm-bindgen-cli at
# the same version as the `wasm-bindgen` crate (see Cargo.toml):
#   cargo install wasm-bindgen-cli --version 0.2.129
# wasm-opt (binaryen) is used if found on PATH, or set WASM_OPT=/path/to/wasm-opt.
set -euo pipefail
cd "$(dirname "$0")"
WASM_BINDGEN="${WASM_BINDGEN:-$HOME/.cargo/bin/wasm-bindgen}"
cargo build --release --target wasm32-unknown-unknown
"$WASM_BINDGEN" --target web --out-dir demo/pkg --out-name sylph_sketch \
  target/wasm32-unknown-unknown/release/sylph_sketch_wasm.wasm
WASM_OPT="${WASM_OPT:-$(command -v wasm-opt || true)}"
if [ -n "$WASM_OPT" ]; then
  "$WASM_OPT" -O3 --enable-bulk-memory --enable-nontrapping-float-to-int --enable-sign-ext --enable-mutable-globals \
    demo/pkg/sylph_sketch_bg.wasm -o demo/pkg/sylph_sketch_bg.wasm
fi
ls -l demo/pkg/sylph_sketch_bg.wasm
