# sketch-wasm: sylph read sketching in the browser

Feasibility spike: sketch FASTQ/FASTA(.gz) reads **locally in the browser**
(WebAssembly, in a Web Worker) and send only the sketch to the existing sylphd
`POST /profile` endpoint. Nothing here touches `web/` (the production page).

**Result: it works.** The sketches are byte-identical to pysylph's (after
sorting the k-mer entries), a 1.55 GB ONT file is sketched in 14 s in Chromium
(native pysylph: 6-7 s), and the server accepts the sketch and returns the same
profile as for pysylph's own sketch. Details below.

## What is in here

| Path | |
|---|---|
| `src/seeding.rs` | k-mer hashing/seeding ported from sylph (scalar `fmh_seeds`, plus an emulation of sylph's x86_64 AVX2 path) |
| `src/sketch.rs` | what pysylph's `Sketcher.sketch_single` / `sketch_paired` do: read dedup (exact set for single, scalable cuckoo filter for pairs), mean read length, bincode `SequencesSketch` writer |
| `src/parser.rs` | streaming FASTQ/FASTA record parser (chunk-safe, CRLF, multi-line FASTA) |
| `src/gzip.rs` | push-based multi-member gzip decoder (`miniz_oxide`) |
| `src/stream.rs` | single / two-stream paired / interleaved front end |
| `src/lib.rs` | `wasm-bindgen` API (`WasmSketcher`) |
| `demo/` | demo page (`index.html`, `worker.js`) |
| `tests/` | native equality tests against pysylph, node check of the wasm build, gzip tests |

Ported code carries attribution comments with file and line numbers. Sources:
[sylph](https://github.com/bluenote-1577/sylph) (MIT OR Apache-2.0, (c) 2023 Jim Shaw) at commit
`4b5c2beb05312d9da9006529b12ad24ec47f0914` (MrTomRod/sylph, `expose-library-api`, the revision
pysylph pins) and [pysylph](https://github.com/althonos/pysylph) (MIT, Martin Larralde), branch
`multi-db`. No dependency on the `sylph` crate (it pulls in needletail, zlib-ng, rayon, clap ...);
dependencies are `wasm-bindgen`, `fxhash`, `memchr`, `scalable_cuckoo_filter` (+`rand`,
`rand_xoshiro`), `miniz_oxide`, `crc32fast`. All build for `wasm32-unknown-unknown`.

## Build and run

```bash
# once: wasm-bindgen-cli, same version as the crate (Cargo.toml pins `=0.2.129`)
cargo install wasm-bindgen-cli --version 0.2.129
./build.sh                      # -> demo/pkg/ (ES module, --target web)
python3 -m http.server -d demo 8799
# open http://127.0.0.1:8799/
```

`build.sh` runs `wasm-opt -O3` if it is on `PATH` (not used for the numbers below).
Any static file server works as long as it serves `.wasm` as `application/wasm`.

JS API (`demo/worker.js` is a complete example):

```js
import init, { WasmSketcher } from './pkg/sylph_sketch.js';
await init();
const s = new WasmSketcher(200, 31, true);   // c, k (21|31), deduplicate
s.push(uint8Chunk);          // decompressed FASTQ/FASTA text, any chunking
// or: s.push_gz(rawGzipChunk)               // multi-member gzip handled in wasm
// paired: s.push_r1(..)/s.push_r2(..) (alternate!), or s.push_interleaved(..)  (+ *_gz variants)
s.end_file();                // optional: several files into one sketch
s.reads(); s.bases(); s.kmers();             // progress counters
const sylsp = s.finish('sample');            // Uint8Array = a .sylsp file
s.free();
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

All of these are identical (`SKETCH_WASM_BIG=1` adds the 1.55 GB ONT file):

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

## Browser measurements

Chromium 155 (Playwright), Linux, 16 cores, demo page served locally, server =
`https://140-238-213-53.sslip.io` (sylphd 0.1.1, GTDB r232). Input
`FAM20866/reads.fastq.gz`: 1,550,645,339 bytes gzip, 4,295,587,662 bytes of FASTQ, 254,233
reads, 2.14 Gbp, mean read length 8.4 kb.

| | total | of which wasm sketching | read + gunzip |
|---|---|---|---|
| native pysylph (reference, threaded gunzip via dnaio) | 6.4-7.1 s | | |
| native, this crate, single thread (zlib-rs gunzip) | 11.6 s | | |
| node 22, wasm + node zlib | 17.3 s | | |
| **Chromium, `DecompressionStream`** | **13.8 s** | 5.7 s | 8.1 s |
| Chromium, gunzip in wasm (`push_gz`) | 21.6 s | (incl. inflate) | |
| Chromium, k12 R1 (2.5k reads), single / paired | 6 ms / 24 ms | | |

Result: sketch 5,787,287 bytes, 482,270 k-mers (identical to native).
`POST /profile` with that sketch: **3.7 s** (5.8 MB upload + server profiling, 1 request),
response: one genome, *Mammaliicoccus vitulinus* (GCF_029024305.1), taxonomic abundance 100 %,
adjusted ANI 99.05 %, containment 8181/10998 -- identical to the response for pysylph's own
sketch (checked with `curl`). k12 paired gave one hit (100 %, ANI 97.37).

Memory: the wasm heap stays small (37-40 MB; it holds the k-mer table, here 482k entries). Peak
RSS of the biggest Chromium process was ~1.1 GB with `DecompressionStream` and ~0.8 GB with the
wasm gunzip (both include the browser's stream buffers/file cache, not just our data).
The page itself is not blocked: all work is in the worker.

Rough sizes: `.wasm` = **132 KB** (65 KB gzipped; 89 KB without the gzip decoder), plus the glue JS (see `demo/pkg/sylph_sketch.js`); no `wasm-opt` run.

## Findings and limitations

* **`DecompressionStream('gzip')` rejects multi-member gzip** in Chromium 155 ("Junk found after
  end of compressed data"), also for BGZF and `cat a.gz b.gz`. The demo's default (`auto`) uses
  the native stream (fastest) and falls back to the wasm decoder (`push_gz`, 1.5x slower in
  total on this file) on that error, which for BGZF happens in the first block. For a
  large `cat`'ed file the error appears at the end of the first member and the run restarts
  (the sketch of a failed attempt is discarded). A simpler, slower-but-predictable alternative is to
  always use the wasm gunzip. Firefox/Safari were not tested.
* **Single thread.** A Web Worker is one thread; SharedArrayBuffer/wasm threads need
  cross-origin isolation (COOP/COEP headers), which GitHub Pages cannot set (a service-worker
  shim such as coi-serviceworker can work around it, not tried). Parallel sketching would mean
  several workers sketching disjoint parts of the file into separate tables and merging them,
  but gzip is sequential, so only multi-member / BGZF or multiple files parallelise, and the
  read dedup state (`kmer_to_pair_table`) is order dependent, so results would no longer match
  pysylph exactly.
* **Memory is 32-bit wasm: 4 GB max**, and the k-mer table plus dedup set cost an estimated few tens of
  bytes per distinct sketched k-mer (not measured) (single end: the pair set only grows for reads <= 400 bp and
  k-mers seen < 4 times; paired mode additionally allocates a cuckoo filter of ~tens of MB on first
  use). ONT / bacterial isolates are tiny; deep short-read metagenomes with hundreds of millions of
  distinct k-mers would exceed it. A `kmers()` counter is exposed so the page can warn or abort.
* The `File` is read as a stream, so file size is not limited by memory. Paired two-file mode
  must alternate R1/R2 chunks (the demo does); the sketcher queues unmatched mates.
* Seeding speed: ~380 Mbp/s in wasm (scalar, no `simd128`). A `simd128` port of the AVX2 loop
  would help only if sketching rather than gunzip dominated, which it does not for ONT.
* Not done: Firefox/Safari tests, `wasm-opt`, progress for the wasm-gunzip case in
  "decompressed bytes", HTTP error UX, cancellation, retry/rate-limit handling.

## Next steps for integrating into `web/`

1. Build `sylph_sketch_bg.wasm` + glue in CI and ship them next to `web/app.js` (about 200 KB, cacheable); copy `demo/worker.js` logic, keeping the `auto` gunzip fallback (or always wasm gunzip).
2. Read `c`/`k` from `/info` (the demo already does) and refuse files when `max_upload_mb` would be exceeded (sketch size is roughly 12 bytes x k-mers).
3. Add a "sketch in your browser" path next to the existing assembly upload: pick files (R1/R2 or interleaved), show progress, POST to `/profile`, reuse the existing result renderer.
4. Decide on paired UX (name-based R1/R2 detection as in `sylphd.sketching.sample_name_from_path`) and the memory guard (`kmers()` limit, message).
5. Pin the sylph/pysylph revision in the crate docs and keep the equality tests in CI (`cargo test`, needs the sylphd venv) so a pysylph bump that changes seeding or the bincode layout is noticed.
6. Optionally: a size/time warning for files > a few GB, and `wasm-opt`/`simd128` builds.
