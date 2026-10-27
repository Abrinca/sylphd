"""Wire protocol details: request parameters, result rows, TSV formatting.

Requests
--------
``POST /profile`` and ``POST /query`` take a ``multipart/form-data`` body:

* one ``sketch`` file part per sample, holding the raw ``SampleSketch`` bytes
  (``SampleSketch.to_bytes()``/``.sylsp`` contents);
* an optional ``params`` form field with a JSON object of profiling thresholds;
* names of the samples are taken from ``params["names"]`` (a JSON list, same
  order as the ``sketch`` parts) and fall back to the part file names.

Responses
---------
A JSON list with one object per sample::

    [{"name": "sample-a", "kind": "profile", "rows": [{...}, ...]}, ...]

Each row starts with the sylph columns (same names as sylph's TSV output),
followed by ``accession`` and the selected metadata columns.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .metadata import METADATA_COLUMNS, accession_from_genome_name

#: Columns of ``sylph query`` output, in sylph's order.
QUERY_COLUMNS: tuple[str, ...] = (
    "Sample_file",
    "Genome_file",
    "Adjusted_ANI",
    "Eff_cov",
    "ANI_5-95_percentile",
    "Eff_lambda",
    "Lambda_5-95_percentile",
    "Median_cov",
    "Mean_cov_geq1",
    "Containment_ind",
    "Naive_ANI",
    "Contig_name",
)

#: Columns of ``sylph profile`` output, in sylph's order.  ``Eff_cov`` becomes
#: ``True_cov`` when ``estimate_unknown`` is set (as in sylph).
PROFILE_COLUMNS: tuple[str, ...] = (
    "Sample_file",
    "Genome_file",
    "Taxonomic_abundance",
    "Sequence_abundance",
    "Adjusted_ANI",
    "Eff_cov",
    "ANI_5-95_percentile",
    "Eff_lambda",
    "Lambda_5-95_percentile",
    "Median_cov",
    "Mean_cov_geq1",
    "Containment_ind",
    "Naive_ANI",
    "kmers_reassigned",
    "Contig_name",
)

#: Columns appended to the sylph ones.
EXTRA_COLUMNS: tuple[str, ...] = ("accession",)

MODES = ("query", "profile")


class ProtocolError(ValueError):
    """Raised for malformed request parameters."""


@dataclass(frozen=True)
class ProfileParams:
    """Thresholds forwarded to :class:`pysylph.Profiler`.

    The defaults match ``sylph``'s command line defaults.
    """

    minimum_ani: float | None = None
    seq_id: float | None = None
    estimate_unknown: bool = False
    min_number_kmers: int = 10
    min_contain: int = 7
    screen_ani: float = 85.0

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "ProfileParams":
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise ProtocolError("'params' must be a JSON object")
        known = {
            "minimum_ani",
            "seq_id",
            "estimate_unknown",
            "min_number_kmers",
            "min_contain",
            "screen_ani",
            "names",
            "fields",
            "mode",
        }
        unknown = sorted(set(data) - known)
        if unknown:
            raise ProtocolError(f"unknown parameter(s): {', '.join(unknown)}")

        return cls(
            minimum_ani=_optional_float(data, "minimum_ani", 0.0, 100.0),
            seq_id=_optional_float(data, "seq_id", 0.0, 100.0),
            estimate_unknown=_optional_bool(data, "estimate_unknown", False),
            min_number_kmers=_integer(data, "min_number_kmers", 10, minimum=0),
            min_contain=_integer(data, "min_contain", 7, minimum=0),
            screen_ani=_number(data, "screen_ani", 85.0, 0.0, 100.0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "minimum_ani": self.minimum_ani,
            "seq_id": self.seq_id,
            "estimate_unknown": self.estimate_unknown,
            "min_number_kmers": self.min_number_kmers,
            "min_contain": self.min_contain,
            "screen_ani": self.screen_ani,
        }

    def profiler_kwargs(self) -> dict[str, Any]:
        return self.to_dict()


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _number(
    data: Mapping[str, Any],
    key: str,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if key not in data or data[key] is None:
        return default
    value = data[key]
    if not _is_number(value) or not math.isfinite(float(value)):
        raise ProtocolError(f"'{key}' must be a finite number")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ProtocolError(f"'{key}' must be >= {minimum:g}")
    if maximum is not None and number > maximum:
        raise ProtocolError(f"'{key}' must be <= {maximum:g}")
    return number


def _optional_float(
    data: Mapping[str, Any],
    key: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if not _is_number(value) or not math.isfinite(float(value)):
        raise ProtocolError(f"'{key}' must be a finite number or null")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ProtocolError(f"'{key}' must be >= {minimum:g}")
    if maximum is not None and number > maximum:
        raise ProtocolError(f"'{key}' must be <= {maximum:g}")
    return number


def _integer(
    data: Mapping[str, Any], key: str, default: int, minimum: int | None = None
) -> int:
    if key not in data or data[key] is None:
        return default
    value = data[key]
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolError(f"'{key}' must be an integer")
    if minimum is not None and value < minimum:
        raise ProtocolError(f"'{key}' must be >= {minimum}")
    return value


def _optional_bool(data: Mapping[str, Any], key: str, default: bool) -> bool:
    if key not in data or data[key] is None:
        return default
    value = data[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    raise ProtocolError(f"'{key}' must be a boolean")


def columns_for_mode(mode: str, params: ProfileParams | None = None) -> tuple[str, ...]:
    """Sylph-like columns of ``mode``, honouring the ``True_cov`` header swap."""
    if mode == "query":
        return QUERY_COLUMNS
    if mode == "profile":
        columns = list(PROFILE_COLUMNS)
        if params is not None and params.estimate_unknown:
            columns[columns.index("Eff_cov")] = "True_cov"
        return tuple(columns)
    raise ValueError(f"unknown mode {mode!r}")


def result_row(
    result: Any,
    sample_name: str,
    *,
    mode: str = "query",
    metadata: Mapping[str, Any] | None = None,
    fields: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Convert a `pysylph` result object into a JSON-friendly row."""
    genome = result.genome_sketch
    genome_file = genome.name
    accession = accession_from_genome_name(genome_file)

    row: dict[str, Any] = {
        "Sample_file": sample_name,
        "Genome_file": genome_file,
        "Adjusted_ANI": float(result.ani),
        "Eff_cov": float(result.coverage),
        "ANI_5-95_percentile": _pair(result.ani_ci),
        "Eff_lambda": None if result.effective_lambda is None else float(result.effective_lambda),
        "Lambda_status": result.lambda_status,
        "Lambda_5-95_percentile": _pair(result.lambda_ci),
        "Median_cov": float(result.median_coverage),
        "Mean_cov_geq1": float(result.mean_coverage),
        "Containment_ind": [int(result.containment[0]), int(result.containment[1])],
        "Naive_ANI": float(result.ani_naive),
        "Contig_name": genome.description,
    }
    if mode == "profile":
        row["Taxonomic_abundance"] = float(result.taxonomic_abundance)
        row["Sequence_abundance"] = float(result.sequence_abundance)
        row["kmers_reassigned"] = int(result.kmers_reassigned)
    elif mode != "query":
        raise ValueError(f"unknown mode {mode!r}")
    row["accession"] = accession

    if metadata is not None:
        selected = METADATA_COLUMNS if fields is None else tuple(fields)
        for column in selected:
            row[column] = metadata.get(column)
    return row


