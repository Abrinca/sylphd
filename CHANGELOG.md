# Changelog

All notable changes to `sylphd` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Removed `--terms-url` and the `terms_url` field of `GET /info`.** Put terms of use into the
  `--about` text instead (plain text is fine).
- `sylphd serve`: `--name` and `--about` are checked once, before the database is loaded. The
  deployment scripts take the display name from `SYLPH_NAME` in `site.env`, so it can contain spaces.
- Sample names (explicit or derived from a file name) must not contain tabs, newlines or other
  control characters and are limited to 1024 characters; the server answers `400`, the client
  stops with an error.
- `sylphd taxprof`/`--taxmatrix`: an existing directory works as an output prefix without a trailing
  `/`; strain rows (`t__`) are always listed after the species rows (also in the web app).
- **Job admission.** `--max-queue` now limits jobs (samples), not requests: a request is admitted
  as a whole or refused with `503` before anything runs, the check happens before the upload body is
  read, and concurrent requests can no longer slip past a zero-length queue. A batch larger than
  `--max-jobs` + `--max-queue` is always refused.
- Unexpected profiling failures are logged on the server (without uploaded data); clients get a
  generic message. The multipart form is closed deterministically.
- The profiling jobs and `LoadedDatabase` moved from `server.py` to `profiling.py`; the argument
  parser moved from `cli.py` to `cli_parser.py`.
- `query`, `profile` and `assembly` check their output paths before sketching or contacting the server.
- `sketch-wasm/demo/` was removed: the web page in `web/` is the one copy of the sketching workers.
  `sketch-wasm/build.sh` builds into `web/wasm` by default.
- Deployment: `/srv/sylphd` and `releases/` are owned by root; the updater builds a release as the
  service user inside a directory it hands over and makes the finished release root-owned.
- **Terms of the public service:** regular, light academic use of the API from your own scripts is
  free too; heavy and commercial use need an agreement. The public instance accepts raw FASTA/FASTQ
  on `POST /assembly` (`--allow-sequences`), so the terms no longer say it is sketch-only; raw
  sequences sent through the API reach the server, are processed in memory and are not stored or
  logged. The shipped example unit shows `--allow-sequences` again; for other operators it stays
  optional. New curl example in the HTTP API reference.
- The metadata file has one format (the reader for the plain-table "format 1" was removed).
- The package version lives only in `src/sylphd/__init__.py`.
- The browser tests (XSS check of the operator text, `.sylsp` header parser, taxonomy parity) run in
  GitHub Actions (`web-tests.yml`) and fail there instead of being skipped.

### Fixed

- **uvicorn no longer rewrites the client address from `X-Forwarded-For`**, so `--trusted-proxy`
  decides whom to believe. Before, a local client could pick its own rate-limit identity.
- **Species assignment uses the unrounded ANI**: 94.996 is no longer called a species-level hit.
- `/query` and `/profile`: more than 4096 sketches is answered `400` (as documented now), not `413`.
- Downloads: a failed or interrupted `aria2c` transfer keeps its control file and is never resumed
  by length; an HTTP resume needs a `206` with a matching `Content-Range`; the MD5 is checked per
  mirror and a corrupt copy falls back to the next mirror; two downloads that would write the same
  file are rejected.
- `sylphd setup --force` builds the new database next to the old one and replaces it only on
  success; metadata paths containing `#` or `?` work; malformed TSV rows report their line number.
- Taxonomic profiles: sample names that sanitise to the same file name are rejected before
  anything is written. `sylphd download` reports `httpx` errors as ordinary errors.
- Web page: pasting text after choosing a file keeps the text; the file name is no longer sent to
  the server (it is only a label in the results); the Reads tab warns when the first read names of
  R1 and R2 differ; "no size limit" now says 2 GiB; the new logo is used and the stale
  `apple-touch-icon` link is gone.
- **Gzip.** The server and the client read concatenated gzip members (`cat a.gz b.gz`) as the browser
  does, and reject truncated streams with a clear error. The browser sketcher rejects gzip headers
  over 64 KiB and an out-of-range `chunk_size`.
- Browser, paired reads: memory stays bounded when R1 and R2 have different read lengths (the
  sketcher is fed the mate that is behind in records). GC of an assembly is counted by the sketcher,
  so FASTA comments and blank lines no longer distort it. Read names with `_R1_001` are recognised
  by `sylphd` as well, as in the web page.
- Docs: `--max-queue`, the memory use of an upload and the sketch-size numbers were corrected.
- Docs: `--allow-sequences` is optional (the web page and the shipped unit do not need it), the
  containment denominator is the reference genome's k-mer count, and several other statements that no
  longer matched the code were corrected.

## [0.1.3] - 2026-10-29

### Added

- **Server description.** `sylphd serve --about FILE` (UTF-8 Markdown, at most 16 KiB, re-read when
  the file changes) and `--name TEXT` (at most 80 characters) are reported by `GET /info` as `name`
  and `about` (`{"format": "markdown", "text": ...}`), absent when not set. The web page replaces
  the lone status pill with a server panel: status, name, address, *Change server*, the fact chips
  and the operator's text under *From the server operator*, rendered with the vendored
  [marked](https://marked.js.org/) and [DOMPurify](https://github.com/cure53/DOMPurify) (an
  allowlist: raw HTML, images and tables are not rendered, only http, https and mailto links) and
  marked as unverified when you are connected to a server other than the default.
- **Web page: upload a ready-made sketch.** The Reads tab accepts one `.sylsp` instead of reads
  (no sketching: the step is shown as skipped), and the Assembly tab sends a dropped `.sylsp` to
  `POST /assembly` as it is, marked *uploaded sketch*. The page reads the sketch header and
  refuses a `c` or `k` that does not fit the server before uploading anything.
