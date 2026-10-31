"""``/info``'s per-database taxonomy coverage, computed at startup."""

from __future__ import annotations

from pathlib import Path

from conftest import ECOLI_PLAIN, ECOLI_TWO_STAGE
from starlette.testclient import TestClient

from sylphd.metadata_build import build_metadata_db
from sylphd.profiling import DatabaseSpec
from sylphd.server import ServerConfig, create_app
from sylphd.taxonomy_sources import TaxonomySource


def _taxonomy_file(path: Path, keys: dict[str, str]) -> Path:
    path.write_text(
        "".join(f"{key}\td__Bacteria;p__Test;s__{lineage}\n" for key, lineage in keys.items()),
        newline="",
    )
    return path


def test_info_reports_how_many_genomes_each_source_covers(tmp_path: Path) -> None:
    # The ecoli database's genomes are named test_files/e.coli-K12.fasta.gz and
    # so on: sylph-tax's probe for "e.coli-K12.fasta.gz" includes "e.coli-K12".
    both = TaxonomySource(
        "Both",
        _taxonomy_file(tmp_path / "both.tsv", {"e.coli-K12": "K12", "e.coli-o157": "O157"}),
    )
    one = TaxonomySource(
        "One", _taxonomy_file(tmp_path / "one.tsv", {"e.coli-EC590": "EC590"})
    )
    none = TaxonomySource("None", _taxonomy_file(tmp_path / "none.tsv", {"nothing": "X"}))
    metadata = build_metadata_db(
        [], tmp_path / "meta.sqlite", taxonomy=[both, one, none]
    )
    app = create_app(ServerConfig(db_path=ECOLI_TWO_STAGE, meta_path=Path(metadata["path"])))
    with TestClient(app) as client:
        databases = client.get("/info").json()["databases"]
    assert databases[0]["genomes"] == 3
    # Sources with n > 0 only, in source order; "None" covers no genome.
    assert databases[0]["taxonomy_sources"] == [
        {"name": "Both", "genomes": 2},
        {"name": "One", "genomes": 1},
    ]


def test_coverage_is_empty_without_metadata_and_per_database(tmp_path: Path) -> None:
    metadata = build_metadata_db(
        [],
        tmp_path / "meta.sqlite",
        taxonomy=[TaxonomySource("K12", _taxonomy_file(tmp_path / "k12.tsv", {"e.coli-K12": "K12"}))],
    )
    app = create_app(
        ServerConfig(
            db_paths=(
                DatabaseSpec(ECOLI_TWO_STAGE),
                DatabaseSpec(ECOLI_PLAIN, "plain"),
            ),
            meta_path=Path(metadata["path"]),
        )
    )
    with TestClient(app) as client:
        databases = client.get("/info").json()["databases"]
    assert [database["taxonomy_sources"] for database in databases] == [
        [{"name": "K12", "genomes": 1}],
        [{"name": "K12", "genomes": 1}],
    ]
    plain = create_app(ServerConfig(db_path=ECOLI_PLAIN))
    with TestClient(plain) as client:
        assert client.get("/info").json()["databases"][0]["taxonomy_sources"] == []
