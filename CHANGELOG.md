# Changelog

All notable changes to `sylphd` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.1] - 2026-10-30

### Added

- **Server observability.** Every response carries `X-Request-Id`, and `sylphd serve` logs one
  line per request (method, path, status, elapsed ms, client identity, request id) instead of
  uvicorn's access log. `/health` gains `jobs_running` and `jobs_queued`.
- **Gzip.** JSON responses of 1 KB or more are gzip-compressed when the client sends
  `Accept-Encoding: gzip`.
- **`--retries N`** for the commands that talk to a server: 429 and 503 answers are retried after
  `Retry-After` (exponential backoff, capped at 30 s). Default 0, the old behaviour.

### Changed

- **Metadata files older than format 3 are no longer read or updated.** `MetadataDB` and
  `setup --update` refuse them with a message to rebuild with `sylphd setup`; the format 2 reader
  fallbacks, the 2 → 3 migration and the `taxonomy_overlap` backfill are gone.
- **Web: one set of shared helpers for all three modes** (sortable table, segmented controls,
  bar rows, formatters, lineage/rank tables, file names). The per-contig table gains the mobile
  sort control; byte sizes are decimal (kB/MB/GB) everywhere and the in-browser input cap is
  2 GB; downloads are named `sylphd_<file>_<assembly|contigs|reads>.<ext>`; JSON downloads are
  pretty-printed; the per-contig scale and the reads taxonomy/rank/abundance choices are
  remembered; per-contig progress counts a batch once it has been classified.
- `server.py` refuses requests through one `RequestError` path and maps job failures in one place;
  response bodies and status codes are unchanged. A non-integer `Content-Length` on `/assembly`
  is a 400.
- Clients and the web page no longer fall back for servers older than 0.3.0: profiles need the
  server's `taxprof`, and `/contigs` is assumed to exist.
- **Web per-contig results follow the Reads and single-assembly look.** A Summary stat grid; a
  Composition card with one stacked-bar segment per queried contig coloured by consensus taxon
  (√length "Compressed" or linear scale), striped aggregate segments and a "contigs not shown"
  pill for skipped contigs, and legend rows that filter the table; a contig table with length and
  ANI bars, GC difference from the assembly mean, a rank chip, and accession links (GTDB-lineage
  hits link to GTDB whatever their source is named).

### Fixed

- Docs and the `--min-contig-length` help: skipped contigs are counted, not listed; stale
  statements updated.

## [0.3.0] - 2026-10-30

### Added

- **Taxonomic profiles are computed on the server.** `POST /profile` accepts
  `params.taxprof: ["gtdb", "ncbi", "tax"]` (validated against the metadata file's columns and
  sylph-tax sources, and advertised in `/info` under `/profile`) and every returned sample then
  carries `taxprof[taxonomy] = {columns, taxonomies_used, clades, sylphmpa}`: the aggregated clade
  table (`clade_name`, `relative_abundance`, `sequence_abundance`, `ani`/`coverage` on strain
  rows) **and** the exact `.sylphmpa` text, so neither the CLI nor the web page formats or
  aggregates anything. The logic exists once, in `sylphd/taxonomy.py`; `sylphd profile
  --taxprof/--taxmatrix` writes the server's text/clades (the matrix remains a trivial
  clades-by-sample rendering), and the web Reads tab charts the server's clades, including the
  `tax` profile of UHGV/fungi hits. `web/taxonomy.js` and its JavaScript parity tests are gone.
- **`/info` says what each database is and which taxonomy it really uses.** Every entry of
  `databases` gains `kind` (`two-stage` for a `.syl2db`, `plain` for a `.syldb`) and `gtdb`
  (`true` when a sample of up to 100 genome names of that database are GTDB accessions of the
  metadata file), so the web panel shows `GTDB r<release>` only when a GTDB database is actually
  served — a fungi-only or UHGV server with GTDB metadata for taxonomy lookups no longer claims
  to be GTDB r232. `metadata.taxonomy_sources[].rows` is renamed to `keys`, and overlapping
  sources are reported once per pair in `metadata.taxonomy_overlaps` (`{"sources": [a, b],
  "keys": n, "examples": [...]}`, in setup order, the later source wins) instead of a
  both-direction `overlaps` dict on every source; `sylphd info` and the web panel follow.
