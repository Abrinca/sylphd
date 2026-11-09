# setup

```text
sylphd setup [-m FILE...] [--taxonomy SPEC...] -o PATH [--release N] [--reps-only]
             [--update] [--cache-dir DIR] [--ncbi-taxonomy] [--force] [-q]

SPEC := NAME | NAME=PATH | PATH
```

Streams GTDB metadata TSV files (gzip or plain) into a SQLite file that `sylphd serve --meta` joins
to every result, and imports [sylph-tax](https://github.com/bluenote-1577/sylph-tax) taxonomy files
next to it. The imported sources are stored now so that a later release can resolve hits from
non-GTDB databases (UHGV viruses, fungi, MAGs, ...) to a lineage and a host; `setup` itself only
stores and reports them. The layout and columns are in the
[metadata database](../reference/metadata.md) reference.

| Option | Default | Meaning |
|---|---|---|
| `-m, --metadata FILE ...` | none | GTDB metadata TSV files (`*.tsv.gz`) |
| `--taxonomy SPEC` | none | taxonomy source, repeatable: a sylph-tax name, `NAME=PATH` or `PATH` |
| `-o, --output PATH` | required | SQLite file to write |
| `--update` | off | add the sources to an existing file instead of building one |
| `--cache-dir DIR` | `<output dir>/taxonomy` | where downloaded taxonomy files are kept |
| `--ncbi-taxonomy` | off | give the sylph-tax genomes that GTDB does not cover an NCBI lineage |
| `--release N` | none | GTDB release to record in the file (and the name of the virtual source) |
| `--reps-only` | off | store only species representatives (`gtdb_representative` true), `-m` only |
| `--force` | off | replace the output file (only once the new one is built; a failure keeps the old one) |
| `-q, --quiet` | off | no progress output |

At least one of `-m` and `--taxonomy` is required. `SPEC` is:

- a **sylph-tax name**: `UHGV_default`, `UHGV_ictv`, `FungiRefSeq-latest`, `GTDB_r232`,
  `GlobDB_r232`, ... (`sylphd download --list-taxonomies` prints all of them with sizes, row counts
  and the database each one goes with). The file is downloaded into `--cache-dir`, resuming
  partial transfers and verifying the size (and MD5 when the publisher has one) before it is used;
- **`NAME=PATH`**: a local file under a chosen source name, read as it is (gzip is detected by
  content). Useful to override a name or to import your own file;
- **`PATH`**: a local file; the source name defaults to the file stem
  (`uhgv_default_metadata.tsv.gz` → `uhgv_default_metadata`).

```bash
# GTDB metadata plus UHGV and fungal lineages for the target trio
sylphd setup -m data/bac120_metadata_r232.tsv.gz data/ar53_metadata_r232.tsv.gz \
             --taxonomy UHGV_default --taxonomy FungiRefSeq-latest \
             -o data/meta.sqlite --release 232

# a local file under a chosen name
sylphd setup -m data/*.tsv.gz --taxonomy UHGV=./my_uhgv.tsv.gz -o data/meta.sqlite

# add sources to an existing file ; atomic
sylphd setup --update --taxonomy UHGV_ictv -o data/meta.sqlite
```

All ~900k GTDB genomes are stored by default (about 200 MB). `--reps-only` stores about 200k
(about 80 MB); lookups of other accessions then find no metadata. When two input files contain the
same accession, the later row wins.

The GTDB metadata becomes the **virtual** source `GTDB_r<release>`: its lineages are not stored a
second time, so importing the sylph-tax catalogue file of the *same* release (`GTDB_r232` with
`--release 232`) is redundant and that source is skipped with a message. A catalogue file of a
different release (`GTDB_r214`) and a local file of any other name are imported normally; a local
file named exactly like the virtual source is refused, because importing it would replace it.
Sources are stored in the order given; for a key found in two sources the later one wins (as the
later file wins in sylph-tax). Re-importing a source name replaces it.

**Import one taxonomy per database.** Overlapping sources are accepted, never silently: after each
import `setup` counts the keys the source shares with the sources already in the file — including
the virtual GTDB source, whose keys are the metadata accessions — and, for every pair that shares
keys, prints a warning naming both sources, the number of shared keys, the first five keys and the
rule that applies: *the source imported later wins for these keys; import only one of them, or
rebuild without one*. The import still succeeds (a warning, not an error); the counts are stored in
the file and shown by [`sylphd info`](info.md) and `GET /info`. This is the same collision that
`profile --tax-source NAME` can work around per request.

`--update` copies the existing file (the `-o` path, or `-m` when you want a separate output) and
adds the sources; it needs no GTDB TSV files. The copy is written next to the output and replaces
it only on success, so an interrupted or failed update leaves the old file untouched.
`--release` and `--reps-only` cannot be used with `--update`.

## NCBI lineages

GTDB's metadata has no row for a genome of another source (a `FungiRefSeq-latest` key
`GCF_000002495.2_MG8`, say), so its NCBI taxonomy view, NCBI Taxonomy links and `ncbi_*` columns
would be empty. `--ncbi-taxonomy` fills them, for a build and for `--update`:

