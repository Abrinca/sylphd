# rust: `sylphd-core` and `sylphd-wasm`

Cargo workspace for sylphd's client-side Rust crates:

* **`sylphd-core/`** — pure library (no `wasm-bindgen`, no I/O, no networking): FASTA/FASTQ streaming parser, sylph read and per-contig sketchers (over the `sylph` library core), sample naming and input specification parsing, assembly stats, `.sylsp` header validation, per-contig batch planning, the wire-protocol document model with its `api_version` check and per-contig run merge, result rendering (sylph-identical TSV, summaries, caveats, `info` text), and optional native `std::io::Read` + `flate2` (`io` feature) and `rayon` parallel sketching (`rayon` feature).
* **`sylphd-wasm/`** — browser WebAssembly bindings (`WasmSketcher`, `WasmContigSketcher`, `WasmGunzip`, `WasmCrc`, plus the document/renderer functions below) and the push/pull multi-member gzip decoder used by the web app (`web/`, "Reads" and "Assembly" modes).

Originally built as a feasibility spike (`sketch-wasm/`) to sketch FASTQ/FASTA(.gz) reads **locally in the browser**
(WebAssembly, in Web Workers) and send only the sketch to the existing sylphd
`POST /profile` endpoint. The workers in `web/` are the only copy of the pipeline.

**Result: it works.** The sketches are byte-identical to pysylph's (after
sorting the k-mer entries), a 1.55 GB ONT file is sketched in Chromium in **9.7 s** with a
two-worker pipeline (gunzip in one worker, sketching in another; native pysylph: 6-7 s), and the
server accepts the sketch and returns the same profile as for pysylph's own sketch.
One decompression path for everything (plain gzip, BGZF, `cat a.gz b.gz`): `flate2` with the
pure-Rust `zlib-rs` backend, compiled to wasm. Details below.

## What is in here

