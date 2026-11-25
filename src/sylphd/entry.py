"""The ``sylphd`` command: a friendly message when the optional packages are missing.

The base install holds only what :mod:`sylphd.ncbitax` needs; the command line
imports pysylph, and ``serve`` also starlette and uvicorn, so without them
``sylphd`` says which extra to install instead of showing a traceback.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

#: Top-level modules that only an extra installs.
OPTIONAL_MODULES = frozenset(
    {"pysylph", "dnaio", "starlette", "uvicorn", "multipart", "python_multipart"}
)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["diff"]:
        # Pure standard library: works in the base install, without any extra.
        from .parity import main as diff

        return diff(args[1:])
    if args[:1] == ["taxmatrix"]:
        # Pure standard library: works in the base install, without any extra.
        from .taxmatrix import main as taxmatrix

        return taxmatrix(args[1:])
    try:
        from .cli import main as run
    except ImportError as error:
        if (error.name or "").split(".")[0] not in OPTIONAL_MODULES:
            raise
        print(
            f"sylphd: the command line needs a package that is not installed "
            f"({error.name}).\n"
            "  to profile against a server:  pip install 'sylphd[client]'\n"
            "  to run a server:              pip install 'sylphd[server]'\n"
            "  (the Python API sylphd.ncbitax needs neither: 'sylphd[taxonomy]')",
            file=sys.stderr,
        )
        return 2
    return run(args)