```bash
sylphd setup -m data/bac120_metadata_r232.tsv.gz \
             --taxonomy FungiRefSeq-latest --ncbi-taxonomy \
             -o data/meta.sqlite --release 232 --cache-dir data/cache
```

Each run downloads NCBI's `new_taxdump.tar.gz` and streams the assembly summary that can hold the
needed accessions (`ftp.ncbi.nlm.nih.gov/pub/taxonomy/new_taxdump/` and `/genomes/ASSEMBLY_REPORTS/`):
RefSeq for `GCF_` accessions, GenBank for `GCA_` ones. The taxdump's `rankedlineage.dmp`, `nodes.dmp`
and `merged.dmp` are extracted into a temporary directory under `--cache-dir` and deleted with the
tarball when the run ends, so nothing NCBI is kept (and nothing is stale); the summary is streamed
and only the requested accessions are kept, so the (about 1.8 GB) GenBank summary is never stored
and is not fetched when only RefSeq accessions are needed. The sizes are checked against the
servers' `Content-Length`.

For every key of every sylph-tax source whose assembly accession has no GTDB metadata row (a GTDB
row is never replaced), the assembly accession in the key (`GC[AF]_\d+\.\d+`) is matched exactly (version included) against the summaries; if the
version is missing there, the same accession base is used only when the source requests a single
version of it, else the key is skipped. The taxid then comes from the summary and the lineage from
`rankedlineage.dmp` (a merged taxid is followed through `merged.dmp`), with `nodes.dmp` filling the node's own rank (a species node's species
column in `rankedlineage.dmp` holds its ancestors and is empty) — never from an organism name. `ncbi_taxonomy` keeps GTDB's
`d__…;p__…;c__…;o__…;f__…;g__…;s__…` shape, with NCBI's superkingdom (or domain) as `d__`; NCBI's
kingdom is not one of the seven ranks and is left out. The rows are stored like GTDB metadata rows
(accession key, GTDB columns `NULL`), so every reader finds them without a change; GTDB rows keep
their own NCBI data. Every `--ncbi-taxonomy` run deletes and rebuilds these rows, so running
`--update --ncbi-taxonomy` again is idempotent. `--update` without the flag keeps them, deletes the
ones no source key refers to any more and drops the counts of the sources it replaced.

The summary reports, per source, how many keys were mapped, how many carry no assembly accession,
how many are not in the summaries and how many have a taxid without a lineage, e.g.:

```text
  FungiRefSeq-latest: 661 keys (sylphtax)
    NCBI lineage: 660 mapped, 0 without an assembly accession, 1 not in the summaries, 0 without a lineage
```

`GET /info` and `sylphd info` report the same counts (`metadata.taxonomy_sources[].ncbi`). The
downloads happen only at `setup` time; serving does no extra work.
