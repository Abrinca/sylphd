// Sketching worker: File -> (gunzip) -> WASM sketcher -> .sylsp bytes.
//
// Message in:  {files: File[], c, k, paired, deduplicate}
// Messages out: {type: 'progress', ...} (every ~150 ms), {type: 'done', sketch, stats},
//               {type: 'error', message}

import init, { WasmSketcher } from './pkg/sylph_sketch.js';

const PROGRESS_MS = 150;

async function isGzip(file) {
  const head = new Uint8Array(await file.slice(0, 2).arrayBuffer());
  return head.length === 2 && head[0] === 0x1f && head[1] === 0x8b;
}

// File -> stream of decompressed bytes. `counter.bytes` tracks compressed
// bytes read from disk, for the progress bar.
async function openStream(file, counter) {
  const gz = await isGzip(file);
  let stream = file.stream().pipeThrough(
    new TransformStream({
      transform(chunk, controller) {
        counter.bytes += chunk.byteLength;
        controller.enqueue(chunk);
      },
    }),
  );
  if (gz) stream = stream.pipeThrough(new DecompressionStream('gzip'));
  return stream.getReader();
}

self.onmessage = async ({ data }) => {
  try {
    await run(data);
  } catch (error) {
    self.postMessage({ type: 'error', message: String(error && error.message ? error.message : error) });
  }
};

async function run({ files, c, k, paired, deduplicate }) {
  const t0 = performance.now();
  const wasm = await init();
  const tInit = performance.now() - t0;

  const sketcher = new WasmSketcher(c, k, deduplicate);
  const total = files.reduce((n, f) => n + f.size, 0);
  const counter = { bytes: 0 };
  let decompressed = 0;
  let sketchMs = 0;
  let lastReport = 0;

  const report = (force = false) => {
    const now = performance.now();
    if (!force && now - lastReport < PROGRESS_MS) return;
    lastReport = now;
    self.postMessage({
      type: 'progress',
      read: counter.bytes,
      total,
      decompressed,
      reads: sketcher.reads(),
      bases: sketcher.bases(),
      kmers: sketcher.kmers(),
      wasmMemory: wasm.memory.buffer.byteLength,
      elapsed: now - t0,
    });
  };

  const feed = (method, chunk) => {
    const t = performance.now();
    sketcher[method](chunk);
    sketchMs += performance.now() - t;
    decompressed += chunk.byteLength;
    report();
  };

  if (paired) {
    if (files.length !== 2) throw new Error('paired mode needs exactly two files (R1, R2)');
    const [r1, r2] = await Promise.all(files.map((f) => openStream(f, counter)));
    // Alternate between the mates so the sketcher never queues much of one side.
    let done1 = false;
    let done2 = false;
    while (!done1 || !done2) {
      if (!done1) {
        const { value, done } = await r1.read();
        if (done) done1 = true;
        else feed('push_r1', value);
      }
      if (!done2) {
        const { value, done } = await r2.read();
        if (done) done2 = true;
        else feed('push_r2', value);
      }
    }
  } else {
    for (const file of files) {
      const reader = await openStream(file, counter);
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        feed('push', value);
      }
      sketcher.end_file();
    }
  }

  const name = files[0].name.replace(/(_R?1)?\.(f(ast)?[aqn]a?|fq|fna)(\.gz)?$/i, '') || 'sample';
  const tf = performance.now();
  const sketch = sketcher.finish(name);
  sketchMs += performance.now() - tf;
  report(true);
  const stats = {
    name,
    paired,
    sketchBytes: sketch.byteLength,
    reads: sketcher.reads(),
    bases: sketcher.bases(),
    kmers: sketcher.kmers(),
    inputBytes: total,
    decompressedBytes: decompressed,
    wasmMemory: wasm.memory.buffer.byteLength,
    initMs: tInit,
    sketchMs,
    totalMs: performance.now() - t0,
  };
  sketcher.free();
  self.postMessage({ type: 'done', sketch, stats }, [sketch.buffer]);
}
