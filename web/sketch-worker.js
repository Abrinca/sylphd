// Stage 2 of the pipeline: decompressed chunks -> WASM sketcher -> .sylsp bytes.
//
// Receives chunks from one decompress worker (single-end) or two (paired: R1
// and R2). Every processed chunk is acked on the port it came from, which
// gives the decompress worker a credit for the next one.
//
// Paired input: the sketcher queues a mate until its partner arrives, and equal byte chunks hold
// different numbers of records (75 bp vs 150 bp mates). So the mate that is behind in records
// (sketcher.unmatched()) is fed next, which keeps the queue at about one chunk of records.
//
// Messages to the main thread: {type: 'progress'}, {type: 'done', sketch, stats}, {type: 'error'}

import init, { WasmSketcher, WasmCrc } from './wasm/sylph_sketch.js';

const PROGRESS_MS = 150;

self.onmessage = async ({ data }) => {
  try {
    await run(data);
  } catch (error) {
    self.postMessage({ type: 'error', message: String(error && error.message ? error.message : error) });
  }
};

async function run({ module, ports, paired, interleaved = false, c, k, deduplicate, name }) {
  const t0 = performance.now();
  const wasm = await init({ module_or_path: module });
  const sketcher = new WasmSketcher(c, k, deduplicate);

  const queues = ports.map(() => []);
  const crcs = ports.map(() => new WasmCrc());
  const stats = { sketchMs: 0, crcMs: 0, idleMs: 0, chunks: 0, maxQueued: 0, decompressedBytes: 0 };
  let lastReport = 0;
  let idleSince = performance.now();
  let finished = false;

  const report = (force = false) => {
    const now = performance.now();
    if (!force && now - lastReport < PROGRESS_MS) return;
    lastReport = now;
    self.postMessage({
      type: 'progress',
      reads: sketcher.reads(),
      bases: sketcher.bases(),
      kmers: sketcher.kmers(),
      wasmMemory: wasm.memory.buffer.byteLength,
      sketchMs: stats.sketchMs,
      crcMs: stats.crcMs,
      idleMs: stats.idleMs,
      queued: queues.reduce((n, q) => n + q.length, 0),
    });
  };

  const feed = (method, item, i) => {
    const bytes = new Uint8Array(item.buf);
    let t = performance.now();
    sketcher[method](bytes);
    stats.sketchMs += performance.now() - t;
    if (item.trailers) {
      // gzip CRC32 check, deferred from the decompress worker (see WasmGunzip 'deferred')
      t = performance.now();
      crcs[i].update(bytes, item.trailers);
      stats.crcMs += performance.now() - t;
    }
    const buf = item.buf;
    stats.decompressedBytes += buf.byteLength;
    stats.chunks++;
  };

  const methods = paired ? ['push_r1', 'push_r2'] : [interleaved ? 'push_interleaved' : 'push'];

  function pump() {
    for (;;) {
      if (!paired) {
        const item = queues[0].shift();
        if (!item) break;
        if (item.t === 'chunk') { feed(methods[0], item, 0); ports[0].postMessage(1); }
        else if (item.t === 'endfile') { sketcher.end_file(); crcs[0].end_file(); }
        else if (item.t === 'end') { finish(); return; }
        continue;
      }
      // paired: ignore file boundary markers (one file per mate)
      queues.forEach((q, i) => { while (q.length && q[0].t === 'endfile') { q.shift(); crcs[i].end_file(); } });
      const [h0, h1] = [queues[0][0], queues[1][0]];
      if (!h0 || !h1) break; // wait for both mates
      if (h0.t === 'end' && h1.t === 'end') { finish(); return; }
      let i = sketcher.unmatched() > 0 ? 1 : 0;
      if (queues[i][0].t !== 'chunk') i = 1 - i; // that mate has ended; finish() reports the count mismatch
      feed(methods[i], queues[i].shift(), i);
      ports[i].postMessage(1);
    }
    report();
  }

  function finish() {
    if (finished) return;
    finished = true;
    const tf = performance.now();
    const sketch = sketcher.finish(name);
    stats.sketchMs += performance.now() - tf;
    report(true);
    const result = {
      name,
      paired,
      interleaved,
      gcBases: sketcher.gc(),
      sketchBytes: sketch.byteLength,
      reads: sketcher.reads(),
      bases: sketcher.bases(),
      kmers: sketcher.kmers(),
      wasmMemory: wasm.memory.buffer.byteLength,
      ...stats,
      lifetimeMs: performance.now() - t0,
    };
    sketcher.free();
    self.postMessage({ type: 'done', sketch, stats: result }, [sketch.buffer]);
  }

  ports.forEach((port, i) => {
    port.onmessage = ({ data }) => {
      try {
        stats.idleMs += performance.now() - idleSince;
        queues[i].push(data);
        stats.maxQueued = Math.max(stats.maxQueued, queues.reduce((n, q) => n + q.length, 0));
        pump();
      } catch (error) {
        self.postMessage({ type: 'error', message: String(error && error.message ? error.message : error) });
      }
      idleSince = performance.now();
    };
  });
  self.postMessage({ type: 'ready' });
}
