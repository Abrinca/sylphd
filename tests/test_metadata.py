"""Tests for the metadata download/build/lookup layer."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from sylphd.metadata import (
    COLUMNS,
    METADATA_COLUMNS,
    MetadataDB,
    accession_from_genome_name,
    normalize_accession,
)
from sylphd.metadata_build import build_metadata_db

HEADER = ["accession", *METADATA_COLUMNS]


def _row(accession: str, **overrides: str) -> list[str]:
    values = {column.name: "none" for column in COLUMNS}
    values["gtdb_taxonomy"] = "d__Bacteria;p__Test"
    values.update(overrides)
    return [accession, *[values[column.name] for column in COLUMNS]]


def _write_tsv(path: Path, rows: list[list[str]], *, compress: bool = True) -> Path:
    text = "\t".join(HEADER) + "\n"
    text += "\n".join("\t".join(row) for row in rows) + "\n"
    if compress:
        with gzip.open(path, "wt", newline="") as handle:
            handle.write(text)
    else:
        path.write_text(text, newline="")
    return path


def _synthetic_rows() -> list[list[str]]:
    """Rows covering every column, NULLs, booleans, numerics and repeats."""
    taxonomy = "d__Bacteria;p__Pseudomonadota;c__Gammaproteobacteria"
    return [
        _row(
            "RS_GCF_000005845.2",
            gtdb_taxonomy=taxonomy,
            gtdb_representative="t",
            gtdb_genome_representative="RS_GCF_000005845.2",
            gtdb_type_species_of_genus="t",
            gtdb_type_designation_ncbi_taxa="type strain of Escherichia",
            ncbi_taxid="562",
            ncbi_species_taxid="562",
            ncbi_taxonomy="d__Bacteria;p__Pseudomonadota",
            ncbi_taxonomy_unfiltered=taxonomy + ";s__Escherichia coli",
            ncbi_organism_name='Escherichia coli "K-12"; substr',
            ncbi_strain_identifiers="K-12",
            ncbi_isolate="soil",
            ncbi_genome_category="environmental",
            ncbi_assembly_level="Complete Genome",
            ncbi_refseq_category="representative genome",
            ncbi_type_material_designation="type strain",
            ncbi_biosample="SAMN02604091",
            ncbi_bioproject="PRJNA59999",
            checkm2_completeness="99.5",
            checkm2_contamination="0.1",
            genome_size="4641652",
            gc_percentage="50.79",
            contig_count="1",
            n50_contigs="4641652",
            mimag_high_quality="t",
        ),
        _row(
            "GB_GCA_000000001.1",
            gtdb_taxonomy=taxonomy,
            gtdb_representative="f",
            gtdb_genome_representative="RS_GCF_000005845.2",
            ncbi_taxonomy_unfiltered="d__Bacteria",
            ncbi_genome_category="",
            ncbi_assembly_level="Scaffold",
            gc_percentage="35.27835845112408",
            genome_size="1192581",
            mimag_high_quality="f",
        ),
        # every value is a NULL marker
        _row("GB_GCA_000000002.1", gtdb_taxonomy="none"),
        _row(
            "GB_GCA_000000003.1",
            gtdb_taxonomy="d__Archaea",
            gtdb_representative="t",
            ncbi_organism_name="Ünïcode name — ünd mehr",
            ncbi_bioproject="PRJNA59999",
            ncbi_taxid="0",
            checkm2_completeness="0.0",
        ),
    ]


class TestAccessions:
    @pytest.mark.parametrize(
        "value, expected",
        [
            ("RS_GCF_000005845.2", "GCF_000005845.2"),
            ("GB_GCA_964372435.1", "GCA_964372435.1"),
            ("gca_964372435.1", "GCA_964372435.1"),
            (" GCF_000005845.2 ", "GCF_000005845.2"),
        ],
    )
    def test_normalize(self, value: str, expected: str) -> None:
        assert normalize_accession(value) == expected

    @pytest.mark.parametrize(
        "name, expected",
        [
            ("/db/GCA_964372435.1_genomic.fna.gz", "GCA_964372435.1"),
            ("GCA_964372435.1_genomic.fna", "GCA_964372435.1"),
            ("GCA_964372435.1_genomic.fasta.gz", "GCA_964372435.1"),
            ("GCF_000005845.2_ASM584v2_genomic.fna.gz", "GCF_000005845.2"),
            ("RS_GCF_000005845.2_genomic.fna.gz", "GCF_000005845.2"),
            ("GCA_964372435.1.fna.gz", "GCA_964372435.1"),
            ("GCA_964372435.1.fa.gz", "GCA_964372435.1"),
            ("C:\\db\\GCA_964372435.1_genomic.fna.gz", "GCA_964372435.1"),
            ("test_files/e.coli-K12.fasta.gz", None),
            ("reads.fastq.gz", None),
            ("", None),
        ],
    )
    def test_from_genome_name(self, name: str, expected: str | None) -> None:
        assert accession_from_genome_name(name) == expected


class TestBuildMetadata:
    def test_build_and_lookup(self, tmp_path: Path) -> None:
        source = _write_tsv(
            tmp_path / "bac120_metadata_r232.tsv.gz",
            [
                _row(
                    "RS_GCF_000005845.2",
                    gtdb_representative="t",
                    gtdb_type_species_of_genus="f",
                    ncbi_taxid="562",
                    genome_size="4641652",
                    gc_percentage="50.79",
                    contig_count="1",
                    mimag_high_quality="t",
                    ncbi_isolate="",
                ),
                _row(
                    "GB_GCA_964372435.1",
                    gtdb_representative="f",
                    ncbi_taxid="na",
                    genome_size="",
                    mimag_high_quality="f",
                ),
                # duplicate accession: the last row wins
                _row("RS_GCF_000005845.2", genome_size="111", gtdb_representative="f"),
            ],
        )
        output = tmp_path / "meta.sqlite"
        stats = build_metadata_db([source], output, release=232)
        assert stats["rows"] == 2
        assert output.is_file()

        with MetadataDB(output) as db:
            assert set(db.columns) == set(METADATA_COLUMNS)
            assert db.info["release"] == "232"
            summary = db.summary()
            assert summary["rows"] == 2
            assert summary["sources"][0]["rows"] == 3

            found = db.lookup(["GCF_000005845.2", "GCA_964372435.1", "GCA_000000000.1"])
            assert set(found) == {"GCF_000005845.2", "GCA_964372435.1"}
            gcf = found["GCF_000005845.2"]
            # last row won
            assert gcf["genome_size"] == 111
            assert gcf["gtdb_representative"] == 0
            # "none"/"na"/"" became NULL
            assert gcf["ncbi_isolate"] is None
            gca = found["GCA_964372435.1"]
            assert gca["ncbi_taxid"] is None
            assert gca["genome_size"] is None
            assert gca["mimag_high_quality"] == 0
            assert gca["gtdb_taxonomy"] == "d__Bacteria;p__Test"

    def test_plain_tsv_and_float_values(self, tmp_path: Path) -> None:
        source = _write_tsv(
            tmp_path / "plain.tsv",
            [_row("GCA_000001.1", gc_percentage="35.27835845112408", genome_size="1192581")],
            compress=False,
        )
        output = tmp_path / "meta.sqlite"
        build_metadata_db([source], output)
        with MetadataDB(output) as db:
            row = db.lookup(["GCA_000001.1"])["GCA_000001.1"]
            assert row["genome_size"] == 1192581
            assert row["gc_percentage"] == pytest.approx(35.27835845112408)
            assert db.info["release"] == ""

    def test_missing_column(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.tsv"
        path.write_text("accession\tgenome_size\nGCA_1\t1\n")
        with pytest.raises(ValueError, match="missing column"):
            build_metadata_db([path], tmp_path / "out.sqlite")

    def test_malformed_row_reports_line_number(self, tmp_path: Path) -> None:
        path = _write_tsv(tmp_path / "big.tsv", [_row("GCA_1"), _row("GCA_2")], compress=False)
        text = path.read_text().replace("GCA_2", "x" * 200_000)  # over csv's field limit
        path.write_text(text)
        with pytest.raises(ValueError, match=r"big\.tsv: line 3: .*field larger"):
            build_metadata_db([path], tmp_path / "out.sqlite")

    def test_refuses_overwrite(self, tmp_path: Path) -> None:
        source = _write_tsv(tmp_path / "one.tsv", [_row("GCA_1")])
        output = tmp_path / "meta.sqlite"
        build_metadata_db([source], output)
        with pytest.raises(FileExistsError):
            build_metadata_db([source], output)
        build_metadata_db([source], output, force=True)

    def test_opens_path_with_uri_characters(self, tmp_path: Path) -> None:
        source = _write_tsv(tmp_path / "one.tsv", [_row("GCA_1")])
        output = tmp_path / "meta#1?x%20.sqlite"
        build_metadata_db([source], output)
        with MetadataDB(output) as db:
            assert set(db.lookup(["GCA_1"])) == {"GCA_1"}

    def test_failed_force_rebuild_keeps_old_database(self, tmp_path: Path) -> None:
        source = _write_tsv(tmp_path / "one.tsv", [_row("GCA_1")])
        output = tmp_path / "meta.sqlite"
        build_metadata_db([source], output)
        before = output.read_bytes()

        broken = tmp_path / "broken.tsv"
        broken.write_text("accession\tgenome_size\nGCA_1\t1\n")
        with pytest.raises(ValueError, match="missing column"):
            build_metadata_db([broken], output, force=True)
        with pytest.raises(FileNotFoundError):
            build_metadata_db([tmp_path / "nope.tsv"], output, force=True)

        assert output.read_bytes() == before
        assert sorted(path.name for path in tmp_path.glob("meta*")) == ["meta.sqlite"]


class TestDictLayout:
    @pytest.fixture()
    def built(self, tmp_path: Path) -> Path:
        source = _write_tsv(tmp_path / "meta.tsv.gz", _synthetic_rows())
        output = tmp_path / "meta.sqlite"
        build_metadata_db([source], output, release=232)
        return output

    def test_view_returns_the_original_values(self, built) -> None:
        taxonomy = "d__Bacteria;p__Pseudomonadota;c__Gammaproteobacteria"
        non_null = {
            "GCF_000005845.2": {
                "gtdb_taxonomy": taxonomy,
                "gtdb_representative": 1,
                "gtdb_genome_representative": "RS_GCF_000005845.2",
                "gtdb_type_species_of_genus": 1,
                "gtdb_type_designation_ncbi_taxa": "type strain of Escherichia",
                "ncbi_taxid": 562,
                "ncbi_species_taxid": 562,
                "ncbi_taxonomy": "d__Bacteria;p__Pseudomonadota",
                "ncbi_taxonomy_unfiltered": taxonomy + ";s__Escherichia coli",
                "ncbi_organism_name": 'Escherichia coli "K-12"; substr',
                "ncbi_strain_identifiers": "K-12",
                "ncbi_isolate": "soil",
                "ncbi_genome_category": "environmental",
                "ncbi_assembly_level": "Complete Genome",
                "ncbi_refseq_category": "representative genome",
                "ncbi_type_material_designation": "type strain",
                "ncbi_biosample": "SAMN02604091",
                "ncbi_bioproject": "PRJNA59999",
                "checkm2_completeness": 99.5,
                "checkm2_contamination": 0.1,
                "genome_size": 4641652,
                "gc_percentage": 50.79,
                "contig_count": 1,
                "n50_contigs": 4641652,
                "mimag_high_quality": 1,
            },
            "GCA_000000001.1": {
                "gtdb_taxonomy": taxonomy,
                "gtdb_representative": 0,
                "gtdb_genome_representative": "RS_GCF_000005845.2",
                "ncbi_taxonomy_unfiltered": "d__Bacteria",
                "ncbi_assembly_level": "Scaffold",
                "gc_percentage": 35.27835845112408,
                "genome_size": 1192581,
                "mimag_high_quality": 0,
            },
            "GCA_000000002.1": {},  # every cell NULL
            "GCA_000000003.1": {
                "gtdb_taxonomy": "d__Archaea",
                "gtdb_representative": 1,
                "ncbi_organism_name": "Ünïcode name — ünd mehr",
                "ncbi_bioproject": "PRJNA59999",
                "ncbi_taxid": 0,
                "checkm2_completeness": 0.0,
            },
        }
        with MetadataDB(built) as db:
            assert db.columns == METADATA_COLUMNS
            found = db.lookup(list(non_null))
        assert found == {
            accession: {column: values.get(column) for column in METADATA_COLUMNS}
            for accession, values in non_null.items()
        }

    def test_reps_only_filtering(self, tmp_path: Path) -> None:
        source = _write_tsv(
            tmp_path / "meta.tsv.gz",
            [
                _row("GCA_000000001.1", gtdb_representative="t"),
                _row("GCA_000000002.1", gtdb_representative="f"),
                _row("GCA_000000003.1", gtdb_representative="none"),
                _row("GCA_000000004.1", gtdb_representative="t"),
            ],
        )
        output = tmp_path / "reps.sqlite"
        stats = build_metadata_db([source], output, reps_only=True)
        assert stats["rows"] == 2
        with MetadataDB(output) as db:
            assert db.info["reps_only"] == "true"
            assert db.summary()["reps_only"] is True
            assert db.summary()["rows"] == 2
            found = db.lookup(
                ["GCA_000000001.1", "GCA_000000002.1", "GCA_000000003.1", "GCA_000000004.1"]
            )
            assert set(found) == {"GCA_000000001.1", "GCA_000000004.1"}

    def test_info_fields(self, built) -> None:
        with MetadataDB(built) as db:
            assert db.info["format_version"] == "3"
            assert db.info["reps_only"] == "false"
            assert db.info["release"] == "232"
            assert db.info["sylphtax_rules"] == "1.9.2"
            assert json.loads(db.info["columns"]) == list(METADATA_COLUMNS)
            summary = db.summary()
            assert summary["format_version"] == "3"
            assert summary["reps_only"] is False
            assert summary["rows"] == 4
            # the GTDB metadata is registered as one virtual source
            assert summary["taxonomy_sources"] == [
                {
                    "name": "GTDB_r232",
                    "kind": "metadata",
                    "origin": "metadata_data",
                    "md5": None,
                    "keys": 3,  # one row has no gtdb_taxonomy
                    "has_host": False,
                }
            ]
            assert summary["taxonomy_overlaps"] == []
