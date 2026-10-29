// Stage 2 of the pipeline: decompressed chunks -> WASM sketcher -> .sylsp bytes.
//
// Two modes, chosen by the first message:
//   reads   (default): one sample sketch of all reads, like `sylph sketch`
//   contigs:           one sample sketch + genome-style k-mer count per record
//
// Receives chunks from one decompress worker (single-end) or two (paired: R1
// and R2). Every processed chunk is acked on the port it came from, which
// gives the decompress worker a credit for the next one.
//
// Paired input: the sketcher queues a mate until its partner arrives, and equal byte chunks hold
// different numbers of records (75 bp vs 150 bp mates). So the mate that is behind in records
// (sketcher.unmatched()) is fed next, which keeps the queue at about one chunk of records.
//
// Contigs mode: after every chunk the finished records are posted to the main
// thread as one batch; the chunk credit is only returned once the main thread
// has consumed (and acknowledged) that batch, so at most `credits` chunks of
// records are in flight and memory stays bounded for large assemblies.
//
// Messages to the main thread: {type: 'progress'}, {type: 'contigs', records,
// ack}, {type: 'done', sketch, stats}, {type: 'error'}

import init, { WasmSketcher, WasmContigSketcher, WasmCrc } from './wasm/sylph_sketch.js';

const PROGRESS_MS = 150;

// Set by runContigs: handles the main thread's {type: 'ack'} messages.
let onAck = null;

self.onmessage = async ({ data }) => {
  try {
    if (data.type === 'ack') {
      if (onAck) onAck(data);
      return;
    }
    if (data.mode === 'contigs') await runContigs(data);
    else await run(data);
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

// Contigs mode: one record -> one sample sketch + genome-style k-mer count.
async function runContigs({ module, port, c, k, minLength, minSpacing }) {
  const t0 = performance.now();
  const wasm = await init({ module_or_path: module });
  const sketcher = new WasmContigSketcher(c, k, minLength, minSpacing);
  const crc = new WasmCrc();
  const stats = {
    sketchMs: 0, crcMs: 0, idleMs: 0, chunks: 0, decompressedBytes: 0,
    records: 0, sketched: 0, skipped: 0, batches: 0,
  };
  // Batches whose chunk credit is released when the main thread acks them.
  const deferred = new Set();
  let ackId = 0;
  let lastReport = 0;
  let idleSince = performance.now();
  let finished = false;

  const report = (force = false) => {
    const now = performance.now();
    if (!force && now - lastReport < PROGRESS_MS) return;
    lastReport = now;
    self.postMessage({
      type: 'progress',
      records: sketcher.seen(),
      sketched: sketcher.sketched(),
      skipped: sketcher.skipped(),
      bases: sketcher.bases(),
      wasmMemory: wasm.memory.buffer.byteLength,
      sketchMs: stats.sketchMs,
      crcMs: stats.crcMs,
      idleMs: stats.idleMs,
      queued: 0,
    });
  };

  onAck = ({ ack }) => {
    if (deferred.delete(ack)) port.postMessage(1);
  };

  // Hands every finished record to the main thread; `credit` says whether the
  // chunk that produced them may be acked after the batch is processed.
  const flush = (credit) => {
    const records = sketcher.drain();
    stats.records = sketcher.seen();
    stats.sketched = sketcher.sketched();
    stats.skipped = sketcher.skipped();
    if (!records.length) {
      if (credit) port.postMessage(1);
      return;
    }
    const ack = ++ackId;
    if (credit) deferred.add(ack);
    stats.batches++;
    const transfer = [];
    for (const record of records) if (record.sketch) transfer.push(record.sketch.buffer);
    self.postMessage({ type: 'contigs', records, ack }, transfer);
  };

  const feed = (item) => {
    const bytes = new Uint8Array(item.buf);
    let t = performance.now();
    sketcher.push(bytes);
    stats.sketchMs += performance.now() - t;
    if (item.trailers) {
      t = performance.now();
      crc.update(bytes, item.trailers);
      stats.crcMs += performance.now() - t;
    }
    stats.decompressedBytes += bytes.byteLength;
    stats.chunks++;
    flush(true);
    report();
  };

  function finish() {
    if (finished) return;
    finished = true;
    // normally a no-op (end_file already ran on the 'endfile' marker); guards
    // against an input that ends without one
    sketcher.finish();
    flush(false);
    report(true);
    const result = {
      contigs: sketcher.seen(),
      records: sketcher.seen(),
      sketched: sketcher.sketched(),
      skipped: sketcher.skipped(),
      bases: sketcher.bases(),
      wasmMemory: wasm.memory.buffer.byteLength,
      ...stats,
      lifetimeMs: performance.now() - t0,
    };
    sketcher.free();
    self.postMessage({ type: 'done', stats: result });
  }

  port.onmessage = ({ data }) => {
    try {
      stats.idleMs += performance.now() - idleSince;
      if (data.t === 'chunk') feed(data);
      else if (data.t === 'endfile') { sketcher.end_file(); crc.end_file(); flush(false); }
      else if (data.t === 'end') finish();
    } catch (error) {
      self.postMessage({ type: 'error', message: String(error && error.message ? error.message : error) });
    }
    idleSince = performance.now();
  };
  self.postMessage({ type: 'ready' });
}
