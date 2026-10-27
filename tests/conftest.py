"""Shared fixtures: paths to the pysylph test data copied into ``tests/data``."""

from pathlib import Path

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
