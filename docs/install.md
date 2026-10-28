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

If you would rather not install a Rust toolchain at all, install from a
[release](#from-a-release-no-rust-needed) instead: those wheels are prebuilt for CPython 3.14 on
manylinux hosts.

## From a release (no Rust needed)

Every tagged release carries the wheels, so a server needs neither a Rust toolchain nor a C
compiler. Download them with `gh`, point `uv` at the asset list, and install:

```bash
uv venv --python 3.14
uv pip install --find-links https://github.com/Abrinca/sylphd/releases/expanded_assets/v0.1.0 \
               "sylphd==0.1.0"
sylphd --version
```

`--find-links` accepts the release's asset list; `uv` fetches the wheels from GitHub's release
download URLs. The same command works into an existing environment (`uv pip install` without the
`uv venv` line), and for another release version, replace `v0.1.0` and `0.1.0` with the tag and the
version it names.

For an offline machine, or to keep the wheels around, download them first:

```bash
mkdir -p sylphd-wheels && cd sylphd-wheels
gh release download v0.1.0 --pattern '*.whl'
uv pip install --find-links . "sylphd==0.1.0"
```

A release contains:

| Asset | Platform |
| --- | --- |
| `sylphd-X.Y.Z-py3-none-any.whl`, `sylphd-X.Y.Z.tar.gz` | all (pure Python) |
| `pysylph-0.1.3+g<commit>-cp314-cp314-manylinux_2_28_x86_64.whl` | Linux x86_64 |
| `pysylph-0.1.3+g<commit>-cp314-cp314-manylinux_2_28_aarch64.whl` | Linux aarch64 |
| `dnaio-1.2.4-cp314-cp314-manylinux_2_17_aarch64.manylinux_2_28_aarch64.whl` | Linux aarch64 |
| `SHA256SUMS` | checksums for all of the above |

Notes:

- The `pysylph` wheels are built from the exact commit pinned in `uv.lock` (the `multi-db` fork),
  for CPython 3.14, against manylinux_2_28. That glibc floor covers Oracle Linux 9 and 10, RHEL 9
  and 10 and Ubuntu 22.04 and newer. There is no wheel for other Python versions or platforms.
- The `pysylph` version has a local label (`0.1.3+g<commit>`) so it can never be confused with
  PyPI's `pysylph` 0.1.3. Check which one you got:

  ```bash
  python -c "import pysylph; print(pysylph.__version__)"
  ```

  This must print a version ending in `+g<commit>`.
- `dnaio` is a Cython extension and PyPI publishes no linux/aarch64 wheel, so the release carries
  one; `uv` prefers it over building from the `dnaio` sdist.
- To prove that nothing is compiled, install with `--only-binary :all:`: `uv` then fails rather
  than falling back to a source build.

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
