// Stage 1 of the pipeline: File -> (gunzip in WASM) -> ~2 MB chunks -> sketch worker.
//
// One instance per input stream (single-end: all files in order; paired: one
// instance per mate file). Gzip is detected by magic bytes; multi-member gzip,
// BGZF and plain gzip are all handled by the same WasmGunzip. Plain text files
// are only re-chunked.
//
// Backpressure: the sketch worker acks every chunk it has processed (one
// message on `port` = one credit); at most `credits` chunks are in flight.
//
// Messages to the main thread: {type: 'progress' | 'finished' | 'error', stream, ...}

import init, { WasmGunzip } from './pkg/sylph_sketch.js';

const PROGRESS_MS = 150;

self.onmessage = async ({ data }) => {
  try {
    await run(data);
  } catch (error) {
    self.postMessage({ type: 'error', stream: data.stream, message: String(error && error.message ? error.message : error) });
  }
};

async function isGzip(file) {
  const head = new Uint8Array(await file.slice(0, 2).arrayBuffer());
  return head.length === 2 && head[0] === 0x1f && head[1] === 0x8b;
}

async function run({ module, port, files, stream, chunkSize, credits: initialCredits, crc = 'deferred', engine = 'wasm' }) {
  await init({ module_or_path: module });

  const stats = { read: 0, decompressed: 0, readMs: 0, gunzipMs: 0, waitMs: 0 };
  const t0 = performance.now();
  let credits = initialCredits;
  let wake = null;
  port.onmessage = () => {
    credits++;
    if (wake) { wake(); wake = null; }
  };
  let lastReport = 0;
  const report = (type = 'progress') => {
    const now = performance.now();
    if (type === 'progress' && now - lastReport < PROGRESS_MS) return;
    lastReport = now;
    self.postMessage({ type, stream, ...stats, elapsed: now - t0 });
  };

  // exactly-sized ArrayBuffer for transfer
  const exact = (u8) => (u8.byteOffset === 0 && u8.byteLength === u8.buffer.byteLength ? u8.buffer : u8.slice().buffer);

  // `trailers`: gzip members that ended inside this chunk, for the CRC check in the sketch worker
  async function send(u8, trailers) {
    if (u8.byteLength === 0) return;
    while (credits === 0) {
      const t = performance.now();
      await new Promise((resolve) => { wake = resolve; });
      stats.waitMs += performance.now() - t;
    }
    credits--;
    stats.decompressed += u8.byteLength;
    const buf = exact(u8);
    port.postMessage({ t: 'chunk', buf, trailers }, [buf]);
    report();
  }

  // a final empty chunk may still carry trailers
  const sendOrEmpty = async (u8, trailers) => {
    if (u8.byteLength) return send(u8, trailers);
    while (credits === 0) await new Promise((resolve) => { wake = resolve; });
    credits--;
    port.postMessage({ t: 'chunk', buf: new ArrayBuffer(0), trailers });
  };

  for (const file of files) {
    const gz = await isGzip(file);
    // engine 'native' (DecompressionStream) is an experiment for comparisons: it
    // rejects multi-member gzip, so the demo always uses 'wasm'.
    const native = gz && engine === 'native';
    const decoder = gz && !native ? new WasmGunzip(chunkSize, crc) : null;
    let source = file.stream();
    if (native) {
      source = source
        .pipeThrough(new TransformStream({ transform(c, ctl) { stats.read += c.byteLength; ctl.enqueue(c); } }))
        .pipeThrough(new DecompressionStream('gzip'));
    }
    const reader = source.getReader();
    let parts = [];
    let partsSize = 0;
    for (;;) {
      const tr = performance.now();
      const { value, done } = await reader.read();
      stats.readMs += performance.now() - tr; // for 'native' this includes the gunzip
      if (done) break;
      if (!native) stats.read += value.byteLength;
      if (decoder) {
        let t = performance.now();
        decoder.push(value);
        for (;;) {
          const out = decoder.pull();
          stats.gunzipMs += performance.now() - t;
          if (out === undefined) break;
          const trailers = crc === 'deferred' ? decoder.take_trailers() : undefined;
          await send(out, trailers);
          t = performance.now();
        }
      } else {
        parts.push(value);
        partsSize += value.byteLength;
        if (partsSize >= chunkSize) {
          const merged = new Uint8Array(partsSize);
          let offset = 0;
          for (const p of parts) { merged.set(p, offset); offset += p.byteLength; }
          parts = [];
          partsSize = 0;
          await send(merged);
        }
      }
      report();
    }
    if (decoder) {
      const t = performance.now();
      const rest = decoder.finish(); // throws if the gzip data is truncated
      const trailers = crc === 'deferred' ? decoder.take_trailers() : undefined;
      stats.gunzipMs += performance.now() - t;
      decoder.free();
      if (rest.byteLength || (trailers && trailers.length)) await sendOrEmpty(rest, trailers);
    } else if (partsSize) {
      const merged = new Uint8Array(partsSize);
      let offset = 0;
      for (const p of parts) { merged.set(p, offset); offset += p.byteLength; }
      await send(merged);
    }
    port.postMessage({ t: 'endfile' });
  }
  port.postMessage({ t: 'end' });
  report('finished');
}