| Path | |
|---|---|
| `sylphd-core/src/sketch.rs` | thin adapter over sylph's `ReadSketcher` / `PairedReadSketcher`: progress counters (reads/bases/gc), pysylph's mean read length, deterministic bincode `SequencesSketch` writer |
| `sylphd-core/src/contig.rs` | per-contig mode: each FASTA/FASTQ record as its own sample sketch (`ReadSketcher`), plus its genome-style k-mer count (sylph's `GenomeSketcher`: duplicate removal + 30 bp minimum spacing, as `Sketcher.sketch_genome`) |
| `sylphd-core/src/parser.rs` | streaming FASTQ/FASTA record parser (chunk-safe, CRLF, multi-line FASTA, header/name) |
| `sylphd-core/src/stream.rs` | single / two-stream paired / interleaved front end (push API + native `Read` / `flate2` helpers behind the `io` feature) |
| `sylphd-core/src/io.rs` | native `MaybeGzReader` (`flate2::read::MultiGzDecoder`, auto-detecting gzip magic) |
| `sylphd-core/src/input.rs` | sample naming, `--sample` spec parsing, paired-path resolution, assembly stats, contig filtering/skip counters, native/rayon sample sketching |
| `sylphd-core/src/sketchcheck.rs` | `.sylsp` header validation and `c`/`k` compatibility checks |
| `sylphd-core/src/batch.rs` | per-contig greedy batch planner (`max_contigs`, `params` JSON budget, `max_upload_mb`) |
| `sylphd-core/src/document.rs` | JSON document model of `/info`, `/messages`, `/profile`, `/query`, `/assembly` and `/contigs`; `SUPPORTED_API_VERSION` check; per-contig run merge (`merge_summaries`, `finalize_summary`, `merge_contigs_run`) |
| `sylphd-core/src/render.rs` | renderer ported from Python: profile/query and per-contig TSV, assembly hits TSV + summary + note, the two per-contig summary lines, caveats, `/info` text and `/messages` lines |
| `sylphd-core/tests/` | native equality tests against pysylph (`pysylph_equality.rs`), renderer golden tests (`render.rs` against `tests/data/*_golden.json`), stream tests, input/sketchcheck/batch unit tests |
| `sylphd-wasm/src/gzip.rs` | push/pull multi-member gzip decoder: own header/trailer/member loop around `flate2`'s raw inflate (`zlib-rs` backend), CRC32 inline, deferred or off |
| `sylphd-wasm/src/lib.rs` | `wasm-bindgen` API (`WasmSketcher`, `WasmContigSketcher`, `WasmGunzip`, `WasmCrc`, and `check_api_version`, `render_*`, `merge_contigs_run`) |
| `sylphd-wasm/tests/` | gzip tests (`gzip.rs`), node check of the wasm build (`node_check.mjs`), renderer/merge exports (`render_check.mjs`), per-contig parity (`contigs_check.mjs`, `contigs_pipeline_check.mjs`), `browser_check.mjs` (`web/sketching.js` and its workers in Firefox/Chromium via playwright-core), `bench_gunzip.mjs`, `make_fixtures.py` |
| `.cargo/config.toml` | builds wasm32 with `+simd128` |

The k-mer seeding, read/duplicate handling and genome-style sketching are not
reimplemented here: they come from the
[sylph](https://github.com/bluenote-1577/sylph) library core (MIT OR
Apache-2.0, (c) 2023 Jim Shaw; fork `MrTomRod/sylph`, pinned `rev` from branch `sketch-core`),
the same kernels pysylph exposes. `sylph` is pulled with
`default-features = false` (no needletail/rayon/clap/zlib-ng stack) plus
`approx-dedup` (the seeded cuckoo filter behind pysylph's paired default).
`getrandom`'s `custom` backend (for the filter's discarded `thread_rng()` seeding) is only
enabled in `sylphd-wasm` for `wasm32`.

## Build and run

```bash
# once: wasm-bindgen-cli, same version as the crate (Cargo.toml pins `=0.2.129`)
cargo install wasm-bindgen-cli --version 0.2.129
./sylphd-wasm/build.sh          # -> ../web/wasm/ (ES module, --target web; gitignored)
python3 -m http.server -d ../web 8799
# open http://127.0.0.1:8799/ (the page needs a sylphd server for /info)
```

`build.sh` runs `wasm-opt -O3` if it is on `PATH` (not used for the numbers below).
Any static file server works as long as it serves `.wasm` as `application/wasm`.

The `sylph` dependency is pinned to a specific commit (`rev`) in `rust/Cargo.toml`, and
`build.sh` uses `--locked`.

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

### Per-contig mode (`POST /contigs`)

`WasmContigSketcher` streams a FASTA/FASTQ file record by record. For each
record it emits the record's **sample** sketch (`.sylsp` bytes, exactly what the
read sketcher produces for that one sequence) and its **genome-style** k-mer
count (what `len(pysylph.Sketcher(c, k).sketch_genome(name, [seq]).kmers)`
gives: selected c-subsampled hashes with duplicates removed and a greedy 30 bp
minimum spacing along the sequence). That count is the denominator of the
contig-relative ANI on the server, see `docs/reference/http-api.md` and
`scratch/per-contig-ani/FINDINGS.md`.

```js
// wasm API: poll the finished records after every push
const s = new WasmContigSketcher(200, 31, 20000);  // c, k, min_length
s.push(decompressedChunk);                          // or s.push_gz(gzipChunk)
for (const rec of s.drain()) {
  // rec = { index, name, length, gcCount, contigKmers, sketch }
}
s.end_file();                                       // the last FASTA/FASTQ record finishes here
for (const rec of s.drain()) { /* ... */ }

// browser API (web/sketching.js): the same through the two-worker pipeline
const run = SylphdSketch.sketchContigs(file, { c, k, minLength: 20000 }, (contig) => {
  // contig = { index, name, headerName, length, gcCount, gc, contigKmers, sketch }
});
const stats = await run.done;  // records, sketched, skipped, bases, chunks, ...
```