- **A hit's display identifier.** Every `/assembly` and `/contigs` hit gains `genome_id`: the
  accession for a GTDB hit (or any hit that has one), the `tax_key` for a hit a non-GTDB taxonomy
  source resolved, else the first contig id, else the genome file. The web result card/table and
  the `sylphd assembly` tables use it, so a UHGV hit no longer says "Assigned from
  votus_full.fna.gz" (the file every UHGV genome shares). The species-threshold caveats now name
  the actual source (`UHGV_ictv (viruses): ...`, `FungiRefSeq-latest (fungi): ...`), and the web
  relation legend lists the ranks of the hit's own lineage instead of GTDB's ranks for a UHGV or
  fungal lineage.
- **`/info` advertises the `params` each POST endpoint accepts**: `params: {"/profile": [...],
  "/query": [...], "/contigs": [...], "/assembly": [...]}`. The lists come from the validators
  (`ProfileParams` fields plus `names`/`fields`/`mode`/`tax_sources`, `per_contig.PARAM_KEYS`, the
  `/assembly` query-string keys), not a second hand-maintained copy, so a client can hide an
  option an older server does not know instead of probing it with a 400. `/profile` and `/query`
  responses echo the **effective** profiling parameters (`params`, defaults included), as
  `/assembly` already does.

