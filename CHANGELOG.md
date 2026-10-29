# Changelog

All notable changes to `sylphd` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Taxonomy sources in the metadata file (format version 3).** `sylphd setup --taxonomy SPEC`
  imports [sylph-tax](https://github.com/bluenote-1577/sylph-tax) files (`key<TAB>lineage[<TAB>host]`,
  gzip or plain, header rows allowed) next to the GTDB metadata, so a server can later give a
  lineage to hits from UHGV, fungal RefSeq, MAG and GlobDB databases. `SPEC` is a sylph-tax name
  (`UHGV_default`, `UHGV_ictv`, `FungiRefSeq-latest`, `GTDB_r232`, `GlobDB_r232`, ...), `NAME=PATH`
  or `PATH`; a bare name is downloaded into `--cache-dir` (default `<output dir>/taxonomy`) with
  the existing resume/verify engine. `setup -m` is now optional, so a file can hold taxonomy
  sources alone. Version 3 is **purely additive**: new `taxonomy_source`, `taxonomy_entry`,
  `dict_tax_lineage` and `dict_tax_host` tables; the `metadata` view and its columns are untouched,
  version 2 files keep loading and serving exactly as before, and a version 2 reader opens a
  version 3 file (a test runs a frozen copy of the previous release's reader, taken from commit
  `e72b807`, against one). The full GTDB metadata is registered as one *virtual* source
  `GTDB_r<release>` over `metadata_data`, so no lineage is stored twice; the sylph-tax catalogue
  file of that *same* release is skipped with a message when `-m` already imported it (a different
  release, e.g. `GTDB_r214` next to r232, and a local file are imported normally). Keys are stored
  verbatim (case sensitive, as sylph-tax does), lineages and hosts are dictionary-encoded, sources
  keep the `setup` order (`ord`), and a key repeated within a source keeps its last lineage while a
  host is only replaced by a row that has one — as in sylph-tax.
- **`sylphd setup --update`** adds sources to an existing metadata file and doubles as the
  version 2 → 3 migration (no GTDB TSV files needed). It copies the file next to the output in a
  uniquely named temporary file (safe with parallel runs and with a source called `*.tmp`), adds
  the tables/sources and replaces the output only on success — an interrupted update leaves the old
  file untouched, like `setup --force` — and keeps the permissions of the file it replaces.
  Re-importing a source name replaces it and keeps its
  position.
- **`sylphd download --taxonomy NAME`** and **`--list-taxonomies`** add a second catalogue next to
  the database one: sylph-tax's files with sizes and MD5 sums read from the Zenodo API (GlobDB r232
  on the university fileshare has no published MD5, so only its size is checked). Every database in
  the catalogue now names the taxonomy file it goes with (`Database.taxonomy`, plus UHGV's
  `UHGV_ictv` alternative), and the taxonomy table lists the paired database. Taxonomy files are
  small, so their checksum is always verified (no `--verify` switch); if a catalogue entry lacks
  size/MD5, the Zenodo API is asked at download time, and an entry that still has no MD5 is refused
  rather than accepted on its size (GlobDB r232, whose publisher publishes no checksum, is the one
  size-only entry).
- **`/info` and `sylphd info` report the taxonomy sources** of a format 3 metadata file: name,
  kind, origin, MD5, key count and whether hosts are present (`metadata.taxonomy_sources`), plus
  `metadata.sylphtax_rules`, the sylph-tax version whose file formats and rules the build
  implements (`1.9.2`). Both keys are absent for a format 2 file, so existing output is unchanged.
