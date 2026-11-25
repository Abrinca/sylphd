// Exercises the per-contig planner, contig naming and `.sylsp` header exports of
// the built wasm module, for tests/test_web_contigs_js.py and tests/test_web_sylsp_js.py.
//
//   node rust/sylphd-wasm/tests/core_check.mjs < payload.json
//
// stdin (any subset of): {"names": [raw id, ...],
//                         "plan": {"contigs": [...], "limits": {...}, "request": {...}},
//                         "sylsp": [path, ...],
//                         "compat": [[c, k, db_c, db_k], ...]}
// stdout: {"names": [...], "plan": [...] | {"error": ...}, "sylsp": [header | {"error": ...}],
//          "compat": [text | null, ...]}
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import {
  initSync, plan_contig_requests, WasmContigNames, sylsp_tail_start, sylsp_header,
  sketch_incompatibility,
} from "../../../web/wasm/sylph_sketch.js";

const here = path.dirname(fileURLToPath(import.meta.url));
initSync({ module: readFileSync(path.join(here, "../../../web/wasm/sylph_sketch_bg.wasm")) });

const payload = JSON.parse(readFileSync(0, "utf8"));
const attempt = (f) => {
  try {
    return f();
  } catch (e) {
    return { error: String(e) };
  }
};
const out = {};

if (payload.names) {
  const tracker = new WasmContigNames();
  out.names = payload.names.map((raw, i) => tracker.next_name(raw, i + 1));
}
if (payload.plan) {
  const { contigs, limits, request } = payload.plan;
  out.plan = attempt(() => JSON.parse(
    plan_contig_requests(JSON.stringify(contigs), JSON.stringify(limits), JSON.stringify(request)),
  ));
}
if (payload.sylsp) {
  // The same two reads the page makes: the first 8 bytes, then the tail.
  out.sylsp = payload.sylsp.map((file) => attempt(() => {
    const bytes = readFileSync(file);
    const [count, start] = sylsp_tail_start(bytes.subarray(0, 8), bytes.length);
    const tail = bytes.subarray(start, Math.min(bytes.length, start + 4096));
    return JSON.parse(sylsp_header(tail, count, start));
  }));
}
if (payload.compat) {
  out.compat = payload.compat.map(([c, k, dbC, dbK]) => sketch_incompatibility(c, k, dbC, dbK) ?? null);
}
console.log(JSON.stringify(out));
