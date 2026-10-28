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
