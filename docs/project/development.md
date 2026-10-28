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
| `test_web.py` | the public-web features: `/assembly`, rate limit, client identity, queue limit, CORS |
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

`web/` is a static page (plain HTML, CSS and JS, no build step and no external requests) that talks
to a `sylphd serve --allow-sequences` instance through `GET /info` and `POST /assembly`. It takes a
pasted or uploaded FASTA (plain or gzip, up to the server's `--max-sequence-mb`), shows the species
assignment, or the closest relative, with an ANI gauge, and lists the closest GTDB genomes in a
sortable table that can be downloaded as TSV or JSON. See
[Using the web app against your server](../guides/public-server.md#using-the-web-app-against-your-server)
for running it locally.

## This documentation

The docs are built with [Zensical](https://zensical.org/) from `docs/` and `zensical.toml`:

```bash
uvx zensical serve                    # live preview
uvx zensical build --clean --strict   # what CI runs: warnings fail the build
```

The GitHub Actions workflow `.github/workflows/pages.yml` builds the docs and deploys them
together with the web app to GitHub Pages: the web app at `/` and the documentation at `/docs/`.
