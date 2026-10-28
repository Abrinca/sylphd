# Install

## Requirements

- **Python 3.14** or newer (the project is tested on 3.14 only).
- [uv](https://docs.astral.sh/uv/).
- A **Rust toolchain** (for example via [rustup](https://rustup.rs/)). `sylphd` depends on the
  [pysylph](https://github.com/MrTomRod/pysylph) fork, branch `multi-db`, which is built from git
  at install time. `cmake` is provided automatically in uv's isolated build environment
  (`extra-build-dependencies` in `pyproject.toml`).
- A sylph database: `.syl2db` (two-stage) or `.syldb` (plain). See
  [Quick start](quickstart.md#get-a-database).

Linux on x86_64 and aarch64 is supported. The first install compiles the Rust extension and takes a
few minutes.

## From a clone

```bash
git clone https://github.com/Abrinca/sylphd
cd sylphd
uv sync
uv run sylphd --help
```

## Into another project

```bash
uv add "sylphd @ git+https://github.com/Abrinca/sylphd"
```

## As a tool

```bash
uv tool install "sylphd @ git+https://github.com/Abrinca/sylphd"
sylphd --version
```

The same command works on the server machine and on client machines. Clients need the package too,
because they sketch locally with pysylph.

## Documentation

The documentation is built with [Zensical](https://zensical.org/) from `docs/`:

```bash
uvx zensical serve
```
