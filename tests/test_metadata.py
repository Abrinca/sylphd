"""Tests for the metadata download/build/lookup layer."""

from __future__ import annotations

import gzip
import json
import sqlite3
from pathlib import Path

import pytest

from sylphd.metadata import (
    COLUMNS,
    DICT_ENCODED_COLUMNS,
    METADATA_COLUMNS,
    MetadataDB,
    _iter_rows,
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


def _write_plain_table_db(
    path: Path, source: Path, *, format_version: str = "1"
) -> Path:
    """Write the pre-dictionary layout: one verbatim table named ``metadata``."""
    connection = sqlite3.connect(path)
    try:
        column_sql = ", ".join(f'"{column.name}" {column.sql_type}' for column in COLUMNS)
        connection.execute(
            f"CREATE TABLE metadata (accession TEXT PRIMARY KEY, {column_sql})"
        )
        connection.execute("CREATE TABLE info (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        placeholders = ", ".join("?" * (1 + len(COLUMNS)))
        rows = [(accession, *values) for accession, values in _iter_rows(source)]
        connection.executemany(
            f"INSERT OR REPLACE INTO metadata VALUES ({placeholders})", rows
        )
        connection.executemany(
            "INSERT OR REPLACE INTO info (key, value) VALUES (?, ?)",
            [
                ("format_version", format_version),
                ("release", "232"),
                ("rows", str(len(rows))),
            ],
        )
        connection.commit()
    finally:
        connection.close()
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


def _view_rows(path: Path, accessions: list[str]) -> dict[str, tuple[object, ...]]:
    connection = sqlite3.connect(path)
    try:
        placeholders = ", ".join("?" * len(accessions))
        rows = connection.execute(
            f"SELECT * FROM metadata WHERE accession IN ({placeholders}) ORDER BY accession",
            accessions,
        ).fetchall()
    finally:
        connection.close()
    return {row[0]: row for row in rows}


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


class TestDictLayout:
    @pytest.fixture()
    def built(self, tmp_path: Path) -> tuple[Path, Path]:
        source = _write_tsv(tmp_path / "meta.tsv.gz", _synthetic_rows())
        output = tmp_path / "meta.sqlite"
        build_metadata_db([source], output, release=232)
        return output, source

    def test_metadata_is_a_view_over_a_without_rowid_table(self, built) -> None:
        output, _ = built
        connection = sqlite3.connect(output)
        try:
            objects = {
                row[1]: (row[0], row[2] or "")
                for row in connection.execute(
                    "SELECT type, name, sql FROM sqlite_master"
                )
            }
        finally:
            connection.close()
        assert objects["metadata"][0] == "view"
        assert "FROM metadata_data" in objects["metadata"][1]
        assert objects["metadata_data"][0] == "table"
        assert "WITHOUT ROWID" in objects["metadata_data"][1]
        assert "info" in objects

    def test_dictionaries_replace_the_repetitive_text(self, built) -> None:
        output, _ = built
        connection = sqlite3.connect(output)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            base_columns = {
                row[1]: row[2]
                for row in connection.execute("PRAGMA table_info(metadata_data)")
            }
            extra_indexes = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index' "
                    "AND name NOT LIKE 'sqlite_autoindex%'"
                )
            ]
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            distinct_taxonomies = connection.execute(
                "SELECT COUNT(*) FROM dict_gtdb_taxonomy"
            ).fetchone()[0]
            shared = connection.execute(
                "SELECT COUNT(*) FROM metadata_data "
                "WHERE gtdb_taxonomy = (SELECT id FROM dict_gtdb_taxonomy "
                "WHERE value = ?)",
                ("d__Bacteria;p__Pseudomonadota;c__Gammaproteobacteria",),
            ).fetchone()[0]
        finally:
            connection.close()
        for name in DICT_ENCODED_COLUMNS:
            assert f"dict_{name}" in tables
            assert base_columns[name] == "INTEGER"
        assert base_columns["genome_size"] == "INTEGER"
        assert base_columns["gc_percentage"] == "REAL"
        # only accession is queried, so no secondary indexes are needed
        assert extra_indexes == []
        assert page_size == 8192
        assert distinct_taxonomies == 2
        assert shared == 2

    def test_view_values_match_the_plain_table(self, built) -> None:
        output, source = built
        plain = _write_plain_table_db(source.with_name("plain-meta.sqlite"), source)
        accessions = [row[0] for row in _iter_rows(source)]
        assert _view_rows(output, accessions) == _view_rows(plain, accessions)

        connection = sqlite3.connect(output)
        plain_connection = sqlite3.connect(plain)
        try:
            view_info = [
                (row[1], row[2]) for row in connection.execute("PRAGMA table_info(metadata)")
            ]
            plain_info = [
                (row[1], row[2])
                for row in plain_connection.execute("PRAGMA table_info(metadata)")
            ]
            nulls = connection.execute(
                "SELECT * FROM metadata WHERE accession = 'GCA_000000002.1'"
            ).fetchone()
        finally:
            connection.close()
            plain_connection.close()
        assert view_info == plain_info
        assert list(view_info[0]) == ["accession", "TEXT"]
        # NULL cells stay NULL even though they go through the dictionaries
        assert nulls[1:] == (None,) * len(METADATA_COLUMNS)

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
        output, _ = built
        with MetadataDB(output) as db:
            assert db.info["format_version"] == "2"
            assert db.info["reps_only"] == "false"
            assert db.info["release"] == "232"
            assert json.loads(db.info["columns"]) == list(METADATA_COLUMNS)
            summary = db.summary()
            assert summary["format_version"] == "2"
            assert summary["reps_only"] is False
            assert summary["rows"] == 4


class TestOldFormatCompatibility:
    def test_plain_table_database_is_readable(self, tmp_path: Path) -> None:
        source = _write_tsv(
            tmp_path / "meta.tsv.gz",
            [
                _row("GCA_000000001.1", gtdb_taxonomy="d__Bacteria", genome_size="42"),
                _row("GCA_000000002.1"),
            ],
        )
        path = _write_plain_table_db(tmp_path / "old.sqlite", source)
        with MetadataDB(path) as db:
            assert db.columns == METADATA_COLUMNS
            assert db.info["format_version"] == "1"
            summary = db.summary()
            assert summary["rows"] == 2
            assert summary["format_version"] == "1"
            assert summary["reps_only"] is False
            found = db.lookup(["GCA_000000001.1", "GCA_000000002.1", "GCA_9"])
            assert set(found) == {"GCA_000000001.1", "GCA_000000002.1"}
            assert found["GCA_000000001.1"]["genome_size"] == 42
            assert found["GCA_000000001.1"]["gtdb_taxonomy"] == "d__Bacteria"
            assert found["GCA_000000002.1"]["genome_size"] is None
