// Sketches every record of a FASTA/FASTQ file (plain or gzip) with the built
// wasm module's per-contig API and prints the records as JSON, for the Python
// parity test (tests/test_web_contigs_js.py).
//
//   node sketch-wasm/tests/contigs_check.mjs FILE C K MIN_LENGTH CHUNK plain|gz [scalar]
//
// Output: {"records":[{index,name,length,gcCount,contigKmers,sketch}],...}
// where sketch is base64 or null, plus counters that check the streaming API
// (records emitted before end_file, queued records after draining).
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { initSync, WasmContigSketcher } from "../../web/wasm/sylph_sketch.js";

const here = path.dirname(fileURLToPath(import.meta.url));
initSync({ module: readFileSync(path.join(here, "../../web/wasm/sylph_sketch_bg.wasm")) });

const [file, c, k, minLength, chunk, mode = "plain", scalar = "0"] = process.argv.slice(2);
const data = readFileSync(file);
const sketcher = new WasmContigSketcher(
  Number(c), Number(k), Number(minLength), undefined, scalar === "1",
);

const records = [];
const take = () => {
  for (const record of sketcher.drain()) {
    records.push({
      index: record.index,
      name: record.name,
      length: record.length,
      gcCount: record.gcCount,
      contigKmers: record.contigKmers,
      sketch: record.sketch ? Buffer.from(record.sketch).toString("base64") : null,
    });
  }
};

const size = Number(chunk);
let beforeEnd = 0;
if (mode === "gz") {
  for (let i = 0; i < data.length; i += size) {
    sketcher.push_gz(data.subarray(i, i + size));
    take();
  }
} else {
  const text = data.toString("latin1");
  for (let i = 0; i < text.length; i += size) {
    sketcher.push(Buffer.from(text.slice(i, i + size), "latin1"));
    take();
  }
}
beforeEnd = records.length;
sketcher.end_file();
take();
const result = {
  records,
  beforeEnd,
  seen: sketcher.seen(),
  sketched: sketcher.sketched(),
  skipped: sketcher.skipped(),
  bases: sketcher.bases(),
  pending: sketcher.pending(),
};
sketcher.free();
console.log(JSON.stringify(result));
