# Install

`sylphd` is two packages:

| Package | What it is | Needs |
|---|---|---|
| `sylphd` | the `sylphd` command and the server: pure Python (`py3-none-any`) | Python 3.14 or newer |
| `sylphd-bin` | `sylphd-client`, the native client that does the sketching and the result tables, packaged as a platform wheel (`py3-none-<platform>`) | nothing: it is a static binary |

The `sylphd` command runs the server and admin commands in Python and **forwards every query
command** (`info`, `profile`, `query`, `assembly`, `render`) to `sylphd-client`. Nothing on the
client side needs a Rust toolchain, a C compiler or `cmake`; the binary arrives as a wheel.

## Which extra

| Extra | For | Brings |
|---|---|---|
| `sylphd[client]` | machines that profile against a server: `profile`, `query`, `assembly`, `info` | `sylphd-bin` (the native client), and with it Python 3.14 |
| `sylphd[server]` | the machine that runs `sylphd serve`, `setup` and `download` | everything `client` has, plus `pysylph`, `dnaio`, `httpx`, `starlette`, `uvicorn`, `python-multipart` and [`omnitax`](guides/ncbi-taxonomy.md) (the taxonomies the server serves, and that `setup` reads) |

The base install (`pip install sylphd`) has no dependencies: it runs `sylphd diff` and `sylphd taxmatrix`, and
says what to install for the rest. To look taxa up on a server you need no sylphd at all: install
[`omnitax`](guides/ncbi-taxonomy.md).

## Requirements

- **A platform wheel for `sylphd-bin`**: Linux x86_64 and aarch64 (musl or glibc), macOS arm64 and
  x86_64, Windows x86_64. Nothing is compiled on your machine.
- **Python 3.14** for the `sylphd` command (only the thin wrapper; the client itself is a binary).
  `uv tool install` fetches Python 3.14 for you.
