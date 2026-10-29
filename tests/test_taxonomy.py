"""Unit tests for the client-side taxonomy aggregation (``sylphd/taxonomy.py``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sylphd.taxonomy import (
    TAXPROF_COLUMNS,
    TaxonomyError,
    aggregate_rows,
    aggregate_samples,
    clade_matrix_lines,
    parse_lineage,
    parse_taxonomies,
    resolve_taxonomy_columns,
    strain_label,
    taxprof_lines,
    write_outputs,
)

GTDB_SPECIES = (
    "d__Bacteria;p__Bacillota;c__Clostridia;o__Clostridiales;"
    "f__Clostridiaceae;g__Clostridium;s__Clostridium difficile"
)


def _row(
    *,
    gtdb: str | None = None,
    ncbi: str | None = None,
    relative: float = 100.0,
    sequence: float = 100.0,
    ani: float = 99.0,
    coverage: float = 1.0,
    accession: str | None = "GCA_000000001.1",
    genome: str = "GCA_000000001.1_genomic.fna.gz",
) -> dict[str, object]:
    return {
        "Genome_file": genome,
        "Taxonomic_abundance": relative,
        "Sequence_abundance": sequence,
        "Adjusted_ANI": ani,
        "Eff_cov": coverage,
        "accession": accession,
        "gtdb_taxonomy": gtdb,
        "ncbi_taxonomy": ncbi,
    }


class TestParseLineage:
    def test_gtdb(self) -> None:
        assert parse_lineage(GTDB_SPECIES) == (
            "d__Bacteria",
            "p__Bacillota",
            "c__Clostridia",
            "o__Clostridiales",
            "f__Clostridiaceae",
            "g__Clostridium",
            "s__Clostridium difficile",
        )

    def test_ncbi_internal_gaps_are_filled(self) -> None:
        lineage = parse_lineage(
            "d__Bacteria;p__Bacillota;c__Clostridia;o__;f__;"
            "g__Candidatus Avispirillum;s__"
        )
        assert lineage == (
            "d__Bacteria",
            "p__Bacillota",
            "c__Clostridia",
            "o__unclassified Clostridia",
            "f__unclassified Clostridia",
            "g__Candidatus Avispirillum",
        )

    def test_trailing_gaps_are_dropped(self) -> None:
        assert parse_lineage("d__Bacteria;p__Bacillota;c__;o__;f__;g__;s__") == (
            "d__Bacteria",
            "p__Bacillota",
        )

    def test_leading_gap_uses_root(self) -> None:
        assert parse_lineage("d__;p__Bacillota") == (
            "d__unclassified root",
            "p__Bacillota",
        )

    @pytest.mark.parametrize("taxonomy", [None, "", "   ", "none", "NA", ";", ";;"])
    def test_missing_taxonomy(self, taxonomy: str | None) -> None:
        assert parse_lineage(taxonomy) == ()

    def test_unclassified_string(self) -> None:
        # No rank prefix: nothing named, so the row is unclassified.
        assert parse_lineage("unclassified") == ()

    def test_whitespace_and_unknown_prefixes(self) -> None:
        assert parse_lineage(" d__Bacteria ; p__Bacillota ;t__GCA_1; b__Viruses ") == (
            "d__Bacteria",
            "p__Bacillota",
        )

    def test_first_named_token_wins(self) -> None:
        assert parse_lineage("d__Bacteria;d__Viruses;p__X") == (
            "d__Bacteria",
            "p__X",
        )


class TestStrainLabel:
    def test_accession_wins(self) -> None:
        assert strain_label(_row(accession="GCA_1", genome="other.fna")) == "GCA_1"

    def test_falls_back_to_basename(self) -> None:
        assert strain_label(_row(accession=None, genome="/db/x/GCA_2.fna.gz")) == (
            "GCA_2.fna.gz"
        )

    def test_empty_values(self) -> None:
        assert strain_label({"accession": "  ", "Genome_file": None}) == "unknown"


class TestAggregateRows:
    def test_sums_every_rank(self) -> None:
        rows = [
            _row(
                gtdb=GTDB_SPECIES,
                relative=60.0,
                sequence=30.0,
                ani=99.5,
                coverage=4.0,
                accession="A",
            ),
            _row(
                gtdb=(
                    "d__Bacteria;p__Bacillota;c__Clostridia;o__Clostridiales;"
                    "f__Clostridiaceae;g__Clostridium;s__Clostridium botulinum"
                ),
                relative=40.0,
                sequence=20.0,
                ani=98.0,
                coverage=2.0,
                accession="B",
            ),
        ]
        clades = aggregate_rows(rows, "gtdb")
        by_name = {item.clade: item for item in clades}

        for clade in (
            "d__Bacteria",
            "d__Bacteria|p__Bacillota",
            "d__Bacteria|p__Bacillota|c__Clostridia",
            "d__Bacteria|p__Bacillota|c__Clostridia|o__Clostridiales",
            "d__Bacteria|p__Bacillota|c__Clostridia|o__Clostridiales|f__Clostridiaceae",
            "d__Bacteria|p__Bacillota|c__Clostridia|o__Clostridiales|f__Clostridiaceae"
            "|g__Clostridium",
        ):
            assert by_name[clade].relative == pytest.approx(100.0)
            assert by_name[clade].sequence == pytest.approx(50.0)
            assert by_name[clade].ani is None
            assert by_name[clade].coverage is None

        species_a = by_name[
            "d__Bacteria|p__Bacillota|c__Clostridia|o__Clostridiales|"
            "f__Clostridiaceae|g__Clostridium|s__Clostridium difficile"
        ]
        species_b = by_name[
            "d__Bacteria|p__Bacillota|c__Clostridia|o__Clostridiales|"
            "f__Clostridiaceae|g__Clostridium|s__Clostridium botulinum"
        ]
        assert (species_a.relative, species_a.sequence) == (pytest.approx(60.0), pytest.approx(30.0))
        assert (species_b.relative, species_b.sequence) == (pytest.approx(40.0), pytest.approx(20.0))

        strain_a = by_name[species_a.clade + "|t__A"]
        assert strain_a.relative == pytest.approx(60.0)
        assert strain_a.ani == pytest.approx(99.5)
        assert strain_a.coverage == pytest.approx(4.0)
        assert strain_a.is_strain

    def test_order_is_depth_then_abundance(self) -> None:
        rows = [
            _row(gtdb=GTDB_SPECIES, relative=10.0, accession="A"),
            _row(
                gtdb=(
                    "d__Bacteria;p__Pseudomonadota;c__Gammaproteobacteria;"
                    "o__Enterobacterales;f__Enterobacteriaceae;g__Escherichia;"
                    "s__Escherichia coli"
                ),
                relative=90.0,
                accession="B",
            ),
        ]
        clades = aggregate_rows(rows, "gtdb")
        depths = [item.depth for item in clades]
        assert depths == sorted(depths)
        # Same depth, higher abundance first.
        assert clades[1].clade == "d__Bacteria|p__Pseudomonadota"
        assert clades[2].clade == "d__Bacteria|p__Bacillota"
        # The t__ rows come after all rank rows.
        assert [item.clade for item in clades if item.is_strain] == [
            clades[-2].clade,
            clades[-1].clade,
        ]

    def test_strains_below_a_truncated_lineage_come_last(self) -> None:
        # The first strain hangs below the genus, at the depth of a species row.
        rows = [
            _row(ncbi="d__Bacteria;p__P;c__C;o__O;f__F;g__G;s__", relative=90.0, accession="A"),
            _row(ncbi="d__Bacteria;p__P;c__C;o__O;f__F;g__G;s__S", relative=10.0, accession="B"),
        ]
        clades = aggregate_rows(rows, "ncbi")
        assert [item.is_strain for item in clades] == [False] * 7 + [True] * 2
        assert clades[-2].clade.endswith("|t__A")  # strains by abundance

    def test_no_renormalisation(self) -> None:
        # --estimate-unknown leaves abundance below 100; keep it as reported.
        clades = aggregate_rows([_row(gtdb=GTDB_SPECIES, relative=37.5)], "gtdb")
        assert clades[0].clade == "d__Bacteria"
        assert clades[0].relative == pytest.approx(37.5)

    def test_unclassified_rows(self) -> None:
        clades = aggregate_rows(
            [_row(gtdb=None, ncbi=None, accession=None, genome="/x/genome.fna")],
            "ncbi",
        )
        assert [item.clade for item in clades] == [
            "unclassified",
            "unclassified|t__genome.fna",
        ]
        assert clades[1].is_strain
        assert clades[1].ani == pytest.approx(99.0)

    def test_per_sample_aggregation(self) -> None:
        samples = [
            {"name": "a", "rows": [_row(gtdb=GTDB_SPECIES, relative=100.0, accession="A")]},
            {
                "name": "b",
                "rows": [
                    _row(gtdb=GTDB_SPECIES, relative=40.0, accession="A"),
                    _row(
                        gtdb="d__Bacteria;p__Pseudomonadota;c__X;o__Y;f__Z;g__G;s__S",
                        relative=60.0,
                        accession="B",
                    ),
                ],
            },
        ]
        tables = aggregate_samples(samples, "gtdb")
        assert [name for name, _ in tables] == ["a", "b"]
        first = {item.clade: item.relative for item in tables[0][1]}
        second = {item.clade: item.relative for item in tables[1][1]}
        assert first["d__Bacteria"] == pytest.approx(100.0)
        assert second["d__Bacteria"] == pytest.approx(100.0)
        assert "d__Bacteria|p__Pseudomonadota" not in first
        assert second["d__Bacteria|p__Pseudomonadota"] == pytest.approx(60.0)


class TestTaxprof:
    def _clades(self) -> list:
        return aggregate_rows(
            [
                _row(
                    gtdb=GTDB_SPECIES,
                    relative=75.0,
                    sequence=25.0,
                    ani=99.25,
                    coverage=3.5,
                ),
                _row(
                    gtdb="d__Bacteria;p__Pseudomonadota;c__X;o__Y;f__Z;g__G;s__S",
                    relative=25.0,
                    sequence=75.0,
                    accession="B",
                ),
            ],
            "gtdb",
        )

    def test_header_and_columns(self) -> None:
        lines = list(taxprof_lines("sample-a", "gtdb", self._clades()))
        assert lines[0] == "#SampleID\tsample-a\tTaxonomies_used:['gtdb']"
        assert lines[1].split("\t") == list(TAXPROF_COLUMNS)
        for line in lines[2:]:
            assert len(line.split("\t")) == 5

    def test_non_strain_rows_have_na(self) -> None:
        lines = list(taxprof_lines("s", "gtdb", self._clades()))
        root = lines[2].split("\t")
        assert root[:3] == ["d__Bacteria", "100.0", "100.0"]
        assert root[3:] == ["NA", "NA"]

        strains = [line.split("\t") for line in lines[2:] if "|t__" in line]
        assert len(strains) == 2
        strain = next(item for item in strains if item[0].endswith("|t__GCA_000000001.1"))
        assert float(strain[3]) == pytest.approx(99.25)
        assert float(strain[4]) == pytest.approx(3.5)


class TestMatrix:
    def _table(self, samples):
        return aggregate_samples(samples, "gtdb")

    def test_multiple_samples_and_missing_taxa(self) -> None:
        shared = "d__Bacteria;p__Bacillota;c__Clostridia;o__O;f__F;g__G;s__S"
        other = "d__Bacteria;p__Pseudomonadota;c__C;o__O;f__F;g__G;s__S2"
        samples = [
            {"name": "one", "rows": [_row(gtdb=shared, relative=100.0, accession="A")]},
            {
                "name": "two",
                "rows": [
                    _row(gtdb=shared, relative=25.0, accession="A"),
                    _row(gtdb=other, relative=75.0, accession="B"),
                ],
            },
        ]
        lines = list(clade_matrix_lines(self._table(samples)))
        assert lines[0] == "clade_name\tone\ttwo"
        rows = dict(line.split("\t", 1) for line in lines[1:])
        assert rows["d__Bacteria"] == "100.0\t100.0"
        shared_clade = "d__Bacteria|p__Bacillota"
        assert rows[shared_clade] == "100.0\t25.0"
        other_clade = "d__Bacteria|p__Pseudomonadota"
        assert rows[other_clade] == "0.0\t75.0"

    def test_sequence_value(self) -> None:
        samples = [
            {
                "name": "one",
                "rows": [_row(gtdb=GTDB_SPECIES, relative=20.0, sequence=80.0)],
            }
        ]
        lines = list(clade_matrix_lines(self._table(samples), value="sequence"))
        assert lines[1] == "d__Bacteria\t80.0"

    def test_row_union_order(self) -> None:
        first = "d__Bacteria;p__Bacillota;c__C;o__O;f__F;g__G;s__S"
        later = "d__Bacteria;p__Pseudomonadota;c__C;o__O;f__F;g__G;s__S2"
        samples = [
            {"name": "one", "rows": [_row(gtdb=first, accession="A")]},
            {"name": "two", "rows": [_row(gtdb=later, accession="B")]},
        ]
        clades = [line.split("\t")[0] for line in list(clade_matrix_lines(self._table(samples)))[1:]]
        assert clades.index("d__Bacteria|p__Bacillota") < clades.index(
            "d__Bacteria|p__Pseudomonadota"
        )


class TestFlags:
    def test_default_both(self) -> None:
        assert parse_taxonomies(None) == ("gtdb", "ncbi")

    def test_single_and_duplicates(self) -> None:
        assert parse_taxonomies("ncbi") == ("ncbi",)
        assert parse_taxonomies("gtdb, ncbi,gtdb") == ("gtdb", "ncbi")

    def test_unknown(self) -> None:
        with pytest.raises(TaxonomyError, match="unknown taxonomy"):
            parse_taxonomies("gtdb,kraken")

    def test_empty(self) -> None:
        with pytest.raises(TaxonomyError, match="at least one"):
            parse_taxonomies(", ")

    def test_resolve_columns(self) -> None:
        metadata = {"columns": ["genome_size", "gtdb_taxonomy", "ncbi_taxonomy"]}
        assert resolve_taxonomy_columns(("gtdb",), metadata) == ["gtdb_taxonomy"]
        assert resolve_taxonomy_columns(("gtdb", "ncbi"), metadata) == [
            "gtdb_taxonomy",
            "ncbi_taxonomy",
        ]

    def test_resolve_without_metadata(self) -> None:
        with pytest.raises(TaxonomyError, match="no metadata database"):
            resolve_taxonomy_columns(("gtdb",), None)

    def test_resolve_missing_column(self) -> None:
        with pytest.raises(TaxonomyError, match="ncbi_taxonomy"):
            resolve_taxonomy_columns(("ncbi",), {"columns": ["gtdb_taxonomy"]})


class TestWriteOutputs:
    def test_file_names(self, tmp_path: Path) -> None:
        samples = [
            {"name": "one", "rows": [_row(gtdb=GTDB_SPECIES, accession="A")]},
            {"name": "two", "rows": [_row(ncbi=GTDB_SPECIES, accession="A")]},
        ]
        written = write_outputs(
            samples,
            taxonomies=("gtdb", "ncbi"),
            taxprof_prefix=str(tmp_path / "p-"),
            taxmatrix_prefix=str(tmp_path / "m-"),
        )
        names = {path.name for path in written}
        assert names == {
            "p-one.gtdb.sylphmpa",
            "p-two.gtdb.sylphmpa",
            "m-gtdb.tsv",
            "p-one.ncbi.sylphmpa",
            "p-two.ncbi.sylphmpa",
            "m-ncbi.tsv",
        }
        assert all(path.is_file() for path in written)

    def test_sample_names_cannot_add_directories(self, tmp_path: Path) -> None:
        samples = [{"name": "a/b", "rows": []}]
        written = write_outputs(
            samples, taxonomies=("gtdb",), taxprof_prefix=str(tmp_path / "p-")
        )
        assert written[0].name == "p-a_b.gtdb.sylphmpa"

    def test_existing_directory_prefix_needs_no_slash(self, tmp_path: Path) -> None:
        samples = [{"name": "one", "rows": []}]
        written = write_outputs(
            samples,
            taxonomies=("gtdb",),
            taxprof_prefix=str(tmp_path),
            taxmatrix_prefix=str(tmp_path / "m-"),  # not a directory: a prefix
        )
        assert {path.parent for path in written} == {tmp_path}
        assert {path.name for path in written} == {"one.gtdb.sylphmpa", "m-gtdb.tsv"}

    def test_colliding_sample_names_write_nothing(self, tmp_path: Path) -> None:
        samples = [
            {"name": "a/b", "rows": [_row(gtdb=GTDB_SPECIES, accession="A")]},
            {"name": "a_b", "rows": [_row(gtdb=GTDB_SPECIES, accession="B")]},
        ]
        with pytest.raises(TaxonomyError, match="'a/b' and 'a_b'"):
            write_outputs(
                samples,
                taxonomies=("gtdb", "ncbi"),
                taxprof_prefix=str(tmp_path / "p-"),
                taxmatrix_prefix=str(tmp_path / "m-"),
            )
        assert not list(tmp_path.iterdir())

    def test_no_prefixes(self) -> None:
        assert write_outputs([], taxonomies=("gtdb",)) == []
