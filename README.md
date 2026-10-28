# sylphd

A small server and client around [sylph](https://github.com/bluenote-1577/sylph):
one long-running server keeps a sylph database in memory, while clients sketch
samples locally and send only the sketches. This avoids paying the database
deserialisation cost (about 2 s and 2 GB of RAM for GTDB r232 c200) in every
process.

The client and server speak HTTP over a Unix socket by default. There is no
TLS and no authentication: run it on a machine you trust, and keep the socket
in a directory only your group can access.

## Requirements

- Python 3.14
- [uv](https://docs.astral.sh/uv/) for development and installation
- A sylph database (`.syl2db` two-stage, or `.syldb` plain)

`sylphd` depends on [pysylph](https://github.com/MrTomRod/pysylph) branch
`multi-db`. The Rust extension is built by uv at install time; `cmake` is
provided automatically through `[tool.uv.extra-build-dependencies]`.

## Install

```console
uv sync                 # development environment
uv run sylphd --help
```

or, in another project:

```console
uv add "sylphd @ git+https://github.com/MrTomRod/sylphd"
```

## Quick start

```console
# 1. Metadata for GTDB r232 (optional but recommended)
sylphd download --release 232 --outdir data
sylphd setup -m data/bac120_metadata_r232.tsv.gz data/ar53_metadata_r232.tsv.gz \
             -o data/gtdb-r232.sqlite --release 232

# 2. Serve a database (one worker, blocking work in threads)
sylphd serve --db gtdb-r232-c200-dbv2.syl2db --meta data/gtdb-r232.sqlite

# 3. In another shell: sketch locally and query
sylphd info
sylphd query -1 sample_R1.fq.gz -2 sample_R2.fq.gz
sylphd profile -r sample.fq.gz -o sample.profile.tsv
```

The default address is `$XDG_RUNTIME_DIR/sylphd.sock`, falling back to
`/tmp/sylphd-$UID.sock`. The server removes a stale socket file at startup and
creates the socket with mode `0660` (owner and group can connect; adjust the
group with `chgrp` if needed).

## Commands

### `sylphd serve`

```
sylphd serve --db PATH [--meta PATH] [--socket PATH | --tcp HOST:PORT]
             [--max-jobs N] [--max-upload-mb N] [--allow-sequences]
             [--max-sequence-mb N] [--rate-limit N/min] [--max-queue N]
             [--trusted-proxy IP] [--cors-origin ORIGIN]
             [--hash] [--log-level LEVEL]
```

- `--db`: database to load at startup. `.syl2db` files are read as two-stage
  databases, everything else as a plain `.syldb`.
- `--meta`: SQLite metadata database from `sylphd setup`; results are joined by
  accession and the metadata columns are added to each row.
- `--max-jobs`: how many profiling jobs may run at once; further requests
  queue. Blocking pysylph calls run in worker threads, so the server stays
  responsive. Default `2`.
- `--max-upload-mb`: request body limit. Default `512`. Sketches are also
  checked structurally before they are decoded; see Wire format.
- `--allow-sequences`: enable `POST /assembly`, which takes raw FASTA/FASTQ
  instead of a sketch. Off by default: without it the endpoint is a 404.
- `--max-sequence-mb`: upload limit of `/assembly`, before decompression.
  Default `20`. Gzip bodies are decompressed up to 5× this limit.
- `--rate-limit N/min`: per-client limit on the POST endpoints (`/assembly`,
  `/profile`, `/query`), e.g. `10/min`; also `N/s`. Off by default, for local
  use. Replies are `429` with `Retry-After`.
- `--max-queue N`: how many requests may wait for a job slot; beyond that the
  server answers `503` with `Retry-After` instead of queueing. Unlimited by
  default.
- `--trusted-proxy IP`: a peer whose `X-Forwarded-For` header is believed;
  repeatable. Use `--trusted-proxy unix` when a reverse proxy talks to the
  server over the default Unix socket. Without it the header is ignored and
  the socket peer is the client identity.
- `--cors-origin ORIGIN`: allowed CORS origin for a browser front end;
  repeatable, none by default. `GET`/`POST` and the `Content-Type` header are
  allowed, and `Retry-After` is exposed. `--cors-origin local` allows
  `localhost`, `127.0.0.1`, `0.0.0.0` and `[::1]` on any port, for development.
- `--hash`: compute the database SHA-256 at startup (off by default, slow on a
  20 GB database). Reported by `/info`.
- Exactly one worker process runs: sharing the in-memory index is the point.

### `sylphd info`

Prints database name, path, size, genome count, `c`, `k`, `screen_c`, metadata
summary and versions. `--json` prints the raw `/info` payload.

### `sylphd profile` / `sylphd query`

Inputs (each file or group is one sample):

| Syntax | Meaning |
|---|---|
| `sylphd query reads.fq.gz` | one single-end sample (positional) |
| `sylphd query -r a.fq b.fq` | two single-end samples |
| `sylphd query -1 a_R1.fq -2 a_R2.fq` | one paired sample |
| `sylphd query -1 a_R1.fq b_R1.fq -2 a_R2.fq b_R2.fq` | two paired samples |
| `sylphd query --interleaved a.fq` | one interleaved paired sample |
| `sylphd query --sample "s1:a.fq,b.fq"` | one sample from two files |
| `sylphd query --sample "s1:a1.fq+a2.fq,b1.fq+b2.fq"` | one sample from two pairs |
| `sylphd query sample.sylsp` | an existing sketch, sent as-is |

- A sample name defaults to the file base name without compression/read
  suffixes; paired names also lose `_R1`/`_R2`/`.R1`/`.R2`/`_1`/`_2`.
- `--sample NAME:SPEC` takes a comma-separated list of inputs; `R1+R2` marks a
  pair. All inputs of a sample must be single-end or all paired.
- `--sample-name NAME` renames a single sample.
- `-t/--threads` sketches samples in parallel locally (default `4`).
- Thresholds: `--minimum-ani`, `--seq-id`, `--estimate-unknown`,
  `--min-number-kmers`, `--min-contain`, `--screen-ani` (sylph's defaults).
- `--no-adjust`: report the naive containment ANI (sylph's `Naive_ANI`)
  instead of the coverage-adjusted ANI. Use it when the sample is an assembly
  or a genome: every k-mer then occurs once, the coverage-based correction is
  meaningless and inflates ANI to 100. `Adjusted_ANI` then equals
  `Naive_ANI`, and the `ANI_5-95_percentile` column is left empty because the
  interval still belongs to the adjusted estimate.
- `--fields` selects metadata columns (comma-separated, `all` by default,
  `none` to omit them); `--format json` prints the server payload.
- Output is TSV on stdout, or `-o FILE`.

The output mirrors sylph's TSV columns and appends `accession` plus the selected
metadata columns:

```
query:   Sample_file Genome_file Adjusted_ANI Eff_cov ANI_5-95_percentile
         Eff_lambda Lambda_5-95_percentile Median_cov Mean_cov_geq1
         Containment_ind Naive_ANI Contig_name accession <metadata...>
profile: ... Taxonomic_abundance Sequence_abundance ... kmers_reassigned ...
```

With `--estimate-unknown`, `Eff_cov` is labelled `True_cov`, as in sylph.

#### Taxonomic profiles (`--taxprof`, `--taxmatrix`)

`sylphd profile` can fold its ANI hits into per-rank taxonomic profiles on the
client, mirroring `sylph-tax taxprof` and `sylph-tax merge`. It only needs the
`gtdb_taxonomy` and `ncbi_taxonomy` metadata columns, so the server must run
with `--meta`. The client always requests those two columns, even when
`--fields` hides them from the main TSV.

```console
sylphd profile -r sample.fq.gz \
    --taxonomy gtdb,ncbi \
    --taxprof results/ \
    --taxmatrix results/
```

- `--taxonomy LIST`: `gtdb`, `ncbi`, or both, comma-separated. Default
  `gtdb,ncbi`. Only `profile` has these flags.
- `--taxprof PREFIX`: writes one MetaPhlAn-style file per sample and taxonomy,
  `PREFIX<sample>.<gtdb|ncbi>.sylphmpa`, in sylph-tax's format: the
  `#SampleID ... Taxonomies_used:['gtdb']` comment line, then
  `clade_name relative_abundance sequence_abundance ANI (if strain-level)
  Coverage (if strain-level)`.
- `--taxmatrix PREFIX`: writes one wide table per taxonomy,
  `PREFIX<gtdb|ncbi>.tsv`, with a `clade_name` column followed by one column
  per sample. Clades missing from a sample are `0.0`.
- `--taxmatrix-value relative|sequence`: value used by `--taxmatrix`; default
  `relative` (`Taxonomic_abundance`). `sequence` uses `Sequence_abundance`.

Both flags can be combined with the normal TSV/JSON output (`-o`, `--format`).
The prefix is used verbatim, so end it with `/` (or a directory that exists) to
write into a directory; the directory is not created.

A `.sylphmpa` file looks like this:

```
#SampleID	sample-a	Taxonomies_used:['gtdb']
clade_name	relative_abundance	sequence_abundance	ANI (if strain-level)	Coverage (if strain-level)
d__Bacteria	100.0	100.0	NA	NA
d__Bacteria|p__Bacillota	60.0	42.5	NA	NA
d__Bacteria|p__Bacillota|c__Clostridia	60.0	42.5	NA	NA
...
d__Bacteria|p__Bacillota|c__Clostridia|...|s__Clostridium difficile	60.0	42.5	NA	NA
d__Bacteria|p__Bacillota|c__Clostridia|...|s__Clostridium difficile|t__GCA_000000001.1	60.0	42.5	99.79	1.234
```

Each row is a cumulative lineage: a clade's abundances are the sums of all
genomes below it, so `d__Bacteria` is the sum of every hit of that sample.
Rows are ordered rank by rank, and within a rank by decreasing relative
abundance (ties keep the order the server returned). Only `t__` rows carry
`Adjusted_ANI` and `Eff_cov` (or `True_cov`); the other rows have `NA` there.
`sylph-tax merge` can read these files directly.

Lineage rules:

- GTDB strings name every rank (`d__..;p__..;c__..;o__..;f__..;g__..;s__..`);
  NCBI strings may leave ranks empty.
- Trailing empty ranks are dropped: the lineage stops at the last named rank
  and the `t__` genome hangs directly below it (an empty `s__` puts the
  strain under the genus).
- An empty rank *before* a named one is filled with
  `{rank}__unclassified {nearest named ancestor}`, e.g.
  `o__unclassified Clostridia`, so every genome of a sample uses the same
  path for that rank. With no named ancestor the suffix is `root`.
- `t__` is the genome accession (`GCA_...`/`GCF_...`, as stored in the
  `accession` column, like sylph-tax's taxonomy files) or, when no accession
  is known, the `Genome_file` base name.
- Rows without any usable taxonomy (no metadata, no accession match, `none`)
  go to a single `unclassified` clade, with the genome as `t__` below it.
  sylph-tax's default for such rows is `NO_TAXONOMY;t__<file>:<contig>`.
- Abundances are summed exactly as the server reports them; they are never
  renormalised. With `--estimate-unknown` the root clade may therefore stay
  below 100.

The matrix columns are sample names in request order; rows are all clades seen
in any sample, in first-seen order (the same order sylph-tax merge produces
when joining its profiles in that order).

### `sylphd assembly`

```
sylphd assembly FILE [--top N] [--min-ani ANI] [--name NAME] [--json] [-o FILE]
```

Sends one assembly (FASTA or FASTQ, plain or gzip) to `POST /assembly` and
prints a compact table of its closest database genomes:

```
rank  ani    relation      shared_rank  accession        gtdb_species      ncbi_organism_name     ncbi_taxid
1     99.53  same species  species      GCA_000005845.2  Escherichia coli  Escherichia coli K-12  562
2     90.12  same genus    genus        GCF_000...       Escherichia sp...  ...                    ...
```

One line on stderr says whether the query was assigned to a species
(`assigned to species Escherichia coli (99.53 % ANI)`) or only has a closest
relative (`no species-level match (< 95 % ANI); closest relative ...`).

- `--top` is 1-50 (default `10`), `--min-ani` is 70-100 (default `80`).
- `--name` sets the query name (default: the file base name).
- `--json` prints the raw response, `-o FILE` writes the table to a file.
- The ANI is the naive containment ANI (`no_adjust`), which is the meaningful
  measure for an assembly; the server also relates every hit to the query's
  species assignment through its GTDB lineage (see [`POST /assembly`](#post-assembly)).
- The server must run with `--allow-sequences`.

### `sylphd download`

```
sylphd download [--release 232] [--outdir DIR] [--only {bac120,ar53}] [--force]
```

Downloads `bac120_metadata_r{release}.tsv.gz` and `ar53_metadata_r{release}.tsv.gz`
from `https://data.gtdb.ecogenomic.org/releases/release{release}/{release}.0/`,
checking each against `MD5SUM.txt` from the same directory. Downloads go to
`*.part` and are renamed only after the checksum matches. Existing files are
verified and reused unless `--force` is given. `--base-url` points at a mirror.

### `sylphd setup`

```
sylphd setup -m FILE... -o PATH [--release N] [--reps-only] [--force] [-q]
```

Streams GTDB metadata TSV files (gzip or plain) into a SQLite file:

- view `metadata` with the columns below, primary key `accession` (the
  `RS_`/`GB_` prefix is stripped, so `RS_GCF_000005845.2` becomes
  `GCF_000005845.2`);
- table `info` with the source files, release, build date, `sylphd` version,
  format version and whether the file is representatives-only;
- `none`, `na` and empty values become `NULL`;
- booleans `t`/`f` become integers `1`/`0`;
- numeric columns are `INTEGER`/`REAL`.

The repetitive text columns (both taxonomies, organism names, assembly
metadata, …) are dictionary-encoded into `dict_<column>` tables; the base table
`metadata_data` stores ids and is keyed by `accession` `WITHOUT ROWID`, and the
`metadata` view joins the two back to the original columns and values. That is
~200 MB for the full release instead of ~630 MB, with lookups still in the low
milliseconds. Files from older `sylphd` versions (a plain `metadata` table)
remain readable.

All ~900k GTDB genomes are stored by default. `--reps-only` stores only rows
with `gtdb_representative` true (~200k, ~80 MB); lookups of other accessions
then find no metadata. When two input files contain the same accession, the
later row wins. Re-run with `--force` to replace an existing file.

Columns: `gtdb_taxonomy`, `gtdb_representative`,
`gtdb_genome_representative`, `gtdb_type_species_of_genus`,
`gtdb_type_designation_ncbi_taxa`, `ncbi_taxid`, `ncbi_species_taxid`,
`ncbi_taxonomy`, `ncbi_taxonomy_unfiltered`, `ncbi_organism_name`,
`ncbi_strain_identifiers`, `ncbi_isolate`, `ncbi_genome_category`,
`ncbi_assembly_level`, `ncbi_refseq_category`,
`ncbi_type_material_designation`, `ncbi_biosample`, `ncbi_bioproject`,
`checkm2_completeness`, `checkm2_contamination`, `genome_size`,
`gc_percentage`, `contig_count`, `n50_contigs`, `mimag_high_quality`.

Genome names such as `.../GCA_964372435.1_genomic.fna.gz` map to
`GCA_964372435.1`; names without a recognisable accession get no metadata.

## Wire format

`GET /health`, `GET /info` return JSON. `POST /query` and `POST /profile` take
`multipart/form-data`:

- one `sketch` file part per sample, containing the raw bytes of a
  `pysylph.SampleSketch` (a `.sylsp` file);
- an optional `params` field with a JSON object:
  `minimum_ani`, `seq_id`, `estimate_unknown`, `min_number_kmers`,
  `min_contain`, `screen_ani`, `no_adjust`, `names` (one name per sketch),
  `fields` (metadata columns), `mode` (optional, must match the endpoint).

The response is a JSON list with one object per sample, in request order:

```json
[{"name": "sample-a", "kind": "profile", "rows": [{"Sample_file": "sample-a", "Genome_file": "...", "Adjusted_ANI": 99.79, "ANI_5-95_percentile": [99.64, 99.95], "Eff_lambda": 0.36, "Lambda_status": "OK", "Lambda_5-95_percentile": [0.34, 0.38], "Median_cov": 1.0, "Mean_cov_geq1": 1.187, "Containment_ind": [6208, 21899], "Naive_ANI": 96.02, "Contig_name": "...", "Taxonomic_abundance": 100.0, "Sequence_abundance": 100.0, "kmers_reassigned": 0, "accession": "GCA_...", "gtdb_taxonomy": "..."}]}]
```

Errors are JSON objects `{"error": "..."}` with a 4xx/5xx status: 400 for bad
parameters, sketches with the wrong `c`/`k` or malformed sketches; 413 when the
body or sketch count exceeds the limit; 415 for non-multipart requests; 429
when rate limited and 503 when the job queue is full (both with a
`Retry-After` header).

With `no_adjust`, `Adjusted_ANI` is the naive containment ANI and
`ANI_5-95_percentile` is `null`: the confidence interval still describes the
(disabled) adjustment.

Clients sketch with the server's `c`/`k`; existing sketches must match `k`
exactly and must not be sparser than the database (`c <= db.c`).

Uploaded sketches are checked structurally before they are decoded: the
`SampleSketch` header (`c`, `k`, names, `paired`, mean read length) is parsed
directly from the bytes, and its k-mer count prefix is verified against the
actual upload size. The count must be checked first because sylph's
deserializer allocates the k-mer map from that prefix up front, so an 8-byte
upload declaring 100 million k-mers would abort the process. Malformed
sketches are rejected with a 400 before any such allocation can happen. The
layout is tied to the pinned sylph version (`ref/sylph/src/types.rs`); the test
suite compares the parsed header against a decoded sketch to catch drift.

## Web API

A browser front end needs to send sequences, not sketches, so `sylphd serve
--allow-sequences` adds one endpoint for exactly that case. Everything is
opt-in; without the flag `POST /assembly` is a 404.

### `POST /assembly`

Body: one assembly, raw FASTA or FASTQ, plain or gzip (the gzip magic bytes
`\x1f\x8b` select decompression; the `Content-Type` header is otherwise
informational). All records of the body form one sample.

```
Content-Type: text/plain | application/octet-stream | application/gzip
```

| Query parameter | Meaning |
|---|---|
| `top` | number of hits, 1-50 (default `10`) |
| `min_ani` | minimum ANI in percent, 70-100 (default `80`) |
| `name` | query name (default `assembly`, max 1024 characters) |

The server sketches the assembly with the database's `c`/`k`, runs
`Profiler(db, no_adjust=True)` and keeps the `top` hits sorted by ANI. The
response is:

```json
{"query": {"name": "assembly", "records": 1, "length": 6013491,
           "gc": 50.76, "sketch_kmers": 200},
 "database": {"name": "gtdb-r232-c200-dbv2.syl2db", "genomes": 199937,
              "c": 200, "k": 31},
 "params": {"top": 10, "min_ani": 80.0},
 "thresholds": {"species": 95.0, "imprecise_below": 85.0},
 "assignment": {"rank": "species", "taxon": "Escherichia coli",
                "gtdb_taxonomy": "d__Bacteria;...;s__Escherichia coli",
                "accession": "GCF_000005845.2", "ani": 98.65},
 "closest": {"rank": "species", "taxon": "Escherichia coli",
             "gtdb_taxonomy": "d__Bacteria;...;s__Escherichia coli",
             "accession": "GCF_000005845.2", "ani": 98.65},
 "elapsed_ms": 412.7,
 "hits": [{"rank": 1, "ani": 98.65, "containment": [7096, 10826],
           "containment_fraction": 0.655, "relation": "same species",
           "shared_rank": "species", "ani_species_level": true,
           "genome_file": "/db/GCA_000005845.2_genomic.fna.gz",
           "accession": "GCF_000005845.2",
           "gtdb_taxonomy": "d__Bacteria;...;s__Escherichia coli",
           "ncbi_organism_name": "Escherichia coli K-12", "...": null}]}
```

- `ani` is the naive containment ANI (`no_adjust=True`); the coverage-adjusted
  ANI is meaningless for assemblies, so there is no confidence interval here.
- `containment` is `[contained k-mers, sample k-mers]` and
  `containment_fraction` is their ratio.
- **Species assignment.** `95` % ANI is GTDB's species circumscription and the
  only hard threshold (`thresholds.species`). If the best hit reaches it, the
  query is assigned to that hit's GTDB species and `assignment` is
  `{"rank": "species", "taxon", "gtdb_taxonomy", "accession", "ani"}`
  (`taxon` and `gtdb_taxonomy` are `null` without metadata). Otherwise
  `assignment` is `null`: there is no species-level match.
  `closest` has the same shape for the best hit (`null` without hits).
  `thresholds.imprecise_below` (`85`) is only a hint that k-mer ANI below it is
  imprecise; it never changes an assignment.
- **Hit relations** come from the GTDB lineage (`d__;p__;c__;o__;f__;g__;s__`),
  not from the ANI. `shared_rank` is the deepest rank a hit shares with the
  reference lineage (`species`, `genus`, `family`, `order`, `class`, `phylum`,
  `domain` or `null`); a rank only counts when all shallower ranks agree.
  `relation` is the human label:

  | Situation | `relation` of the hit |
  |---|---|
  | assigned, shares rank *R* with the assigned species | `same species`, `same genus`, `same family`, `same order`, `same class`, `same phylum`, `same domain` |
  | assigned, shares nothing | `different domain` |
  | not assigned, the best hit | `closest relative` (`shared_rank` is `null`) |
  | not assigned, other hits, sharing rank *R* with the best hit | `same genus as closest relative`, ... |
  | not assigned, sharing nothing with the best hit | `different domain from closest relative` |
  | a hit without lineage next to classified hits | `unknown lineage` |
  | no metadata at all (ANI only) | `same species` for `ani >= 95`, else `related` |

  A hit in the same GTDB species as the assignment is `same species` whatever
  its ANI. `ani_species_level` is `ani >= 95` for every hit: a hit that has it
  but a different GTDB species name (a species boundary case) gets the
  lineage relation, e.g. `same genus`, plus `ani_species_level: true`.
- The former `interpretation` field is gone; `relation` replaces it.
- Every metadata column of the server's metadata database is added flat to each
  hit, or `null` when the accession has no metadata. A server without `--meta`
  returns all columns as `null`, so the shape is stable.
- Errors: `400` for an empty, malformed or non-ACGTN body and for bad query
  parameters; `413` when the body exceeds `--max-sequence-mb` or its
  decompressed size exceeds five times that limit; `415` for an unsupported
  `Content-Type`; `429`/`503` with `Retry-After` from the abuse limits below.
- Uploads are never logged: only sizes, timings and request outcomes appear in
  the server log.

### Abuse limits

`--rate-limit`, `--max-queue`, `--trusted-proxy` and `--cors-origin` apply to
`/assembly`, `/profile` and `/query`; `/health` and `/info` are always served.
A rate-limited request gets `429` and a `Retry-After` in seconds; a request
that would wait behind `--max-queue` others gets `503` and `Retry-After: 1`.

The client identity is the socket peer. Only with `--trusted-proxy <ip>` (or
`--trusted-proxy unix` for a Unix-socket peer, e.g. Caddy) is
`X-Forwarded-For` read, and then the rightmost address that is not itself a
trusted proxy is used. Unix-socket peers without a trusted proxy all share one
bucket called `unix`.

CORS is off unless `--cors-origin` is given. Preflights allow `GET`/`POST` and
the `Content-Type` request header, and `Retry-After` is exposed to JavaScript.

Example public deployment behind a reverse proxy:

```console
sylphd serve --db gtdb-r232-c200-dbv2.syl2db --meta gtdb-r232.sqlite \
    --tcp 127.0.0.1:8765 --allow-sequences --max-jobs 4 --max-queue 16 \
    --rate-limit 10/min \
    --cors-origin https://mrtomrod.github.io
```

Caddy (or nginx) terminates TLS and forwards to `127.0.0.1:8765`; add
`--trusted-proxy 127.0.0.1` so clients get individual rate-limit buckets.

## Web page

`web/` is a static page (plain HTML/CSS/JS, no build step, no external
requests) that talks to a `sylphd serve --allow-sequences` instance through
`GET /info` and `POST /assembly`. It takes a pasted or uploaded FASTA
(plain or gzip, up to the server's `--max-sequence-mb`), shows the species assignment
(or the closest relative) with an ANI gauge, and lists the closest GTDB genomes in a sortable table
(downloadable as TSV or JSON). The workflow `.github/workflows/pages.yml`
deploys it to GitHub Pages on pushes to `main` that touch `web/`.

Local development, against a server started with `--cors-origin local`
(allows any loopback origin, whether you open the page as `localhost`,
`127.0.0.1` or `0.0.0.0`):

```console
python -m http.server 8000 -d web
# open http://localhost:8000/?api=http://127.0.0.1:8765
```

The default API base URL is set in `web/config.js`; the `?api=` query
parameter overrides it per visit. The browser must be allowed by the server's
CORS origin, and an HTTPS page can only call an HTTPS API (or localhost).

## Development

```console
uv run pytest
```

Tests use pysylph's small test data copied into `tests/data` (ecoli databases,
K-12 reads, `.sylsp` sketches) and include an in-process Starlette client and
one real Unix-socket round trip against a `sylphd serve` subprocess. The large
GTDB database is only needed for manual runs.

## How to cite

You are using sylphd. If it helps your research, please cite:

- **sylphd**: this web page and its API server. Thomas Roder at
  [Abrinca Genomics](https://abrinca.com).
  [GitHub](https://github.com/MrTomRod/sylphd), [Docs](https://github.com/MrTomRod/sylphd#readme)
- **pysylph**: Python bindings used by the server. Martin Larralde. PyO3
  bindings and Python interface to sylph.
  [GitHub](https://github.com/althonos/pysylph), [Docs](https://pysylph.readthedocs.io/)
- **sylph**: the method. Jim Shaw and Yun William Yu. Rapid species-level
  metagenome profiling and containment estimation with sylph (2024). *Nature
  Biotechnology*. [doi:10.1038/s41587-024-02412-y](https://doi.org/10.1038/s41587-024-02412-y).
  [GitHub](https://github.com/bluenote-1577/sylph), [Docs](https://sylph-docs.github.io/)
- **GTDB**: taxonomy and genome metadata (<https://gtdb.ecogenomic.org/>), as
  GTDB recommends:
  - Parks DH, Chaumeil P-A, Mussig AJ, Rinke C, Chuvochina M, Hugenholtz P.
    GTDB release 10: a complete and systematic taxonomy for 715 230 bacterial
    and 17 245 archaeal genomes (2025). *Nucleic Acids Research*.
    [doi:10.1093/nar/gkaf1040](https://doi.org/10.1093/nar/gkaf1040)
  - Parks DH, Chuvochina M, Rinke C, Mussig AJ, Chaumeil P-A, Hugenholtz P.
    GTDB: an ongoing census of bacterial and archaeal diversity through a
    phylogenetically consistent, rank normalized and complete genome-based
    taxonomy (2022). *Nucleic Acids Research* 50(D1):D785-D794.
    [doi:10.1093/nar/gkab776](https://doi.org/10.1093/nar/gkab776)

sylph and pysylph are MIT licensed; the test data in `tests/data` comes from
pysylph's test suite. The web page shows the same list at the bottom.

## License

AGPL-3.0-or-later, see `LICENSE`. We are open to negotiating more permissive terms: <https://abrinca.com/contact>.
