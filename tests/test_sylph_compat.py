"""Compatibility with the real ``sylph`` binary (a guard for new sylph releases).

These tests run stock ``sylph query`` / ``sylph profile`` and sylphd on the same
inputs and require byte-identical TSV.  They skip cleanly when no ``sylph`` is
available, so the normal ``uv run pytest`` does not need it.

Run them::

    SYLPH_BIN=/path/to/sylph uv run pytest tests/test_sylph_compat.py -v -rs
    # or, with sylph on PATH (e.g. ``pixi add sylph`` / bioconda):
    uv run pytest tests/test_sylph_compat.py -v -rs

Set ``SYLPH_COMPAT_REQUIRED=1`` (CI does) to fail instead of skip when the
binary is missing.

What is compared
----------------
*Profiling* (``TestProfilingParity``): the input of both sides is the same
**sylph-made** sample sketch and the same database, so sketching
nondeterminism (stock sylph's paired-end de-duplication is probabilistic and
multithreaded) cannot make the test flaky.  sylph's TSV is compared with the
TSV that sylphd renders from its server response (``rows_to_tsv``), for
``query``, ``profile`` and ``profile -u``, single-end and paired-end sketches,
a high-coverage sample (``Eff_lambda`` ``HIGH``, ``NA-NA`` intervals), and a
sample without read statistics (``-u``: ``inf``, ``NaN``, ``LOW``).

The only normalisations, each applied to *both* sides:

* ``Sample_file`` is dropped: sylph prints the name stored in the sketch
  (or the file path), sylphd prints the upload name.
* sylphd's extra trailing column ``accession`` is dropped (sylphd always
  appends it, then metadata columns; none here, as no metadata is loaded).

Everything else, header included, must be equal byte for byte.

*Sketching* (``TestSketchingAgainstPysylph``, clearly separate): ``pysylph``
against ``sylph sketch`` of the same reads.  Single-end sketches must be
identical (same k-mers and counts).  For paired reads sylph removes
duplicated k-mers between the mates with a hash-based, thread-dependent
procedure, and pysylph does it deterministically, so a small difference in
the number of k-mers is tolerated (``PAIRED_TOLERANCE``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import dnaio
import pysylph
import pytest
from conftest import CLODF13_FASTA, DATA, ECOLI_PLAIN, ECOLI_TWO_STAGE, K12_R1, K12_R2
from starlette.testclient import TestClient

from sylphd.protocol import ProfileParams, rows_to_tsv
from sylphd.server import ServerConfig, create_app

#: Relative difference of the k-mer count of a paired sketch we accept.
PAIRED_TOLERANCE = 0.02


def _find_sylph() -> str | None:
    wanted = os.environ.get("SYLPH_BIN")
    if wanted:
        return wanted if Path(wanted).is_file() else None
    return shutil.which("sylph")


SYLPH = _find_sylph()

if SYLPH is None:
    if os.environ.get("SYLPH_COMPAT_REQUIRED"):
        raise RuntimeError("SYLPH_COMPAT_REQUIRED is set but no sylph binary was found")
    pytest.skip(
        "no sylph binary: set SYLPH_BIN=/path/to/sylph or put sylph on PATH",
        allow_module_level=True,
    )


def _sylph(*args: str | Path, cwd: Path | None = None) -> str:
    done = subprocess.run(
        [SYLPH, *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
    )
    assert done.returncode == 0, f"sylph {' '.join(map(str, args))} failed:\n{done.stderr}"
    return done.stdout


@pytest.fixture(scope="module")
def sylph_version() -> str:
    return _sylph("--version").strip()


def _write_fastq(path: Path, reads: list[str]) -> None:
    with open(path, "w") as out:
        for index, sequence in enumerate(reads):
            out.write(f"@r{index}\n{sequence}\n+\n{'I' * len(sequence)}\n")


@pytest.fixture(scope="module")
def inputs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Sample sketches made by the real sylph (shared by both sides)."""
    work = tmp_path_factory.mktemp("sylph-inputs")
    with dnaio.open(CLODF13_FASTA) as reader:
        genome = "".join(record.sequence for record in reader)
    # ~30x coverage of the plasmid by 100 bp reads: median > 2, so lambda is HIGH.
    reads = [genome[i : i + 100] for i in range(0, len(genome) - 100, 3)]
    deep = work / "deep.fastq"
    _write_fastq(deep, reads)
    # The plasmid as "reads": every k-mer once, no read statistics (LOW).
    contig_like = work / "contig_like.fastq"
    _write_fastq(contig_like, [genome])

    out = {}
    for label, args in {
        "deep": ["sketch", deep, "-d", work / "deep"],
        "contig_like": ["sketch", contig_like, "-d", work / "contig_like"],
        "k12_single": ["sketch", K12_R1, "-d", work / "k12_single"],
        "k12_paired": ["sketch", "-1", K12_R1, "-2", K12_R2, "-d", work / "k12_paired"],
    }.items():
        _sylph(*args, "-t", "1")
        (path,) = sorted((work / label).glob("*.sylsp"))
        out[label] = path
    return out


