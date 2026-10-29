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
| `test_taxonomy.py` | `--taxprof` and `--taxmatrix` |

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
  (`application/vnd.sylph.sketch`; no size limit, no `--allow-sequences` needed). A browser that
  cannot run the sketcher gets a notice and a disabled submit button; the page has no raw-upload
  path. It shows the species assignment, or the closest
  relative, with an ANI gauge, and lists the closest GTDB genomes in a sortable table that can be
  downloaded as TSV or JSON.
- **Reads** sketches reads in the browser (`reads.js`, the two workers `decompress-worker.js` and
  `sketch-worker.js`, and the WebAssembly module from `sketch-wasm/`) and sends only the sketch to
  `POST /profile`. `taxonomy.js` ports the lineage rules of `sylphd/taxonomy.py` (tested against it
  in `tests/test_web_taxonomy_js.py`). See [Profiling reads in the browser](../guides/profiling-in-browser.md).

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
