# download

```text
sylphd download [--database NAME] [--url URL] [--outdir DIR]
                [--list-databases] [--verify] [--no-aria2c] [--force]

sylphd download [--release RELEASE] [--outdir DIR] [--only {ar53,bac120}]
                [--base-url URL] [--force]
```

Without `--database` or `--url`, `download` fetches the GTDB metadata files
`bac120_metadata_r{release}.tsv.gz` and `ar53_metadata_r{release}.tsv.gz` from
`https://data.gtdb.ecogenomic.org/releases/release{release}/{release}.0/` and checks each against
the `MD5SUM.txt` of the same directory.

With `--database NAME`, it fetches a pre-built sylph database (`.syl2db` or `.syldb`) from the
built-in catalogue. The databases are built and hosted by the
[sylph project](https://sylph-docs.github.io/pre%E2%80%90built-databases/); `sylphd` only knows
their names, sizes and mirrors.

| Option | Default | Meaning |
|---|---|---|
| `--database NAME` | none | pre-built database to fetch (repeatable); `list` prints the catalogue |
| `--url URL` | catalogue mirrors | fetch from this source instead of the catalogue mirrors (repeatable; the file name is taken from the URL, so two URLs must not end in the same name) |
| `--list-databases` | off | print the catalogue and exit |
| `--outdir DIR` | `.` | output directory |
| `--verify` | off | also compare the MD5 with the catalogue entry |
| `--no-aria2c` | off | never use `aria2c`, even if it is installed |
| `--force` | off | re-download files that already exist |
| `--release RELEASE` | `232` | GTDB metadata release |
| `--only {ar53,bac120}` | both | download only this metadata domain (repeatable) |
| `--base-url URL` | GTDB | mirror of the GTDB metadata release directory |

```bash
# the recommended database: GTDB r232, c200, two-stage (about 19 GB)
sylphd download --database gtdb-r232-c200-dbv2.syl2db --outdir data

# what is available?
sylphd download --list-databases

# fetch from your own mirror, and verify the checksum
sylphd download --database gtdb-r232-c200-dbv2.syl2db \
                --url https://mirror.example.org/gtdb-r232-c200-dbv2.syl2db --verify
```

## How files are verified

For databases, the size in the catalogue is checked first; it is instant and always on. With
`--verify`, `sylphd` also computes the MD5 in 8 MiB chunks and compares it with the catalogue entry
when one is published. **That reads the whole file**: a 20 GB database takes minutes on a slow
disk. Some mirrors publish no MD5 for a file; `--verify` then falls back to the size check and says
so.

An existing file with the right size is kept unless `--force` is given, so re-running a command
resumes nothing and starts nothing. Metadata files keep their `MD5SUM.txt` verification.

## Resuming and mirrors

Transfers go to `FILE.part` and are renamed only after the size (and checksum) matches. An
interrupted download resumes from where it stopped using an HTTP `Range` request; a server that
ignores `Range` (or answers without a matching `Content-Range`) simply sends the file again. If a
mirror fails (or its file has the wrong size or checksum), the next one is tried.

If [`aria2c`](https://aria2.github.io/) is on `PATH`, database downloads use it instead, with all
mirrors at once and sixteen connections splitting each file:

```bash
aria2c -s 16 -x 8 -k 5M -c --file-allocation=none -d data -o FILE URL1 URL2
```

`aria2c` prints its own progress and can resume from its `.aria2` control file, which is kept when a
transfer fails or is interrupted. The built-in downloader never resumes such a file: it starts over.
Pass `--no-aria2c` to use the built-in downloader.

Feed the downloaded database to [`sylphd serve`](serve.md) and the metadata files to
[`sylphd setup`](setup.md):

```bash
sylphd download --database gtdb-r232-c200-dbv2.syl2db --outdir data
sylphd download --release 232 --outdir data
sylphd setup -m data/bac120_metadata_r232.tsv.gz data/ar53_metadata_r232.tsv.gz \
             -o data/gtdb-r232.sqlite --release 232
sylphd serve --db data/gtdb-r232-c200-dbv2.syl2db --meta data/gtdb-r232.sqlite
```
