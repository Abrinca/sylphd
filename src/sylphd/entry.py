"""The ``sylphd`` command.

Python owns the command but only runs what is Python: the server and admin commands
(``serve``, ``setup``, ``download``, ``taxdb``, ``taxonomy``, ``taxmatrix``, ``diff``).
Every other invocation is handed, argv unchanged, to the native ``sylphd-client`` binary
(``profile``, ``query``, ``assembly``, ``info``, ``render``; ``os.execv`` on Unix, so exit
codes, signals, stdin and stdout pass straight through).

The base install holds only what :mod:`sylphd.ncbitax` needs; the binary comes with the
``client`` extra and the server needs ``serve``'s packages, so a missing one gets a
friendly message and exit code 2 instead of a traceback.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence

from . import __version__
from .clientbin import INSTALL_HINT, exec_client, find_client_bin

#: Commands that run in Python, with their one-line help.
PYTHON_COMMANDS: dict[str, str] = {
    "serve": "run the server (one process, one worker)",
    "setup": "build the SQLite metadata database from GTDB TSV and sylph-tax files",
    "download": "download GTDB metadata, a sylph database or a taxonomy file",
    "taxdb": "build the NCBI taxonomy file (see `sylphd serve --taxonomy-db`)",
    "taxonomy": "look up NCBI taxa by taxid or name, on a server or in a taxonomy file",
    "taxmatrix": "merge .sylphmpa files or /profile JSON documents into a clade x sample TSV",
    "diff": "check that two sylph / sylph-tax outputs are equivalent",
}

#: Top-level modules that only an extra installs.
OPTIONAL_MODULES = frozenset(
    {"pysylph", "dnaio", "starlette", "uvicorn", "multipart", "python_multipart"}
)

_HELP_FLAGS = ("-h", "--help", "help")


def _help(binary: str | None) -> int:
    width = max(map(len, PYTHON_COMMANDS))
    lines = [
        "usage: sylphd <command> [options]",
        "",
        "Share one in-memory sylph database between runs: clients sketch samples",
        "locally and ask the server for ANI hits or taxonomies.",
        "",
        "Python commands (server and admin; `sylphd <command> --help`):",
        *(f"  {name:<{width}}  {text}" for name, text in PYTHON_COMMANDS.items()),
        "",
        "Client commands (run by the native sylphd-client; `sylphd <command> --help`):",
    ]
    print("\n".join(lines))
    if binary is None:
        print(f"  not available: sylphd-client is not installed; {INSTALL_HINT}")
        print("\noptions:\n  -h, --help     show this help\n  -V, --version  show the version")
        return 0
    done = subprocess.run([binary, "--help"], check=False, capture_output=True, text=True)
    commands: list[str] = []
    in_commands = False
    for line in done.stdout.splitlines():
        if line == "Commands:":
            in_commands = True
        elif line == "Options:":
            in_commands = False
        elif line.strip() and in_commands and not line.startswith("  help "):
            commands.append(line)
    print("\n".join(commands))
    print("\noptions:\n  -h, --help     show this help\n  -V, --version  show the version")
    return 0


def _run_python(command: str, args: list[str]) -> int:
    if command == "diff":
        # Pure standard library: works in the base install, without any extra.
        from .parity import main as diff

        return diff(args[1:])
    if command == "taxmatrix":
        from .taxmatrix import main as taxmatrix

        return taxmatrix(args[1:])
    try:
        from .cli import main as run

        return run(args)
    except ImportError as error:
        if (error.name or "").split(".")[0] not in OPTIONAL_MODULES:
            raise
        print(
            f"sylphd: `sylphd {command}` needs a package that is not installed ({error.name}).\n"
            "  to run a server:  pip install 'sylphd[server]'",
            file=sys.stderr,
        )
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in _HELP_FLAGS:
        try:
            return _help(find_client_bin())
        except FileNotFoundError as error:  # a bad $SYLPHD_CLIENT_BIN
            print(f"sylphd: {error}", file=sys.stderr)
            return 2
    if args[0] in ("-V", "--version"):
        print(f"sylphd {__version__}")
        return 0
    if args[0] in PYTHON_COMMANDS:
        return _run_python(args[0], args)
    try:
        binary = find_client_bin()
    except FileNotFoundError as error:
        print(f"sylphd: {error}", file=sys.stderr)
        return 2
    if binary is None:
        print(
            f"sylphd: `sylphd {args[0]}` is run by the native client (sylphd-client), "
            f"which is not installed.\n  {INSTALL_HINT}",
            file=sys.stderr,
        )
        return 2
    return exec_client(binary, args)
