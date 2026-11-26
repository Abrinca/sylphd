"""The NCBI enrichment of sylph-tax genomes from an omnitax file (``setup --taxonomy-db``).

The taxonomy file is a tiny NCBI tree with the assemblies of ``tests/data/ncbi``, built in memory;
nothing touches the network.  (The nearest taxa are in ``test_unversioned_nearest.py``.)
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from taxfile import (
    FUNGI_ASSEMBLIES,
    FUNGI_MERGED,
    FUNGI_NODES,
    SACCHAROMYCES,
    write_taxonomy_file,
)

from sylphd import cli
from sylphd.metadata import METADATA_COLUMNS, MetadataDB
from sylphd.metadata_build import build_metadata_db, update_metadata_db
from sylphd.taxonomy_sources import TaxonomySource

HEADER = ["accession", *METADATA_COLUMNS]


def _gtdb_row(accession: str, **overrides: str) -> list[str]:
    values = {column: "none" for column in METADATA_COLUMNS}
    values["gtdb_taxonomy"] = "d__Bacteria;p__Test"
    values.update(overrides)
    return [accession, *[values[column] for column in METADATA_COLUMNS]]


@pytest.fixture()
def gtdb_source(tmp_path: Path) -> Path:
    path = tmp_path / "bac120_metadata_r232.tsv"
    row = _gtdb_row(
        "RS_GCF_000005845.2",
        ncbi_taxid="562",
        ncbi_taxonomy="d__Bacteria;p__Pseudomonadota",
        ncbi_organism_name="Escherichia coli K-12",
    )
    path.write_text("\t".join(HEADER) + "\n" + "\t".join(row) + "\n", newline="")
    return path


@pytest.fixture()
def fungi_file(tmp_path: Path) -> Path:
    path = tmp_path / "fungi_refseq_metadata.tsv.gz"
    text = (
        "GCF_000002515.2_MG8\td__Eukaryota;p__Ascomycota;s__Kluyveromyces lactis\n"
        "GCA_000002495.2\td__Eukaryota;p__Ascomycota;s__Neurospora crassa\n"
        "GCF_000002655.9\td__Eukaryota;p__Ascomycota;s__Saccharomyces cerevisiae\n"
        "GCF_999999999.1\td__Eukaryota;p__Ascomycota\n"
        "UHGV-0405173\tvFAM-00013;root\n"
        "GCF_000005845.2\td__Bacteria;p__Test\n"
        "GCF_000005845.2_ASM584v2\td__Bacteria;p__Test\n"
        "GCF_000000003.1\td__Eukaryota\n"
        "GCF_000000004.1\td__Eukaryota\n"
    )
    with gzip.open(path, "wt", newline="") as handle:
        handle.write(text)
    return path


@pytest.fixture()
def taxonomy_db(tmp_path: Path) -> Path:
    return write_taxonomy_file(
        tmp_path / "taxonomies.sqlite", FUNGI_NODES, assemblies=FUNGI_ASSEMBLIES, merged=FUNGI_MERGED,
        gtdb=[("RS_GCF_000005845.2", "d__Bacteria;p__Test")], links=[("RS_GCF_000005845.2", 4932)],
    )


def test_build_stores_ncbi_data_for_the_sylphtax_keys(tmp_path: Path, gtdb_source: Path, fungi_file: Path, taxonomy_db: Path) -> None:
    output = tmp_path / "meta.sqlite"
    stats = build_metadata_db(
        [gtdb_source], output, release=232, taxonomy=[TaxonomySource("FungiRefSeq-latest", fungi_file)], taxonomy_db=taxonomy_db
    )
    sources = {source["name"]: source for source in stats["taxonomy_sources"]}
    assert sources["FungiRefSeq-latest"]["ncbi"] == {
        "gtdb": 2,  # GCF_000005845.2 and GCF_000005845.2_ASM584v2
        "mapped": 4,
        "no_accession": 1,
        "not_in_summary": 2,  # GCF_999999999.1 and GCF_000000004.1: no such assembly
        "ambiguous": 0,
    }
    assert "ncbi" not in sources["GTDB_r232"]
    assert stats["rows"] == 1

    with MetadataDB(output) as db:
        found = db.lookup(["GCF_000002515.2", "GCA_000002495.2", "GCF_000005845.2"])
        # The key _MG8 asks for .2, the file has .1 only: the genome of that number, stored under the key's own accession.
        assert found["GCF_000002515.2"]["ncbi_taxid"] == 4932
        assert found["GCF_000002515.2"]["ncbi_organism_name"] == "Saccharomyces cerevisiae"
        assert found["GCF_000002515.2"]["ncbi_taxonomy"] == SACCHAROMYCES
        assert found["GCA_000002495.2"]["ncbi_taxid"] == 5141
        # A merged taxid resolves to the current one.
        assert db.lookup(["GCF_000000003.1"])["GCF_000000003.1"]["ncbi_taxid"] == 4932
        assert "GCF_000000004.1" not in db.lookup(["GCF_000000004.1"])
        # The suffixed key GCF_000005845.2_ASM584v2 must not replace the GTDB row, which keeps its own NCBI data.
        assert found["GCF_000005845.2"]["gtdb_taxonomy"] == "d__Bacteria;p__Test"
        assert found["GCF_000005845.2"]["ncbi_taxid"] == 562
        assert found["GCF_000005845.2"]["ncbi_organism_name"] == "Escherichia coli K-12"
        # The enrichment row is not a GTDB genome.
        assert db.contains_accessions(["GCF_000002515.2"]) is False
        assert db.contains_accessions(["GCF_000005845.2"]) is True
        assert json.loads(db.info["ncbi_taxonomy"])["FungiRefSeq-latest"]["mapped"] == 4
        assert db.taxonomy_sources()[-1]["ncbi"]["mapped"] == 4


def test_update_enriches_an_existing_file(tmp_path: Path, gtdb_source: Path, fungi_file: Path, taxonomy_db: Path) -> None:
    output = tmp_path / "meta.sqlite"
    build_metadata_db([gtdb_source], output, release=232, taxonomy=[TaxonomySource("FungiRefSeq-latest", fungi_file)])
    stats = update_metadata_db(output, output, taxonomy_db=taxonomy_db)
    sources = {source["name"]: source for source in stats["taxonomy_sources"]}
    assert sources["FungiRefSeq-latest"]["ncbi"]["mapped"] == 4
    assert stats["rows"] == 1
    with MetadataDB(output) as db:
        assert db.lookup(["GCF_000002515.2"])["GCF_000002515.2"]["ncbi_taxid"] == 4932


def test_setup_reports_the_counts(tmp_path: Path, capsys: pytest.CaptureFixture[str], gtdb_source: Path, fungi_file: Path, taxonomy_db: Path) -> None:
    output = tmp_path / "meta.sqlite"
    code = cli.main(
        ["setup", "-m", str(gtdb_source), "--taxonomy", f"FungiRefSeq-latest={fungi_file}", "--taxonomy-db", str(taxonomy_db), "-o", str(output)]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "NCBI lineage: 4 mapped, 1 without an assembly accession" in captured.err
    assert "2 not in the taxonomy file, 0 ambiguous" in captured.err


def test_rerun_is_idempotent_and_update_prunes_orphans(tmp_path: Path, gtdb_source: Path, fungi_file: Path, taxonomy_db: Path) -> None:
    first, second, third = (tmp_path / f"{name}.sqlite" for name in ("a", "b", "c"))
    taxonomy = [TaxonomySource("FungiRefSeq-latest", fungi_file)]
    build_metadata_db([gtdb_source], first, release=232, taxonomy=taxonomy, taxonomy_db=taxonomy_db)

    def rows(path: Path) -> list[tuple[object, ...]]:
        with closing(sqlite3.connect(path)) as connection:
            return connection.execute("SELECT * FROM metadata_data ORDER BY accession").fetchall()

    stats = update_metadata_db(first, second, taxonomy_db=taxonomy_db)
    assert stats["taxonomy_sources"][-1]["ncbi"]["mapped"] == 4  # type: ignore[index]
    assert rows(second) == rows(first)

    # Replacing the source by one with a single key drops the other rows.
    small = tmp_path / "small.tsv"
    small.write_text("GCA_000002495.2\td__Eukaryota\n")
    update_metadata_db(second, third, taxonomy=[TaxonomySource("FungiRefSeq-latest", small)])
    assert [row[0] for row in rows(third)] == ["GCA_000002495.2", "GCF_000005845.2"]
    with MetadataDB(third) as db:
        assert "ncbi" not in db.taxonomy_sources()[-1]


def test_a_file_without_genome_links_is_refused(tmp_path: Path, gtdb_source: Path, fungi_file: Path) -> None:
    bare = write_taxonomy_file(tmp_path / "bare.sqlite", FUNGI_NODES, gtdb=[("RS_GCF_000005845.2", "d__Bacteria;p__Test")])
    with pytest.raises(ValueError, match="NCBI has no genome links"):
        build_metadata_db([gtdb_source], tmp_path / "m.sqlite", release=232, taxonomy=[TaxonomySource("F", fungi_file)], taxonomy_db=bare)
