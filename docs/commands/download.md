# download

```text
sylphd download [--release RELEASE] [--outdir DIR] [--only {ar53,bac120}]
                [--base-url URL] [--force]
```

Downloads `bac120_metadata_r{release}.tsv.gz` and `ar53_metadata_r{release}.tsv.gz` from
`https://data.gtdb.ecogenomic.org/releases/release{release}/{release}.0/` and checks each against
the `MD5SUM.txt` of the same directory.

| Option | Default | Meaning |
|---|---|---|
| `--release RELEASE` | `232` | GTDB release |
| `--outdir DIR` | `.` | output directory |
| `--only {ar53,bac120}` | both | download only this domain (repeatable) |
| `--base-url URL` | GTDB | mirror of the GTDB release directory |
| `--force` | off | re-download files that already exist |

Downloads go to `*.part` files and are renamed only after the checksum matches. Existing files are
verified and reused unless `--force` is given. Feed the files to [`sylphd setup`](setup.md).

```bash
sylphd download --release 232 --outdir data
```
