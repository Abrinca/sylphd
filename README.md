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
             [--max-jobs N] [--max-upload-mb N] [--hash] [--log-level LEVEL]
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
sylphd setup -m FILE... -o PATH [--release N] [--force] [-q]
```

Streams GTDB metadata TSV files (gzip or plain) into a SQLite file:

- table `metadata`, primary key `accession` (the `RS_`/`GB_` prefix is stripped,
  so `RS_GCF_000005845.2` becomes `GCF_000005845.2`);
- table `info` with the source files, release, build date and `sylphd` version;
- `none`, `na` and empty values become `NULL`;
- booleans `t`/`f` become integers `1`/`0`;
- numeric columns are `INTEGER`/`REAL`.

All ~900k GTDB genomes are stored, not only species representatives. When two
input files contain the same accession, the later row wins. Re-run with
`--force` to replace an existing file.

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
  `min_contain`, `screen_ani`, `names` (one name per sketch), `fields`
  (metadata columns), `mode` (optional, must match the endpoint).

The response is a JSON list with one object per sample, in request order:

```json
[{"name": "sample-a", "kind": "profile", "rows": [{"Sample_file": "sample-a", "Genome_file": "...", "Adjusted_ANI": 99.79, "ANI_5-95_percentile": [99.64, 99.95], "Eff_lambda": 0.36, "Lambda_status": "OK", "Lambda_5-95_percentile": [0.34, 0.38], "Median_cov": 1.0, "Mean_cov_geq1": 1.187, "Containment_ind": [6208, 21899], "Naive_ANI": 96.02, "Contig_name": "...", "Taxonomic_abundance": 100.0, "Sequence_abundance": 100.0, "kmers_reassigned": 0, "accession": "GCA_...", "gtdb_taxonomy": "..."}]}]
```

Errors are JSON objects `{"error": "..."}` with a 4xx/5xx status: 400 for bad
parameters, sketches with the wrong `c`/`k` or malformed sketches; 413 when the
body or sketch count exceeds the limit; 415 for non-multipart requests.

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

## Development

```console
uv run pytest
```

Tests use pysylph's small test data copied into `tests/data` (ecoli databases,
K-12 reads, `.sylsp` sketches) and include an in-process Starlette client and
one real Unix-socket round trip against a `sylphd serve` subprocess. The large
GTDB database is only needed for manual runs.

## Credits

- [sylph](https://github.com/bluenote-1577/sylph) by Jim Shaw and Yun William Yu
  does the sketching and profiling (MIT).
- [pysylph](https://github.com/althonos/pysylph) by Martin Larralde provides the
  Python bindings (MIT). The test data in `tests/data` comes from pysylph's
  test suite.
- [GTDB](https://gtdb.ecogenomic.org/) provides the genome metadata, including
  the NCBI fields, that `sylphd download` fetches.

## License

AGPL-3.0-or-later, see `LICENSE`. We are open to negotiating more permissive terms: <https://abrinca.com/contact>.
