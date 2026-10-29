"""Helpers for the single-database ``/info`` and ``/assembly`` goldens.

The goldens in ``tests/data/web-golden`` are what the single-database server
answered before multi-database support existed; ``scripts/capture-web-golden.py``
regenerates them.  Values that change from run to run are normalised away, so
the comparison guards the *shape* of the response (what the web app reads), not
timings or absolute paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

GOLDEN = Path(__file__).parent / "data" / "web-golden"

#: Keys whose value depends on the machine, the run or the version.
VOLATILE: dict[str, Any] = {
    "elapsed_ms": 0.0,
    "version": "<version>",
    "pysylph_version": "<version>",
    "python_version": "<version>",
}


def normalise(value: Any) -> Any:
    """Replace paths, versions and timings so a response is reproducible."""
    if isinstance(value, dict):
        return {
            key: (
                "<path>"
                if key == "path"
                else VOLATILE[key]
                if key in VOLATILE
                else normalise(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [normalise(item) for item in value]
    return value


def load(name: str) -> Any:
    """The golden document ``name`` (``info`` or ``assembly``)."""
    return json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))
