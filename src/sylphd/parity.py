"""``sylphd diff``: are two sylph (or sylph-tax) outputs equivalent?

sylphd works at full precision, while sylph prints rounded values and
sylph-tax sums those rounded values.  So the same run gives different digits
and, for rows that tie, a different order.  This module compares two files
the way that matters:

* the same genomes (or clades);
* every number equal within the rounding of what was printed;
* each file sorted the way sylph / sylph-tax sorts it, so rows may swap places
  between the files only where their values tie.

Only the standard library is used, so the command works in the light install.

Two kinds of file are recognised from their content:

``tsv``
    ``sylph profile`` / ``sylph query`` output (first column ``Sample_file``),
    sylph's or sylphd's.  Columns that sylph does not write (sylphd's
    ``accession`` and metadata columns) are ignored.
``sylphmpa``
    ``sylph-tax taxprof`` output (first line ``#SampleID``), sylph-tax's or
    sylphd's.

Sample names are normalised before they are matched (:func:`normalise_sample`).

Tolerances (all scaled by ``tolerance``, plus a float-noise epsilon of 1e-9):

* TSV, per numeric column: half a unit of the printed precision, i.e. 0.00005
  for the abundances (4 decimals), 0.005 for the ANI columns (2), 0.0005 for
  coverages (3), 0.5 for ``Median_cov`` (0); ``kmers_reassigned`` is exact.
  Text cells (intervals, ``Containment_ind``, ``Contig_name``, ``HIGH``/``LOW``)
  must be identical, and ``inf`` / ``NaN`` must match.
* sylphmpa: relative and sequence abundance of a clade within ``n`` times
  0.00005, ``n`` being the number of strain rows at or below that clade (each
  strain's value was rounded once before sylph-tax summed it); ANI within
  0.005 and coverage within 0.0005; ``NA`` cells identical.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .entry import PYTHON_COMMANDS

#: Absolute slack for float noise, added to every tolerance.
EPSILON = 1e-9

#: Decimals sylph prints in each numeric TSV column (``protocol.format_value``).
TSV_PRECISION: dict[str, int] = {
    "Taxonomic_abundance": 4,
    "Sequence_abundance": 4,
    "Adjusted_ANI": 2,
    "Naive_ANI": 2,
    "Eff_cov": 3,
    "True_cov": 3,
    "Mean_cov_geq1": 3,
    "Eff_lambda": 3,
    "Median_cov": 0,
}

#: The columns sylph writes, in no particular order.  Others are ignored.
TSV_COLUMNS = frozenset(
    {
        "Sample_file",
        "Genome_file",
        "Contig_name",
        "ANI_5-95_percentile",
        "Lambda_5-95_percentile",
        "Containment_ind",
        "kmers_reassigned",
        *TSV_PRECISION,
    }
)

#: Half a unit of an abundance printed with 4 decimals.
ABUNDANCE_HALF_UNIT = 0.00005
ANI_HALF_UNIT = 0.005
COVERAGE_HALF_UNIT = 0.0005

#: Suffixes dropped, repeatedly, from a sample name.
SAMPLE_SUFFIXES = (
    ".gz",
    ".sylsp",
    ".paired",
    ".fastq",
    ".fq",
    ".fasta",
    ".fna",
    ".fa",
)

KINDS = ("tsv", "sylphmpa")


class ParityError(ValueError):
    """The input cannot be compared (unreadable, unknown format, mismatched kinds)."""


def normalise_sample(name: str) -> str:
    """The name two tools agree on: basename, without sketch / read-file suffixes.

    sylph names a sample after its sketch (``-S`` name, or the read file's
    path), sylphd after the upload (``soil.sylsp``, ``bioreactor.paired.sylsp``).
    The rule: take the part after the last ``/``, then strip ``.gz``,
    ``.sylsp``, ``.paired``, ``.fastq``, ``.fq``, ``.fasta``, ``.fna`` and
    ``.fa`` while one of them ends the name.
    """
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    stripped = True
    while stripped:
        stripped = False
        for suffix in SAMPLE_SUFFIXES:
            if name.endswith(suffix) and len(name) > len(suffix):
                name = name[: -len(suffix)]
                stripped = True
    return name


@dataclass(frozen=True)
class Difference:
    """One difference between the two files."""

    subject: str  # genome, clade or "header" / "order"
    column: str
    a: str
    b: str
    tolerance: float | None
    message: str

    def format(self) -> str:
        tol = "" if self.tolerance is None else f" (tolerance {_number(self.tolerance)})"
        return f"{self.subject}: {self.message} [{self.column}: {self.a} vs {self.b}{tol}]"

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "column": self.column,
            "a": self.a,
            "b": self.b,
            "tolerance": self.tolerance,
            "message": self.message,
        }


@dataclass
class SampleResult:
    """The comparison of one sample."""

    sample: str
    kind: str
    compared: int = 0  # genomes (tsv) or clades (sylphmpa)
    differences: list[Difference] = field(default_factory=list)
    #: Differences found, including those past ``max_differences``.
    total: int = 0

    @property
    def equivalent(self) -> bool:
        return self.total == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample": self.sample,
            "kind": self.kind,
            "equivalent": self.equivalent,
            "compared": self.compared,
            "differences_total": self.total,
            "differences": [item.to_dict() for item in self.differences],
        }


@dataclass
class Report:
    samples: list[SampleResult]

    @property
    def equivalent(self) -> bool:
        return all(sample.equivalent for sample in self.samples)

    def to_dict(self) -> dict[str, Any]:
        return {
            "equivalent": self.equivalent,
            "samples": [sample.to_dict() for sample in self.samples],
        }

    def format(self) -> str:
        lines = []
        for sample in self.samples:
            unit = "genomes" if sample.kind == "tsv" else "clades"
            if sample.equivalent:
                lines.append(f"{sample.sample}: equivalent ({sample.compared} {unit})")
                continue
            lines.append(f"{sample.sample}: NOT equivalent, {sample.total} difference(s)")
            lines.extend(f"  {item.format()}" for item in sample.differences)
            if sample.total > len(sample.differences):
                lines.append(f"  … and {sample.total - len(sample.differences)} more")
        return "\n".join(lines)


class _Collector:
    """Counts differences, keeping the first ``limit``."""

    def __init__(self, result: SampleResult, limit: int) -> None:
        self.result = result
        self.limit = limit

    def add(self, difference: Difference) -> None:
        self.result.total += 1
        if len(self.result.differences) < self.limit:
            self.result.differences.append(difference)


def _number(value: float) -> str:
    return f"{value:.10g}"


# ---------------------------------------------------------------------------
# detection and public entry points
# ---------------------------------------------------------------------------


def detect_kind(text: str) -> str:
    """``"tsv"`` or ``"sylphmpa"``, from the first line of ``text``."""
    first = text.split("\n", 1)[0]
    if first.startswith("#SampleID"):
        return "sylphmpa"
    if first.split("\t", 1)[0] == "Sample_file":
        return "tsv"
    raise ParityError(
        "not a sylph profile/query TSV (first column 'Sample_file') nor a "
        "sylph-tax .sylphmpa (first line '#SampleID')"
    )


def compare_texts(
    a: str,
    b: str,
    *,
    tolerance: float = 1.0,
    max_differences: int = 10,
) -> Report:
    """Compare two documents of the same kind (one file each)."""
    kind_a, kind_b = detect_kind(a), detect_kind(b)
    if kind_a != kind_b:
        raise ParityError(f"A is a {kind_a} file, B is a {kind_b} file")
    if kind_a == "tsv":
        return _compare_tsv(a, b, tolerance, max_differences)
    return _compare_sylphmpa([a], [b], tolerance, max_differences)


def compare_files(
    a: str | os.PathLike[str],
    b: str | os.PathLike[str],
    *,
    tolerance: float = 1.0,
    max_differences: int = 10,
) -> Report:
    """Compare two files, or two directories of ``.sylphmpa`` files.

    Directories are paired by the (normalised) sample name in the
    ``#SampleID`` line of each file.
    """
    path_a, path_b = Path(a), Path(b)
    if path_a.is_dir() or path_b.is_dir():
        if not (path_a.is_dir() and path_b.is_dir()):
            raise ParityError("A and B must both be files or both be directories")
        texts_a, texts_b = _read_directory(path_a), _read_directory(path_b)
        return _compare_sylphmpa(texts_a, texts_b, tolerance, max_differences)
    return compare_texts(
        _read(path_a), _read(path_b), tolerance=tolerance, max_differences=max_differences
    )


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ParityError(f"cannot read {path}: {error}") from error


def _read_directory(path: Path) -> list[str]:
    files = sorted(path.glob("*.sylphmpa"))
    if not files:
        raise ParityError(f"no .sylphmpa files in {path}")
    return [_read(file) for file in files]


# ---------------------------------------------------------------------------
# numbers
# ---------------------------------------------------------------------------


def _parse(cell: str) -> float | None:
    try:
        return float(cell)
    except ValueError:
        return None


def _same_number(a: str, b: str, tolerance: float) -> bool:
    """Equal strings, or equal numbers within ``tolerance``; nan/inf must match."""
    if a == b:
        return True
    x, y = _parse(a), _parse(b)
    if x is None or y is None:
        return False
    if math.isnan(x) or math.isnan(y):
        return math.isnan(x) and math.isnan(y)
    if math.isinf(x) or math.isinf(y):
        return x == y
    return abs(x - y) <= tolerance + EPSILON


# ---------------------------------------------------------------------------
# TSV
# ---------------------------------------------------------------------------


@dataclass
class _Table:
    columns: list[str]  # sylph's columns present, in file order
    samples: dict[str, list[dict[str, str]]]  # normalised name -> rows
    raw_names: dict[str, str]


def _parse_tsv(text: str, label: str) -> _Table:
    lines = [line for line in text.split("\n") if line != ""]
    if not lines:
        raise ParityError(f"{label} is empty")
    header = lines[0].split("\t")
    if "Genome_file" not in header:
        raise ParityError(f"{label}: no Genome_file column")
    wanted = [index for index, name in enumerate(header) if name in TSV_COLUMNS]
    columns = [header[index] for index in wanted]
    samples: dict[str, list[dict[str, str]]] = {}
    raw_names: dict[str, str] = {}
    for number, line in enumerate(lines[1:], start=2):
        cells = line.split("\t")
        if len(cells) < len(wanted) or max(wanted) >= len(cells):
            raise ParityError(f"{label} line {number}: too few columns")
        row = {header[index]: cells[index] for index in wanted}
        raw = row["Sample_file"]
        name = normalise_sample(raw)
        if raw_names.setdefault(name, raw) != raw:
            raise ParityError(
                f"{label}: sample names {raw_names[name]!r} and {raw!r} both normalise to {name!r}"
            )
        samples.setdefault(name, []).append(row)
    return _Table(columns, samples, raw_names)


def _keys(rows: list[dict[str, str]], repeated: set[str]) -> list[str]:
    """One unique key per row: the genome file, with the contig for a repeated file."""
    seen: dict[str, int] = {}
    keys = []
    for row in rows:
        key = row["Genome_file"]
        if key in repeated:
            key = f"{key} [{row.get('Contig_name', '')}]"
        count = seen.get(key, 0)
        seen[key] = count + 1
        keys.append(key if count == 0 else f"{key} #{count + 1}")
    return keys


def _compare_tsv(a: str, b: str, tolerance: float, limit: int) -> Report:
    table_a, table_b = _parse_tsv(a, "A"), _parse_tsv(b, "B")
    names_a, names_b = list(table_a.samples), list(table_b.samples)
    if len(names_a) == 1 and len(names_b) == 1:
        pairs = [(names_a[0], names_b[0], names_a[0])]  # one sample each: pair them
    else:
        pairs = [(n, n, n) for n in names_a if n in table_b.samples]
    results: list[SampleResult] = []

    for name_a, name_b, label in pairs:
        result = SampleResult(label, "tsv")
        collector = _Collector(result, limit)
        _compare_tsv_sample(
            table_a, table_b, table_a.samples[name_a], table_b.samples[name_b], tolerance, collector
        )
        results.append(result)
    paired_a = {pair[0] for pair in pairs}
    paired_b = {pair[1] for pair in pairs}
    for name in names_a:
        if name not in paired_a:
            results.append(_missing_sample(name, "B"))
    for name in names_b:
        if name not in paired_b:
            results.append(_missing_sample(name, "A"))
    return Report(results)


def _missing_sample(name: str, where: str) -> SampleResult:
    result = SampleResult(name, "tsv")
    _Collector(result, 1).add(
        Difference(name, "Sample_file", "", "", None, f"sample is missing in {where}")
    )
    return result


def _compare_tsv_sample(
    table_a: _Table,
    table_b: _Table,
    rows_a: list[dict[str, str]],
    rows_b: list[dict[str, str]],
    tolerance: float,
    out: _Collector,
) -> None:
    if table_a.columns != table_b.columns:
        out.add(
            Difference(
                "header",
                "columns",
                ",".join(table_a.columns),
                ",".join(table_b.columns),
                None,
                "the sylph columns differ",
            )
        )
        return
    columns = table_a.columns
    repeated: set[str] = set()
    for rows in (rows_a, rows_b):
        files = [r["Genome_file"] for r in rows]
        repeated |= _dups(files)
    keys_a, keys_b = _keys(rows_a, repeated), _keys(rows_b, repeated)
    out.result.compared = len(rows_a)
    by_key_b = dict(zip(keys_b, rows_b))
    set_a = set(keys_a)
    for key in keys_a:
        if key not in by_key_b:
            out.add(Difference(key, "Genome_file", key, "", None, "genome only in A"))
    for key in keys_b:
        if key not in set_a:
            out.add(Difference(key, "Genome_file", "", key, None, "genome only in B"))
    for key, row_a in zip(keys_a, rows_a):
        row_b = by_key_b.get(key)
        if row_b is None:
            continue
        for column in columns:
            if column in ("Sample_file", "Genome_file"):
                continue
            cell_a, cell_b = row_a[column], row_b[column]
            if column in TSV_PRECISION:
                tol = ABUNDANCE_HALF_UNIT_FOR[column] * tolerance
                if _same_number(cell_a, cell_b, tol):
                    continue
                out.add(
                    Difference(key, column, cell_a, cell_b, tol + EPSILON, "values differ")
                )
            elif column == "kmers_reassigned":
                if not _same_number(cell_a, cell_b, 0.0):
                    out.add(Difference(key, column, cell_a, cell_b, 0.0, "values differ"))
            elif cell_a != cell_b:
                out.add(Difference(key, column, cell_a, cell_b, None, "text differs"))
    sort_column = (
        "Taxonomic_abundance" if "Taxonomic_abundance" in columns else "Adjusted_ANI"
    )
    if sort_column in columns:
        for side, rows, keys in (("A", rows_a, keys_a), ("B", rows_b, keys_b)):
            _check_sorted(rows, keys, sort_column, side, tolerance, out)


def _dups(files: list[str]) -> set[str]:
    seen: set[str] = set()
    out: set[str] = set()
    for item in files:
        (out if item in seen else seen).add(item)
    return out


#: Half a unit of each numeric column's printed precision.
ABUNDANCE_HALF_UNIT_FOR = {
    column: 0.5 * 10.0 ** (-digits) for column, digits in TSV_PRECISION.items()
}


def _check_sorted(
    rows: list[dict[str, str]],
    keys: list[str],
    column: str,
    side: str,
    tolerance: float,
    out: _Collector,
) -> None:
    """The file is sorted by ``column``, descending (sylph's sort)."""
    slack = ABUNDANCE_HALF_UNIT_FOR[column] * tolerance + EPSILON
    previous: float | None = None
    previous_key = ""
    for key, row in zip(keys, rows):
        value = _parse(row[column])
        if value is None or math.isnan(value):
            continue
        if previous is not None and value > previous + slack:
            out.add(
                Difference(
                    key,
                    column,
                    _number(previous),
                    _number(value),
                    slack,
                    f"{side} is not sorted by {column} descending (after {previous_key})",
                )
            )
        previous, previous_key = value, key


# ---------------------------------------------------------------------------
# sylphmpa
# ---------------------------------------------------------------------------


@dataclass
class _Clade:
    name: str
    relative: str
    sequence: str
    ani: str
    coverage: str


@dataclass
class _Mpa:
    header: str
    sample: str
    taxonomies: str
    columns: str
    clades: list[_Clade]


def _parse_sylphmpa(text: str, label: str) -> _Mpa:
    lines = [line for line in text.split("\n") if line != ""]
    if len(lines) < 2 or not lines[0].startswith("#SampleID"):
        raise ParityError(f"{label}: not a .sylphmpa file")
    first = lines[0].split("\t")
    sample = first[1] if len(first) > 1 else ""
    taxonomies = "\t".join(first[2:])
    clades = []
    for number, line in enumerate(lines[2:], start=3):
        cells = line.split("\t")
        if len(cells) < 5:
            raise ParityError(f"{label} line {number}: expected 5 columns, got {len(cells)}")
        clades.append(_Clade(*cells[:5]))
    return _Mpa(lines[0], sample, taxonomies, lines[1], clades)


def _strain_counts(clades: list[_Clade]) -> dict[str, int]:
    """For each clade, the number of strain rows (an ANI value) at or below it."""
    counts: dict[str, int] = {}
    for clade in clades:
        if clade.ani == "NA":
            continue
        path = ""
        for token in clade.name.split("|"):
            path = token if not path else f"{path}|{token}"
            counts[path] = counts.get(path, 0) + 1
    return counts


def _compare_sylphmpa(
    texts_a: Sequence[str], texts_b: Sequence[str], tolerance: float, limit: int
) -> Report:
    files_a = [_parse_sylphmpa(text, "A") for text in texts_a]
    files_b = [_parse_sylphmpa(text, "B") for text in texts_b]
    by_a = _by_sample(files_a, "A")
    by_b = _by_sample(files_b, "B")
    if len(by_a) == 1 and len(by_b) == 1:
        pairs = [(next(iter(by_a.values())), next(iter(by_b.values())))]
    else:
        pairs = [(by_a[n], by_b[n]) for n in by_a if n in by_b]
    results = [_compare_mpa_sample(pa, pb, tolerance, limit) for pa, pb in pairs]
    for name in by_a:
        if name not in by_b and not (len(by_a) == 1 and len(by_b) == 1):
            results.append(_missing_mpa(name, "B"))
    for name in by_b:
        if name not in by_a and not (len(by_a) == 1 and len(by_b) == 1):
            results.append(_missing_mpa(name, "A"))
    return Report(results)


def _by_sample(files: list[_Mpa], label: str) -> dict[str, _Mpa]:
    out: dict[str, _Mpa] = {}
    for file in files:
        name = normalise_sample(file.sample)
        if name in out:
            raise ParityError(f"{label}: two files for sample {name!r}")
        out[name] = file
    return out


def _missing_mpa(name: str, where: str) -> SampleResult:
    result = SampleResult(name, "sylphmpa")
    _Collector(result, 1).add(
        Difference(name, "#SampleID", "", "", None, f"sample is missing in {where}")
    )
    return result


def _compare_mpa_sample(a: _Mpa, b: _Mpa, tolerance: float, limit: int) -> SampleResult:
    result = SampleResult(normalise_sample(a.sample), "sylphmpa", compared=len(a.clades))
    out = _Collector(result, limit)
    if a.taxonomies != b.taxonomies:
        out.add(
            Difference(
                "header", "Taxonomies_used", a.taxonomies, b.taxonomies, None,
                "the #SampleID line differs",
            )
        )
    if normalise_sample(a.sample) != normalise_sample(b.sample):  # single-sample pairing
        out.add(Difference("header", "SampleID", a.sample, b.sample, None, "sample names differ"))
    if a.columns != b.columns:
        out.add(
            Difference("header", "columns", a.columns, b.columns, None, "the column header differs")
        )

    by_b = {clade.name: clade for clade in b.clades}
    names_a = {clade.name for clade in a.clades}
    for clade in a.clades:
        if clade.name not in by_b:
            out.add(Difference(clade.name, "clade_name", clade.name, "", None, "clade only in A"))
    for clade in b.clades:
        if clade.name not in names_a:
            out.add(Difference(clade.name, "clade_name", "", clade.name, None, "clade only in B"))

    strains_a, strains_b = _strain_counts(a.clades), _strain_counts(b.clades)
    for clade in a.clades:
        other = by_b.get(clade.name)
        if other is None:
            continue
        n = max(strains_a.get(clade.name, 0), strains_b.get(clade.name, 0), 1)
        tol_abundance = n * ABUNDANCE_HALF_UNIT * tolerance
        for column, x, y, tol in (
            ("relative_abundance", clade.relative, other.relative, tol_abundance),
            ("sequence_abundance", clade.sequence, other.sequence, tol_abundance),
            ("ANI", clade.ani, other.ani, ANI_HALF_UNIT * tolerance),
            ("Coverage", clade.coverage, other.coverage, COVERAGE_HALF_UNIT * tolerance),
        ):
            if x == "NA" or y == "NA":
                if x != y:
                    out.add(Difference(clade.name, column, x, y, None, "NA in one file only"))
            elif not _same_number(x, y, tol):
                out.add(Difference(clade.name, column, x, y, tol + EPSILON, "values differ"))

    for side, mpa in (("A", a), ("B", b)):
        _check_mpa_sorted(mpa.clades, side, tolerance, out)
    return result


def _check_mpa_sorted(clades: list[_Clade], side: str, tolerance: float, out: _Collector) -> None:
    """Levels ascend; within a level the relative abundance descends."""
    previous_depth = 0
    previous: float | None = None
    previous_name = ""
    for clade in clades:
        depth = clade.name.count("|") + 1
        value = _parse(clade.relative)
        if depth < previous_depth:
            out.add(
                Difference(
                    clade.name, "clade_name", previous_name, clade.name, None,
                    f"{side} is not ordered by level (a shallower clade follows a deeper one)",
                )
            )
        if depth != previous_depth:
            previous = None
        if value is not None and not math.isnan(value):
            if previous is not None and value > previous + EPSILON:
                out.add(
                    Difference(
                        clade.name, "relative_abundance", _number(previous), _number(value), EPSILON,
                        f"{side} is not sorted by relative abundance descending within its level "
                        f"(after {previous_name})",
                    )
                )
            previous = value
        previous_depth, previous_name = depth, clade.name


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _positive_float(text: str) -> float:
    value = float(text)
    if not value > 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("a", metavar="A", help="sylph / sylph-tax output (file, or directory of .sylphmpa)")
    parser.add_argument("b", metavar="B", help="sylphd output of the same kind")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument(
        "--tolerance",
        type=_positive_float,
        default=1.0,
        metavar="FACTOR",
        help="scale every tolerance by FACTOR (default 1)",
    )
    parser.add_argument(
        "--max-differences",
        type=_positive_int,
        default=10,
        metavar="N",
        help="differences to show per sample (default 10)",
    )


def run(args: argparse.Namespace) -> int:
    """Run the command: 0 equivalent, 1 not equivalent, 2 usage or format error."""
    try:
        report = compare_files(
            args.a, args.b, tolerance=args.tolerance, max_differences=args.max_differences
        )
    except ParityError as error:
        print(f"sylphd diff: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.format())
    return 0 if report.equivalent else 1


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sylphd diff",
        description=PYTHON_COMMANDS["diff"],
    )
    add_arguments(parser)
    try:
        args = parser.parse_args(None if argv is None else list(argv))
    except SystemExit as error:
        return int(error.code or 0)
    return run(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
