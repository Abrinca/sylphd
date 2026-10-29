# sketch-wasm: sylph read sketching in the browser

Feasibility spike: sketch FASTQ/FASTA(.gz) reads **locally in the browser**
(WebAssembly, in Web Workers) and send only the sketch to the existing sylphd
`POST /profile` endpoint. The spike is integrated into the production page (`web/`, "Reads" mode, see
below); the workers in `web/` are the only copy of the pipeline (the separate demo page that the
spike started with is gone: it duplicated `web/sketching.js` and the workers).

**Result: it works.** The sketches are byte-identical to pysylph's (after
sorting the k-mer entries), a 1.55 GB ONT file is sketched in Chromium in **9.7 s** with a
two-worker pipeline (gunzip in one worker, sketching in another; native pysylph: 6-7 s), and the
server accepts the sketch and returns the same profile as for pysylph's own sketch.
One decompression path for everything (plain gzip, BGZF, `cat a.gz b.gz`): `flate2` with the
pure-Rust `zlib-rs` backend, compiled to wasm. Details below.

## What is in here

| Path | |
|---|---|
| `src/seeding.rs` | k-mer hashing/seeding ported from sylph (scalar `fmh_seeds`, plus an emulation of sylph's x86_64 AVX2 path) |
| `src/sketch.rs` | what pysylph's `Sketcher.sketch_single` / `sketch_paired` do: read dedup (exact set for single, scalable cuckoo filter for pairs), mean read length, bincode `SequencesSketch` writer |
| `src/parser.rs` | streaming FASTQ/FASTA record parser (chunk-safe, CRLF, multi-line FASTA) |
| `src/gzip.rs` | push/pull multi-member gzip decoder: own header/trailer/member loop around `flate2`'s raw inflate (`zlib-rs` backend), CRC32 inline, deferred or off |
| `src/stream.rs` | single / two-stream paired / interleaved front end |
| `src/lib.rs` | `wasm-bindgen` API (`WasmSketcher`, `WasmGunzip`, `WasmCrc`) |
| `tests/` | native equality tests against pysylph, node check of the wasm build, gzip tests, `browser_check.mjs` (`web/sketching.js` and its workers in Firefox/Chromium via playwright-core), `bench_gunzip.mjs`, `make_fixtures.py` |
| `.cargo/config.toml` | builds wasm32 with `+simd128` |

Ported code carries attribution comments with file and line numbers. Sources:
[sylph](https://github.com/bluenote-1577/sylph) (MIT OR Apache-2.0, (c) 2023 Jim Shaw) at commit
`4b5c2beb05312d9da9006529b12ad24ec47f0914` (MrTomRod/sylph, `expose-library-api`, the revision
pysylph pins) and [pysylph](https://github.com/althonos/pysylph) (MIT, Martin Larralde), branch
`multi-db`. No dependency on the `sylph` crate (it pulls in needletail, zlib-ng, rayon, clap ...);
dependencies are `wasm-bindgen`, `fxhash`, `memchr`, `scalable_cuckoo_filter` (+`rand`,
`rand_xoshiro`), `flate2` (+`zlib-rs`). All build for `wasm32-unknown-unknown`.

## Build and run

```bash
# once: wasm-bindgen-cli, same version as the crate (Cargo.toml pins `=0.2.129`)
cargo install wasm-bindgen-cli --version 0.2.129
./build.sh                      # -> ../web/wasm/ (ES module, --target web; gitignored)
python3 -m http.server -d ../web 8799
# open http://127.0.0.1:8799/ (the page needs a sylphd server for /info)
```

`build.sh` runs `wasm-opt -O3` if it is on `PATH` (not used for the numbers below).
Any static file server works as long as it serves `.wasm` as `application/wasm`.

JS API. The whole pipeline is in `web/` (`sketching.js` starts `decompress-worker.js` -> `sketch-worker.js`); the pieces:

```js
import init, { WasmSketcher, WasmGunzip, WasmCrc } from './wasm/sylph_sketch.js';
await init({ module_or_path: compiledModule });   // a WebAssembly.Module can be shared between workers

// stage 1: compressed bytes -> decompressed chunks (any gzip flavour; plain files skip this)
const gz = new WasmGunzip(2 << 20, 'deferred'); // chunk size; CRC32: 'inline' | 'deferred' | 'off'
gz.push(compressedChunk);
let out; while ((out = gz.pull()) !== undefined) send(out, gz.take_trailers());
send(gz.finish(), gz.take_trailers());          // throws if the gzip data is truncated

// stage 2: decompressed FASTQ/FASTA text, any chunking -> sketch
const crc = new WasmCrc();                       // checks the 'deferred' CRC32s
const s = new WasmSketcher(200, 31, true);       // c, k (21|31), deduplicate
crc.update(chunk, trailers); s.push(chunk);      // paired: s.push_r1(..)/s.push_r2(..) (feed the mate that s.unmatched() says is behind) or s.push_interleaved(..)
s.end_file(); crc.end_file();                    // optional: several files into one sketch
s.reads(); s.bases(); s.gc(); s.kmers();         // progress counters (gc: G/C bases among bases)
const sylsp = s.finish('sample');                // Uint8Array = a .sylsp file
s.free();
// also: s.push_gz(rawGzipChunk) (+ *_gz variants) to gunzip and sketch inside one instance
```

An optional 4th constructor argument `scalar_seeding` selects the non-AVX2 behaviour (see below).

## Equality with pysylph

`cargo test --release` (from `sketch-wasm/`) sketches the inputs with this crate natively
and runs `tests/compare_pysylph.py` with the sylphd venv (`../.venv`, or `SYLPHD_PYTHON`).
Reference sketches come from `pysylph.Sketcher(c, k, deduplicate).sketch_single` /
`.sketch_paired`, fed with `dnaio` exactly as `sylphd.sketching` does. pysylph's bytes are
re-emitted with the k-mer entries sorted (this crate always writes them sorted, sylph writes hash-map
order, which no reader depends on) and compared **byte for byte**; the profile against
`tests/data/ecoli.syl2db` is compared as well.

All of these are identical (`SKETCH_WASM_BIG=1` adds a large ONT file, which you put at
`scratch/big/ont.fastq.gz`; the numbers below are for a 1.55 GB one):

| case | |
|---|---|
| `k12` R1, c=200 k=31, dedup on/off; c=100 k=21 | 2.5k reads of 70 bp |
| `k12` R1+R2 paired, dedup on/off | |
| `clodf13.fna.gz` (FASTA) | 1 record |
| simulated 30x E. coli pairs, 100-150 bp, 8 % duplicates: single (dedup on/off), paired, paired k=21 c=50 | 557k reads; exercises dedup and the cuckoo filter |
| the same inputs fed as raw `.gz` through the wasm gzip decoder; `cat a.gz a.gz`; BGZF-like multi-member | |
| ONT `FAM20866/reads.fastq.gz` (1.55 GB, 254k reads, 2.14 Gbp) | 482,270 k-mers, 5.79 MB, same as pysylph |

`node tests/node_check.mjs [--big]` (after `./build.sh` and `cargo test`) loads the **wasm32
build** in node and compares its output byte for byte with the native sketches (32-bit
`usize`, hashing etc. all agree, including paired mode).
`tests/seedcheck/` compares the seeding functions with the sylph crate itself on 450k random
reads (N and lower case included).
`tests/gzip.rs`, and the parser tests in `tests/pysylph_equality.rs`, cover chunk boundaries
(1-byte chunks), CRLF, missing final newline, multi-line FASTA, quality lines starting with `@`,
corrupt/truncated gzip and a few malformed inputs.
The sketches also pass `sylphd.sketchcheck` and `SampleSketch.from_bytes`, and the live server
profiles them.

### Differences and things to know

* **Seeding differs between sylph builds.** `sketch::extract_markers` uses an AVX2 implementation
  on x86_64 that splits each read into four segments and *drops the last `(n-k+1) mod 4` k-mers*
  (and reads of exactly `k` bases); the scalar code (aarch64, no AVX2) considers all k-mers. So
  pysylph's sketches already differ by CPU. Default here: the **AVX2 behaviour, emulated
  in scalar code** (same k-mers and insertion order, bit-identical to pysylph on x86_64, which is
  what the server and most users run). `scalar_seeding = true` gives the other behaviour, verified
  against `sylph::seeding::fmh_seeds`. Statistically the two are equivalent (up to 3 k-mers per
  read at the ends differ).
* Entries are written sorted by k-mer; byte order otherwise as sylph's bincode layout (see
  `src/sylphd/sketchcheck.py`). `file_name` = the name passed to `finish`, `sample_name` = None.
* Same restrictions as `Sketcher`: `k` is 21 or 31; the paired path uses the default `fpr`
  (0.0001, seeded RNG 7), like `sketch_paired`'s default.
* Mate pairing is by record order, as in `sketch_paired`; read names are not compared.
* Only 4-line FASTQ (like dnaio); FASTA may be multi-line. Every record is a "read", FASTA contigs
  included (this is what `sketch_single` does with an assembly).

## Design: two-worker pipeline

```
main thread ── File handles ──> decompress worker(s) ── ~2 MB chunks (transferred) ──> sketch worker ──> sketch ──> main: POST /profile
  (UI, progress)                  File.stream() -> WasmGunzip     <── 1 ack per chunk ──   WasmSketcher (+ WasmCrc)
```

* **Decompress worker** (one per input stream: single-end = all files in order, paired = one per
  mate): reads the `File` as a stream, detects gzip by its magic bytes, inflates with
  `WasmGunzip` (flate2/zlib-rs; multi-member, BGZF and plain gzip are the same code path) into
  fixed 2 MB chunks and posts them as transferable `ArrayBuffer`s. Plain text is only re-chunked.
* **Backpressure**: credit based. The sketch worker acks every processed chunk on the chunk's
  `MessagePort`; the decompress worker holds at most 4 chunks in flight (8 MB) and waits for a
  credit otherwise. Chunk size (1/2/4 MB) and credits were varied with URL parameters in the former demo page;
  1-4 MB made no measurable difference, so `web/sketching.js` fixes 2 MB and 4.
* **Paired-end**: two decompress workers, one per file. The sketch worker consumes one R1 chunk and
  one R2 chunk at a time, always the mate that is behind in *records* (`unmatched()`; equal byte
  chunks of 75 bp and 150 bp mates hold different numbers of records), so at most about one
  chunk of unmatched mates (a few MB) is queued.
  Simple and correct: same sketch bytes as native, 3 workers in total.
* **CRC32** of the gzip members (integrity of the input) costs ~15 % of the gunzip time, and gunzip
  is the bottleneck stage. So it is *deferred*: the decompress worker only records
  `(end offset, crc, isize)` per member and sends them with the chunk the member ended in; the
  sketch worker, which has idle time, hashes the chunk (`WasmCrc`) and compares. Corrupt or
  truncated input is still detected (tested in the browser: "gzip CRC32 mismatch", "gzip data is
  truncated"), the error just arrives from the sketch worker. `WasmGunzip` also has the `inline` and `off` CRC modes (used for the measurements below).
* The compiled `WebAssembly.Module` is created once on the main thread and posted to the
  workers; each worker instantiates its own copy (separate memory; the sketcher's heap is ~40 MB for this file).
* The main thread only shows progress (a message every 150 ms per worker) and does the POST.

Why not `DecompressionStream`: it is a bit faster in isolation (below), but it rejects multi-member
gzip in Chromium 155 ("Junk found after end of compressed data": BGZF, `cat a.gz b.gz`), and
BGZF/`cat` detection by sniffing headers is unreliable. In the pipeline the wasm decoder is on par
with it (measured with an experiment switch in the former demo page), so there is a
single path.

## Browser measurements

Chromium 155 (Playwright), Linux, 16 cores, test page served locally, server =
`https://sylphd.abrinca.com` (sylphd 0.1.1, GTDB r232). Input
`FAM20866/reads.fastq.gz`: 1,550,645,339 bytes gzip, 4,295,587,662 bytes of FASTQ, 254,233
reads, 2.14 Gbp, mean read length 8.4 kb. Wasm built with `simd128`.

**Gunzip alone** (worker, nothing else running, with the former `bench.html`; node: `tests/bench_gunzip.mjs`):

| | time | compressed MB/s | decompressed MB/s |
|---|---|---|---|
| read the `File` only | 0.7 s | 2150 | |
| Chromium `DecompressionStream` | 8.2 s | 188 | 522 |
| Chromium `WasmGunzip` zlib-rs, CRC inline | 11.0 s | 141 | 392 |
| Chromium `WasmGunzip` zlib-rs, CRC off | 9.6 s | 162 | 449 |
| node 22: `WasmGunzip` (CRC inline) / node's native `zlib` | 9.0 s / 9.7 s | 172 / 160 | 477 / 444 |
| *previous: wasm `miniz_oxide`* | *~16 s (derived: 21.6 s total - 5.7 s sketch, sequential)* | *~97* | *~270* |

zlib-rs in wasm is ~1.5x faster than miniz_oxide and ~1.3x slower than Chromium's native zlib
(with CRC). `simd128` barely matters: no-simd build 9.29 s vs 9.12 s in node for gunzip,
~4 % on the sketching loop (5.7 s -> 5.5 s); kept because it is free, can be dropped.

**End to end** (`wall` = from file selection to sketch in the main thread; stage busy times are
summed per worker; "idle" = sketch worker waiting for data):

| run | wall | gunzip busy | sketch busy | CRC busy | sketch worker idle |
|---|---|---|---|---|---|
| native pysylph (threaded gunzip) | 6.4-7.1 s | | | | |
| native, this crate, one thread | 11.6 s | | | | |
| *previous: sequential, `DecompressionStream`* | *13.8 s* | *8.1 (read+gunzip)* | *5.7* | | |
| *previous: sequential, wasm miniz_oxide* | *21.6 s* | *~16* | *5.7* | | |
| pipeline, wasm gunzip, CRC inline | 11.0 s | 10.2 s | 5.7 s | (in gunzip) | 5.4 s |
| pipeline, wasm gunzip, CRC off | 9.6 s | 8.8 s | 5.5 s | | 4.1 s |
| **pipeline, wasm gunzip, CRC deferred (default)** | **9.7 s** (9.66/9.77 in 2 runs) | 8.85 s | 5.46 s | 1.47 s | 2.7 s |
| pipeline with `DecompressionStream` (experiment) | 10.2 s | 7.7 s (incl. read) | 5.5 s | | 4.7 s |
| pipeline, deferred CRC, Firefox 157 | 13.5 s | 10.1 s | 7.7 s | 1.3 s | 4.3 s |

Reading it: decompression (one thread, inherently sequential for a single gzip member) is the
bottleneck at 8.8 s; sketching (5.5 s) and the CRC (1.5 s) hide behind it; the pipeline wall is
gunzip time + ~0.8 s (file read, first chunk, finish). 9.7 s is 1.4-1.5x native pysylph, which
gunzips in a separate thread pool. Peak queue depth: 1 chunk, backpressure waits 0.1-0.3 s, so the
credits never limit throughput. The wasm heap of the sketcher stays at 40 MB.
Not faster to go below ~9 s without parallel gunzip (only possible for multi-member/BGZF inputs
and would change nothing for a single-member file like this one).

Result: sketch 5,787,287 bytes, 482,270 k-mers (entry hash identical to the native sketch, in
Chromium and Firefox). `POST /profile` with it: **3.7 s** (5.8 MB upload + server profiling,
1 request), response: one genome, *Mammaliicoccus vitulinus* (GCF_029024305.1), taxonomic
abundance 100 %, adjusted ANI 99.05 %, containment 8181/10998 -- identical to the response for
pysylph's own sketch (checked with `curl`). k12 paired gave one hit (100 %, ANI 97.37).

Gzip flavours checked in Chromium **and** Firefox through the pipeline (SHA-256 of the k-mer
entries equal to the native `sim_single` sketch): single-member gzip, real BGZF (64 KiB blocks +
EOF block, 24.9 MB), three `cat`ed gzips, plain text; k12 paired and k12 x2 / multi-file single
(`tests/browser_check.mjs`, fixtures from `tests/make_fixtures.py`).

Memory: peak RSS of the biggest Chromium process was ~0.8-1.1 GB in the previous sequential runs
(it includes the browser's stream buffers and file cache). Not re-measured for the pipeline;
what we control is small: 8 MB of chunks in flight and the ~40 MB sketcher heap.

`.wasm` = **142 KB** (with simd128, zlib-rs; no `wasm-opt` run), glue JS 26 KB.

BYOB reads into wasm memory (item 4 of the iteration) were skipped: the read stage costs 0.6 s in
total and overlaps with gunzip, and what it could save is the one memcpy of the compressed
chunk into wasm memory (1.5 GB, ~0.1-0.2 s); below the 0.5 s threshold.

## Findings and limitations

* **`DecompressionStream('gzip')` rejects multi-member gzip** in Chromium 155 ("Junk found after
  end of compressed data"), also for BGZF and `cat a.gz b.gz`. Hence the wasm decoder, used for
  all gzip input. Firefox 157 was only tested with the wasm path (works, same sketches);
  Safari is untested.
* **Gunzip of a single member is sequential**: ~9 s for this file whatever we do around it. Only
  multi-member inputs could be decompressed in parallel (needs member offsets, i.e. a BGZF index or
  a scan), and parallel *sketching* into separate tables would not reproduce pysylph's
  order-dependent read dedup exactly.
* **Threads/SharedArrayBuffer** need cross-origin isolation (COOP/COEP headers), which GitHub Pages
  cannot set; the pipeline deliberately uses only message passing (transferables) and works
  without it.
* **Memory is 32-bit wasm: 4 GB max** per instance, and the k-mer table plus dedup set cost an
  estimated few tens of bytes per distinct sketched k-mer (not measured) (single end: the pair set
  only grows for reads <= 400 bp and k-mers seen < 4 times; paired mode additionally allocates a
  cuckoo filter of ~tens of MB on first use). ONT / bacterial isolates are tiny; deep short-read
  metagenomes with hundreds of millions of distinct k-mers would exceed it. A `kmers()` counter is
  exposed so the page can warn or abort.
* The `File` is read as a stream, so file size is not limited by memory.
* Seeding speed: ~390 Mbp/s per worker in wasm. The AVX2 emulation loop is scalar; a real `simd128`
  port would only matter if sketching became the bottleneck (it is not for gzip input, where
  decompression takes ~8.8 s; for plain FASTQ it is: ~5.5 s of sketching vs 0.7 s of reading, so
  plain FASTQ should finish in roughly 6 s instead of 9.7 s; not measured end to end).
* Not done: Safari, `wasm-opt`, HTTP error UX, cancellation, retry/rate-limit handling, a memory
  guard, re-measuring peak RSS for the pipeline.

## Integration into `web/`

Done on branch `wasm-sketch`: the web page has a **Reads** mode (`web/reads.js`, `web/taxonomy.js`)
next to the **Assembly** mode.

* Build: `./build.sh ../web/wasm` writes the module and glue into `web/wasm/` (gitignored).
  `scripts/preview-pages.sh` and `.github/workflows/pages.yml` run it (Rust toolchain and
  `wasm-bindgen-cli` pinned there; the CLI is the prebuilt release binary, checked by sha256).
* One copy of the workers: `web/decompress-worker.js` / `web/sketch-worker.js` (module path
  `./wasm/`; the sketch worker also supports interleaved input). `tests/browser_check.mjs` runs
  them through `web/sketching.js`.
* `c`/`k` come from `/info`; paired detection, memory guard (warn at 1.5 GB, abort at 3.6 GB of
  wasm memory), cancellation, `429`/`503`/network retry with the sketch kept in memory and the
  progress UI are in `web/reads.js`.
* The Assembly tab uses the same pipeline (`web/sketching.js`): single-end sketching of a pasted
  FASTA or a file, with the G/C count (`WasmSketcher.gc()`) for the GC% of the summary,
  and sends `application/vnd.sylph.sketch` to `POST /assembly`. Verified identical to the server
  sketching the same FASTA (hits, assignment, closest, records, length, GC).
* Still open: Safari, `wasm-opt` in CI, keeping the equality tests (`cargo test`, needs the sylphd
  venv) in CI, a time/size warning for files over a few GB.
