"""``sylphd taxmatrix`` parity with ``sylph-tax merge``.

``sylph-tax merge`` (``merge_sylph_taxprof.merge_data``) is the reference for
the wide table: an outer join over one column of the per-sample ``.sylphmpa``
files, clade names sorted lexicographically when there are two or more files,
``0`` for a missing value, and ``dropna(how="all")`` for ``ANI``/``Coverage``.

Two layers, from the design note's parity plan:

* :func:`test_golden`, :func:`test_taxmatrix_command_from_sylphmpa_matches_golden`
  and :func:`test_taxmatrix_command_from_profile_json_matches_golden` compare
  ``sylphd taxmatrix`` byte for byte with checked-in files that the real
  ``sylph-tax merge`` wrote (``scripts/regen-taxmatrix-golden.py``).  They
  always run, so CI without sylph-tax still guards the format.
* :func:`test_matches_sylph_tax_merge` reruns ``merge_data`` live on the same
  ``.sylphmpa`` files that ``sylphd taxmatrix`` reads, and is skipped when
  ``sylph_tax`` is not importable (it needs pandas; the project venv does not
  install it).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import aggregate_samples

from sylphd import taxmatrix
from sylphd.taxprofile import (
    MATRIX_VALUES,
    build_taxprof,
    clade_matrix_lines,
    taxprof_lines,
)

DATA = Path(__file__).parent / "data" / "taxmatrix"

#: ``sylph-tax merge --column`` for each ``sylphd taxmatrix --value``.
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
    """The matrix from ``clade_matrix_lines`` directly (with final newline)."""
    return "\n".join(clade_matrix_lines(table(body), value)) + "\n"


def _write_sylphmpa_files(body: dict, directory: Path, prefix: str = "") -> list[Path]:
    files = []
    for name, clades in table(body):
        path = directory / f"{prefix}{name}.gtdb.sylphmpa"
        path.write_text("\n".join(taxprof_lines(name, "gtdb", clades)) + "\n")
        files.append(path)
    return files


def _write_profile_json(body: dict, path: Path) -> Path:
    samples = body["samples"]
    doc = {
        "api_version": 1,
        "params": {},
        "samples": [{"name": sample["name"]} for sample in samples],
        "table": {"columns": ["Sample_file"], "data": []},
        "caveats": [],
        "taxprof": {
            sample["name"]: build_taxprof(sample["name"], ("gtdb",), sample["rows"])
            for sample in samples
        },
    }
    path.write_text(json.dumps(doc))
    return path


@pytest.mark.parametrize("case", [name for name, _ in cases()])
@pytest.mark.parametrize("value", MATRIX_VALUES)
def test_golden(case: str, value: str) -> None:
    """Byte-for-byte against what the real ``sylph-tax merge`` wrote."""
    body = dict(cases())[case]
    expected = (DATA / f"{case}.{value}.tsv").read_text()
    assert render(body, value) == expected


@pytest.mark.parametrize("case", [name for name, _ in cases()])
@pytest.mark.parametrize("value", MATRIX_VALUES)
def test_taxmatrix_command_from_sylphmpa_matches_golden(
    case: str, value: str, tmp_path: Path
) -> None:
    """``sylphd taxmatrix`` on ``.sylphmpa`` files matches the golden byte for byte."""
    body = dict(cases())[case]
    files = _write_sylphmpa_files(body, tmp_path)
    out = tmp_path / "matrix.tsv"
    code = taxmatrix.main(["--value", value, "-o", str(out), *(str(f) for f in files)])
    assert code == 0
    expected = (DATA / f"{case}.{value}.tsv").read_text()
    assert out.read_text() == expected


@pytest.mark.parametrize("case", [name for name, _ in cases()])
@pytest.mark.parametrize("value", MATRIX_VALUES)
def test_taxmatrix_command_from_profile_json_matches_golden(
    case: str, value: str, tmp_path: Path
) -> None:
    """``sylphd taxmatrix`` on a saved ``/profile`` JSON doc matches the golden."""
    body = dict(cases())[case]
    doc_path = _write_profile_json(body, tmp_path / f"{case}.json")
    out = tmp_path / "matrix.tsv"
    code = taxmatrix.main(["--value", value, "-o", str(out), str(doc_path)])
    assert code == 0
    expected = (DATA / f"{case}.{value}.tsv").read_text()
    assert out.read_text() == expected


def test_matches_sylph_tax_merge(tmp_path: Path) -> None:
    """Live parity: run ``sylphd taxmatrix`` and ``merge_data`` on the same files."""
    merge_sylph_taxprof = pytest.importorskip(
        "sylph_tax.merge_sylph_taxprof",
        reason="sylph-tax (and pandas) not installed",
    )
    for case, body in cases():
        files = _write_sylphmpa_files(body, tmp_path, prefix=f"{case}-")
        for value in MATRIX_VALUES:
            merged = merge_sylph_taxprof.merge_data(files, SYLPH_TAX_COLUMNS[value])
            expected = merged.to_csv(sep="\t")
            assert render(body, value) == expected, (case, value)
            out = tmp_path / f"{case}.{value}.tsv"
            assert (
                taxmatrix.main(
                    ["--value", value, "-o", str(out), *(str(f) for f in files)]
                )
                == 0
            )
            assert out.read_text() == expected, (case, value)