- [uv](https://docs.astral.sh/uv/) (recommended), or pip.
- A sylph database for the server: `.syl2db` (two-stage) or `.syldb` (plain). See
  [Quick start](quickstart.md#get-a-database).

The **server** still needs `pysylph` and `dnaio`, which are Rust and Cython extensions. Take them
from a [release](#from-a-release-no-compiler-needed); building them from source needs a Rust
toolchain and `cmake`. With the release wheels the server installs with `--only-binary :all:` too.

## The client, in one line

```bash
uv tool install 'sylphd[client]'
sylphd --version
sylphd info --url https://sylphd.abrinca.com
```

That is the whole client: `uv` brings Python 3.14, the `sylphd-bin` platform wheel brings the
native binary, and no compiler runs. With pip: `pip install 'sylphd[client]'`.

## From a release (no compiler needed)

Every tagged release on [GitHub](https://github.com/Abrinca/sylphd/releases) carries the same
wheels, the server's `pysylph` and `dnaio` wheels (the server install needs them, see above) and
the standalone binary. Install straight from the asset list, or download the wheels first for an
offline machine:

```bash
# online: uv fetches the wheels from GitHub's release URLs
uv venv --python 3.14
uv pip install --find-links https://github.com/Abrinca/sylphd/releases/expanded_assets/vX.Y.Z \
               "sylphd[server]==X.Y.Z"

# offline: keep the wheels next to the machine
mkdir -p sylphd-wheels && cd sylphd-wheels
gh release download --repo Abrinca/sylphd --pattern '*.whl'
uv pip install --find-links . "sylphd[client]"
```

A release contains:

| Asset | Platform |
| --- | --- |
| `sylphd-X.Y.Z-py3-none-any.whl`, `sylphd-X.Y.Z.tar.gz` | all (pure Python) |
| `sylphd_bin-X.Y.Z-py3-none-manylinux_2_28_x86_64.whl` | Linux x86_64 (glibc) |
| `sylphd_bin-X.Y.Z-py3-none-manylinux_2_28_aarch64.whl` | Linux aarch64 (glibc) |
| `sylphd_bin-X.Y.Z-py3-none-musllinux_1_2_x86_64.whl` | Linux x86_64 (musl) |
| `sylphd_bin-X.Y.Z-py3-none-musllinux_1_2_aarch64.whl` | Linux aarch64 (musl) |
| `sylphd_bin-X.Y.Z-py3-none-macosx_11_0_arm64.whl` | macOS arm64 |
| `sylphd_bin-X.Y.Z-py3-none-macosx_11_0_x86_64.whl` | macOS x86_64 |
| `sylphd_bin-X.Y.Z-py3-none-win_amd64.whl` | Windows x86_64 |
| `sylphd-client-X.Y.Z-<target>.tar.gz` / `.zip` | the standalone binary (no Python) |
| `pysylph-0.1.3+g<commit>-cp314-cp314-manylinux_2_28_<arch>.whl` | the server's profiler, Linux only |
| `dnaio-<version>-cp314-cp314-manylinux_2_28_aarch64.whl` | the server's parser, Linux aarch64 |
| `SHA256SUMS` | checksums for all of the above |

Notes:

- A `sylphd-bin` wheel serves **every** Python 3 (it has no extension module), so it is not tied to
  CPython 3.14 the way `pysylph` is.
- The `pysylph` wheels are built from the exact commit pinned in `uv.lock` (the `multi-db` fork) for
  CPython 3.14, against manylinux_2_28. That glibc floor covers RHEL 9/10, Oracle Linux 9/10 and
  Ubuntu 22.04 or newer. The version carries a local label (`0.1.3+g<commit>`) so it cannot be
  confused with PyPI's `pysylph` 0.1.3: `python -c "import pysylph; print(pysylph.__version__)"`
  must end in `+g<commit>`.
- `dnaio` is a Cython extension and PyPI publishes no linux/aarch64 wheel, so the release carries
  one.
- To prove that nothing is compiled, install with `--only-binary :all:`: `uv` then fails instead of
  falling back to a source build.

## The standalone binary

Machines without Python (or without a writable Python environment) use the archive:

```bash
curl -fsSL -o sylphd-client.tar.gz \
  https://github.com/Abrinca/sylphd/releases/download/vX.Y.Z/sylphd-client-X.Y.Z-x86_64-unknown-linux-musl.tar.gz
tar -xzf sylphd-client.tar.gz
./sylphd-client --help
```

It is the same program as `sylphd-bin`'s `sylphd-client`, with the same commands and exit codes,
except that the Python commands (`serve`, `setup`, `download`, `taxmatrix`,
`diff`) do not exist there: it carries the query commands and `render` only. On Windows use the
`.zip`; on macOS the archive matching `uname -m`.

## With Docker

Every release also publishes a container image for linux/amd64 and linux/arm64, with the wheels
above installed into `ghcr.io/astral-sh/uv:python3.14-trixie-slim`. The image is bare: no default
command, no exposed port and no user, so you say what to run. Keep the databases in a volume:

```bash
docker run --rm -v ./data:/data -p 127.0.0.1:8080:8080 abrincagenomics/sylphd:X.Y.Z \
    sylphd serve --tcp 0.0.0.0:8080 --db /data/gtdb-r232-c200-dbv2.syl2db \
                 --meta /data/gtdb-r232.sqlite
```

## From a clone

```bash
git clone https://github.com/Abrinca/sylphd
cd sylphd
uv sync        # builds sylphd-bin from rust/sylphd-client (needs a Rust toolchain)
uv run sylphd --help
```

A clone is the only case that needs Rust: `uv sync` compiles `sylphd-client` from
`rust/sylphd-client` so that the tests run the binary you just changed. `uv sync` also installs
`sylphd[server]` and the test tools.

## Into another project

```bash
uv add "sylphd[client]"                        # + the native client
```

## As a tool

```bash
uv tool install "sylphd[client]"   # a client machine
uv tool install --find-links <release assets> "sylphd[server]"   # the server machine (pysylph wheel from the release)
sylphd --version
```

If `sylphd` reports that `sylphd-client is not installed`, the query commands have no binary:
install the `client` extra (or put the standalone binary on `PATH` and point
`$SYLPHD_CLIENT_BIN` at it).

## Coding agent skill

To let a coding agent such as Claude Code run sylphd for you, install the skill with the GitHub CLI:

```bash
gh skill install Abrinca/sylphd sylphd --agent claude-code --scope user
```

The skill installs the command line itself when it is missing (`uv tool install`, after asking).
See [Coding agents](guides/coding-agents.md) for what it does and how to use it.

## Documentation

The documentation is built with [Zensical](https://zensical.org/) from `docs/`:

```bash
uvx zensical serve
```
