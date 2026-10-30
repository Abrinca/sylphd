# Development

```bash
git clone https://github.com/Abrinca/sylphd
cd sylphd
uv sync
uv run pytest
```

The build needs a Rust toolchain; see [Install](../install.md).

## Tests

The tests use pysylph's small test data, copied into `tests/data`: E. coli databases (`ecoli.syl2db`
and `ecoli.syldb`), K-12 reads and `.sylsp` sketches. They include an in-process Starlette client,
and one real Unix-socket round trip against a `sylphd serve` subprocess. The large GTDB database is
only needed for manual runs.

| File | Covers |
|---|---|
| `test_server.py` | the Starlette app: endpoints, errors, metadata join, socket cleanup, concurrency |
| `test_web.py` | the public-web features: raw-sequence `/assembly`, rate limit, client identity, queue limit, CORS |
| `test_assembly_sketch.py` | `/assembly` with a sketch: same results as the raw path, works without `--allow-sequences`, malformed sketches, `sylphd assembly` with and without `--local-sketch` |
| `test_classify.py` | species assignment and hit relations for `/assembly` |
| `test_sketchcheck.py` | structural validation of uploaded sketches, including drift against a decoded sketch |
| `test_client.py` | client base URLs, address precedence, path prefixes |
| `test_cli.py` | the command line, including the Unix-socket round trip |
| `test_sketching.py` | inputs, sample names, `.sylsp` |
| `test_metadata.py`, `test_download.py` | `setup` and `download` |
| `test_metadata_taxonomy.py` | format 3 taxonomy tables: build, update, and the v2/v3 reader |
| `test_multi_db.py` | a pooled server equals `pysylph.Profiler(*databases)`, single-database goldens, the shared `Profiler` cache |
| `test_taxonomy.py` | `taxonomy.py`: profile aggregation, `.sylphmpa` and clade rendering |
| `test_taxonomy_resolve.py` | hit → lineage resolution: probe rules, precedence, `tax_*` rows |
| `test_taxprof_payload.py` | the server-side `taxprof` payload: text and round-trippable clades |
| `test_sylphtax_parity.py`, `test_taxmatrix_parity.py` | byte-for-byte parity with the real `sylph-tax` (`tax` profile, `merge`) |
| `test_assembly_per_contig.py` | `POST /contigs` and `sylphd assembly --per-contig` end to end |
| `test_about.py`, `test_deploy.py` | `--name`/`--about` in `/info`, and the `SYLPH_DB` rendering of the systemd unit |
| `test_web_about_browser.py`, `test_web_reads_taxprof.py` | the page in headless Chromium: the operator text from `/info`, and the Reads chart from the server's `taxprof` |
| `test_web_contigs_js.py`, `test_web_contigs_download.py` | browser per-contig sketching against pysylph, and its TSV/JSON against `sylphd assembly --per-contig` |
| `test_web_sylsp_js.py` | `readSylspHeader` against `sylphd.sketchcheck` |
| `web_golden.py` | helper (not a test): the single-database `/info` and `/assembly` goldens |

## Source layout

Two families of modules are split by role; the older module re-exports what tests and docs import.

| Module | Holds |
|---|---|
| `metadata.py` | the metadata file layout, accession and sylph-tax probe helpers, and the read side, `MetadataDB` (also re-exports the names below) |
| `metadata_build.py` | the writers behind `sylphd setup`: `build_metadata_db`, `update_metadata_db`, and the temp-file/replace-on-success context manager they share |
| `taxonomy_sources.py` | importing a sylph-tax file as a `taxonomy_source`: parsing, duplicate-key semantics, overlap counting and warnings |
| `catalog.py` | the static catalogues of pre-built databases and sylph-tax taxonomy files (data only) |
| `download.py` | catalogue lookups and tables, and the transfer engine |
| `cli_parser.py` | one `add_<command>_parser` per subcommand |

## Test data credit

The test data in `tests/data` comes from the test suite of
[pysylph](https://github.com/althonos/pysylph) (MIT licensed). The example genome of the web app
(`web/example/`) is *Mycoplasmoides genitalium* G37, NCBI RefSeq
[GCF_000027325.1](https://www.ncbi.nlm.nih.gov/datasets/genome/GCF_000027325.1/); NCBI data is in
the public domain.

## The web app

`web/` is a static page (plain HTML, CSS and JS, no JavaScript build step and no external requests)
with two equal modes selected by the address: the landing page (no hash), `#assembly` and `#reads`.

- **Assembly** talks to a `sylphd serve` instance through `GET /info` and `POST /assembly`. It
  takes a pasted or uploaded FASTA (plain or gzip), sketches it in the browser with the same
  WebAssembly module as the Reads mode (`sketching.js`) and sends the sketch
  (`application/vnd.sylph.sketch`; input up to 2 GB, no `--allow-sequences` needed). A browser that
  cannot run the sketcher gets a notice and a disabled submit button; the page has no raw-upload
  path. It shows the species assignment, or the closest
  relative, with an ANI gauge, and lists the closest genomes in a sortable table that can be
  downloaded as TSV or JSON (its species column says *Taxon* for a non-GTDB hit). With **Per
  contig** checked, `contigs.js` sketches every record and sends it to `POST /contigs` in batches
  of `limits.max_contigs`, then shows the per-contig table, composition card and consensus summary.
- **Reads** sketches reads in the browser (`reads.js`, the two workers `decompress-worker.js` and
  `sketch-worker.js`, and the WebAssembly module from `sketch-wasm/`) and sends only the sketch to
  `POST /profile`. The taxonomic clades are aggregated **on the server** (`sylphd/taxonomy.py`) and
  returned in the `taxprof` response; the page charts them and never ports the aggregation
  (`tests/test_web_reads_taxprof.py` renders them in a real browser). See
  [Profiling reads in the browser](../guides/profiling-in-browser.md).

The WebAssembly module is not committed: `sketch-wasm/build.sh ../web/wasm` builds it (Rust with the
`wasm32-unknown-unknown` target and `wasm-bindgen-cli` at the version pinned in
`sketch-wasm/Cargo.toml`), and the Pages workflow and `scripts/preview-pages.sh` do that.
See [Using the web app against your server](../guides/public-server.md#using-the-web-app-against-your-server)
for running it locally.

## This documentation

The docs are built with [Zensical](https://zensical.org/) from `docs/` and `zensical.toml`:

```bash
uvx zensical serve                    # live preview
uvx zensical build --clean --strict   # what CI runs: warnings fail the build
```

The GitHub Actions workflow `.github/workflows/pages.yml` builds the docs and deploys them
together with the web app to GitHub Pages: the web app at `/` and the documentation at `/docs/`.