- **Several databases in one server.** `sylphd serve --db [NAME=]PATH` is repeatable; `NAME`
  (letters, digits, `_`, `.`, `-`) is the name `/info` reports and defaults to the file name, and
  duplicate names are refused. The databases are loaded in parallel threads and profiled as one
  pool, exactly like `sylph profile db1 db2 sample`: `/profile`, `/query` and `/assembly` run
  against all of them, so genomes of all databases compete in the reassignment and abundances add
  up to 100 %. All databases must share `k` (a mismatch is refused at startup, naming the
  databases and their `k`); `c` may differ, and samples are then sketched at the smallest `c`
  (logged as a warning because sketches get bigger). Any mix of `.syl2db` and `.syldb` works.
  `GET /info` keeps `database` as the **pooled view** (`name`/`path` joined, `size`/`genomes`
  summed, `c` the minimum, `screen_c`/`two_stage` only when all agree) so existing clients and the
  web app are unchanged, and adds `databases`: one object per database with `name`, `file`,
  `path`, `size`, `sha256`, `genomes`, `c`, `k`, `screen_c` and `two_stage`. Rows and `/assembly`
  hits carry no per-database field: the taxonomy says where a hit came from. `sylphd info` lists
  the databases when there is more than one. A sketch with `k` other than the common `k`, or a `c`
  above the pool's minimum, is a `400` that names the offending database.
- **A plain `.syldb` reports the smallest `c` of its genomes** (they may differ in `c`), and a
  plain database whose genomes were sketched with different `k` is refused at startup with the
  database named, instead of advertising the first genome's values and failing on the first
  request with a genome-named error. The scan reads only `c`/`k` (measured: +44 ms for 168,536
  genomes, 0 ms for 661); `.syl2db` files are not scanned.
- **Cached `Profiler` objects.** One `pysylph.Profiler` per parameter set is kept (an LRU of 8) and
  shared by all worker threads, instead of building one per request. With two or more databases the
  constructor scans every genome for duplicates (measured: 93 ms for the target trio's 369 k
  genomes), so without the cache every request would pay that; the default profiler is built at
  startup. `pysylph.Profiler` is read-only and safe to share (its `query`/`profile` take `&self` and
  release the GIL), so the lock only guards the cache dictionary. pysylph's duplicate-genome
  `UserWarning` (raised by the constructor) is captured and logged once as a bounded summary
  (count and the first names) instead of vanishing with the first construction.
