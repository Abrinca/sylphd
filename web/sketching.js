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
  // Per-contig mode bounds. The browser keeps one record's sketch at a time, but a
  // pathological input must not keep a tab busy for hours: the wasm parser enforces
  // these (before growing a buffer) and its error message points to the CLI.
  const MAX_CONTIG_RECORDS = 1000000; // records in one per-contig run
  const MAX_CONTIG_LENGTH = 50000000; // bp in a single record

  // WebAssembly SIMD probe (the module is built with simd128): Chrome 91, Firefox 89, Safari 16.4.
  const SIMD_PROBE = new Uint8Array([0, 97, 115, 109, 1, 0, 0, 0, 1, 5, 1, 96, 0, 1, 123, 3, 2, 1, 0, 10, 10, 1, 8, 0, 65, 0, 253, 15, 253, 98, 11]);

  // null when this browser can sketch, else the reason.
  function unsupported() {
    if (typeof WebAssembly !== 'object') return 'This browser has no WebAssembly support.';
    if (typeof Worker === 'undefined' || !(window.Blob && Blob.prototype.stream)) {
      return 'This browser lacks Web Workers or streaming file access.';
    }
    if (!WebAssembly.validate(SIMD_PROBE)) {
      return 'This browser does not support WebAssembly SIMD (the page needs Chrome 91, Firefox 114, Safari 16.4 or newer).';
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
  //   onProgress({ streams: [{read, decompressed, finished}], sketch: {reads, bases, kmers, wasmMemory, ...} })
  // Returns { done: Promise<{ sketch: Uint8Array, stats }>, cancel() }; cancelling rejects with an AbortError.
  function start({ files, layout = 'single', c, k, name, onProgress = () => {} }) {
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
          { module, ports: channels.map((ch) => ch.port2), paired, interleaved: layout === 'interleaved', c, k, deduplicate: true, name },
          channels.map((ch) => ch.port2));
      })().catch(reject);
    });
    return { done, cancel: () => { cancelled = true; kill(); } };
  }

  const isSylsp = (file) => /\.sylsp$/i.test(file.name || '');

  // A sample name for one contig, unique within the *sent* set: repeated header
  // ids get `_2`, `_3`, ... like sylphd's `unique_contig_name`; a record with no
  // id is `contig-<index>`. `counts` (Map) and `used` (Set) belong to the caller
  // and must only cover records it keeps (the sent batch): doing this for every
  // record of a fragmented assembly would grow with the file.
  function uniqueContigName(raw, counts, used, index) {
    const base = raw || `contig-${index}`;
    let count = (counts.get(base) || 0) + 1;
    let name = count === 1 ? base : `${base}_${count}`;
    while (used.has(name)) {
      count += 1;
      name = `${base}_${count}`;
    }
    counts.set(base, count);
    used.add(name);
    return name;
  }

  // One sample sketch per record of a FASTA/FASTQ file (plain or gzip), for
  // POST /contigs: records are streamed to `onContig` as they finish, so memory
  // stays bounded by the largest record (plus the chunks in flight).
  //
  //   const run = sketchContigs(file, { c, k, minLength: 20000 }, (contig) => {
  //     // contig = { index, name, headerName, length, gcCount, gc, contigKmers, sketch }
  //   });
  //   const stats = await run.done;   // records, sketched, skipped, bases, ...
  //
  // `name` is the record's header id (raw), or `contig-<index>` when the header
  // has none; it is **not** unique. Records are transient -- the caller keeps at
  // most a bounded set -- so unique request names are made for the kept records
  // only, at send time (see uniqueContigName). `sketch` is the .sylsp bytes of
  // the record's sample sketch and `contigKmers` the record's genome-style k-mer
  // count, both null for an empty record or one shorter than `minLength`.
  // `gc` is the GC percent (two decimals), `gcCount` the number of G/C bases.
  // Returns { done, cancel() }; cancelling rejects `done` with an AbortError.
  // The caller must not hold on to the records (a large assembly does not fit
  // in memory) and should send or aggregate them as they arrive.
  // The wasm sketcher refuses more than MAX_CONTIG_RECORDS records and any
  // single record longer than MAX_CONTIG_LENGTH bp (see sketch-wasm/src/contig.rs).
  function sketchContigs(file, { c, k, minLength = 20000, minSpacing, onProgress = () => {} } = {}, onContig = () => {}) {
    let kill = () => {};
    let cancelled = false;
    const done = new Promise((resolve, reject) => {
      (async () => {
        const module = await loadModule();
        if (cancelled) return reject(abortError());
        const sketchWorker = new Worker('sketch-worker.js', { type: 'module' });
        const decompressor = new Worker('decompress-worker.js', { type: 'module' });
        const workers = [sketchWorker, decompressor];
        const channel = new MessageChannel();
        let settled = false;
        const terminate = () => workers.forEach((w) => w.terminate());
        kill = () => { if (!settled) { settled = true; terminate(); reject(abortError()); } };
        const fail = (message) => { if (settled) return; settled = true; terminate(); reject(new Error(workerMessage(message))); };
        const state = { streams: [{ read: 0, decompressed: 0, finished: false }], sketch: {}, contigs: 0 };
        sketchWorker.onmessage = ({ data }) => {
          if (settled) return;
          if (data.type === 'progress') {
            state.sketch = data;
            if (data.wasmMemory > MEM_ABORT) {
              fail('The browser ran out of memory while sketching (memory limit of 4 GB reached). This input is too diverse or too large for in-browser sketching; use the sylphd command line instead.');
            } else onProgress(state);
          } else if (data.type === 'contigs') {
            try {
              for (const record of data.records) {
                state.contigs++;
                onContig({
                  index: record.index,
                  name: record.name || `contig-${record.index}`,
                  headerName: record.name,
                  length: record.length,
                  gcCount: record.gcCount,
                  gc: record.length ? Number(((100 * record.gcCount) / record.length).toFixed(2)) : 0,
                  contigKmers: record.contigKmers,
                  sketch: record.sketch,
                });
              }
            } catch (error) {
              return fail(String(error && error.message ? error.message : error));
            }
            if (data.ack) sketchWorker.postMessage({ type: 'ack', ack: data.ack });
            onProgress(state);
          } else if (data.type === 'done') {
            settled = true; terminate();
            resolve({ ...data.stats, contigs: state.contigs });
          } else if (data.type === 'error') fail(data.message);
        };
        decompressor.onmessage = ({ data }) => {
          if (settled) return;
          if (data.type === 'error') return fail(data.message);
          state.streams[0] = { ...data, finished: data.type === 'finished' };
          onProgress(state);
        };
        decompressor.onerror = (e) => fail(`decompress worker: ${e.message || 'failed to start'}`);
        sketchWorker.onerror = (e) => fail(`sketch worker: ${e.message || 'failed to start'}`);
        decompressor.postMessage(
          { module, port: channel.port1, files: [file], stream: 0, chunkSize: CHUNK, credits: CREDITS, crc: 'deferred', engine: 'wasm' },
          [channel.port1]);
        sketchWorker.postMessage(
          {
            module, port: channel.port2, mode: 'contigs', c, k, minLength, minSpacing,
            maxRecords: MAX_CONTIG_RECORDS, maxRecordLength: MAX_CONTIG_LENGTH,
          },
          [channel.port2]);
      })().catch(reject);
    });
    return { done, cancel: () => { cancelled = true; kill(); } };
  }

  // Header of a ready-made sketch (.sylsp = bincode of sylph's SequencesSketch; same layout as
  // src/sylphd/sketchcheck.py): u64 n, n x (u64 kmer, u32 count), u64 c, u64 k, String file_name,
  // Option<String> sample_name, u8 paired, f64 mean_read_length. Only the 8 bytes at the start and the
  // small tail after the k-mer entries are read, so a 200 MB sketch is not loaded.
  // Resolves { kmers, c, k, fileName, sampleName, paired }; rejects with a short reason.
  async function readSylspHeader(file) {
    const bad = (why) => new Error(`not a valid sketch (${why})`);
    if (file.size < 8) throw bad('too short');
    const n = new DataView(await file.slice(0, 8).arrayBuffer()).getBigUint64(0, true);
    const end = 8n + n * 12n;
    if (end > BigInt(file.size)) throw bad(`declares ${n} k-mers but is only ${file.size} bytes long`);
    const start = Number(end);
    const tail = await file.slice(start, Math.min(file.size, start + 4096)).arrayBuffer();
    const dv = new DataView(tail);
    const u8 = new Uint8Array(tail);
    const dec = new TextDecoder('utf-8', { fatal: true });
    let o = 0;
    const need = (len, what) => { if (o + len > tail.byteLength) throw bad(`truncated in ${what}`); };
    const u64 = (what) => { need(8, what); const v = dv.getBigUint64(o, true); o += 8; return v; };
    const str = (what) => {
      const len = u64(`${what} length`);
      if (len > BigInt(tail.byteLength - o)) throw bad(`${what} is too long`);
      const text = dec.decode(u8.subarray(o, o + Number(len)));
      o += Number(len);
      return text;
    };
    try {
      const c = u64('c');
      const k = u64('k');
      const fileName = str('file_name');
      need(1, 'sample_name');
      const tag = u8[o++];
      if (tag > 1) throw bad('bad sample_name tag');
      const sampleName = tag ? str('sample_name') : null;
      need(1, 'paired');
      const paired = u8[o++] === 1;
      return { kmers: Number(n), c: Number(c), k: Number(k), fileName, sampleName, paired };
    } catch (e) {
      if (e instanceof TypeError) throw bad('text is not valid UTF-8');
      throw e;
    }
  }

  // Why the ready-made sketch `file` (with its readSylspHeader result, or { error }) cannot be sent to
  // the server described by `info` (/info), or null: unreadable, c/k the server does not accept, too
  // big. `what` ('assembly' | 'reads') names the input in the "sketch it again" hint.
  function sketchProblem(file, header, info, what) {
    if (header.error) return `${file.name} is ${header.error}.`;
    const db = (info && info.database) || {};
    if (Number.isFinite(db.c) && Number.isFinite(db.k) && (header.k !== db.k || header.c > db.c)) {
      return `This sketch was made with c=${header.c}, k=${header.k}, but the server uses c=${db.c}, k=${db.k}. A sketch must have the same k and may not be sparser (c ≤ ${db.c}); sketch the ${what} again here.`;
    }
    const maxMb = (info && info.limits && info.limits.max_upload_mb) || 0;
    if (maxMb && file.size > maxMb * 1048576) {
      return `This sketch is ${(file.size / 1048576).toFixed(1)} MB, over the server's upload limit of ${maxMb} MB.`;
    }
    return null;
  }

  window.SylphdSketch = {
    unsupported, loadModule, start, sketchContigs, uniqueContigName, isSylsp,
    readSylspHeader, sketchProblem, MAX_CONTIG_RECORDS, MAX_CONTIG_LENGTH,
  };
})();