- Web page: the server panel lists the databases of a multi-database server and the taxonomy
  sources (with a warning when sources overlap); the Reads tab has an "Estimate read counts"
  option (reads in the table and the TSV, as `sylphd profile --estimate-read-counts`); non-GTDB
  hits show their generic taxon under a "Taxon" column, and non-GTDB assignments show the
  server's species-threshold note. The option is hidden when `/info` says the server does not
  accept `estimate_read_counts` (an older server's 400 still explains itself).

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
  kind, origin, MD5, key count (`keys`) and whether hosts are present (`metadata.taxonomy_sources`),
  the shared-key pairs (`metadata.taxonomy_overlaps`), plus `metadata.sylphtax_rules`, the
  sylph-tax version whose file formats and rules the build implements (`1.9.2`). Both keys are
  absent for a format 2 file, so existing output is unchanged.
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
- `--min-contig-length BP` (default `20000`) skips short contigs; they are not sketched or sent,
  produce no row, and are counted in `summary.skipped_short`. A contig with fewer k-mers than
  sylph's `min_number_kmers` (or with no genome-style k-mer) is counted in
  `summary.skipped_few_kmers` instead of producing a spurious match; the server reports such a
  sketch as `too_few_kmers`. Duplicate record ids get a deterministic `_2`, `_3` suffix.
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
  one `metadata.taxonomy_overlaps` entry per pair by `sylphd info` and `GET /info` (see above).

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
- **`POST /contigs`**, a server endpoint that classifies every contig of a fragmented assembly in
  one request. The body is one `sketch` part per contig plus a `params` object with, per contig,
  `name`, `contig_kmers` (the record's genome-style k-mer count), `length` and `gc`, and the
  options `top`, `min_ani`, `consensus_ratio`, `consensus_z`, `fields` and `tax_sources`. It
  returns one result per contig (`status` `ok`/`no_hit`/`too_few_kmers`, `hits`, `assignment`,
  `closest`, `consensus`) and a per-request `summary` (bases per consensus taxon, not truncated so
  a client can merge requests), the distinct `caveats` and `elapsed_ms`. The whole computation
  (contig-relative ANI, ANI noise, ranking, classification, consensus) now lives on the server in
  `sylphd/per_contig.py`, and the CLI's half in `sylphd/per_contig_client.py`. Inputs are checked
  before anything runs — one `contigs` entry per sketch part, unique valid names, positive
  `contig_kmers` no larger than `length - k + 1`, `length` <= 1e13, `gc` 0-100, `top` 1-50,
  `min_ani` 70-100, `consensus_ratio` >= 1, `consensus_z` >= 0 — and bad input is a `400`. One
  `/contigs` request is one job (its contigs run one after the other inside that slot), so
  `--max-queue` and `--rate-limit` count the request, not its contigs. `/info`
  lists `/contigs` in `endpoints`, and `limits.max_params_bytes` (1 MiB, the form-part limit) so a
  client can budget the JSON `params`, which repeats every contig name. See the
  [HTTP API](../reference/http-api.md#post-contigs).
- **Parsimony ranking inside the ANI noise band.** Among the hits that are statistically
  indistinguishable on the contig side — the same close band the consensus uses,
  `d <= R * d_best + Z * sqrt(sd_best^2 + sd^2)` — **and** that explain at least 95 % of the contig
  (`explained_fraction >= 0.95`, `PARSIMONY_MIN_EXPLAINED`), the reference with the fewest k-mers
  is listed first (the highest `reference_ani`). The saturation condition keeps the rule to
  containment: at k=31 `explained_fraction = 0.95` is already ~99.83 % ANI, so a >= 95 %
  assignment can never be moved below the species threshold, and a 97 % hit is not displaced by a
  smaller reference at 95 %. Everything else keeps the evidence order. `shared`
  saturates at `contig_kmers` when a reference *contains* the contig, so a bacterial assembly that
  carries a lambda contaminant contig used to outrank the true (far smaller) phage genome with
  every k-mer of the contig accounted for. On the real lambda contig of the phase-4a mock against
  GTDB r232 + UHGV, `Lambdavirus` now comes first (`82/210` k-mers, `reference_ani` 97.0) instead
  of the six lambda-carrying GTDB genomes at `83/10 271`. The consensus does **not** drop those
  containing genomes: a close set that spans UHGV and GTDB stays `mixed_sources`.

### Security

- **`POST /contigs` is work-bounded.** `serve --max-contigs N` (default 4096) caps the contigs of
  one request with a `400` that names the limit; `/info` advertises it as `limits.max_contigs`,
  which the CLI and the web page already use as their batch size. The multipart parser itself is
  configured with `max_files=N`, so the `(N+1)`th `sketch` part is refused before it is spooled to
  a temporary file (Starlette's "too many files" error becomes the same friendly `400`); the rest
  of the body is not read. A whole request is one job and a
  contig costs ~0.6 s on a server with plain databases, so a public instance should run
  `--max-contigs 100` (now the example policy in `deploy/`): ~40 minutes of a job slot from one
  ~5 MB upload becomes about a minute. A sketch below the sample k-mer floor (10) is answered as
  `too_few_kmers` **before** the query runs, so a fragmented assembly full of such records costs
  no profiling work.
- **`/contigs` queries and scoring no longer run on the event loop.** The per-contig query *and*
  the scoring/classification/consensus of `sylphd.per_contig` run in the same worker thread while
  the job slot is held, so a slow or long request cannot delay `/health` or `/info`.
- **Repeated `params.fields` are normalised.** Every endpoint that takes `fields` removes
  duplicates (`tuple(dict.fromkeys(...))`, like `tax_sources`) and cannot return more columns than
  the metadata file has, so a request cannot make a hit repeat a key or the metadata join do
  duplicate work.
- **`/info` no longer exposes absolute paths.** `database`/`databases[]` report the database name
  and file name only, `metadata` reports `file` (base name), and a taxonomy source's `origin` is
  its catalogue URL or the base name of a local file. Full paths stay in the server log
  (`sylphd info` follows the new keys).
- **The browser per-contig path is bounded.** Record names are made unique only for the contigs
  actually sent (the `counts`/`used` bookkeeping no longer grows with every record), the wasm
  sketcher refuses more than 1,000,000 records or a single record longer than 50 Mbp with a
  message pointing to `sylphd assembly --per-contig` (checked while parsing, before buffers grow;
  a line split across chunks is measured joined before its carry buffer is extended, so it cannot
  briefly outgrow the limit either).
- **`deploy/setup-server.sh` validates and escapes what it writes.** Database file names (and
  `SYLPH_META`) are restricted to `[A-Za-z0-9._+-]+`, not starting with `.` or `-`; empty aliases
  and multi-line lists are refused; `SYLPH_HTTP_MAX` must be a size. Every value in a systemd
  drop-in goes through one routine that escapes quotes and backslashes, doubles `%`, and rejects
  newlines.

### Changed

- **Metadata files older than format 3 are no longer read or updated.** `MetadataDB` and
  `setup --update` refuse them with a message to rebuild with `sylphd setup`; the format 2 reader
  fallbacks, the 2 → 3 migration and the `taxonomy_overlap` backfill are gone.
- **A `/contigs` request is one job.** Previously every contig counted as one server job, so
  clients had to chunk to `max_jobs + max_queue` (18 contigs on the public instance) and a second
  run of 100 contigs within a minute hit the rate limit. Now the whole request occupies one job
  slot and runs its contigs sequentially inside it; `--max-jobs`/`--max-queue` bound concurrent
  requests and `--rate-limit` counts one POST. The per-request bound is
  `--max-contigs` (4096 by default), advertised by `/info` as `limits.max_contigs`;
  `per_contig_client.chunk_size_for` and the web page use it instead of `max_jobs + max_queue`
  (older servers fall back to `max_sketches`). Queue-full behaviour is unchanged: a second
  request while every job and queue slot is taken is a `503` with `Retry-After: 1`.
- **`deploy/` accepts several databases.** `SYLPH_DB` in `site.env` may be a space-separated list
  of `FILE` or `NAME=FILE` entries, one `--db /srv/sylphd/db/...` argument per database;
  single-database files are unchanged. `setup-server.sh` validates every listed file exists,
  renders the arguments into `SYLPH_DB_ARGS` in the service drop-in (systemd expands
  `$SYLPH_DB_ARGS` unquoted into separate arguments, which `${SYLPH_DB}` cannot do), and gains
  `SYLPHD_DRY_RUN=1` to print the fragment without touching the host. No SELinux change is
  needed: the databases stay plain data in `/srv/sylphd/db`.
- **`--per-contig` counts skipped records instead of listing them.** Records below
  `--min-contig-length` (`summary.skipped_short: {contigs, bp}`) and records not sent for too few
  sketched k-mers or zero genome-style k-mers (`summary.skipped_few_kmers`) are no longer kept as
  per-record objects and no longer produce TSV rows or JSON `contigs` entries; the stderr summary
  says `N contigs (X kb) below 20000 bp not classified`. This keeps memory and output flat for
  FASTQ input with millions of short reads. Name-uniqueness bookkeeping in `sketch_contigs` now
  covers only sent records (skipped records consume no name and do not grow the `counts`/`used`
  sets). Record indexes keep the input order, so skipped records leave gaps.
- **`sylphd assembly --per-contig` now needs a server with `POST /contigs`** (this version or
  newer; an older server answers `404`). The command still reads the file, sketches every record
  and measures its genome-style k-mer count locally, but the contig-relative ANI, the ranking, the
  classification and the consensus are computed on the server, and the per-request summaries are
  added up instead of re-sending every hit to the client. The table, the JSON document and the
  stderr summary are unchanged except for the parsimony tie-break above and the skipped-record
  counting below, and a contig whose genome sketch is empty (a tandem repeat, the ANI denominator
  would be zero) is counted in `summary.skipped_few_kmers` (the server answers `too_few_kmers` for
  such a sketch) and is not sent, because the endpoint requires a positive `contig_kmers`.
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
- Per-contig mode batches contig sketches into `POST /contigs` requests with `no_adjust`,
  `minimum_ani=0`, `screen_ani=0` and `min_contain=3`, chunked by the server's advertised
  `max_contigs` (falling back to `max_sketches`; one request is one job) and by the *measured*
  request size and JSON `params` size, so a highly fragmented assembly (or one with very long
  record ids) works against a server with default limits.
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

- **Metadata files older than format 3 are no longer read or updated.** `MetadataDB` and
  `setup --update` refuse them with a message to rebuild with `sylphd setup`; the format 2 reader
  fallbacks, the 2 → 3 migration and the `taxonomy_overlap` backfill are gone.
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

- **Metadata files older than format 3 are no longer read or updated.** `MetadataDB` and
  `setup --update` refuse them with a message to rebuild with `sylphd setup`; the format 2 reader
  fallbacks, the 2 → 3 migration and the `taxonomy_overlap` backfill are gone.
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

- **Metadata files older than format 3 are no longer read or updated.** `MetadataDB` and
  `setup --update` refuse them with a message to rebuild with `sylphd setup`; the format 2 reader
  fallbacks, the 2 → 3 migration and the `taxonomy_overlap` backfill are gone.
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

[0.3.1]: https://github.com/Abrinca/sylphd/releases/tag/v0.3.1
[0.3.0]: https://github.com/Abrinca/sylphd/releases/tag/v0.3.0
[0.2.0]: https://github.com/Abrinca/sylphd/releases/tag/v0.2.0
[0.1.3]: https://github.com/Abrinca/sylphd/releases/tag/v0.1.3
[0.1.2]: https://github.com/Abrinca/sylphd/releases/tag/v0.1.2
[0.1.1]: https://github.com/Abrinca/sylphd/releases/tag/v0.1.1
[0.1.0]: https://github.com/Abrinca/sylphd/releases/tag/v0.1.0
