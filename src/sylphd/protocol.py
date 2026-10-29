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

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Any

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
#: ``True_cov`` when ``estimate_unknown`` (or ``estimate_read_counts``, which
#: implies it) is set, as in sylph.
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
    #: Report estimated read counts in ``Sequence_abundance`` instead of a
    #: relative abundance (sylph's experimental ``--estimate-read-counts``).
    #: Implies :attr:`estimate_unknown`, as in sylph.
    estimate_read_counts: bool = False
    min_number_kmers: int = 10
    min_contain: int = 7
    screen_ani: float = 85.0
    #: Report the naive containment ANI instead of the adjusted one.  Meant for
    #: assemblies (and genomes) as samples, where every k-mer occurs once and
    #: the coverage-based adjustment is meaningless.
    no_adjust: bool = False

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> ProfileParams:
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise ProtocolError("'params' must be a JSON object")
        known = set(PROFILE_PARAM_KEYS)
        unknown = sorted(set(data) - known)
        if unknown:
            raise ProtocolError(f"unknown parameter(s): {', '.join(unknown)}")

        return cls(
            minimum_ani=_optional_float(data, "minimum_ani", 0.0, 100.0),
            seq_id=_optional_float(data, "seq_id", 0.0, 100.0),
            estimate_unknown=_optional_bool(data, "estimate_unknown", False),
            estimate_read_counts=_optional_bool(data, "estimate_read_counts", False),
            min_number_kmers=integer_field(data, "min_number_kmers", 10, minimum=0),
            min_contain=integer_field(data, "min_contain", 7, minimum=0),
            screen_ani=number_field(data, "screen_ani", 85.0, 0.0, 100.0),
            no_adjust=_optional_bool(data, "no_adjust", False),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "minimum_ani": self.minimum_ani,
            "seq_id": self.seq_id,
            "estimate_unknown": self.estimate_unknown,
            "estimate_read_counts": self.estimate_read_counts,
            "min_number_kmers": self.min_number_kmers,
            "min_contain": self.min_contain,
            "screen_ani": self.screen_ani,
            "no_adjust": self.no_adjust,
        }

    def profiler_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for ``pysylph.Profiler``.

        Every field is one of the constructor's keywords, including
        ``estimate_read_counts``, which pysylph uses to put read counts in
        ``Sequence_abundance`` (and to enable ``estimate_unknown``).
        """
        return self.to_dict()


#: ``params`` keys ``/query`` and ``/profile`` accept.  The profiling fields
#: come from the dataclass itself, so a new parameter is listed in ``/info``
#: without touching a second list; the rest are the request-shape keys the
#: server or the profiler handles outside :class:`ProfileParams`.
PROFILE_PARAM_KEYS: tuple[str, ...] = (
    *(field.name for field in fields(ProfileParams)),
    "names",
    "fields",
    "mode",
    "tax_sources",
)


def params_payload(
    mode: str,
    params: ProfileParams | None,
    names: Sequence[str],
    fields: Sequence[str] | None = None,
    tax_sources: Sequence[str] | None = None,
) -> dict[str, Any]:
    """The JSON object sent as the ``params`` form field of ``/query``/``/profile``."""
    payload: dict[str, Any] = {
        **(params or ProfileParams()).to_dict(),
        "names": list(names),
        "mode": mode,
    }
    if fields is not None:
        payload["fields"] = list(fields)
    if tax_sources is not None:
        payload["tax_sources"] = list(tax_sources)
    return payload


def params_json(
    mode: str,
    params: ProfileParams | None,
    names: Sequence[str],
    fields: Sequence[str] | None = None,
    tax_sources: Sequence[str] | None = None,
) -> str:
    """The exact ``params`` value the client sends (default ``json.dumps`` separators)."""
    return json.dumps(params_payload(mode, params, names, fields, tax_sources))


def params_json_size(
    mode: str,
    params: ProfileParams | None,
    names: Sequence[str],
    fields: Sequence[str] | None = None,
    tax_sources: Sequence[str] | None = None,
) -> int:
    """Length of :func:`params_json` in characters (one char is one UTF-8 byte here).

    ``json.dumps`` escapes non-ASCII characters (``ensure_ascii``), so the
    result is also the byte length of the ``params`` form part.
    """
    return len(params_json(mode, params, names, fields, tax_sources))


def contigs_payload(
    contigs: Sequence[Mapping[str, Any]],
    *,
    options: Mapping[str, Any] | None = None,
    fields: Sequence[str] | None = None,
    tax_sources: Sequence[str] | None = None,
) -> dict[str, Any]:
    """The JSON object sent as the ``params`` field of ``POST /contigs``.

    ``contigs`` holds one object per ``sketch`` part, in the same order, with
    ``name``, ``contig_kmers`` (the record's genome-style k-mer count, the
    denominator of the contig-relative ANI), ``length`` and ``gc``.  ``options``
    are the server-side options of the scoring and the reporting: ``top``,
    ``min_ani``, ``consensus_ratio`` and ``consensus_z``.  ``fields`` and
    ``tax_sources`` keep their meaning from ``/query``.
    """
    payload: dict[str, Any] = {"contigs": [dict(contig) for contig in contigs]}
    if options is not None:
        payload.update(options)
    if fields is not None:
        payload["fields"] = list(fields)
    if tax_sources is not None:
        payload["tax_sources"] = list(tax_sources)
    return payload


def contigs_json(
    contigs: Sequence[Mapping[str, Any]],
    *,
    options: Mapping[str, Any] | None = None,
    fields: Sequence[str] | None = None,
    tax_sources: Sequence[str] | None = None,
) -> str:
    """The exact ``params`` value the client sends to ``POST /contigs``."""
    return json.dumps(
        contigs_payload(
            contigs, options=options, fields=fields, tax_sources=tax_sources
        )
    )


def contigs_json_size(
    contigs: Sequence[Mapping[str, Any]],
    *,
    options: Mapping[str, Any] | None = None,
    fields: Sequence[str] | None = None,
    tax_sources: Sequence[str] | None = None,
) -> int:
    """Length of :func:`contigs_json` in characters (== UTF-8 bytes)."""
    return len(
        contigs_json(
            contigs, options=options, fields=fields, tax_sources=tax_sources
        )
    )


def contig_json_size(contig: Mapping[str, Any]) -> int:
    """Characters one ``params.contigs`` entry adds to the JSON text.

    ``json.dumps`` of the whole object uses the same separators and escaping
    for the nested entry, so a caller can account for a growing list without
    re-encoding it: the first entry adds this many characters plus ``", "`` for
    every later one.
    """
    return len(json.dumps(dict(contig)))


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


#: Upper bound for integer parameters, chosen so the native ``usize``
#: conversion used by pysylph cannot fail.
MAX_INTEGER = 2**63 - 1


def _finite_float(value: object, key: str, *, optional: bool = False) -> float:
    """Convert ``value`` to a finite float or raise :class:`ProtocolError`."""
    suffix = " or null" if optional else ""
    if not _is_number(value):
        raise ProtocolError(f"'{key}' must be a finite number{suffix}")
    try:
        number = float(value)
    except (OverflowError, ValueError) as error:
        raise ProtocolError(f"'{key}' must be a finite number{suffix}") from error
    if not math.isfinite(number):
        raise ProtocolError(f"'{key}' must be a finite number{suffix}")
    return number


def number_field(
    data: Mapping[str, Any],
    key: str,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if key not in data or data[key] is None:
        return default
    number = _finite_float(data[key], key)
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
    number = _finite_float(data[key], key, optional=True)
    if minimum is not None and number < minimum:
        raise ProtocolError(f"'{key}' must be >= {minimum:g}")
    if maximum is not None and number > maximum:
        raise ProtocolError(f"'{key}' must be <= {maximum:g}")
    return number


def integer_field(
    data: Mapping[str, Any],
    key: str,
    default: int,
    minimum: int | None = None,
    maximum: int | None = MAX_INTEGER,
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
    if maximum is not None and value > maximum:
        raise ProtocolError(f"'{key}' must be <= {maximum}")
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
        # ``estimate_read_counts`` implies ``estimate_unknown``, as in sylph.
        if params is not None and (params.estimate_unknown or params.estimate_read_counts):
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
    hide_ani_ci: bool = False,
) -> dict[str, Any]:
    """Convert a `pysylph` result object into a JSON-friendly row.

    With ``no_adjust``, ``ani`` is the naive containment ANI while
    ``ani_ci`` is still the confidence interval of the adjusted estimate;
    ``hide_ani_ci`` drops that misleading value.
    """
    genome = result.genome_sketch
    genome_file = genome.name
    accession = accession_from_genome_name(genome_file)

    row: dict[str, Any] = {
        "Sample_file": sample_name,
        "Genome_file": genome_file,
        "Adjusted_ANI": float(result.ani),
        "Eff_cov": float(result.coverage),
        "ANI_5-95_percentile": None if hide_ani_ci else _pair(result.ani_ci),
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
    *,
    metadata_columns: Sequence[str] | None = None,
) -> list[str]:
    """TSV header: sylph columns, then ``accession`` and metadata columns."""
    header = list(columns_for_mode(mode, params)) + list(EXTRA_COLUMNS)
    if fields is not None:
        header.extend(fields)
    elif metadata_columns is not None:
        header.extend(metadata_columns)
    return header


def format_row(row: Mapping[str, Any], header: Sequence[str]) -> str:
    """Format a row as one TSV line (without the newline).

    ``True_cov`` is the display alias used when ``estimate_unknown`` is set;
    JSON rows keep the value under ``Eff_cov``.  Map the header to that key so
    the column is not empty.
    """
    cells = []
    for column in header:
        value = row.get(column)
        if column == "True_cov" and "True_cov" not in row and "Eff_cov" in row:
            value = row["Eff_cov"]
        cells.append(format_value(column, value))
    return "\t".join(cells)


def rows_to_tsv_lines(
    samples: Iterable[Mapping[str, Any]],
    mode: str,
    *,
    params: ProfileParams | None = None,
    fields: Sequence[str] | None = None,
    metadata_columns: Sequence[str] | None = None,
):
    """Yield the TSV header followed by every result row.

    ``fields=None`` is the wire value for "all": the server returns every
    metadata column.  For display, use ``metadata_columns`` (the server's
    column order) when given, otherwise infer the columns from the returned
    rows in first-seen order.
    """
    if fields is None and metadata_columns is None:
        sample_list = list(samples)
        base = set(header_for(mode, params, []))
        inferred: list[str] = []
        for sample in sample_list:
            for row in sample.get("rows", ()):
                for key in row:
                    if key not in base and key not in inferred:
                        inferred.append(key)
        metadata_columns = inferred
        samples = sample_list
    header = header_for(mode, params, fields, metadata_columns=metadata_columns)
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
    metadata_columns: Sequence[str] | None = None,
) -> str:
    """Render the response samples as a single TSV document."""
    return "\n".join(
        rows_to_tsv_lines(
            samples,
            mode,
            params=params,
            fields=fields,
            metadata_columns=metadata_columns,
        )
    ) + "\n"
