"""``--taxmatrix`` parity with ``sylph-tax merge``.

``sylph-tax merge`` (``merge_sylph_taxprof.merge_data``) is the reference for
the wide table: an outer join over one column of the per-sample ``.sylphmpa``
files, clade names sorted lexicographically when there are two or more files,
``0`` for a missing value, and ``dropna(how="all")`` for ``ANI``/``Coverage``.

Two layers, from the design note's parity plan:

* :func:`test_golden` compares sylphd's table byte for byte with checked-in
  files that the real ``sylph-tax merge`` wrote (``scripts/regen-taxmatrix-golden.py``).
  It always runs, so CI without sylph-tax still guards the format.
* :func:`test_matches_sylph_tax_merge` reruns ``merge_data`` live, from the
  ``.sylphmpa`` files sylphd writes, and is skipped when ``sylph_tax`` is not
  importable (it needs pandas; the project venv does not install it).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sylphd.taxonomy import (
    MATRIX_VALUES,
    aggregate_samples,
    clade_matrix_lines,
    taxprof_lines,
)

DATA = Path(__file__).parent / "data" / "taxmatrix"

#: ``sylph-tax merge --column`` for each ``--taxmatrix-value``.
SYLPH_TAX_COLUMNS = {
    "relative": "relative_abundance",
    "sequence": "sequence_abundance",
    "ani": "ANI",
    "coverage": "Coverage",
}


def cases() -> list[tuple[str, dict]]:
    loaded = json.loads((DATA / "rows.json").read_text())
    return [(name, body) for name, body in loaded.items() if not name.startswith("_")]


def table(body: dict):
    return aggregate_samples(body["samples"], "gtdb")


def render(body: dict, value: str) -> str:
    """The matrix exactly as ``sylphd --taxmatrix`` writes it (with final newline)."""
    return "\n".join(clade_matrix_lines(table(body), value)) + "\n"


@pytest.mark.parametrize("case", [name for name, _ in cases()])
@pytest.mark.parametrize("value", MATRIX_VALUES)
def test_golden(case: str, value: str) -> None:
    """Byte-for-byte against what the real ``sylph-tax merge`` wrote."""
    body = dict(cases())[case]
    expected = (DATA / f"{case}.{value}.tsv").read_text()
    assert render(body, value) == expected


def test_matches_sylph_tax_merge(tmp_path: Path) -> None:
    """Live parity: write the ``.sylphmpa`` files, then run ``merge_data``."""
    merge_sylph_taxprof = pytest.importorskip(
        "sylph_tax.merge_sylph_taxprof",
        reason="sylph-tax (and pandas) not installed",
    )
    for case, body in cases():
        files = []
        for name, clades in table(body):
            path = tmp_path / f"{case}-{name}.gtdb.sylphmpa"
            path.write_text("\n".join(taxprof_lines(name, "gtdb", clades)) + "\n")
            files.append(path)
        for value in MATRIX_VALUES:
            merged = merge_sylph_taxprof.merge_data(files, SYLPH_TAX_COLUMNS[value])
            expected = merged.to_csv(sep="\t")
            assert render(body, value) == expected, (case, value)