`name` there is the first word of the header made unique (`contig`, `contig_2`,
...; an empty header id becomes `contig-<index>`) exactly like
`unique_contig_name`; the sketch's inner `file_name` keeps the raw header name,
which nothing reads (the request names come from `params`). `contigKmers` and
`sketch` are `null` for an empty record or one shorter than `minLength` (the
`--min-contig-length` behaviour), while `length`/`gcCount` are always reported.
Records are delivered as they finish and the chunk credits are only returned
after the main thread has consumed the batch, so memory stays bounded by the
largest record plus a few chunks (assemblies with millions of contigs work; the
caller must not accumulate the records).


## Documents and rendering (`document.rs`, `render.rs`)

Everything the clients do with a server answer lives here: parse the JSON document, check its
`api_version`, merge `/contigs` batches into a run, and render the bytes the command line and the
web page show. There is no I/O and no networking. The Python implementation is the specification
(`tests/data/*_golden.json` hold documents captured from the Python server and the exact strings
the Python renderers produced for them, see `scratch/step3b/generate_goldens.py`).

Rust API:

```rust
use sylphd_core::document::{check_api_version, ProfileDocument, AssemblyDocument, ContigsDocument,
    merge_contigs_run, ContigsRunMergeOptions, InfoDocument, SUPPORTED_API_VERSION};
use sylphd_core::render;

check_api_version(doc.api_version)?;            // "server API version N, this client supports M: update sylphd"
let profile = ProfileDocument::from_json_str(text)?;   // checks api_version first
let tsv = render::render_profile_tsv(&profile);        // == protocol.rows_to_tsv (sylph columns byte-identical)
let caveats = render::render_profile_caveats(&profile); // "sylphd: warning: ..." lines

let assembly = AssemblyDocument::from_json_str(text)?;
let hits = render::render_assembly_tsv(&assembly);      // cli._write_hits_tsv's 11 columns
let line = render::render_assembly_summary(&assembly);  // cli._assignment_summary
let err = render::render_assembly_stderr(&assembly);    // summary line + cli._species_note warning

let batches: Vec<ContigsDocument> = /* one per POST /contigs */;
let run = merge_contigs_run(&batches, &ContigsRunMergeOptions {
    skipped_short: SkippedBucket { contigs: 3, bp: 30_000 },   // client-side skip counters
    skipped_few_kmers: SkippedBucket { contigs: 7, bp: 20_000 },
    min_contig_length: 20_000,
    not_classified: None,                                       // browser-only 100-contig cap
    ..Default::default()
})?;                                                           // same shape as one /contigs response
let tsv = render::render_contigs_tsv(&run);             // PerContigRun.write_tsv
let summary = render::render_contigs_summary(&run);     // PerContigRun.print_summary (2 lines + caveats)

let info = InfoDocument::from_json_str(info_text)?;
let text = render::render_info(&info, &messages);        // sylphd info
```

`render_tsv(table)` renders a bare `{"columns", "data"}` table; the `Eff_lambda` cell takes its
`HIGH`/`LOW` value from the `Lambda_status` column, which is dropped from the TSV. Null cells get
sylph's spellings (`inf`, `NaN`, `NA-NA`, `""`); intervals print as `a-b`, `Containment_ind` as
`a/b`. `Adjusted_ANI` is capped at 100 in the TSV; `Naive_ANI` is not. Metadata and unknown
columns print Python's `str()` (`True`/`False`, `""` for `None`) and the three float-valued
metadata columns keep the `.0`.

The wasm exports mirror these functions and take/return JSON strings:

| export | |
|---|---|
| `supported_api_version()` | `1` |
| `check_api_version(version)` | throws with the "update sylphd" message |
| `render_tsv(docOrTableJson)` | profile/query document (or bare table) → TSV |
| `render_profile_caveats(docJson)` | warning lines |
| `render_assembly_tsv/summary/stderr(docJson)` | assembly hits TSV, one-line summary, summary+note |
| `merge_contigs_run(batchesJson, optionsJson?)` | run document JSON |
| `render_contigs_tsv/summary(docJson)` | per-contig TSV, two summary lines + caveats |
| `render_info(infoJson, messagesJson?)` | `sylphd info` text |

`web/sketching.js` exposes a main-thread instance (`SylphdSketch.loadWasm()`) that reuses the
compiled module of the sketching workers; the web app's downloads and the per-contig run merge go
through it, so the browser has no TSV/summary writers of its own.


