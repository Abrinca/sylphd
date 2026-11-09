"""Tests for the NCBI enrichment of sylph-tax genomes (``setup --ncbi-taxonomy``).

The fixtures are a tiny ``rankedlineage.dmp`` and the two assembly summaries;
the network-facing functions are replaced, so no test touches NCBI.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from sylphd import cli, ncbi_taxonomy
from sylphd.metadata import METADATA_COLUMNS, MetadataDB
from sylphd.metadata_build import build_metadata_db, update_metadata_db
from sylphd.taxonomy_sources import TaxonomySource

DATA = Path(__file__).parent / "data" / "ncbi"
RANKED = DATA / "rankedlineage.dmp"
NODES = DATA / "nodes.dmp"
MERGED = DATA / "merged.dmp"
ASSEMBLIES = (DATA / "assembly_summary_refseq.txt", DATA / "assembly_summary_genbank.txt")

HEADER = ["accession", *METADATA_COLUMNS]

SACCHAROMYCES = (
    "d__Eukaryota;p__Ascomycota;c__Saccharomycetes;o__Saccharomycetales;"
    "f__Saccharomycetaceae;g__Saccharomyces;s__Saccharomyces cerevisiae"
)


def _gtdb_row(accession: str, **overrides: str) -> list[str]:
    values = {column: "none" for column in METADATA_COLUMNS}
    values["gtdb_taxonomy"] = "d__Bacteria;p__Test"
    values.update(overrides)
    return [accession, *[values[column] for column in METADATA_COLUMNS]]


@pytest.fixture()
def gtdb_source(tmp_path: Path) -> Path:
    path = tmp_path / "bac120_metadata_r232.tsv"
    text = "\t".join(HEADER) + "\n"
    text += "\t".join(
        _gtdb_row(
            "RS_GCF_000005845.2",
            ncbi_taxid="562",
            ncbi_taxonomy="d__Bacteria;p__Pseudomonadota",
            ncbi_organism_name="Escherichia coli K-12",
        )
    )
    path.write_text(text + "\n", newline="")
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


def _fake_assemblies(
    accessions: list[str], *, progress: object = None
) -> dict[str, ncbi_taxonomy.Assembly]:
    return ncbi_taxonomy.read_assemblies(ASSEMBLIES, accessions)


def _patch_ncbi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ncbi_taxonomy, "fetch_assemblies", _fake_assemblies)
    monkeypatch.setattr(
        ncbi_taxonomy,
        "fetch_taxdump",
        lambda directory, *, progress=None: (RANKED, NODES, MERGED),
    )


def test_assembly_accession() -> None:
    assert ncbi_taxonomy.assembly_accession("GCF_000002495.2_MG8") == "GCF_000002495.2"
    assert ncbi_taxonomy.assembly_accession("gca_964372435.1_genomic") == "GCA_964372435.1"
    assert ncbi_taxonomy.assembly_accession("UHGV-0405173") is None


def test_exact_version_wins_and_an_ambiguous_base_is_skipped() -> None:
    # .1 is in the summary, .2 is not; one requested version falls back to the base.
    found = ncbi_taxonomy.read_assemblies(ASSEMBLIES, ["GCF_000002515.2"])
    assert found["GCF_000002515.2"] == ncbi_taxonomy.Assembly(4932, "Saccharomyces cerevisiae")
    # Two requested versions of one base: only the exact accession is used.
    found = ncbi_taxonomy.read_assemblies(
        ASSEMBLIES, ["GCF_000002515.1", "GCF_000002515.2"]
    )
    assert set(found) == {"GCF_000002515.1"}
    # A GenBank-only GCA and no match at all.
    found = ncbi_taxonomy.read_assemblies(
        ASSEMBLIES, ["GCA_000002495.2", "GCF_999999999.1"]
    )
    assert found["GCA_000002495.2"].organism == "Neurospora crassa OR74A"
    assert set(found) == {"GCA_000002495.2"}


def test_ranked_lineage_keeps_gtdb_ranks() -> None:
    lineages = ncbi_taxonomy.ranked_lineages(RANKED, NODES, {4932})
    assert lineages[4932] == SACCHAROMYCES


def test_a_node_that_is_a_species_fills_its_own_rank() -> None:
    # rankedlineage.dmp leaves the species column empty for a species node.
    lineages = ncbi_taxonomy.ranked_lineages(RANKED, NODES, {28985})
    assert lineages[28985].endswith("s__Kluyveromyces lactis")


def test_missing_and_no_rank_nodes_leave_their_ranks_empty() -> None:
    lineages = ncbi_taxonomy.ranked_lineages(RANKED, NODES, {8888, 123456})
    assert lineages[8888] == (
        "d__Eukaryota;p__Ascomycota;c__;o__Saccharomycetales;f__;g__Saccharomyces;s__"
    )
    assert 123456 not in lineages  # not in the taxdump at all


def test_merged_taxids_are_followed(tmp_path: Path) -> None:
    assert ncbi_taxonomy.merged_taxids(MERGED, {77777, 4932}) == {77777: 4932}


def test_only_the_summary_a_prefix_needs_is_read() -> None:
    refseq = ncbi_taxonomy.ASSEMBLY_SUMMARIES["GCF_"]
    genbank = ncbi_taxonomy.ASSEMBLY_SUMMARIES["GCA_"]
    assert ncbi_taxonomy.summary_urls(["GCF_000002515.2"]) == (refseq,)
    assert ncbi_taxonomy.summary_urls(["GCA_000002495.2"]) == (genbank,)
    assert set(ncbi_taxonomy.summary_urls(["GCF_1.1", "GCA_2.1"])) == {refseq, genbank}


def test_lookup_without_accessions_downloads_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not fetch without accessions")

    monkeypatch.setattr(ncbi_taxonomy, "fetch_assemblies", fail)
    assert ncbi_taxonomy.lookup([], tmp_path) == {}


def test_build_stores_ncbi_data_for_the_sylphtax_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gtdb_source: Path,
    fungi_file: Path,
) -> None:
    _patch_ncbi(monkeypatch)
    output = tmp_path / "meta.sqlite"
    stats = build_metadata_db(
        [gtdb_source],
        output,
        release=232,
        taxonomy=[TaxonomySource("FungiRefSeq-latest", fungi_file)],
        ncbi_cache=tmp_path / "cache",
    )
    sources = {source["name"]: source for source in stats["taxonomy_sources"]}
    assert sources["FungiRefSeq-latest"]["ncbi"] == {
        "mapped": 4,
        "no_accession": 1,
        "not_in_summary": 1,
        "no_lineage": 1,
    }
    assert "ncbi" not in sources["GTDB_r232"]
    assert stats["rows"] == 1

    with MetadataDB(output) as db:
        found = db.lookup(["GCF_000002515.2", "GCA_000002495.2", "GCF_000005845.2"])
        # The key _MG8 and the version fallback both resolve to .2/.9's base.
        assert found["GCF_000002515.2"]["ncbi_taxid"] == 4932
        assert found["GCF_000002515.2"]["ncbi_organism_name"] == "Saccharomyces cerevisiae"
        assert found["GCF_000002515.2"]["ncbi_taxonomy"] == SACCHAROMYCES
        assert found["GCA_000002495.2"]["ncbi_taxid"] == 5141
        # A merged taxid resolves to the current one; no lineage, no row.
        assert db.lookup(["GCF_000000003.1"])["GCF_000000003.1"]["ncbi_taxid"] == 4932
        assert "GCF_000000004.1" not in db.lookup(["GCF_000000004.1"])
        # The suffixed key GCF_000005845.2_ASM584v2 must not replace the GTDB row.
        assert found["GCF_000005845.2"]["gtdb_taxonomy"] == "d__Bacteria;p__Test"
        # A GTDB row keeps its own NCBI data.
        assert found["GCF_000005845.2"]["ncbi_taxid"] == 562
        assert found["GCF_000005845.2"]["ncbi_organism_name"] == "Escherichia coli K-12"
        # The enrichment row is not a GTDB genome.
        assert db.contains_accessions(["GCF_000002515.2"]) is False
        assert db.contains_accessions(["GCF_000005845.2"]) is True
        assert json.loads(db.info["ncbi_taxonomy"])["FungiRefSeq-latest"]["mapped"] == 4
        assert db.taxonomy_sources()[-1]["ncbi"]["mapped"] == 4


def test_update_enriches_an_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gtdb_source: Path,
    fungi_file: Path,
) -> None:
    output = tmp_path / "meta.sqlite"
    build_metadata_db(
        [gtdb_source],
        output,
        release=232,
        taxonomy=[TaxonomySource("FungiRefSeq-latest", fungi_file)],
    )
    _patch_ncbi(monkeypatch)
    stats = update_metadata_db(output, output, ncbi_cache=tmp_path / "cache")
    sources = {source["name"]: source for source in stats["taxonomy_sources"]}
    assert sources["FungiRefSeq-latest"]["ncbi"]["mapped"] == 4
    assert stats["rows"] == 1
    with MetadataDB(output) as db:
        assert db.lookup(["GCF_000002515.2"])["GCF_000002515.2"]["ncbi_taxid"] == 4932


def test_setup_reports_the_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    gtdb_source: Path,
    fungi_file: Path,
) -> None:
    _patch_ncbi(monkeypatch)
    output = tmp_path / "meta.sqlite"
    code = cli.main(
        [
            "setup",
            "-m",
            str(gtdb_source),
            "--taxonomy",
            f"FungiRefSeq-latest={fungi_file}",
            "--ncbi-taxonomy",
            "--cache-dir",
            str(tmp_path / "cache"),
            "-o",
            str(output),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "NCBI lineage: 4 mapped, 1 without an assembly accession" in captured.err
    assert "1 not in the summaries" in captured.err


def test_rerun_is_idempotent_and_update_prunes_orphans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gtdb_source: Path,
    fungi_file: Path,
) -> None:
    _patch_ncbi(monkeypatch)
    first, second, third = (tmp_path / f"{name}.sqlite" for name in ("a", "b", "c"))
    taxonomy = [TaxonomySource("FungiRefSeq-latest", fungi_file)]
    build_metadata_db(
        [gtdb_source], first, release=232, taxonomy=taxonomy, ncbi_cache=tmp_path / "c"
    )

    def rows(path: Path) -> list[tuple[object, ...]]:
        with closing(sqlite3.connect(path)) as connection:
            return connection.execute(
                "SELECT * FROM metadata_data ORDER BY accession"
            ).fetchall()

    stats = update_metadata_db(first, second, ncbi_cache=tmp_path / "c")
    assert stats["taxonomy_sources"][-1]["ncbi"]["mapped"] == 4  # type: ignore[index]
    assert rows(second) == rows(first)

    # Replacing the source by one with a single key drops the other rows.
    small = tmp_path / "small.tsv"
    small.write_text("GCA_000002495.2\td__Eukaryota\n")
    update_metadata_db(
        second, third, taxonomy=[TaxonomySource("FungiRefSeq-latest", small)]
    )
    assert [row[0] for row in rows(third)] == ["GCA_000002495.2", "GCF_000005845.2"]
    with MetadataDB(third) as db:
        assert "ncbi" not in db.taxonomy_sources()[-1]


def test_taxdump_is_not_left_in_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ncbi_taxonomy, "fetch_assemblies", _fake_assemblies)

    def fetch(directory: Path, *, progress: object = None) -> tuple[Path, ...]:
        (directory / "new_taxdump.tar.gz").write_text("x")
        return RANKED, NODES, MERGED

    monkeypatch.setattr(ncbi_taxonomy, "fetch_taxdump", fetch)
    found = ncbi_taxonomy.lookup(["GCF_000002515.2"], tmp_path / "cache")
    assert found["GCF_000002515.2"].lineage == SACCHAROMYCES
    assert list((tmp_path / "cache").iterdir()) == []
