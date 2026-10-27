"""Tests for the metadata download/build/lookup layer."""

from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path

import pytest

from sylphd.metadata import (
    COLUMNS,
    METADATA_COLUMNS,
    MetadataDB,
    accession_from_genome_name,
    build_metadata_db,
    normalize_accession,
)

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

    def test_refuses_overwrite(self, tmp_path: Path) -> None:
        source = _write_tsv(tmp_path / "one.tsv", [_row("GCA_1")])
        output = tmp_path / "meta.sqlite"
        build_metadata_db([source], output)
        with pytest.raises(FileExistsError):
            build_metadata_db([source], output)
        build_metadata_db([source], output, force=True)

    def test_schema(self, tmp_path: Path) -> None:
        source = _write_tsv(tmp_path / "one.tsv", [_row("GCA_1")])
        output = tmp_path / "meta.sqlite"
        build_metadata_db([source], output)
        connection = sqlite3.connect(output)
        try:
            columns = {
                row[1]: row[2]
                for row in connection.execute("PRAGMA table_info(metadata)")
            }
            assert columns["accession"] == "TEXT"
            assert columns["genome_size"] == "INTEGER"
            assert columns["gc_percentage"] == "REAL"
            assert columns["mimag_high_quality"] == "INTEGER"
            keys = {row[0] for row in connection.execute("SELECT key FROM info")}
            assert {"format_version", "release", "sources", "rows", "columns"} <= keys
        finally:
            connection.close()

    def test_all_columns_present(self) -> None:
        names = [column.name for column in COLUMNS]
        assert names == list(METADATA_COLUMNS)
        assert len(names) == 25