## Equality with pysylph

`cargo test --release` (from `rust/`) sketches the inputs with `sylphd-core` natively
and runs `sylphd-core/tests/compare_pysylph.py` with the sylphd venv (`../.venv`, or `SYLPHD_PYTHON`).
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

`node sylphd-wasm/tests/node_check.mjs [--big]` (after `./sylphd-wasm/build.sh` and `cargo test`) loads the **wasm32
build** in node and compares its output byte for byte with the native sketches (32-bit
`usize`, hashing etc. all agree, including paired mode).
`sylphd-wasm/tests/gzip.rs`, and the parser tests in `sylphd-core/tests/pysylph_equality.rs`, cover chunk boundaries
(1-byte chunks), CRLF, missing final newline, multi-line FASTA, quality lines starting with `@`,
corrupt/truncated gzip and a few malformed inputs.
The sketches also pass `sylphd.sketchcheck` and `SampleSketch.from_bytes`, and the live server
profiles them.

### Per-contig parity

`cargo test` also has unit tests for the genome-style count against a naive
reference implementation in `src/contig.rs` (every k-mer window, canonical
hash, duplicate removal, greedy spacing), for the AVX2 position trimming and for
the streaming record split. `tests/test_web_contigs_js.py` (in the repository's
`tests/`, run with `uv run pytest tests/test_web_contigs_js.py`) then compares
the **built wasm** record by record with pysylph and with the CLI reference
`sylphd.sketching.sketch_contigs`:

| case | |
|---|---|
| random 3 kb, a block repeated twice, N runs, lower case, 10 bp, 20 bp, exactly k, a 4.8 kb tandem repeat | c=200 k=31 and c=50 k=21; chunk sizes 997/4093 so records are split at arbitrary bytes |
| a FASTQ file and a wrapped (multi-line) FASTA, 13/17-byte chunks | first-word names |
| the same file as a two-member gzip, through `WasmContigSketcher.push_gz` | |
| `scratch/taxonomy/phase4a/lambda.fasta` (48.5 kb, 212 genome-style k-mers) | when the file is present |
| a multi-record FASTA with duplicate and empty header ids, 2.2 Mbp record, gzipped | through `sketchContigs` and the real workers (node worker threads), checking batching, ack/backpressure and unique names/gc |

`contigKmers` is identical to `len(Sketcher.sketch_genome(...).kmers)` for every
record, and the sample sketch bytes are identical after sorting the k-mer
entries (the wasm always writes them sorted). Tandem repeats give
`contigKmers == 0` (every k-mer repeated); the sample sketch is still produced.


### Differences and things to know

* **Seeding differs between sylph builds.** sylph's AVX2 kernel on x86_64 splits each read into
  four segments and *drops the last `(n-k+1) mod 4` k-mers* (and reads of exactly `k` bases); the
  scalar code (aarch64, no AVX2) considers all k-mers, so pysylph's sketches already differ by
  CPU. Default here: `sylph::seeding::Seeding::Avx2Compat`, the portable scalar emulation of the
  AVX2 kernel (same k-mers and insertion order, bit-identical to pysylph on x86_64, which is what
  the server and most users run). `scalar_seeding = true` passes `Seeding::Native`, which on
  wasm32 is sylph's scalar kernel (pysylph on a CPU without AVX2). Statistically the two are
  equivalent (up to 3 k-mers per read at the ends differ).
* Entries are written sorted by k-mer; byte order otherwise as sylph's bincode layout (see
  `src/sylphd/sketchcheck.py`). `file_name` = the name passed to `finish`, `sample_name` = None.
* Same restrictions as `Sketcher`: `k` is 21 or 31; the paired path uses the default `fpr`
  (0.0001, seeded RNG 7), like `sketch_paired`'s default.
* Mate pairing is by record order, as in `sketch_paired`; read names are not compared.
* Only 4-line FASTQ (like dnaio); FASTA may be multi-line. Every record is a "read", FASTA contigs
  included (this is what `sketch_single` does with an assembly).