- **`sylphd profile --estimate-read-counts`** (sylph's experimental `--estimate-read-counts`):
  `Sequence_abundance` then holds the estimated number of reads per genome instead of a relative
  abundance. It implies `--estimate-unknown`, so the coverage column is labelled `True_cov` as
  with `-u`. The `estimate_read_counts` key is also accepted in the `/profile` `params` (and, like
  `estimate_unknown`, forwarded unchanged by `/query`). Needs pysylph `f8f45a1` (the `uv.lock` pin
  moved from `f32d840`).
- **`sylphd profile --taxmatrix-value ani|coverage`** writes the strain-level `ANI (if
  strain-level)` and `Coverage (if strain-level)` columns of the `.sylphmpa` as a matrix, like
  `sylph-tax merge --column ANI|Coverage`: only clades with a value in at least one sample appear
  (`dropna(how="all")`) and a clade missing from a sample gets `0`.
- **`sylphd assembly --per-contig`** sketches every FASTA/FASTQ record as its own sample and
  reports the closest genomes per contig, for MAG contamination checks. Each contig gets a
  **contig-relative ANI**, `100 * (shared / contig_kmers) ** (1/k)` (`shared` from the server's
  `Containment_ind`, `contig_kmers` from a genome-style sketch of the contig), so an exact 20 kb
  fragment of a 5 Mb genome reports 100 % instead of ~85 %. The 95 % species assignment and GTDB
  relations use that value, and `--min-ani` filters on it client-side. The table adds `contig`,
  `contig_length`, `gc`, `status`, `shared_kmers`, `contig_kmers`, `explained_fraction` (the
  fraction of the contig the hit explains), `reference_ani` (sylph's genome-denominator ANI, kept
  for continuity) and `gtdb_species`. `--top` (default `1`) and `-o` work in this mode; `--fields`
  selects the metadata columns added per hit and is accepted *only* with `--per-contig` (the
  single-assembly table has fixed columns); `--json` emits a per-contig document. A summary on
  stderr gives the bases per consensus taxon.
- **Taxonomic consensus per contig.** Like CAT/BAT, the command reduces the hits to the best one per
  GTDB species and takes the lowest common rank of the species whose divergence `d = 100 - ANI`
  satisfies `d <= R * d_best + Z * sqrt(sd_best^2 + sd_i^2)`, with the binomial ANI noise
  `sd = (100/k) * sqrt((1-p) / (n*p))`, `p = shared/contig_kmers`, `n = contig_kmers`. This keeps a
  near-exact, well-covered contig at species while a short or noisy one widens to genus or family
  instead of picking one of several indistinguishable species. A species consensus also requires the
  best hit to reach the 95 % species threshold; below it the answer is truncated to genus, so a
  distant contig is not labelled with the species that merely happens to be the closest one. The
  table adds `n_close` (number of close species), `consensus_rank` and `consensus_taxon` after
  `gtdb_species`; the JSON adds a `consensus` object (`rank`, `taxon`, `n_close`, `species`).
  Defaults `R = 1.5`, `Z = 2`, settable with `--consensus-ratio R` and `--consensus-z Z`
  (per-contig only, rejected elsewhere).
- `--min-contig-length BP` (default `20000`) skips short contigs; they are still listed with
  `status` `too_short`. A contig with fewer k-mers than sylph's `min_number_kmers` is reported as
  `no_hit` instead of a spurious match. Duplicate record ids get a deterministic `_2`, `_3` suffix.
- `--quiet` suppresses the (new or existing) stderr summary of `sylphd assembly`.

- **Hit → lineage resolution and the merged `tax` profile.** Every row of `/query`, `/profile`
  and `/assembly` now carries `tax_source`, `tax_key`, `tax_lineage` and `tax_host` when the
  server's metadata file has sylph-tax taxonomy sources (format 3); the columns are listed in
  `metadata.columns` and can be selected with `--fields`. `MetadataDB.resolve` maps a hit's
  `Genome_file`/`Contig_name` to a key exactly as `sylph-to-taxprof` v1.9.2 does: two passes of
  the probes `gf`, `gf + ".gz"`, `glob`, `contig` and the `.fa`/`.fasta`/`.fna` splits, pass 1
  overriding pass 0, keys compared verbatim (UHGV's contig ids and GlobDB's version-stripped keys
  need exactly that). For a key in several sources the **source imported later wins** (the larger
  `ord`, exactly like passing the files later to `sylph-tax -t`): the lineage comes from the
  largest `ord`, and the host from the largest `ord` that has one for that key — so the lineage and
  the host can come from two different files, like sylph-tax's two dictionaries.
  `--tax-source` picks the sources per request when that is not what you want. The keys are looked
  up in batches of 500 unique probe keys (both passes combined) per `IN` query over
  `taxonomy_entry`, plus the same batched lookups over the virtual GTDB rows when the file has one;
  for a typical sample that is one query per 500 keys, not per hit. A hit without any taxonomy has
  `null` in all four columns and never breaks a request. On such a server the default `--fields
  all` TSV appends the four `tax_*` columns after the existing metadata columns (their order is
  unchanged; an explicit `--fields` list is unchanged).
- **`sylphd profile --taxonomy tax`** writes one merged, **sylph-tax-exact** profile per sample
  (`.tax.sylphmpa`) and, with `--taxmatrix`, `tax.tsv`: opaque lineages (no rank parsing), an empty
  token to `UNKNOWN`, `NO_TAXONOMY;t__<file>:<contig>` for unresolved hits, `t__<key>` as the last
  level, rows ordered by number of levels and then by decreasing abundance, and
  `Taxonomies_used:['GTDB_r232', 'UHGV_default']` naming the sources in `setup` order.
  `gtdb` and `ncbi` keep sylphd's own normalisation. `tax` joins the default `--taxonomy` when the
  server has taxonomy sources, so the default `--taxprof` then writes **three** files per sample
  (`.gtdb`, `.ncbi`, `.tax`) instead of two; `--taxonomy gtdb,ncbi` restores the old set.
  `--tax-source NAME` (repeatable) restricts the profile to some of them — server-side, as a new
  optional `params.tax_sources` request field, so a key found only in an excluded source is a miss;
  it is the way to choose between sources that share keys (e.g. `UHGV_default` versus
  `UHGV_ictv`, where the later-imported source otherwise wins). Parity: synthetic goldens covering
  every probe rule — including an empty key, truly empty lineage tokens, a header row, hosts
  resolved across sources (checked against a real `taxprof -a` run), an equal-abundance tie and
  repeated strain rows — are committed and always compared byte for byte, for all four
  `--taxmatrix-value`s and the one-sample path; the same comparisons rerun against the real
  sylph-tax when it is installed.
- **Overlapping taxonomy sources are counted and warned about.** Importing one taxonomy per
  database is the rule, but `setup` (build and `--update`) no longer accepts a silent overlap: when
  a source is imported it counts in SQL the keys it shares with the sources already in the file —
  the virtual GTDB source included, compared on `metadata_data.accession` as `resolve` does — and
  prints one warning per pair, naming both sources, the number of shared keys, the first five keys
  and what happens ("the source imported later (NAME) wins for these keys; import only one of them,
  or rebuild without one"). The warning goes to the logger *and* to stderr, because `setup`
  configures no logging and `-q` silences progress only; the import still succeeds. The counts are
  stored in the new, additive `taxonomy_overlap` table (both directions, format stays 3; a file
  written before the table existed still reads, and the next `--update` fills it) and are shown as
  `overlaps: {"<other source>": <keys>}` per source by `sylphd info` and `GET /info`.

- **`sylphd assembly` classifies hits from any taxonomy source, not only GTDB.** Lineages are
  parsed generically: rank prefixes `d` domain, `r` realm, `k` kingdom, `p` phylum, `c` class,
  `o` order, `f` family, `g` genus, `s` species, `t` strain, and `level 1` ... `level N` for the
  prefix-less UHGV and ICTV lineages (`root;Monodnaviria;...` loses the root). The 95 % species
  call applies to **every** source, and `assignment.taxon` is the last named rank of the hit's
  lineage (a GTDB species, a vOTU, a fungal species). `assignment`, `closest` and every hit gain
  the additive `taxonomy` object (`{rank: taxon}`, shallowest first) and `taxonomy_source`, absent
  only when the hit has no lineage, so a metadata-less response keeps its shape and the
  single-database `/assembly` golden is unchanged. Relations are computed **within one taxonomy
  source**: a hit of another source is `different source` (or `different source from closest
  relative`) with `shared_rank: null`, while GTDB hits always compare with each other. The CLI
  table adds `taxon` and `taxonomy_source` columns.
- **The 95 % caveat outside prokaryotes.** When the best hit is not from GTDB, `assignment` (or
  `closest` without an assignment) carries a `note` and the CLI prints one `sylphd: warning:` line
  on stderr (suppressed by `--quiet`, never in `--json`): the vOTU convention for viruses (MIUViG's
  95 % ANI over 85 % of the shorter genome, which sylph's ANI does not check), "no established ANI
  boundary, a heuristic" for fungi, a generic text for other sources. `assembly --per-contig` and
  `profile --taxprof`/`--taxmatrix` with `--taxonomy tax` print each caveat once per output.
- **Per-contig consensus outside GTDB.** The consensus parses lineages generically and reports the
  rank's display name (`level 4` for a UHGV vSUBGEN); the sub-95 % cap moves one rank up from the
  species-equivalent rank (GTDB species -> genus, a prefix-less vOTU -> its parent level). A close
  set spanning two taxonomy sources has no consensus (`mixed_sources: true` in JSON, empty
  `consensus_rank`/`consensus_taxon` in the table). The table adds a `taxon` column.

### Changed

- `sylphd serve` logs each database it loads (name, kind, genomes, `c`, `k`, load time) and the
  total load time, plus the `Profiler` construction time, on the `sylphd` logger at the
  `--log-level`; a differing `c` between databases is a warning. `ServerConfig` gains a
  keyword-only `db_paths` (the positional `ServerConfig(db_path, meta_path, ...)` still works for
  one database; giving both is an error), and `create_app` takes a `DatabaseSet`.
- `sylphd profile --taxmatrix` now follows `sylph-tax merge` row for row: with two or more
  samples the clade names are sorted lexicographically (one sample keeps the `.sylphmpa` order,
  which is rank by rank, then by abundance), and a missing value is `0`. Before, rows were in
  first-seen order, which `docs/guides/taxonomic-profiles.md` wrongly described as sylph-tax's
  order; the guide and `docs/commands/profile.md` are corrected. Verified byte for byte against
  `sylph-tax merge` for all four `--column` values; a golden and a parity test now guard it.
- Per-contig mode batches contig sketches into `POST /query` requests with `no_adjust`,
  `minimum_ani=0`, `screen_ani=0` and `min_contain=3`, chunked by the server's advertised limits
  (`max_sketches`, `max_jobs` + `max_queue`) and by the *measured* request size and JSON `params`
  size, so a highly fragmented assembly (or one with very long record ids) works against a server
  with default limits.
- `sylphd assembly --json` honours `-o/--output` (it always wrote to stdout before) and still
  writes nothing to stderr, in per-contig mode too; the stderr summary exists only in table mode
  and is now aggregated by consensus taxon and rank instead of by top-hit species.

### Documented

- Multi-database serving: `docs/commands/serve.md` (repeatable `--db`, `NAME=PATH` aliases, the
  `k`/`c` rules, parallel loading, lower `--max-jobs` because each sample scans the plain
  databases), `docs/commands/info.md` (pooled values and the per-database list),
  `docs/reference/http-api.md` (`databases`, the pooled `database` object, the 400 that names the
  limiting database), and `docs/project/design-notes.md` (the cached `Profiler` replaced "a new
  `Profiler` per request"). The design note now carries the measured load times, memory, profiler
  construction and per-sample plain-scan cost of the target GTDB + UHGV + fungi trio.
- Per-contig docs state that `no_hit` can be a two-stage-screen miss rather than proof of absence
  (measured on a real *Leptospira* assembly: 30 of 236 contigs with a genuine 98-100 % match were
  dropped, including one at 22.9 kb), how to read `screen_c` from `sylphd info` / `GET /info`, that
  reported species are GTDB names and may be placeholders (K-12 MG1655 is `G047199095 sp047199095`
  in r232; `--fields ncbi_organism_name` gives the NCBI name), and how the consensus resolves
  99.9 vs 99.1 % to species and 89.9 vs 89.1 % to genus, with a resolution limit below ~90-92 %
  ANI. Chimeric contigs are not detected.

### Fixed

- A `ValueError` from pysylph while profiling (a bad `k`/`c`, for example a database genome denser
  than the sample sketch, which sylph treats as fatal) now becomes an HTTP **400** with pysylph's
  message instead of a 500. Only the `Profiler` constructor and the `profile`/`query` call are
  translated, so unrelated internal errors still surface as a 500.
- The `download` catalogue gave `uhgv_c200_dbv1.syldb` **171,338** genomes; the published file holds
  **168,536** (checked with `len(pysylph.Database)`; its MD5 matches the catalogue entry). The c100
  entry was left unchanged because there is no local copy to check it against.

## [0.2.0] - 2026-10-29

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
- The server counts a sketch's k-mers with pysylph's new `n_kmers` instead of building a Python dict; the
  pysylph pin moves to the fork commit that adds it.
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

[0.2.0]: https://github.com/Abrinca/sylphd/releases/tag/v0.2.0
[0.1.3]: https://github.com/Abrinca/sylphd/releases/tag/v0.1.3
[0.1.2]: https://github.com/Abrinca/sylphd/releases/tag/v0.1.2
[0.1.1]: https://github.com/Abrinca/sylphd/releases/tag/v0.1.1
[0.1.0]: https://github.com/Abrinca/sylphd/releases/tag/v0.1.0