@pytest.fixture(scope="module")
def deep_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The plasmid as a one-genome sylph database (made by sylph)."""
    work = tmp_path_factory.mktemp("sylph-db")
    _sylph("sketch", "-g", CLODF13_FASTA, "-o", work / "clodf13", "-t", "1")
    return work / "clodf13.syldb"


def _normalise_sylph(tsv: str) -> str:
    """sylph's TSV without ``Sample_file`` (the name stored in the sketch)."""
    return "".join(_drop_columns(line, {0}) + "\n" for line in tsv.splitlines())


def _normalise_sylphd(tsv: str, extra: int = 1) -> str:
    """sylphd's TSV without ``Sample_file`` and the trailing ``accession`` column."""
    lines = tsv.splitlines()
    width = len(lines[0].split("\t"))
    drop = {0, *range(width - extra, width)}
    return "".join(_drop_columns(line, drop) + "\n" for line in lines)


def _drop_columns(line: str, drop: set[int]) -> str:
    return "\t".join(cell for i, cell in enumerate(line.split("\t")) if i not in drop)


def _sylphd_tsv(db: Path, mode: str, sketch: Path, **params: object) -> str:
    app = create_app(ServerConfig(db_path=db))
    body = {"params": json.dumps({"fields": [], **params})}
    files = [("sketch", ("sample.sylsp", sketch.read_bytes()))]
    with TestClient(app) as client:
        response = client.post(f"/{mode}", data=body, files=files)
    assert response.status_code == 200, response.text
    return rows_to_tsv(
        response.json(), mode, params=ProfileParams.from_mapping(params), fields=[]
    )


def _compare(db: Path, mode: str, sketch: Path, sylph_flags: list[str], **params: object) -> str:
    sylph_out = _sylph(mode, db, sketch, "-t", "1", *sylph_flags)
    sylphd_out = _sylphd_tsv(db, mode, sketch, **params)
    expected = _normalise_sylph(sylph_out)
    assert _normalise_sylphd(sylphd_out) == expected
    return expected


@pytest.mark.parametrize("database", [ECOLI_PLAIN, ECOLI_TWO_STAGE], ids=["syldb", "syl2db"])
class TestProfilingParity:
    @pytest.mark.parametrize("sample", ["k12_paired", "k12_single"])
    @pytest.mark.parametrize("mode", ["query", "profile"])
    def test_reads(self, inputs, database, mode, sample, sylph_version) -> None:
        # k12_single has too few k-mers to hit anything: the (empty) outputs
        # must then also agree, header included.
        _compare(database, mode, inputs[sample], [])

    def test_profile_estimate_unknown(self, inputs, database) -> None:
        _compare(database, "profile", inputs["k12_paired"], ["-u"], estimate_unknown=True)


class TestFormattingRules:
    """HIGH/LOW, NA-NA, inf and NaN, exactly as sylph prints them."""

    @pytest.mark.parametrize("mode", ["query", "profile"])
    def test_high_coverage(self, inputs, deep_database, mode) -> None:
        expected = _compare(deep_database, mode, inputs["deep"], [])
        row = expected.splitlines()[1].split("\t")
        assert "HIGH" in row and "NA-NA" in row  # the case that was empty cells

    def test_high_coverage_estimate_unknown(self, inputs, deep_database) -> None:
        _compare(deep_database, "profile", inputs["deep"], ["-u"], estimate_unknown=True)

    @pytest.mark.parametrize("mode", ["query", "profile"])
    def test_no_read_statistics(self, inputs, deep_database, mode) -> None:
        expected = _compare(deep_database, mode, inputs["contig_like"], [])
        assert "LOW" in expected.splitlines()[1].split("\t")

    def test_no_read_statistics_estimate_unknown(self, inputs, deep_database) -> None:
        expected = _compare(
            deep_database, "profile", inputs["contig_like"], ["-u"], estimate_unknown=True
        )
        row = expected.splitlines()[1].split("\t")
        assert "inf" in row and "NaN" in row


# ---------------------------------------------------------------------------
# Sketching: pysylph against ``sylph sketch`` (separate from the TSV parity).
# ---------------------------------------------------------------------------


def _reads(path: Path) -> list[str]:
    with dnaio.open(path) as reader:
        return [record.sequence for record in reader]


class TestSketchingAgainstPysylph:
    def test_single_end_is_identical(self, inputs) -> None:
        theirs = pysylph.SampleSketch.load(inputs["k12_single"])
        ours = pysylph.Sketcher(c=theirs.c, k=theirs.k).sketch_single("k12", _reads(K12_R1))
        # The serialised bytes differ only by the stored name, so compare content.
        assert ours.kmer_counts == theirs.kmer_counts
        assert ours.mean_read_length == theirs.mean_read_length

    def test_paired_end_is_close(self, inputs) -> None:
        theirs = pysylph.SampleSketch.load(inputs["k12_paired"])
        ours = pysylph.Sketcher(c=theirs.c, k=theirs.k).sketch_paired(
            "k12", _reads(K12_R1), _reads(K12_R2)
        )
        a, b = ours.kmer_counts, theirs.kmer_counts
        differing = len(a.keys() ^ b.keys()) + sum(
            1 for key in a.keys() & b.keys() if a[key] != b[key]
        )
        assert differing <= PAIRED_TOLERANCE * max(len(a), len(b)), (len(a), len(b), differing)
