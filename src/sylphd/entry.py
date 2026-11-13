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
    return run(argv)