* Per-contig mode uses the same seeding choice for the sample sketch and the genome-style count
  (both go through sylph's `..._positions` functions, which drop the same AVX2 tail), a fixed
  `min_spacing` of 30 (constructor argument, matching `Sketcher`), and skips records that are empty
  or shorter than `min_length` exactly like `sketch_contigs`. A tandem repeat therefore reports
  `contigKmers == 0` while its sample sketch is still produced; `POST /contigs` refuses such a
  contig, which is what the CLI does too. The serialized inner `file_name` is the raw header name,
  not the unique request name (`sketchContigs` puts that into the request `params`).

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
* **Per-contig mode** (`sketchContigs`): the same decompress worker and a single port. The sketch
  worker posts the records it finished as a batch and returns that chunk's credit only when the
  main thread *acks* the batch, so at most the 4 in-flight chunks' records are queued; the main
  thread hands each record to `onContig` and drops it.
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
`https://sylphd.abrinca.com` (sylphd 0.1.1 at the time of the measurements, GTDB r232). Input
`FAM20866/reads.fastq.gz`: 1,550,645,339 bytes gzip, 4,295,587,662 bytes of FASTQ, 254,233
reads, 2.14 Gbp, mean read length 8.4 kb. Wasm built with `simd128`.

**Gunzip alone** (worker, nothing else running, with the former `bench.html`; node: `sylphd-wasm/tests/bench_gunzip.mjs`):

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
(`sylphd-wasm/tests/browser_check.mjs`, fixtures from `sylphd-wasm/tests/make_fixtures.py`).

Memory: peak RSS of the biggest Chromium process was ~0.8-1.1 GB in the previous sequential runs
(it includes the browser's stream buffers and file cache). Not re-measured for the pipeline;
what we control is small: 8 MB of chunks in flight and the ~40 MB sketcher heap.

`.wasm` = **164 KB** (with simd128, zlib-rs, per-contig mode; no `wasm-opt` run), glue JS 34 KB.
The per-contig API adds ~24 KB: it is the only part that builds JS objects (`js-sys`
`Object`/`Reflect`/`Uint8Array`) for the records, which was 142 KB before it.

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
* Not done: Safari, `wasm-opt`, re-measuring peak RSS for the pipeline.

## Integration into `web/`

Done on branch `wasm-sketch`: the web page has a **Reads** mode (`web/reads.js`; it charts the
server's `taxprof` clades) next to the **Assembly** mode.

* Build: `./sylphd-wasm/build.sh` writes the module and glue into `web/wasm/` (gitignored).
  `scripts/preview-pages.sh` and `.github/workflows/pages.yml` run it (Rust toolchain and
  `wasm-bindgen-cli` pinned there; the CLI is the prebuilt release binary, checked by sha256).
* One copy of the workers: `web/decompress-worker.js` / `web/sketch-worker.js` (module path
  `./wasm/`; the sketch worker also supports interleaved input). `sylphd-wasm/tests/browser_check.mjs` runs
  them through `web/sketching.js`.
* `c`/`k` come from `/info`; paired detection, memory guard (warn at 1.5 GB, abort at 3.6 GB of
  wasm memory), cancellation, `429`/`503`/network retry with the sketch kept in memory and the
  progress UI are in `web/reads.js`.
* The Assembly tab uses the same pipeline (`web/sketching.js`): single-end sketching of a pasted
  FASTA or a file, with the G/C count (`WasmSketcher.gc()`) for the GC% of the summary,
  and sends `application/vnd.sylph.sketch` to `POST /assembly`. Verified identical to the server
  sketching the same FASTA (hits, assignment, closest, records, length, GC).
* The per-contig pipeline (`SylphdSketch.sketchContigs`, `POST /contigs`) is the Assembly tab's
  **Per contig** mode in `web/contigs.js`; `tests/test_web_contigs_js.py` covers the sketching end
  to end (through the real workers on node worker threads) and
  `tests/test_web_contigs_download.py` the TSV/JSON downloads.
* Still open: Safari, `wasm-opt` in CI, keeping the equality tests (`cargo test`, needs the sylphd
  venv) in CI, a time/size warning for files over a few GB.
