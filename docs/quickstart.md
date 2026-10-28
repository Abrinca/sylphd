# Quick start

## Get a database

`sylphd` serves a sylph database that you provide: a two-stage `.syl2db` or a plain `.syldb`. The
recommended database is `gtdb-r232-c200-dbv2.syl2db`: GTDB r232, `c=200`, about 200k species
representatives, about 19 GB. It comes from the
[sylph project](https://sylph-docs.github.io/pre%E2%80%90built-databases/), which builds and hosts
the pre-sketched databases; `sylphd download` only knows where to get them.

```bash
sylphd download --database gtdb-r232-c200-dbv2.syl2db --outdir data
```

Without `--database` the same command fetches the GTDB metadata instead; see below. If
[`aria2c`](https://aria2.github.io/) is installed, the download uses it with all mirrors at once;
otherwise a resumable built-in downloader is used. `--list-databases` shows the other GTDB
releases (r226, r220, r214), GlobDB, viral and eukaryotic databases. See
[`download`](commands/download.md).

The server reads `c` and `k` from the database, and clients sketch with the same values.

## Get metadata

The metadata database is optional but recommended: it adds GTDB and NCBI taxonomy, genome quality
and more to every hit, and it is required for [taxonomic profiles](guides/taxonomic-profiles.md)
and for species assignment in [`sylphd assembly`](guides/assembly.md).

```bash
sylphd download --release 232 --outdir data
sylphd setup -m data/bac120_metadata_r232.tsv.gz data/ar53_metadata_r232.tsv.gz \
             -o data/gtdb-r232.sqlite --release 232
```

`download` checks each metadata file against GTDB's `MD5SUM.txt`. `setup` builds a SQLite file of
about 200 MB for the full release (about 80 MB with `--reps-only`). See
[`download`](commands/download.md), [`setup`](commands/setup.md) and the
[metadata database](reference/metadata.md).

## Serve

```bash
sylphd serve --db data/gtdb-r232-c200-dbv2.syl2db --meta data/gtdb-r232.sqlite
```

The server listens on a Unix socket, by default `$XDG_RUNTIME_DIR/sylphd.sock` (or
`/tmp/sylphd-$UID.sock` if `XDG_RUNTIME_DIR` is not set). Startup takes a few seconds for the
database to load. The socket is created with mode `0660`: its owner and group can connect.

Use `--socket PATH` for another socket, or `--tcp HOST:PORT` to listen on TCP.

## Profile reads

In another shell:

```bash
sylphd info

# ANI hits (sylph query)
sylphd query -1 sample_R1.fq.gz -2 sample_R2.fq.gz

# taxonomic abundances (sylph profile)
sylphd profile -r sample.fq.gz -o sample.profile.tsv
```

Clients find the server at `$SYLPHD_SOCKET`, else the default socket path. A client on another
machine uses `--tcp HOST:PORT` or `--url`; see [Server address](commands/address.md). The output is a
TSV with sylph's columns followed by `accession` and the metadata columns. More in
[Profiling reads](guides/profiling-reads.md).

## Identify an assembly

The endpoint behind `sylphd assembly` and the [web app](https://abrinca.github.io/sylphd/) takes
the sketch of an assembly, which both make locally, so a plain server is enough:

```bash
sylphd serve --db data/gtdb-r232-c200-dbv2.syl2db --meta data/gtdb-r232.sqlite
```

(`--allow-sequences` is only needed if raw FASTA uploads from older clients should work too.)

```bash
sylphd assembly assembly.fasta --top 5
```

The table goes to stdout (tab-separated), and one summary line to stderr:

```text
rank  ani    relation      shared_rank  accession        gtdb_species      ncbi_organism_name     ncbi_taxid
1     99.53  same species  species      GCA_000005845.2  Escherichia coli  Escherichia coli K-12  562
...
assigned to species Escherichia coli (99.53 % ANI)
```

See [Identifying an assembly](guides/assembly.md).
