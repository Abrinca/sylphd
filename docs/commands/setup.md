# setup

```text
sylphd setup -m FILE... -o PATH [--release N] [--reps-only] [--force] [-q]
```

Streams GTDB metadata TSV files (gzip or plain) into a SQLite file that `sylphd serve --meta` joins
to every result. The layout and columns are in the [metadata database](../reference/metadata.md)
reference.

| Option | Default | Meaning |
|---|---|---|
| `-m, --metadata FILE ...` | required | GTDB metadata TSV files (`*.tsv.gz`) |
| `-o, --output PATH` | required | SQLite file to write |
| `--release N` | none | GTDB release to record in the file |
| `--reps-only` | off | store only species representatives (`gtdb_representative` true) |
| `--force` | off | replace the output file (only once the new one is built; a failure keeps the old one) |
| `-q, --quiet` | off | no progress output |

```bash
sylphd setup -m data/bac120_metadata_r232.tsv.gz data/ar53_metadata_r232.tsv.gz \
             -o data/gtdb-r232.sqlite --release 232
```

All ~900k GTDB genomes are stored by default (about 200 MB). `--reps-only` stores about 200k
(about 80 MB); lookups of other accessions then find no metadata. When two input files contain the
same accession, the later row wins. Without `--force` an existing output file is not replaced.
