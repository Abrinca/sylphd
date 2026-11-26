"""Unit, CLI and light-install tests for ``sylphd taxmatrix`` and ``parse_sylphmpa``."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from sylphd import entry, taxmatrix
from sylphd.taxprofile import (
    CladeAbundance,
    TaxonomyError,
    aggregate_rows,
    aggregate_sylphtax_rows,
    build_taxprof,
    clade_matrix_lines,
    parse_sylphmpa,
    taxprof_lines,
)

SRC = Path(__file__).resolve().parent.parent / "src"

GTDB_A = (
    "d__Bacteria;p__Bacillota;c__Clostridia;o__Clostridiales;"
    "f__Clostridiaceae;g__Clostridium;s__Clostridium difficile"
)
GTDB_B = (
    "d__Bacteria;p__Pseudomonadota;c__Gammaproteobacteria;o__Enterobacterales;"
    "f__Enterobacteriaceae;g__Escherichia;s__Escherichia coli"
)
NCBI_A = GTDB_A.replace("s__Clostridium difficile", "s__Clostridioides difficile")


def _sample_rows_one() -> list[dict]:
    return [
        {
            "Genome_file": "GCA_000000001.1_genomic.fna.gz",
            "accession": "GCA_000000001.1",
            "Taxonomic_abundance": 60.0,
            "Sequence_abundance": 55.0,
            "Adjusted_ANI": 99.25,
            "Eff_cov": 12.5,
            "gtdb_taxonomy": GTDB_A,
            "ncbi_taxonomy": NCBI_A,
            "tax_lineage": "vFAM-1;vOTU-1",
            "tax_key": "UHGV-1",
            "tax_source": "UHGV",
        },
        {
            "Genome_file": "GCA_000000002.1_genomic.fna.gz",
            "accession": "GCA_000000002.1",
            "Taxonomic_abundance": 40.0,
            "Sequence_abundance": 45.0,
            "Adjusted_ANI": 97.5,
            "Eff_cov": 4.25,
            "gtdb_taxonomy": GTDB_B,
            "ncbi_taxonomy": GTDB_B,
            "tax_lineage": "vFAM-1;;vOTU-2",
            "tax_key": "UHGV-2",
            "tax_source": "UHGV",
        },
    ]


def _sample_rows_two() -> list[dict]:
    return [
        {
            "Genome_file": "GCA_000000001.1_genomic.fna.gz",
            "accession": "GCA_000000001.1",
            "Taxonomic_abundance": 100.0,
            "Sequence_abundance": 100.0,
            "Adjusted_ANI": 99.75,
            "Eff_cov": 8.0,
            "gtdb_taxonomy": GTDB_A,
            "ncbi_taxonomy": NCBI_A,
            "tax_lineage": "vFAM-1;vOTU-1",
            "tax_key": "UHGV-1",
            "tax_source": "UHGV",
        }
    ]


def _profile_doc(
    samples: list[tuple[str, list[dict]]],
    taxonomies: tuple[str, ...],
    *,
    tax_sources: tuple[str, ...] = ("UHGV",),
) -> dict:
    return {
        "api_version": 1,
        "params": {"minimum_ani": None, "estimate_unknown": False},
        "samples": [{"name": name} for name, _ in samples],
        "table": {"columns": ["Sample_file"], "data": []},
        "caveats": [],
        "taxprof": {
            name: build_taxprof(name, taxonomies, rows, tax_sources=tax_sources)
            for name, rows in samples
        },
    }


# ---------------------------------------------------------------------------
# parse_sylphmpa round-trip & validation
# ---------------------------------------------------------------------------


class TestParseSylphmpa:
    @pytest.mark.parametrize("taxonomy", ["gtdb", "ncbi", "tax"])
    def test_round_trip(self, taxonomy: str) -> None:
        rows = _sample_rows_one()
        original = (
            aggregate_sylphtax_rows(rows)
            if taxonomy == "tax"
            else aggregate_rows(rows, taxonomy)
        )
        used = ["UHGV"] if taxonomy == "tax" else [taxonomy]
        text = "\n".join(taxprof_lines("sample-a", taxonomy, original, taxonomies_used=used)) + "\n"

        name, parsed = parse_sylphmpa(text)
        assert name == "sample-a"
        assert len(parsed) == len(original)
        for got, want in zip(parsed, original):
            assert (got.clade, got.relative, got.sequence, got.ani, got.coverage) == (
                want.clade,
                want.relative,
                want.sequence,
                want.ani,
                want.coverage,
            )
        re_rendered = (
            "\n".join(taxprof_lines(name, taxonomy, parsed, taxonomies_used=used)) + "\n"
        )
        assert re_rendered == text

    def test_round_trip_preserves_mid_path_strain_stats(self) -> None:
        # sylph-tax marks any clade containing "t__" anywhere in its path with ANI/coverage.
        clades = [
            CladeAbundance("a|t__x|child", 50.0, 50.0, 98.5, 3.0, strain_stats=True),
        ]
        text = "\n".join(taxprof_lines("s1", "tax", clades, taxonomies_used=["S"])) + "\n"
        name, parsed = parse_sylphmpa(text)
        assert name == "s1"
        assert parsed[0].has_strain_stats is True
        assert parsed[0].ani == 98.5
        assert parsed[0].coverage == 3.0
        assert (
            "\n".join(taxprof_lines(name, "tax", parsed, taxonomies_used=["S"])) + "\n"
            == text
        )

    def test_empty_sample_round_trip(self) -> None:
        text = "\n".join(taxprof_lines("empty-sample", "gtdb", [])) + "\n"
        name, parsed = parse_sylphmpa(text)
        assert name == "empty-sample"
        assert parsed == []

    def test_six_column_virus_host_and_crlf_and_comments(self) -> None:
        text = (
            "#SampleID\tviral-1\tTaxonomies_used:['UHGV']\r\n"
            "# extra comment\r\n"
            "clade_name\trelative_abundance\tsequence_abundance\t"
            "ANI (if strain-level)\tCoverage (if strain-level)\tVirus_host (if viral)\r\n"
            "vFAM-1\t100.0\t100.0\tNA\tNA\tNA\r\n"
            "vFAM-1|t__V1\t100.0\t100.0\t99.5\t4.0\td__Bacteria\r\n"
        )
        name, clades = parse_sylphmpa(text)
        assert name == "viral-1"
        assert clades == [
            CladeAbundance("vFAM-1", 100.0, 100.0, None, None),
            CladeAbundance("vFAM-1|t__V1", 100.0, 100.0, 99.5, 4.0),
        ]

    @pytest.mark.parametrize(
        ("text", "match"),
        [
            ("", "not a .sylphmpa"),
            ("Sample_file\tGenome_file\n", "not a .sylphmpa"),
            ("#SampleID\n", "missing sample name"),
            ("#SampleID\t\tTaxonomies_used:['gtdb']\n", "missing sample name"),
            ("#SampleID\ts1\n", "missing column header"),
            ("#SampleID\ts1\nwrong\theader\n", "unexpected .sylphmpa header"),
            (
                ("#SampleID\ts1\n"
                "clade_name\trelative_abundance\tsequence_abundance\t"
                "ANI (if strain-level)\tCoverage (if strain-level)\n"
                "d__Bacteria\t100.0\t100.0\n"),
                "expected at least 5 columns",
            ),
            (
                ("#SampleID\ts1\n"
                "clade_name\trelative_abundance\tsequence_abundance\t"
                "ANI (if strain-level)\tCoverage (if strain-level)\n"
                "\t100.0\t100.0\tNA\tNA\n"),
                "empty clade_name",
            ),
            (
                ("#SampleID\ts1\n"
                "clade_name\trelative_abundance\tsequence_abundance\t"
                "ANI (if strain-level)\tCoverage (if strain-level)\n"
                "d__Bacteria\tnotanumber\t100.0\tNA\tNA\n"),
                "invalid relative_abundance",
            ),
            (
                ("#SampleID\ts1\n"
                "clade_name\trelative_abundance\tsequence_abundance\t"
                "ANI (if strain-level)\tCoverage (if strain-level)\n"
                "d__Bacteria|t__A\t100.0\t100.0\tbad\tNA\n"),
                "invalid ANI",
            ),
        ],
    )
    def test_malformed_inputs(self, text: str, match: str) -> None:
        with pytest.raises(TaxonomyError, match=match):
            parse_sylphmpa(text, label="bad.sylphmpa")


# ---------------------------------------------------------------------------
# /profile JSON documents & mixed inputs
# ---------------------------------------------------------------------------


class TestProfileDocumentInput:
    def test_single_taxonomy_auto_selected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        doc = _profile_doc([("one", _sample_rows_one()), ("two", _sample_rows_two())], ("gtdb",))
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(doc))

        assert taxmatrix.main([str(path)]) == 0
        out = capsys.readouterr().out
        expected = (
            "\n".join(
                clade_matrix_lines(
                    [
                        ("one", aggregate_rows(_sample_rows_one(), "gtdb")),
                        ("two", aggregate_rows(_sample_rows_two(), "gtdb")),
                    ],
                    "relative",
                )
            )
            + "\n"
        )
        assert out == expected

    def test_multi_taxonomy_requires_selector(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        doc = _profile_doc(
            [("one", _sample_rows_one()), ("two", _sample_rows_two())],
            ("gtdb", "ncbi", "tax"),
        )
        path = tmp_path / "multi.json"
        path.write_text(json.dumps(doc))

        assert taxmatrix.main([str(path)]) == 2
        err = capsys.readouterr().err
        assert "multiple taxonomies" in err
        assert "gtdb, ncbi, tax" in err
        assert "--taxonomy" in err

        # Selecting each taxonomy succeeds and writes the right lineages.
        assert taxmatrix.main(["--taxonomy", "ncbi", str(path)]) == 0
        ncbi_out = capsys.readouterr().out
        assert "s__Clostridioides difficile" in ncbi_out

        assert taxmatrix.main(["--taxonomy", "GTDB", str(path)]) == 0
        gtdb_out = capsys.readouterr().out
        assert "s__Clostridium difficile" in gtdb_out

        assert taxmatrix.main(["--taxonomy", "tax", str(path)]) == 0
        tax_out = capsys.readouterr().out
        assert "vFAM-1|vOTU-1" in tax_out

    def test_tax_source_taxonomy_selector(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        doc = _profile_doc(
            [("one", _sample_rows_one())],
            ("gtdb", "tax:UHGV"),
        )
        path = tmp_path / "src.json"
        path.write_text(json.dumps(doc))

        assert taxmatrix.main(["--taxonomy", "TAX:UHGV", str(path)]) == 0
        out = capsys.readouterr().out
        assert "vFAM-1|vOTU-1" in out

    def test_missing_taxonomy_in_document_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        doc = _profile_doc([("one", _sample_rows_one())], ("gtdb",))
        path = tmp_path / "one.json"
        path.write_text(json.dumps(doc))

        assert taxmatrix.main(["--taxonomy", "ncbi", str(path)]) == 2
        err = capsys.readouterr().err
        assert "no taxonomy 'ncbi'" in err
        assert "gtdb" in err

    def test_multiple_json_documents_different_taxonomies_without_selector_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        a.write_text(json.dumps(_profile_doc([("one", _sample_rows_one())], ("gtdb",))))
        b.write_text(json.dumps(_profile_doc([("two", _sample_rows_two())], ("ncbi",))))

        assert taxmatrix.main([str(a), str(b)]) == 2
        assert "different taxonomies" in capsys.readouterr().err

    def test_multiple_json_documents_same_taxonomy_merge(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        a.write_text(json.dumps(_profile_doc([("one", _sample_rows_one())], ("gtdb",))))
        b.write_text(json.dumps(_profile_doc([("two", _sample_rows_two())], ("gtdb",))))

        assert taxmatrix.main([str(a), str(b)]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == "clade_name\tone\ttwo"

    def test_mixed_json_and_sylphmpa(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        doc = _profile_doc([("one", _sample_rows_one())], ("gtdb", "ncbi"))
        json_file = tmp_path / "run1.json"
        json_file.write_text(json.dumps(doc))

        mpa_file = tmp_path / "two.gtdb.sylphmpa"
        mpa_file.write_text(
            "\n".join(
                taxprof_lines("two", "gtdb", aggregate_rows(_sample_rows_two(), "gtdb"))
            )
            + "\n"
        )

        assert taxmatrix.main(["--taxonomy", "gtdb", str(json_file), str(mpa_file)]) == 0
        out = capsys.readouterr().out
        expected = (
            "\n".join(
                clade_matrix_lines(
                    [
                        ("one", aggregate_rows(_sample_rows_one(), "gtdb")),
                        ("two", aggregate_rows(_sample_rows_two(), "gtdb")),
                    ]
                )
            )
            + "\n"
        )
        assert out == expected

    def test_json_payload_fallback_to_sylphmpa_text(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        doc = _profile_doc([("one", _sample_rows_one())], ("gtdb",))
        del doc["taxprof"]["one"]["gtdb"]["clades"]
        path = tmp_path / "mpa-only.json"
        path.write_text(json.dumps(doc))

        assert taxmatrix.main([str(path)]) == 0
        out = capsys.readouterr().out
        assert out.splitlines()[0] == "clade_name\tone"
        assert "d__Bacteria\t100.0" in out

    def test_preserves_samples_order_even_when_json_keys_sorted(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        doc = _profile_doc(
            [("zebra", _sample_rows_one()), ("alpha", _sample_rows_two())], ("gtdb",)
        )
        path = tmp_path / "ordered.json"
        path.write_text(json.dumps(doc, sort_keys=True))

        assert taxmatrix.main([str(path)]) == 0
        assert capsys.readouterr().out.splitlines()[0] == "clade_name\tzebra\talpha"

    def test_empty_sample_in_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        doc = _profile_doc([("one", _sample_rows_one()), ("empty", [])], ("gtdb",))
        path = tmp_path / "with-empty.json"
        path.write_text(json.dumps(doc))

        assert taxmatrix.main([str(path)]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == "clade_name\tone\tempty"
        assert "d__Bacteria\t100.0\t0.0" in lines


# ---------------------------------------------------------------------------
# CLI behavior, errors (exit 2), and light install
# ---------------------------------------------------------------------------


class TestCommand:
    def test_output_file_and_dash_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mpa = tmp_path / "one.gtdb.sylphmpa"
        mpa.write_text(
            "\n".join(
                taxprof_lines("one", "gtdb", aggregate_rows(_sample_rows_one(), "gtdb"))
            )
            + "\n"
        )
        out_file = tmp_path / "out.tsv"
        assert taxmatrix.main(["-o", str(out_file), str(mpa)]) == 0
        assert capsys.readouterr().out == ""
        assert taxmatrix.main(["-o", "-", str(mpa)]) == 0
        assert capsys.readouterr().out == out_file.read_text()

    def test_cli_main_dispatch(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mpa = tmp_path / "one.gtdb.sylphmpa"
        mpa.write_text(
            "\n".join(
                taxprof_lines("one", "gtdb", aggregate_rows(_sample_rows_one(), "gtdb"))
            )
            + "\n"
        )
        assert entry.main(["taxmatrix", str(mpa)]) == 0
        assert capsys.readouterr().out.splitlines()[0] == "clade_name\tone"

    def test_errors_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mpa = tmp_path / "one.gtdb.sylphmpa"
        mpa.write_text(
            "\n".join(
                taxprof_lines("one", "gtdb", aggregate_rows(_sample_rows_one(), "gtdb"))
            )
            + "\n"
        )
        junk = tmp_path / "junk.txt"
        junk.write_text("hello world\n")
        bad_json = tmp_path / "bad.json"
        bad_json.write_text("{not valid json")
        no_taxprof = tmp_path / "no-taxprof.json"
        no_taxprof.write_text(json.dumps({"api_version": 1, "samples": [{"name": "a"}]}))
        wrong_ver = tmp_path / "v99.json"
        wrong_ver.write_text(json.dumps({"api_version": 99, "taxprof": {"a": {"gtdb": {}}}}))

        unversioned = tmp_path / "unversioned.json"
        unversioned.write_text(json.dumps({"taxprof": {"a": {"gtdb": {}}}, "samples": [{"name": "a"}]}))

        # No files / bad flag
        assert taxmatrix.main([]) == 2
        assert taxmatrix.main(["--value", "bogus", str(mpa)]) == 2
        # Missing / unreadable / directory
        assert taxmatrix.main([str(tmp_path / "missing.sylphmpa")]) == 2
        assert taxmatrix.main([str(tmp_path)]) == 2
        # Unrecognized / malformed
        assert taxmatrix.main([str(junk)]) == 2
        assert taxmatrix.main([str(bad_json)]) == 2
        assert taxmatrix.main([str(no_taxprof)]) == 2
        assert taxmatrix.main([str(wrong_ver)]) == 2
        assert taxmatrix.main([str(unversioned)]) == 2
        assert "no api_version" in capsys.readouterr().err.splitlines()[-1]
        # Duplicate sample name
        assert taxmatrix.main([str(mpa), str(mpa)]) == 2
        # --taxonomy with only .sylphmpa files
        assert taxmatrix.main(["--taxonomy", "gtdb", str(mpa)]) == 2
        # Unwritable output path
        assert taxmatrix.main(["-o", str(tmp_path / "nope" / "out.tsv"), str(mpa)]) == 2
        capsys.readouterr()

    def test_runs_in_the_light_install(self, tmp_path: Path) -> None:
        """No pysylph, httpx, starlette ...: ``sylphd taxmatrix`` needs only stdlib."""
        mpa1 = tmp_path / "one.gtdb.sylphmpa"
        mpa2 = tmp_path / "two.gtdb.sylphmpa"
        mpa1.write_text(
            "\n".join(
                taxprof_lines("one", "gtdb", aggregate_rows(_sample_rows_one(), "gtdb"))
            )
            + "\n"
        )
        mpa2.write_text(
            "\n".join(
                taxprof_lines("two", "gtdb", aggregate_rows(_sample_rows_two(), "gtdb"))
            )
            + "\n"
        )
        blocked = "{'pysylph', 'dnaio', 'starlette', 'uvicorn', 'httpx', 'multipart'}"
        code = (
            "import sys\n"
            "class Block:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            f"        if name.split('.')[0] in {blocked}:\n"
            "            raise ImportError('blocked ' + name, name=name)\n"
            "sys.meta_path.insert(0, Block())\n"
            f"sys.path.insert(0, {str(SRC)!r})\n"
            "from sylphd.entry import main\n"
            f"sys.exit(main(['taxmatrix', {str(mpa1)!r}, {str(mpa2)!r}]))\n"
        )
        done = subprocess.run(
            [sys.executable, "-I", "-c", code], check=False, capture_output=True, text=True
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout.splitlines()[0] == "clade_name\tone\ttwo"
