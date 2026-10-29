// Runs the built wasm module under node and compares its sketches byte for
// byte with the ones written by the native `cargo test` (target/equality/*.sylsp),
// i.e. checks that wasm32 and x86_64 agree (32-bit FxHash, usize, ...).
//
//   ./build.sh && cargo test --release && node tests/node_check.mjs [--big]
import { readFileSync, createReadStream } from "node:fs";
import { gunzipSync, createGunzip } from "node:zlib";
import { initSync, WasmSketcher, WasmGunzip, WasmCrc } from "../../web/wasm/sylph_sketch.js";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "../..");
initSync({ module: readFileSync(path.join(here, "../../web/wasm/sylph_sketch_bg.wasm")) });

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
  // raw_gz cases go through WasmGunzip (the browser worker's path) instead of
  // sketcher.push_gz, so both gzip entry points are exercised
  const gunzipper = () => {
    // deferred CRC: the browser pipeline checks it in the sketch worker
    const gz = new WasmGunzip(1 << 16, "deferred");
    const crc = new WasmCrc();
    return {
      feed(chunk, push) {
        gz.push(chunk);
        let out;
        while ((out = gz.pull()) !== undefined) { push(out); crc.update(out, gz.take_trailers()); }
      },
      end(push) {
        const rest = gz.finish();
        push(rest);
        crc.update(rest, gz.take_trailers());
        crc.end_file();
      },
    };
  };
  const read = (f) => (c.raw_gz ? readFileSync(abs(f)) : load(f));
  if (c.big && !c.raw_gz) {
    // stream, like the browser does
    await new Promise((resolve, reject) => {
      const gunzip = createGunzip();
      createReadStream(abs(c.files[0])).pipe(gunzip);
      gunzip.on("data", (b) => s.push(b));
      gunzip.on("end", resolve);
      gunzip.on("error", reject);
    });
  } else if (c.big) {
    await new Promise((resolve, reject) => {
      const stream = createReadStream(abs(c.files[0]), { highWaterMark: 1 << 20 });
      const g = gunzipper();
      stream.on("data", (b) => g.feed(b, (o) => s.push(o)));
      stream.on("end", () => { g.end((o) => s.push(o)); resolve(); });
      stream.on("error", reject);
    });
    s.end_file();
  } else if (c.kind === "paired") {
    const [a, b] = c.files.map((f) => chunks(read(f), 5000));
    const [g1, g2] = [gunzipper(), gunzipper()];
    const p1 = (o) => s.push_r1(o), p2 = (o) => s.push_r2(o);
    for (let i = 0; i < Math.max(a.length, b.length); i++) {
      if (c.raw_gz) {
        if (a[i]) g1.feed(a[i], p1);
        if (b[i]) g2.feed(b[i], p2);
      } else {
        if (a[i]) p1(a[i]);
        if (b[i]) p2(b[i]);
      }
    }
    if (c.raw_gz) { g1.end(p1); g2.end(p2); }
  } else {
    for (const f of c.files) {
      const g = c.raw_gz ? gunzipper() : null;
      for (const piece of chunks(read(f), 4093)) {
        if (g) g.feed(piece, (o) => s.push(o)); else s.push(piece);
      }
      if (g) g.end((o) => s.push(o));
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
