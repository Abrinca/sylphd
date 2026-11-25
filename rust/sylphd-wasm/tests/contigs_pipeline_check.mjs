// Runs `SylphdSketch.sketchContigs` (web/sketching.js) end to end under node:
// the two real workers (web/decompress-worker.js, web/sketch-worker.js) run on
// node:worker_threads instead of Web Workers, and `fetch` is shimmed for the
// local wasm file. Prints the delivered contigs and the done stats as JSON.
//
//   node sketch-wasm/tests/contigs_pipeline_check.mjs FILE C K MIN_LENGTH
//
// Used by tests/test_web_contigs_js.py; it checks the worker protocol
// (batching, ack/backpressure, gzip in the decompress worker) as well as the
// per-record output.
import { Worker as NodeWorker } from "node:worker_threads";
import { readFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const web = path.resolve(here, "../../web");
const wasm = path.join(web, "wasm/sylph_sketch_bg.wasm");

// A Worker that executes the real worker module in a node worker thread.
// `self.postMessage` is bridged to parentPort by tests/worker_shim.mjs.
class BrowserLikeWorker {
  constructor(url) {
    this.worker = new NodeWorker(path.join(here, "worker_shim.mjs"), {
      workerData: { url: pathToFileURL(path.resolve(web, url)).href },
    });
    this.onmessage = null;
    this.onerror = null;
    this.worker.on("message", (data) => this.onmessage && this.onmessage({ data }));
    this.worker.on("error", (error) => this.onerror && this.onerror(error));
  }
  postMessage(data, transfer) {
    this.worker.postMessage(data, transfer);
  }
  terminate() {
    void this.worker.terminate();
  }
}

const wasmBytes = readFileSync(wasm);
const realFetch = globalThis.fetch;
globalThis.fetch = async (url) => (
  String(url).includes("sylph_sketch_bg.wasm")
    ? new Response(wasmBytes, { headers: { "content-type": "application/wasm" } })
    : realFetch(url)
);

globalThis.Worker = BrowserLikeWorker;
globalThis.window = globalThis;
globalThis.self = globalThis;

const [, , file, c, k, minLength] = process.argv;
await import(pathToFileURL(path.join(web, "sketching.js")).href);

const records = [];
const fileObj = new File([readFileSync(file)], path.basename(file));
const run = globalThis.SylphdSketch.sketchContigs(
  fileObj,
  { c: Number(c), k: Number(k), minLength: Number(minLength) },
  (contig) => {
    records.push({
      index: contig.index,
      name: contig.name,
      headerName: contig.headerName,
      length: contig.length,
      gcCount: contig.gcCount,
      gc: contig.gc,
      contigKmers: contig.contigKmers,
      sketch: contig.sketch ? Buffer.from(contig.sketch).toString("base64") : null,
    });
  },
);
const stats = await run.done;
console.log(JSON.stringify({ records, stats }));
