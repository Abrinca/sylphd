"""Shared fixtures: paths to the pysylph test data copied into ``tests/data``."""

import os
from pathlib import Path

import pytest

DATA = Path(__file__).parent / "data"

ECOLI_TWO_STAGE = DATA / "ecoli.syl2db"
ECOLI_PLAIN = DATA / "ecoli.syldb"
CLODF13_PLAIN = DATA / "clodf13.syldb"
O157_SKETCH = DATA / "o157.sylsp"
K12_PAIRED_SKETCH = DATA / "k12.paired.sylsp"
K12_R1_SKETCH = DATA / "k12.R1.sylsp"
K12_R1 = DATA / "k12.R1.fq.gz"
K12_R2 = DATA / "k12.R2.fq.gz"
MIX_SKETCH = DATA / "mix.sylsp"
CLODF13_FASTA = DATA / "clodf13.fna.gz"


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return DATA


@pytest.fixture(scope="session")
def tiny_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A one-genome database built from ``clodf13.fna.gz`` (for assembly tests)."""
    import dnaio
    import pysylph

    directory = tmp_path_factory.mktemp("tiny")
    with dnaio.open(CLODF13_FASTA) as reader:
        contigs = [record.sequence for record in reader]
    genome = pysylph.Sketcher(c=200, k=31).sketch_genome(
        "GCA_000000001.1_genomic.fna.gz", contigs
    )
    path = directory / "tiny.syldb"
    pysylph.Database([genome]).dump(str(path))
    return path


@pytest.fixture(scope="session")
def tiny_metadata(
    tmp_path_factory: pytest.TempPathFactory, tiny_database: Path
) -> Path:
    """Metadata for the single genome of :func:`tiny_database`."""
    from sylphd.metadata import METADATA_COLUMNS
    from sylphd.metadata_build import build_metadata_db

    directory = tmp_path_factory.mktemp("meta")
    source = directory / "tiny.tsv"
    header = ["accession", *METADATA_COLUMNS]
    values = {column: "none" for column in METADATA_COLUMNS}
    values.update(
        gtdb_taxonomy="d__Bacteria;p__Test;c__Test",
        genome_size="9957",
        gc_percentage="51.5",
        mimag_high_quality="t",
    )
    row = ["GCA_000000001.1", *[values[column] for column in METADATA_COLUMNS]]
    source.write_text("\t".join(header) + "\n" + "\t".join(row) + "\n")
    output = directory / "meta.sqlite"
    build_metadata_db([source], output, release=232)
    return output


REPO = Path(__file__).resolve().parent.parent


def find_client_binary() -> Path | None:
    """The ``sylphd-client`` binary the tests run: ``$SYLPHD_CLIENT_BIN``, the dev venv, a cargo build.

    ``uv sync`` builds ``sylphd-bin`` from ``rust/sylphd-client`` into the venv; CI sets
    ``SYLPHD_REQUIRE_CLIENT_BIN=1`` so a missing binary fails the run instead of skipping it.
    """
    from sylphd.clientbin import find_client_bin

    override = os.environ.get("SYLPHD_CLIENT_BIN")
    if override:
        path = Path(override)
        return path if path.is_file() else None
    found = find_client_bin()
    if found:
        return Path(found)
    for profile in ("release", "debug"):
        path = REPO / "rust" / "target" / profile / "sylphd-client"
        if path.is_file():
            return path
    return None


def require_client_binary() -> Path:
    """The binary, or a module-level skip (a failure when ``SYLPHD_REQUIRE_CLIENT_BIN`` is set).

    Call it at import time of a module that runs the binary::

        CLIENT = require_client_binary()

    It also points ``$SYLPHD_CLIENT_BIN`` at the binary, so the ``sylphd`` command that the
    tests start as a subprocess finds the same one.
    """
    path = find_client_binary()
    if path is None:
        message = "sylphd-client is not installed (uv sync, or cargo build -p sylphd-client)"
        if os.environ.get("SYLPHD_REQUIRE_CLIENT_BIN"):
            raise RuntimeError(message + ": SYLPHD_REQUIRE_CLIENT_BIN is set")
        pytest.skip(message, allow_module_level=True)
    os.environ["SYLPHD_CLIENT_BIN"] = str(path)
    return path


def aggregate_samples(samples, taxonomy: str = "gtdb"):
    """The oracle the parity tests compare against: the server's aggregation of each sample."""
    from sylphd.taxprofile import aggregate_rows

    return [
        (str(sample.get("name") or "sample"), aggregate_rows(sample.get("rows") or (), taxonomy))
        for sample in samples
    ]
