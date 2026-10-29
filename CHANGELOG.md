# Changelog

All notable changes to `sylphd` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.2]: https://github.com/Abrinca/sylphd/releases/tag/v0.1.2
[0.1.1]: https://github.com/Abrinca/sylphd/releases/tag/v0.1.1
[0.1.0]: https://github.com/Abrinca/sylphd/releases/tag/v0.1.0
