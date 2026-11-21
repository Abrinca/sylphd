"""``sylphd diff``: equivalence of sylph / sylph-tax output and sylphd's.

Synthetic files test the rules one by one; ``tests/data/parity`` holds real
GlobDB samples (soil and a paired-end bioreactor): stock sylph 1.0.0's TSV and
stock sylph-tax 1.9.2's ``.sylphmpa`` against sylphd's, for the same sketch.
No binary is needed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from sylphd import parity
from sylphd.parity import compare_files, compare_texts, normalise_sample
from sylphd.protocol import format_value

DATA = Path(__file__).parent / "data" / "parity"
SRC = Path(__file__).parent.parent / "src"

PROFILE_COLUMNS = (
    "Sample_file Genome_file Taxonomic_abundance Sequence_abundance Adjusted_ANI "
    "True_cov ANI_5-95_percentile Eff_lambda Lambda_5-95_percentile Median_cov "
    "Mean_cov_geq1 Containment_ind Naive_ANI kmers_reassigned Contig_name"
).split()


def row(sample="s", genome="g1", abundance="1.0000", ani="99.00", **cells: str) -> dict[str, str]:
    values = {
        "Sample_file": sample,
        "Genome_file": genome,
        "Taxonomic_abundance": abundance,
        "Sequence_abundance": abundance,
        "Adjusted_ANI": ani,
        "True_cov": "5.000",
        "ANI_5-95_percentile": "NA-NA",
        "Eff_lambda": "HIGH",
        "Lambda_5-95_percentile": "NA-NA",
        "Median_cov": "5",
        "Mean_cov_geq1": "5.000",
        "Containment_ind": "10/20",
        "Naive_ANI": ani,
        "kmers_reassigned": "0",
        "Contig_name": f"{genome}-c",
    }
    values.update(cells)
    return values


def tsv(rows: list[dict[str, str]], extra: tuple[str, ...] = ()) -> str:
    columns = list(PROFILE_COLUMNS)
    lines = ["\t".join([*columns, *extra])]
    for item in rows:
        lines.append("\t".join([*(item[c] for c in columns), *(item.get(c, "") for c in extra)]))
    return "\n".join(lines) + "\n"


def three(**changes: str) -> list[dict[str, str]]:
    rows = [
        row(genome="g1", abundance="3.0000"),
        row(genome="g2", abundance="2.0000"),
        row(genome="g3", abundance="1.0000"),
    ]
    for key, value in changes.items():
        rows[0][key] = value
    return rows


def failures(report: parity.Report) -> list[str]:
    return [item.column for sample in report.samples for item in sample.differences]


# ---------------------------------------------------------------------------
# TSV
# ---------------------------------------------------------------------------


class TestTsv:
    def test_equal(self) -> None:
        report = compare_texts(tsv(three()), tsv(three()))
        assert report.equivalent and report.samples[0].compared == 3

    def test_sylphd_extra_columns_are_ignored(self) -> None:
        rows = three()
        for item in rows:
            item["accession"], item["gtdb_taxonomy"] = "GCF_1", "d__X"
        extra = ("accession", "gtdb_taxonomy")
        assert compare_texts(tsv(three()), tsv(rows, extra=extra)).equivalent

    @pytest.mark.parametrize(
        ("column", "a", "inside", "outside"),
        [
            ("Taxonomic_abundance", "3.0000", "3.00005", "3.00006"),
            ("Adjusted_ANI", "99.00", "99.005", "99.006"),
            ("True_cov", "5.000", "5.0005", "5.0006"),
            ("Median_cov", "5", "5.5", "5.6"),
        ],
    )
    def test_numeric_tolerance(self, column: str, a: str, inside: str, outside: str) -> None:
        assert compare_texts(tsv(three(**{column: a})), tsv(three(**{column: inside}))).equivalent
        report = compare_texts(tsv(three(**{column: a})), tsv(three(**{column: outside})))
        assert not report.equivalent and failures(report) == [column]

    def test_tolerance_factor_scales(self) -> None:
        a, b = tsv(three()), tsv(three(Taxonomic_abundance="3.0001"))
        assert not compare_texts(a, b).equivalent
        assert compare_texts(a, b, tolerance=2.5).equivalent

    def test_text_columns_must_match(self) -> None:
        for column, value in (
            ("Eff_lambda", "LOW"),
            ("Containment_ind", "11/20"),
            ("ANI_5-95_percentile", "98.00-99.00"),
            ("kmers_reassigned", "1"),
        ):
            report = compare_texts(tsv(three()), tsv(three(**{column: value})))
            assert failures(report) == [column], column

    def test_ties_swapped_pass(self) -> None:
        a = [
            row(genome="g1", abundance="2.0000"),
            row(genome="g2", abundance="2.0000"),
            row(genome="g3", abundance="1.0000"),
        ]
        assert compare_texts(tsv(a), tsv([a[1], a[0], a[2]])).equivalent

    def test_ties_within_tolerance_swapped_pass(self) -> None:
        a = [
            row(genome="g1", abundance="2.0000"),
            row(genome="g2", abundance="1.99996"),
            row(genome="g3", abundance="1.0000"),
        ]
        b = [row(genome="g2", abundance="2.0000"), row(genome="g1", abundance="1.99996"), a[2]]
        assert compare_texts(tsv(a), tsv(b)).equivalent

    def test_real_order_difference_fails(self) -> None:
        a = three()
        report = compare_texts(tsv(a), tsv([a[2], a[1], a[0]]))
        assert not report.equivalent
        assert any("not sorted" in d.message for d in report.samples[0].differences)

    def test_query_is_sorted_by_ani(self) -> None:
        columns = "Sample_file Genome_file Naive_ANI Adjusted_ANI Eff_cov Contig_name".split()

        def query(rows: list[tuple[str, str]]) -> str:
            lines = ["\t".join(columns)]
            lines += [f"s\t{g}\t{a}\t{a}\t1.000\t{g}-c" for g, a in rows]
            return "\n".join(lines) + "\n"

        good = query([("g1", "99.00"), ("g2", "98.00")])
        assert compare_texts(good, good).equivalent
        assert not compare_texts(good, query([("g2", "98.00"), ("g1", "99.00")])).equivalent

    def test_repeated_genome_file_is_told_apart_by_contig(self) -> None:
        rows = [
            row(genome="g1", abundance="3.0000", Contig_name="c1"),
            row(genome="g1", abundance="2.0000", Contig_name="c2"),
        ]
        assert compare_texts(tsv(rows), tsv(rows)).equivalent
        changed = [rows[0], {**rows[1], "Taxonomic_abundance": "1.0000"}]
        report = compare_texts(tsv(rows), tsv(changed))
        assert [d.subject for d in report.samples[0].differences] == ["g1 [c2]"]

    def test_missing_genome(self) -> None:
        report = compare_texts(tsv(three()), tsv(three()[:2]))
        assert [d.message for d in report.samples[0].differences] == ["genome only in A"]
        report = compare_texts(tsv(three()[:2]), tsv(three()))
        assert [d.message for d in report.samples[0].differences] == ["genome only in B"]

    def test_header_mismatch(self) -> None:
        a = tsv(three())
        report = compare_texts(a, a.replace("True_cov", "Eff_cov"))
        assert not report.equivalent and failures(report) == ["columns"]

    def test_nan_and_inf(self) -> None:
        cells = {"Taxonomic_abundance": "NaN", "Sequence_abundance": "NaN", "True_cov": "inf"}
        a = [row(genome="g1", **cells)]
        assert compare_texts(tsv(a), tsv(a)).equivalent
        for column, value in (
            ("True_cov", "5.000"),
            ("Taxonomic_abundance", "0.0000"),
            ("True_cov", "-inf"),
        ):
            b = [row(genome="g1", **{**cells, column: value})]
            assert failures(compare_texts(tsv(a), tsv(b))) == [column]

    def test_multi_sample_and_names(self) -> None:
        a = tsv([row(sample="soil", genome="g1"), row(sample="bio", genome="g1")])
        b = tsv([row(sample="soil.sylsp", genome="g1"), row(sample="bio.paired", genome="g1")])
        report = compare_texts(a, b)
        assert report.equivalent and [s.sample for s in report.samples] == ["soil", "bio"]

    def test_one_sample_each_pairs_whatever_the_names(self) -> None:
        assert compare_texts(tsv([row(sample="a")]), tsv([row(sample="b")])).equivalent

    def test_sample_only_in_one_file(self) -> None:
        a = tsv([row(sample="x"), row(sample="y")])
        b = tsv([row(sample="x"), row(sample="z")])
        report = compare_texts(a, b)
        assert not report.equivalent
        assert {s.sample for s in report.samples if not s.equivalent} == {"y", "z"}

    def test_differences_are_limited(self) -> None:
        a = tsv([row(genome=f"g{i}") for i in range(5)])
        b = tsv([row(genome=f"g{i}", Eff_lambda="LOW") for i in range(5)])
        sample = compare_texts(a, b, max_differences=2).samples[0]
        assert sample.total == 5 and len(sample.differences) == 2


@pytest.mark.parametrize(
    ("raw", "name"),
    [
        ("soil", "soil"),
        ("soil.sylsp", "soil"),
        ("bioreactor.paired.sylsp", "bioreactor"),
        ("/data/reads/ERR1.fastq.gz", "ERR1"),
        ("dir/sample.fq", "sample"),
        ("x.fna.gz", "x"),
        (".gz", ".gz"),
    ],
)
def test_normalise_sample(raw: str, name: str) -> None:
    assert normalise_sample(raw) == name


def test_precision_table_follows_format_value() -> None:
    for column, digits in parity.TSV_PRECISION.items():
        if column == "Eff_lambda":
            continue
        printed = format_value(column, 1.23456789)
        decimals = len(printed.split(".")[1]) if "." in printed else 0
        assert decimals == digits, column


# ---------------------------------------------------------------------------
# sylphmpa
# ---------------------------------------------------------------------------

MPA_HEADER = (
    "clade_name\trelative_abundance\tsequence_abundance\t"
    "ANI (if strain-level)\tCoverage (if strain-level)"
)

Clade = tuple[str, str, str, str, str]


def mpa(clades: list[Clade], sample: str = "s", used: str = "['X']") -> str:
    lines = [f"#SampleID\t{sample}\tTaxonomies_used:{used}", MPA_HEADER]
    lines += ["\t".join(c) for c in clades]
    return "\n".join(lines) + "\n"


def tree(root: str = "10.0", s1: str = "6.0", s2: str = "4.0") -> list[Clade]:
    na = ("NA", "NA")
    return [
        ("d__A", root, root, *na),
        ("d__A|p__B", s1, s1, *na),
        ("d__A|p__C", s2, s2, *na),
        ("d__A|p__B|t__x", s1, s1, "99.0", "2.0"),
        ("d__A|p__C|t__y", s2, s2, "98.0", "1.0"),
    ]


class TestSylphmpa:
    def test_equal(self) -> None:
        report = compare_texts(mpa(tree()), mpa(tree()))
        assert report.equivalent and report.samples[0].compared == 5

    def test_sum_tolerance_scales_with_strain_rows(self) -> None:
        # the root has two strain rows below it: 2 * 0.00005 = 0.0001
        assert compare_texts(mpa(tree()), mpa(tree(root="10.0001"))).equivalent
        assert not compare_texts(mpa(tree()), mpa(tree(root="10.00011"))).equivalent
        # a strain-level clade has one: 0.00005
        assert compare_texts(mpa(tree()), mpa(tree(s1="6.00005"))).equivalent
        assert not compare_texts(mpa(tree()), mpa(tree(s1="6.00006"))).equivalent

    def test_ani_and_coverage_tolerance(self) -> None:
        def with_strain(ani: str, cov: str) -> str:
            clades = tree()
            clades[3] = ("d__A|p__B|t__x", "6.0", "6.0", ani, cov)
            return mpa(clades)

        base = with_strain("99.0", "2.0")
        assert compare_texts(base, with_strain("99.005", "2.0005")).equivalent
        assert failures(compare_texts(base, with_strain("99.006", "2.0"))) == ["ANI"]
        assert failures(compare_texts(base, with_strain("99.0", "2.0006"))) == ["Coverage"]

    def test_na_must_match(self) -> None:
        clades = tree()
        clades[3] = ("d__A|p__B|t__x", "6.0", "6.0", "NA", "NA")
        report = compare_texts(mpa(tree()), mpa(clades))
        assert set(failures(report)) == {"ANI", "Coverage"}

    def test_missing_clade(self) -> None:
        report = compare_texts(mpa(tree()), mpa(tree()[:-1]))
        assert [d.message for d in report.samples[0].differences] == ["clade only in A"]

    def test_header_mismatches(self) -> None:
        other = mpa(tree(), used="['Y']")
        assert failures(compare_texts(mpa(tree()), other)) == ["Taxonomies_used"]
        renamed = mpa(tree()).replace("sequence_abundance", "seq")
        assert failures(compare_texts(mpa(tree()), renamed)) == ["columns"]

    def test_tied_clades_may_swap(self) -> None:
        a = tree(s1="5.0", s2="5.0")
        b = [a[0], a[2], a[1], a[4], a[3]]
        assert compare_texts(mpa(a), mpa(b)).equivalent

    def test_unsorted_file_fails(self) -> None:
        a = tree()
        report = compare_texts(mpa(a), mpa([a[0], a[2], a[1], a[3], a[4]]))
        assert not report.equivalent
        assert all("not sorted" in d.message for d in report.samples[0].differences)
        deeper_first = compare_texts(mpa(a), mpa([a[3], a[0], a[1], a[2], a[4]]))
        assert any("level" in d.message for d in deeper_first.samples[0].differences)

    def test_sample_names_are_normalised(self) -> None:
        assert compare_texts(mpa(tree(), sample="bio"), mpa(tree(), sample="bio.paired")).equivalent

    def test_directories_pair_by_sample(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        for name in ("x", "y"):
            (a / f"{name}.sylphmpa").write_text(mpa(tree(), sample=name))
            (b / f"{name}.tax.X.sylphmpa").write_text(mpa(tree(), sample=f"{name}.sylsp"))
        report = compare_files(a, b)
        assert report.equivalent and [s.sample for s in report.samples] == ["x", "y"]
        (b / "y.tax.X.sylphmpa").write_text(mpa(tree(root="11.0"), sample="y"))
        assert [s.sample for s in compare_files(a, b).samples if not s.equivalent] == ["y"]


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------


class TestCommand:
    def test_exit_codes_and_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        a, b, c = tmp_path / "a.tsv", tmp_path / "b.tsv", tmp_path / "c.tsv"
        a.write_text(tsv(three()))
        b.write_text(tsv(three()))
        c.write_text(tsv(three(Taxonomic_abundance="3.5000")))
        assert parity.main([str(a), str(b)]) == 0
        assert "equivalent" in capsys.readouterr().out
        assert parity.main([str(a), str(c), "--json"]) == 1
        report = json.loads(capsys.readouterr().out)
        assert report["equivalent"] is False
        difference = report["samples"][0]["differences"][0]
        assert (difference["subject"], difference["column"]) == ("g1", "Taxonomic_abundance")
        assert parity.main([str(a), str(c), "--tolerance", "20000"]) == 0

    def test_usage_and_format_errors_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        good, junk = tmp_path / "good.tsv", tmp_path / "junk.txt"
        good.write_text(tsv(three()))
        junk.write_text("hello\n")
        mpa_file = tmp_path / "x.sylphmpa"
        mpa_file.write_text(mpa(tree()))
        assert parity.main([str(good), str(junk)]) == 2
        assert parity.main([str(good), str(mpa_file)]) == 2  # kinds differ
        assert parity.main([str(good), str(tmp_path / "missing")]) == 2
        assert parity.main([str(good)]) == 2  # usage
        capsys.readouterr()

    def test_runs_in_the_light_install(self) -> None:
        """No pysylph, httpx, starlette ...: ``sylphd diff`` needs the standard library."""
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
            f"sys.exit(main(['diff', {str(DATA / 'soil.sylph.tsv')!r}, "
            f"{str(DATA / 'soil.sylphd.tsv')!r}]))\n"
        )
        done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        assert "equivalent" in done.stdout


# ---------------------------------------------------------------------------
# real data (GlobDB r232, stock sylph 1.0.0 / sylph-tax 1.9.2 vs sylphd)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sample", ["soil", "bioreactor"])
class TestRealData:
    def test_tsv(self, sample: str) -> None:
        """Per-genome rows, whatever the order of tied rows."""
        report = compare_files(DATA / f"{sample}.sylph.tsv", DATA / f"{sample}.sylphd.tsv")
        assert report.equivalent, report.format()
        assert report.samples[0].compared > 100

    def test_sylphmpa(self, sample: str) -> None:
        """sylph-tax sums sylph's rounded TSV, sylphd the unrounded values."""
        a, b = DATA / f"{sample}.sylph-tax.sylphmpa", DATA / f"{sample}.sylphd.sylphmpa"
        report = compare_files(a, b)
        assert report.equivalent, report.format()
        assert a.read_text() != b.read_text()  # not byte-identical: that is the point

    def test_corrupted_value_is_caught(self, sample: str) -> None:
        lines = (DATA / f"{sample}.sylphd.sylphmpa").read_text().splitlines()
        cells = lines[3].split("\t")
        cells[1] = str(float(cells[1]) + 0.01)
        lines[3] = "\t".join(cells)
        reference = (DATA / f"{sample}.sylph-tax.sylphmpa").read_text()
        report = compare_texts(reference, "\n".join(lines) + "\n")
        assert not report.equivalent

        tsv_lines = (DATA / f"{sample}.sylphd.tsv").read_text().splitlines()
        cells = tsv_lines[5].split("\t")
        cells[4] = f"{float(cells[4]) - 0.5:.2f}"
        tsv_lines[5] = "\t".join(cells)
        reference = (DATA / f"{sample}.sylph.tsv").read_text()
        assert not compare_texts(reference, "\n".join(tsv_lines) + "\n").equivalent

    def test_dropped_genome_is_caught(self, sample: str) -> None:
        lines = (DATA / f"{sample}.sylphd.tsv").read_text().splitlines()
        del lines[7]
        reference = (DATA / f"{sample}.sylph.tsv").read_text()
        report = compare_texts(reference, "\n".join(lines) + "\n")
        assert [d.message for d in report.samples[0].differences] == ["genome only in A"]


def test_soil_shows_the_rounding_and_tie_differences() -> None:
    """The reason the check is not byte-for-byte."""
    sylph_tax = (DATA / "soil.sylph-tax.sylphmpa").read_text().splitlines()
    sylphd = (DATA / "soil.sylphd.sylphmpa").read_text().splitlines()
    assert sylph_tax[2].split("\t")[:2] == ["d__Bacteria", "99.99930000000002"]
    assert sylphd[2].split("\t")[:2] == ["d__Bacteria", "100.00000000000003"]

    def genomes(name: str) -> list[str]:
        return [line.split("\t")[1] for line in (DATA / name).read_text().splitlines()[1:]]

    a, b = genomes("soil.sylph.tsv"), genomes("soil.sylphd.tsv")
    assert sorted(a) == sorted(b) and a != b  # the same genomes, tied rows swapped
