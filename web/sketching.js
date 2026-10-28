// In-browser sketching shared by the Reads and Assembly modes: the WebAssembly module, the support
// check and the two-worker pipeline (decompress worker(s) -> sketch worker, see
// sketch-wasm/README.md). Plain JS, no dependencies. Exposes window.SylphdSketch.
(() => {
  'use strict';

  const WASM_URL = 'wasm/sylph_sketch_bg.wasm';
  const CHUNK = 2 << 20; // decompressed bytes per chunk
  const CREDITS = 4; // chunks in flight per decompress worker
  const GIB = 1024 ** 3;
  const MEM_ABORT = 3.6 * GIB; // wasm memory is capped at 4 GiB per instance

  // WebAssembly SIMD probe (the module is built with simd128): Chrome 91, Firefox 89, Safari 16.4.
  const SIMD_PROBE = new Uint8Array([0, 97, 115, 109, 1, 0, 0, 0, 1, 5, 1, 96, 0, 1, 123, 3, 2, 1, 0, 10, 10, 1, 8, 0, 65, 0, 253, 15, 253, 98, 11]);

  // null when this browser can sketch, else the reason.
  function unsupported() {
    if (typeof WebAssembly !== 'object') return 'This browser has no WebAssembly support.';
    if (typeof Worker === 'undefined' || !(window.Blob && Blob.prototype.stream)) {
      return 'This browser lacks Web Workers or streaming file access.';
    }
    if (!WebAssembly.validate(SIMD_PROBE)) {
      return 'This browser does not support WebAssembly SIMD (needs Chrome 91, Firefox 89, Safari 16.4 or newer).';
    }
    return null;
  }

  let modulePromise = null;
  function loadModule() {
    if (!modulePromise) {
      modulePromise = (async () => {
        let res;
        try { res = await fetch(WASM_URL); } catch { throw new Error('The WebAssembly sketcher could not be downloaded.'); }
        if (!res.ok) {
          throw new Error(`The WebAssembly sketcher is missing from this site (${WASM_URL}: HTTP ${res.status}). When serving web/ yourself, build it first with sketch-wasm/build.sh ../web/wasm.`);
        }
        try {
          return await WebAssembly.compileStreaming(res.clone());
        } catch {
          return WebAssembly.compile(await res.arrayBuffer()); // wrong MIME type: compile from bytes
        }
      })();
      modulePromise.catch(() => { modulePromise = null; });
    }
    return modulePromise;
  }

  // Friendly text for what the workers throw.
  function workerMessage(msg) {
    const m = String(msg || '');
    if (/unreachable|memory|out of bounds|allocation/i.test(m)) {
      return 'The browser ran out of memory while sketching. This input is too diverse or too large for in-browser sketching; sketch it with the sylphd command line instead.';
    }
    if (/CRC32/i.test(m)) return `The file is corrupt (${m}).`;
    if (/truncated/i.test(m)) return `The file is incomplete (${m}). Was the download or copy cut off?`;
    return m;
  }

  function abortError() { const e = new Error('cancelled'); e.name = 'AbortError'; return e; }

  // Sketch `files` (File/Blob list; gzip is detected by content) with the database's c and k.
  //   layout: 'single' (all files in order, one sample) | 'paired' (files = [R1, R2]) | 'interleaved'
  //   stats:  also count the GC bases (assemblies: records, length and GC for the summary)
  //   onProgress({ streams: [{read, decompressed, finished}], sketch: {reads, bases, kmers, wasmMemory, ...} })
  // Returns { done: Promise<{ sketch: Uint8Array, stats }>, cancel() }; cancelling rejects with an AbortError.
  function start({ files, layout = 'single', c, k, name, stats = false, onProgress = () => {} }) {
    let kill = () => {};
    let cancelled = false;
    const done = new Promise((resolve, reject) => {
      (async () => {
        const module = await loadModule();
        if (cancelled) return reject(abortError());
        const paired = layout === 'paired';
        const streams = paired ? [[files[0]], [files[1]]] : [files];
        const sketchWorker = new Worker('sketch-worker.js', { type: 'module' });
        const decompressors = streams.map(() => new Worker('decompress-worker.js', { type: 'module' }));
        const workers = [sketchWorker, ...decompressors];
        const channels = streams.map(() => new MessageChannel());
        let settled = false;
        const terminate = () => workers.forEach((w) => w.terminate());
        kill = () => { if (!settled) { settled = true; terminate(); reject(abortError()); } };
        const fail = (message) => { if (settled) return; settled = true; terminate(); reject(new Error(workerMessage(message))); };
        const state = { streams: streams.map(() => ({ read: 0, decompressed: 0, finished: false })), sketch: {} };
        sketchWorker.onmessage = ({ data }) => {
          if (settled) return;
          if (data.type === 'progress') {
            state.sketch = data;
            if (data.wasmMemory > MEM_ABORT) {
              fail('The browser ran out of memory while sketching (memory limit of 4 GB reached). This input is too diverse or too large for in-browser sketching; use the sylphd command line instead.');
            } else onProgress(state);
          } else if (data.type === 'done') {
            settled = true; terminate();
            resolve(data);
          } else if (data.type === 'error') fail(data.message);
        };
        decompressors.forEach((w, i) => {
          w.onmessage = ({ data }) => {
            if (settled) return;
            if (data.type === 'error') return fail(data.message);
            state.streams[i] = { ...data, finished: data.type === 'finished' };
            onProgress(state);
          };
          w.onerror = (e) => fail(`decompress worker: ${e.message || 'failed to start'}`);
        });
        sketchWorker.onerror = (e) => fail(`sketch worker: ${e.message || 'failed to start'}`);
        streams.forEach((list, i) => {
          decompressors[i].postMessage(
            { module, port: channels[i].port1, files: list, stream: i, chunkSize: CHUNK, credits: CREDITS, crc: 'deferred', engine: 'wasm' },
            [channels[i].port1]);
        });
        sketchWorker.postMessage(
          { module, ports: channels.map((ch) => ch.port2), paired, interleaved: layout === 'interleaved', c, k, deduplicate: true, name, gc: stats },
          channels.map((ch) => ch.port2));
      })().catch(reject);
    });
    return { done, cancel: () => { cancelled = true; kill(); } };
  }

  window.SylphdSketch = { unsupported, loadModule, start };
})();
