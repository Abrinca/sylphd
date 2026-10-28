// Decompression-only throughput of WasmGunzip under node (compare with the
// browser numbers in the README):  node tests/bench_gunzip.mjs FILE.gz [--no-verify]
import { readFileSync, createReadStream } from "node:fs";
import { createGunzip } from "node:zlib";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { initSync, WasmGunzip } from "../demo/pkg/sylph_sketch.js";

const here = path.dirname(fileURLToPath(import.meta.url));
initSync({ module: readFileSync(path.join(here, "../demo/pkg/sylph_sketch_bg.wasm")) });
const file = process.argv[2];
const verify = !process.argv.includes("--no-verify");

async function run(name, fn) {
  const t = performance.now();
  const [inBytes, outBytes] = await fn();
  const s = (performance.now() - t) / 1000;
  console.log(`${name}: ${s.toFixed(2)} s, ${(inBytes / 1e6 / s).toFixed(0)} MB/s compressed, ${(outBytes / 1e6 / s).toFixed(0)} MB/s decompressed`);
}

await run(`wasm zlib-rs (verify crc=${verify})`, async () => {
  const gz = new WasmGunzip(2 << 20, verify);
  let inBytes = 0, outBytes = 0;
  for await (const chunk of createReadStream(file, { highWaterMark: 1 << 20 })) {
    inBytes += chunk.length;
    gz.push(chunk);
    let out;
    while ((out = gz.pull()) !== undefined) outBytes += out.length;
  }
  outBytes += gz.finish().length;
  return [inBytes, outBytes];
});

await run("node zlib (native)", async () => {
  let inBytes = 0, outBytes = 0;
  const src = createReadStream(file, { highWaterMark: 1 << 20 });
  src.on("data", (b) => (inBytes += b.length));
  const gunzip = createGunzip();
  src.pipe(gunzip);
  for await (const chunk of gunzip) outBytes += chunk.length;
  return [inBytes, outBytes];
});
