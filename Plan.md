# Plan: sylph server and client (`sylphd`)

A small standalone, public repo and Python package (`sylphd`, GitHub `MrTomRod/sylphd`). A long-running server keeps one sylph database loaded in memory. Clients sketch their samples locally, send the sketches, and get rich result rows back.

Managed by uv. Python >= 3.14, tested on 3.14 only.

## Why

- Every process that uses pysylph deserializes the database into its own memory. For GTDB r232 c200 that takes about 2 s and about 2 GB of RAM per process (the screening index; the 20 GB second stage is read lazily from the file and shared via the page cache).
- A server pays this once and shares one copy across runs, parallel jobs and users.

## Target

- Database: `gtdb-r232-c200-dbv2.syl2db` (GTDB r232, two-stage, ~200k species representatives). Copy from a local copy (CoW filesystem).
- One database per server process.
- pysylph: fork `MrTomRod/pysylph` branch `multi-db` (local checkout `ref/pysylph`), which depends on `MrTomRod/sylph` branch `expose-library-api` (`ref/sylph`). uv git dependency (builds with Rust at install time).

## Decisions

| Topic | Decision |
|---|---|
| CLI | `sylphd serve`, `sylphd profile`, `sylphd query`, `sylphd info`, `sylphd download`, `sylphd setup` |
| Transport | HTTP over a Unix socket by default; `--tcp host:port` option. No TLS, no auth. |
| Server stack | Starlette + uvicorn, exactly one worker process. Blocking pysylph calls in a thread pool; a semaphore caps concurrent profiling jobs (queue the rest). New `Profiler` per request (cheap; holds references only). |
| Sketch transfer | Add `SampleSketch.to_bytes()` / `SampleSketch.from_bytes()` to the pysylph `multi-db` branch. Server enforces an upload size limit and checks sketch `c`/`k` against the database. |
| Requests | Several samples per request. |
| Responses | JSON rows from the server; the CLI writes TSV (sylph-like columns + metadata columns). |
| DB identity | Database file name and size (optional hash, off by default). Reported by `/info`. |
| Fallback | None. The client requires a server. |
| Service | Plain `sylphd serve`; no systemd unit. |
| Tests | pytest on Python 3.14 with pysylph's small test data (`ecoli.syl2db`, sample reads). GTDB only for manual runs. |

## Architecture

- **Server** (`sylphd serve --db X.syl2db --meta X.sqlite [--socket PATH | --tcp HOST:PORT]`): loads the database at startup. Endpoints:
  - `GET /health`;
  - `GET /info`: database name, size, number of genomes, `c`, `k`, `screen_c`, metadata DB info, versions of sylphd/pysylph;
  - `POST /profile` and `POST /query`: one or more uploaded sketches plus the usual sylph thresholds (`minimum_ani`, `seq_id`, `estimate_unknown`, `min_number_kmers`, `min_contain`, `screen_ani`).
- **Client library and CLI:**
  - asks the server for `c`/`k`, then sketches locally;
  - inputs: single-end reads (also long reads, assemblies) `-r`, paired `-1/-2`, interleaved paired `--interleaved`, several files per sample (concatenated into one sketch), and existing `.sylsp` files (sent as they are);
  - FASTA/FASTQ via dnaio;
  - sends the sketches and writes/returns the rows.
- **Why sketches and not reads:** sketching is per-sample CPU work and belongs to the client. Sketches are small (about 1–2 MB for a 40 Mb read pool). No file paths cross the boundary.

## Outputs

Each result row combines:
1. **sylph:** sample name, genome file, ANI (adjusted, naive), ANI CI, effective coverage, lambda CI/status, median/mean coverage, contained/total k-mers; for `profile` also sequence/taxonomic abundance and k-mers reassigned.
2. **Metadata** (joined by accession, see below), selectable with `--fields`.

## Metadata DB

- `sylphd download [--release 232]`: fetches `bac120_metadata_r232.tsv.gz` and `ar53_metadata_r232.tsv.gz` from `data.gtdb.ecogenomic.org` and checks them against `MD5SUM.txt`.
- `sylphd setup`: builds a SQLite file (view `metadata` over a `WITHOUT ROWID` base table keyed by `accession`; repetitive text is dictionary-encoded in `dict_*` tables) from those files. It stores all ~900k genomes unless `--reps-only` is given. GTDB's `none`/`na` become NULL.
- Accession key: strip `RS_`/`GB_` from GTDB accessions. Sylph genome names (`.../GCA_964372435.1_genomic.fna.gz`) map to their file name without `_genomic.fna.gz`.
- Columns:
  - GTDB: `gtdb_taxonomy`, `gtdb_representative`, `gtdb_genome_representative`, `gtdb_type_species_of_genus`, `gtdb_type_designation_ncbi_taxa`
  - NCBI: `ncbi_taxid`, `ncbi_species_taxid`, `ncbi_taxonomy`, `ncbi_taxonomy_unfiltered`, `ncbi_organism_name`, `ncbi_strain_identifiers`, `ncbi_isolate`, `ncbi_genome_category`, `ncbi_assembly_level`, `ncbi_refseq_category`, `ncbi_type_material_designation`, `ncbi_biosample`, `ncbi_bioproject`
  - Quality: `checkm2_completeness`, `checkm2_contamination`, `genome_size`, `gc_percentage`, `contig_count`, `n50_contigs`, `mimag_high_quality`
- No NCBI lineage TaxIDs / taxdump. Taxonomy strings stay as one column each (no per-rank split for now).

## Phases

1. pysylph: `SampleSketch.to_bytes()/from_bytes()` on `multi-db`, with tests; push.
2. sylphd: package skeleton, `download`, `setup`, server, client, CLI, tests; push to `MrTomRod/sylphd` (public).
3. Manual end-to-end run against GTDB r232 c200.
4. Stretch: `merge` (sum taxonomic abundance per GTDB or NCBI rank).
