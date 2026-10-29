# download

```text
sylphd download [--database NAME] [--taxonomy NAME] [--url URL] [--outdir DIR]
                [--list-databases] [--list-taxonomies] [--verify] [--no-aria2c]
                [--force]

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

With `--taxonomy NAME`, it fetches a sylph-tax taxonomy file from the second catalogue. The names
are sylph-tax's (`UHGV_default`, `UHGV_ictv`, `FungiRefSeq-latest`, `GTDB_r232`, `GlobDB_r232`, ...);
`--list-taxonomies` prints them with size, row count and the database each one is paired with.
Most files come from the [sylph-tax Zenodo records](https://zenodo.org/records/14320496), which
publish a size and MD5 per file; GlobDB r232 lives on `fileshare.lisc.univie.ac.at` and has no
published MD5, so only its size is checked.

| Option | Default | Meaning |
|---|---|---|
| `--database NAME` | none | pre-built database to fetch (repeatable); `list` prints the catalogue |
| `--taxonomy NAME` | none | sylph-tax taxonomy file to fetch (repeatable); `list` prints the catalogue |
| `--url URL` | catalogue mirrors | fetch this database from this source instead of the catalogue mirrors (repeatable; the file name is taken from the URL, so two URLs must not end in the same name); with `--database`, applies to that database |
| `--list-databases` | off | print the database catalogue and exit |
| `--list-taxonomies` | off | print the taxonomy catalogue and exit |
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

# taxonomy files for the viral and fungal databases (a few MB each)
sylphd download --taxonomy UHGV_default --taxonomy UHGV_ictv \
                --taxonomy FungiRefSeq-latest --outdir data

# what is available?
sylphd download --list-databases
sylphd download --list-taxonomies

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

Taxonomy files are small (12 KB to 27 MB), so their size and MD5 are **always** checked; there is
no `--verify` switch for them. Sizes and checksums were read from the Zenodo API
(`/api/records/<id>`) when the catalogue was written. If a catalogue entry lacks them, `sylphd`
asks the Zenodo API at download time. An entry that ends up with no MD5 is refused — a size alone
cannot tell the published file from a corrupted or replaced one — except GlobDB r232, whose
publisher offers no checksum, so it is checked by size only.

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
sylphd download --taxonomy UHGV_default --taxonomy FungiRefSeq-latest --outdir data/taxonomy
sylphd setup -m data/bac120_metadata_r232.tsv.gz data/ar53_metadata_r232.tsv.gz \
             --taxonomy UHGV_default --taxonomy FungiRefSeq-latest \
             --cache-dir data/taxonomy -o data/meta.sqlite --release 232
sylphd serve --db data/gtdb-r232-c200-dbv2.syl2db --meta data/meta.sqlite
```

`sylphd setup --taxonomy NAME` downloads a missing file itself, so a separate `download
--taxonomy` is only needed when you want to see the files in advance.