def _pair(value: object) -> list[float] | None:
    if value is None:
        return None
    low, high = value  # type: ignore[misc]
    return [float(low), float(high)]


def _format_float(value: float, digits: int) -> str:
    return f"{value:.{digits}f}"


def format_value(column: str, value: Any) -> str:
    """Format one cell of the TSV output, mirroring sylph's formatting."""
    if value is None:
        return ""
    if column in {"Sample_file", "Genome_file", "Contig_name", "accession"}:
        return str(value)
    if column == "Adjusted_ANI" or column == "Naive_ANI":
        return _format_float(float(value), 2)
    if column in {"Eff_cov", "True_cov", "Mean_cov_geq1"}:
        return _format_float(float(value), 3)
    if column == "Eff_lambda":
        return _format_float(float(value), 3)
    if column in {"ANI_5-95_percentile", "Lambda_5-95_percentile"}:
        low, high = value  # type: ignore[misc]
        return f"{low:.2f}-{high:.2f}"
    if column == "Median_cov":
        return _format_float(float(value), 0)
    if column == "Containment_ind":
        contained, total = value  # type: ignore[misc]
        return f"{int(contained)}/{int(total)}"
    if column in {"Taxonomic_abundance", "Sequence_abundance"}:
        return _format_float(float(value), 4)
    if column == "kmers_reassigned":
        return str(int(value))
    if isinstance(value, float):
        return repr(value)
    return str(value)


def header_for(
    mode: str,
    params: ProfileParams | None = None,
    fields: Sequence[str] | None = None,
) -> list[str]:
    """TSV header: sylph columns, then ``accession`` and metadata columns."""
    header = list(columns_for_mode(mode, params)) + list(EXTRA_COLUMNS)
    if fields is not None:
        header.extend(fields)
    return header


def format_row(row: Mapping[str, Any], header: Sequence[str]) -> str:
    """Format a row as one TSV line (without the newline)."""
    return "\t".join(format_value(column, row.get(column)) for column in header)


def rows_to_tsv_lines(
    samples: Iterable[Mapping[str, Any]],
    mode: str,
    *,
    params: ProfileParams | None = None,
    fields: Sequence[str] | None = None,
):
    """Yield the TSV header followed by every result row."""
    header = header_for(mode, params, fields)
    yield "\t".join(header)
    for sample in samples:
        for row in sample.get("rows", ()):
            yield format_row(row, header)


def rows_to_tsv(
    samples: Iterable[Mapping[str, Any]],
    mode: str,
    *,
    params: ProfileParams | None = None,
    fields: Sequence[str] | None = None,
) -> str:
    """Render the response samples as a single TSV document."""
    return "\n".join(rows_to_tsv_lines(samples, mode, params=params, fields=fields)) + "\n"