- Header chip *Upload* with the server's `max_upload_mb`, the largest sketch it accepts, visible on
  every view; the other chips explain themselves in their tooltips.

## [0.1.2] - 2026-10-29

### Added

- **Web page: Reads mode.** The page now has two equal modes, *Assembly* and *Reads* (hash routes
  `#assembly` and `#reads`, a landing page and a tab bar). In Reads mode FASTQ/FASTA reads (plain
  or gzip; single-end, paired or interleaved) are sketched in the browser with a WebAssembly build
  of the sylph sketcher (`sketch-wasm/`) and only the sketch is uploaded to `POST /profile`. The
  results show an abundance chart per rank (GTDB or NCBI taxonomy), a sortable genome table and
  downloads (TSV, JSON, `.sylphmpa`, `.sylsp`). See *Profiling reads in the browser* in the docs.
- The Pages workflow and `scripts/preview-pages.sh` build the WebAssembly sketcher into
  `web/wasm/` (not committed).
- **Assemblies are sketched locally too.** `POST /assembly` accepts a precomputed sketch
  (`Content-Type: application/vnd.sylph.sketch`, the bytes of a `.sylsp`), validated like
  `/profile` sketches and bounded by `--max-upload-mb`, with optional informational `records`,
  `length` and `gc` query parameters. It always works, so a server started without
  `--allow-sequences` can never receive a sequence (the public instance runs that way). The
  response gains `query.input` (`sketch` or `sequence`).
- **Web page, Assembly mode**: sketches in the browser (any size, no 20 MB limit) and sends only the
  sketch, with a *sketched locally* badge on the result. There is no raw-upload path in the page:
  a browser that cannot run the sketcher gets a notice (the public instance is sketch-only).
- **`sylphd assembly`** sketches locally by default; `--no-local-sketch` uploads the raw sequence
  to servers started with `--allow-sequences`.

### Changed

- `--allow-sequences` now only gates *raw sequence* uploads to `/assembly`; the endpoint itself is
  always served (`/info` lists `/assembly` in `endpoints`). Without the flag a raw upload is still a
  404, now with an explanatory message. `--max-sequence-mb` applies to raw sequences only.
- The rate limit covers `/assembly` on every server.

## [0.1.1] - 2026-10-28

### Added

- **`sylphd download --database NAME`** fetches pre-built sylph databases from a built-in
  catalogue (`--list-databases`; GTDB r214–r232, GlobDB, UHGG, UHGV, IMG/VR, OceanDNA, SMAG,
  RefSeq fungi) using all known mirrors. Uses `aria2c` when installed (parallel, multi-mirror,
  resumable), otherwise a built-in resumable downloader. Checks the file size by default;
  `--verify` also checks the MD5. `--url` adds or replaces sources.
- **Deployment bundle** (`deploy/`): `setup-server.sh` (Caddy from the official COPR,
  dnf-automatic, uv with a self-update timer, SELinux policy for the Caddy → sylphd socket),
  `update.sh` (installs a GitHub release with checksum verification, health check and
  automatic rollback), a hardened `sylphd.service` and a Caddyfile template.
- **Docs**: "What does same species mean?" background page on the 95 % ANI species call, linked
  from the web page.

### Changed

- **Web page**: the ANI gauge fills up to the value with zone colours (red and striped below
  85 %, orange, green from 95 %), uses a stretched scale for the high range, and marks the
  value with a clearly visible knob. The page now points at the public server.

### Fixed

- `deploy/sylphd.service`: `Environment=` values with spaces are quoted, and variables inside
  paths are braced, so systemd passes all flags and finds the database.

## [0.1.0] - 2026-10-28

The first release. `sylphd` is a long-running [sylph](https://github.com/bluenote-1577/sylph)
server with a sketching client: the database is loaded once and shared, clients send sketches.

### Added

- **`sylphd serve`**: loads a `.syl2db` (two-stage) or `.syldb` database into memory and serves
  query/profile/assembly requests over a Unix socket (`$XDG_RUNTIME_DIR/sylphd.sock`, mode `0660`)
  or TCP. Optional GTDB metadata SQLite, upload limits, rate limit, request queue, trusted-proxy
  handling, CORS and an opt-in endpoint for raw sequences (`--allow-sequences`).
- **Client commands** that sketch locally and send only sketches: `sylphd info`, `sylphd query`
  (ANI hits) and `sylphd profile` (taxonomic abundances), for single-end, paired, interleaved and
  multi-file samples, or ready-made `.sylsp` sketches.
- **Output formats**: sylph's TSV columns plus `accession` and metadata columns, JSON, and
  `sylph-tax`-style `.sylphmpa` profiles, clade × sample matrices and sequence matrices.
- **`sylphd assembly`**: identifies a FASTA assembly against the database, with a species
  assignment at 95 % ANI and the lineage relation of every hit.
- **`sylphd download` and `sylphd setup`**: fetch GTDB metadata (checksums verified) and build the
  SQLite metadata database.
- **Web app** (<https://abrinca.github.io/sylphd/>): paste an assembly, get its closest GTDB
  genomes.
- **Release wheels** (see
  [Install from a release](https://abrinca.github.io/sylphd/docs/install/#from-a-release-no-rust-needed))
  for Linux x86_64 and aarch64, CPython 3.14, so a production server can install without a Rust
  toolchain.

[0.1.3]: https://github.com/Abrinca/sylphd/releases/tag/v0.1.3
[0.1.2]: https://github.com/Abrinca/sylphd/releases/tag/v0.1.2
[0.1.1]: https://github.com/Abrinca/sylphd/releases/tag/v0.1.1
[0.1.0]: https://github.com/Abrinca/sylphd/releases/tag/v0.1.0
