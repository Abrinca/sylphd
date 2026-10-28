# <img src="web/img/sylphd-icon-tile.svg" alt="" width="40" height="40" align="top"> sylphd

A small server and client around [sylph](https://github.com/bluenote-1577/sylph): one
long-running server keeps a sylph database in memory, while clients sketch samples locally and
send only the sketches. This avoids paying the database deserialisation cost (about 2 s and 2 GB
of RAM for GTDB r232 c200) in every process. Results come back as TSV or JSON, joined with GTDB
metadata, and the client can build `sylph-tax`-style taxonomic profiles.

- **Web app:** <https://abrinca.github.io/sylphd/>: paste an assembly, get its closest GTDB genomes.
- **Documentation:** <https://abrinca.github.io/sylphd/docs/>

The client and server speak HTTP over a Unix socket by default; clients can also use a TCP address
or an `http://`/`https://` URL (`--url`). The server has no TLS and no authentication: run it on a
machine you trust, or behind a reverse proxy.

## Install

Requires Python 3.14, [uv](https://docs.astral.sh/uv/) and a Rust toolchain (the
[pysylph](https://github.com/MrTomRod/pysylph) fork, branch `multi-db`, is built from git at
install time). You also need a sylph database (`.syl2db` or `.syldb`).

```console
git clone https://github.com/Abrinca/sylphd && cd sylphd
uv sync
uv run sylphd --help
```

or, in another project: `uv add "sylphd @ git+https://github.com/Abrinca/sylphd"`.

## Quick start

```console
# 1. Get the recommended database (GTDB r232 c200, ~19 GB) and the GTDB metadata
sylphd download --database gtdb-r232-c200-dbv2.syl2db --outdir data
sylphd download --release 232 --outdir data
sylphd setup -m data/bac120_metadata_r232.tsv.gz data/ar53_metadata_r232.tsv.gz \
             -o data/gtdb-r232.sqlite --release 232

# 2. Serve it
sylphd serve --db data/gtdb-r232-c200-dbv2.syl2db --meta data/gtdb-r232.sqlite

# 3. In another shell: sketch locally and ask the server
sylphd query -1 sample_R1.fq.gz -2 sample_R2.fq.gz
sylphd profile -r sample.fq.gz -o sample.profile.tsv
```

`sylphd download --list-databases` shows the other pre-built databases (GTDB r226/r220/r214,
GlobDB, viral and eukaryotic catalogues). They are built and hosted by the
[sylph project](https://sylph-docs.github.io/pre%E2%80%90built-databases/); the downloader uses
[`aria2c`](https://aria2.github.io/) when it is installed and resumes interrupted transfers
otherwise.

The default address is `$XDG_RUNTIME_DIR/sylphd.sock`. To identify an assembly, start the server
with `--allow-sequences` and run `sylphd assembly assembly.fasta`.

## Documentation

The [documentation](https://abrinca.github.io/sylphd/docs/) covers installation, a quick start,
guides (profiling reads, identifying an assembly, taxonomic profiles, running a public server),
the commands, the HTTP API, the metadata database and security notes. It is built with
[Zensical](https://zensical.org/) from `docs/`: `uvx zensical serve`.

## Development

```console
uv run pytest
```

The tests use pysylph's small test data, copied into `tests/data`.

## How to cite

You are using sylphd. If it helps your research, please cite sylphd, pysylph, sylph (Shaw and Yu,
*Nature Biotechnology* 2024, [doi:10.1038/s41587-024-02412-y](https://doi.org/10.1038/s41587-024-02412-y))
and GTDB. The full list with all references is in the
[documentation](https://abrinca.github.io/sylphd/docs/project/citing/) and at the bottom of the web
app.

## License

sylphd is licensed under the GNU Affero General Public License, version 3 or later
(AGPL-3.0-or-later); see [`LICENSE`](LICENSE). We are open to negotiating more permissive terms:
<https://abrinca.com/contact>. Use of the public web service is covered by its
[terms](https://abrinca.github.io/sylphd/terms.html).
