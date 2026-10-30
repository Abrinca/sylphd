# setup

```text
sylphd setup [-m FILE...] [--taxonomy SPEC...] -o PATH [--release N] [--reps-only]
             [--update] [--cache-dir DIR] [--force] [-q]

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
