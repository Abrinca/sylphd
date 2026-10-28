"""Shared fixtures: paths to the pysylph test data copied into ``tests/data``."""

from pathlib import Path

import dnaio
import pysylph
import pytest

DATA = Path(__file__).parent / "data"

ECOLI_TWO_STAGE = DATA / "ecoli.syl2db"
ECOLI_PLAIN = DATA / "ecoli.syldb"
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
    from sylphd.metadata import METADATA_COLUMNS, build_metadata_db

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
