#!/usr/bin/env python3
"""Regenerate the ``--taxmatrix`` goldens from the real ``sylph-tax merge``.

Manual, not part of the test suite: the checked-in goldens are the output of
``sylph_tax.merge_sylph_taxprof.merge_data`` (the function behind
``sylph-tax merge``), so the always-running test in
``tests/test_taxmatrix_parity.py`` compares sylphd against sylph-tax itself
rather than against sylphd's own output.

Usage (sylphd is pure Python here, ``taxonomy.py`` needs no dependencies)::

    PYTHONPATH=src .venv-sylph-tax/bin/python scripts/regen-taxmatrix-golden.py

where ``.venv-sylph-tax`` is a venv with ``ref/sylph-tax`` installed
(``uv venv scratch/sylph-tax-venv && uv pip install -e ref/sylph-tax``).
``tests/test_taxmatrix_parity.py`` additionally reruns this comparison with
``sylph_tax`` importable, so the goldens only matter for CI without it.

Input: ``tests/data/taxmatrix/rows.json`` (samples of ``sylphd profile`` rows).
Output: ``tests/data/taxmatrix/<case>.<value>.tsv`` for every
``--taxmatrix-value``, written by pandas exactly as ``sylph-tax merge`` does.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from sylph_tax.merge_sylph_taxprof import merge_data

from sylphd.taxonomy import MATRIX_VALUES, aggregate_samples, taxprof_lines

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "tests" / "data" / "taxmatrix"

#: ``--taxmatrix-value`` to ``sylph-tax merge --column``.
SYLPH_TAX_COLUMNS = {
    "relative": "relative_abundance",
    "sequence": "sequence_abundance",
    "ani": "ANI",
    "coverage": "Coverage",
}


def main() -> int:
    cases = json.loads((DATA / "rows.json").read_text())
    for case, body in cases.items():
        if case.startswith("_"):
            continue
        samples = body["samples"]
        with tempfile.TemporaryDirectory() as directory:
            files = []
            for name, clades in aggregate_samples(samples, "gtdb"):
                path = Path(directory) / f"{name}.gtdb.sylphmpa"
                path.write_text("\n".join(taxprof_lines(name, "gtdb", clades)) + "\n")
                files.append(path)
            for value in MATRIX_VALUES:
                merged = merge_data(files, SYLPH_TAX_COLUMNS[value])
                output = DATA / f"{case}.{value}.tsv"
                merged.to_csv(output, sep="\t")
                print(f"wrote {output.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
