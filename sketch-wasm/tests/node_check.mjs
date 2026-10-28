// Runs the built wasm module under node and compares its sketches byte for
// byte with the ones written by the native `cargo test` (target/equality/*.sylsp),
// i.e. checks that wasm32 and x86_64 agree (32-bit FxHash, usize, ...).
//
//   ./build.sh && cargo test --release && node tests/node_check.mjs [--big]
import { readFileSync, createReadStream } from "node:fs";
import { gunzipSync, createGunzip } from "node:zlib";
import { initSync, WasmSketcher } from "../demo/pkg/sylph_sketch.js";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "../..");
initSync({ module: readFileSync(path.join(here, "../demo/pkg/sylph_sketch_bg.wasm")) });

const cases = JSON.parse(readFileSync(path.join(here, "cases.json"), "utf8"));
const abs = (f) => (path.isAbsolute(f) ? f : path.join(root, f));
const load = (f) => { const b = readFileSync(abs(f)); return b[0] === 0x1f && b[1] === 0x8b ? gunzipSync(b) : b; };
let failed = 0;

function chunks(buf, size) {
  const out = [];
  for (let i = 0; i < buf.length; i += size) out.push(buf.subarray(i, i + size));
  return out;
}

for (const c of cases) {
  if (c.big && !process.argv.includes("--big")) continue;
  const expected = readFileSync(path.join(here, "../target/equality", c.name + ".sylsp"));
  const s = new WasmSketcher(c.c ?? 200, c.k ?? 31, c.deduplicate ?? true);
  const t0 = performance.now();
  if (c.big) {
    // stream, like the browser does
    await new Promise((resolve, reject) => {
      const gz = createGunzip();
      createReadStream(abs(c.files[0])).pipe(gz);
      gz.on("data", (b) => s.push(b));
      gz.on("end", resolve);
      gz.on("error", reject);
    });
  } else if (c.kind === "paired") {
    const [a, b] = c.files.map((f) => chunks(load(f), 5000));
    for (let i = 0; i < Math.max(a.length, b.length); i++) {
      if (a[i]) s.push_r1(a[i]);
      if (b[i]) s.push_r2(b[i]);
    }
  } else {
    for (const f of c.files) {
      for (const piece of chunks(load(f), 4093)) s.push(piece);
      s.end_file();
    }
  }
  const got = Buffer.from(s.finish(c.name));
  const ms = performance.now() - t0;
  const same = Buffer.compare(got, expected) === 0;
  if (!same) failed++;
  console.log(`${same ? "OK  " : "FAIL"} ${c.name}: ${got.length} bytes, ${s.reads()} reads, ${s.kmers()} k-mers, ${ms.toFixed(0)} ms`);
}
process.exit(failed ? 1 : 0);
