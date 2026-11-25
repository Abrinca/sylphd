# Quick start

## Install

```bash
uv tool install 'sylphd[client]'
sylphd --version
```

The `client` extra installs `sylphd` (pure Python) and `sylphd-bin`, the native client: no Rust
toolchain, no pysylph, no compiler. The machine that runs `sylphd serve` installs
`sylphd[server]` instead. See [Install](install.md) for the other platforms and the standalone binary.

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

The public server uses `globdb_r232_sylph_v2_c200.syl2db` instead: GlobDB r232 (346,232 genomes, 31.2
GB, GTDB taxonomy), which needs about 8 GiB of RAM against 6 GiB for GTDB. See the
[GlobDB evaluation](project/globdb-evaluation.md) and [`deploy/README.md`](https://github.com/Abrinca/sylphd/blob/main/deploy/README.md#globdb-configuration-the-public-instance)
before choosing.

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

The sketching and the tables are the native `sylphd-client`'s (`sylphd` forwards to it); every
command takes `--json` for the server's document, and `sylphd render FILE.json` turns such a
document back into the table, without a server.

Clients find the server at `$SYLPHD_SOCKET`, else the default socket path. A client on another
machine uses `--tcp HOST:PORT` or `--url`; see [Server address](commands/address.md). The output is a
TSV with sylph's columns followed by `accession` and the metadata columns. More in
[Profiling reads](guides/profiling-reads.md). To choose between `query`, `profile` and `assembly`, see
[Which one to use](guides/profiling-reads.md#which-one-to-use).

## Identify an assembly

The endpoint behind `sylphd assembly` and the [web app](https://abrinca.github.io/sylphd/) takes
the sketch of an assembly, which both make locally, so a plain server is enough:

```bash
sylphd serve --db data/gtdb-r232-c200-dbv2.syl2db --meta data/gtdb-r232.sqlite
```

(`--allow-sequences` additionally accepts raw FASTA, for `curl` and `sylphd assembly --no-local-sketch`.)

```bash
sylphd assembly assembly.fasta --top 5
```

The table goes to stdout (tab-separated), and one summary line to stderr:

```text
rank  ani    relation      shared_rank  accession        genome_id        taxon             gtdb_species      taxonomy_source  ncbi_organism_name     ncbi_taxid
1     99.53  same species  species      GCA_000005845.2  GCA_000005845.2  Escherichia coli  Escherichia coli  GTDB_r232        Escherichia coli K-12  562
...
assigned to species Escherichia coli (99.53 % ANI)
```

See [Identifying an assembly](guides/assembly.md).
