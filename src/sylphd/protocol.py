"""Wire protocol details: request parameters and result rows (rendering lives in sylphd-core).

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
A JSON document with ``api_version``, effective ``params``, ``samples``,
``table`` (``{"columns": [...], "data": [[...], ...]}``, pandas
``orient="split"`` without an index), ``caveats``, and (on ``/profile`` with
``taxprof``) ``taxprof``::

    {"api_version": 1, "params": {...},
     "samples": [{"name": "sample-a", "kmers": 1234, "mean_read_length": 150.0,
                  "paired": false, "reads": 1000, "bases": 150000}],
     "table": {"columns": ["Sample_file", ..., "Lambda_status", "accession", ...],
               "data": [[...]]},
     "caveats": []}

``table.columns`` is the final ordered header (with ``True_cov`` in place of
``Eff_cov`` when ``estimate_unknown`` or ``estimate_read_counts`` is set) plus
``Lambda_status`` before ``accession``, then ``genome_id``.

Optional per-sample statistics a client measured while sketching go in the
``params`` field as ``samples``: a list, one object per ``sketch`` part (same
order), each ``{"name"?, "reads": int|null, "bases": int|null}``.  The response's
``samples`` entries are ``{"name", "kmers", "mean_read_length", "paired",
"reads", "bases"}``: the first three come from the uploaded sketch header, the
last two are the client's values (``null`` when not sent).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Any

from .assembly import genome_id
from .metadata import METADATA_COLUMNS, accession_from_genome_name

#: Wire-protocol version reported by ``GET /info`` and every result document.
#: Bump on any breaking change to the response shape.
API_VERSION: int = 1

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
EXTRA_COLUMNS: tuple[str, ...] = ("accession", "genome_id")

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
    # /profile only: the taxonomies whose clade tables and .sylphmpa text the
    # server should render (`["gtdb", "ncbi", "tax"]`).
    "taxprof",
    # Client-measured per-sample statistics (``reads``, ``bases``), optional.
    "samples",
)


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
) -> dict[str, Any]:
    """Convert a `pysylph` result object into a JSON-friendly row.

    With ``no_adjust``, ``ani`` is the naive containment ANI while ``ani_ci``
    is still the confidence interval of the adjusted estimate, exactly as in
    stock sylph; the row keeps it (clients may choose not to show it).
    """
    genome = result.genome_sketch
    genome_file = genome.name
    accession = accession_from_genome_name(genome_file)

    row: dict[str, Any] = {
        "Sample_file": sample_name,
        "Genome_file": genome_file,
        "Adjusted_ANI": float(result.ani),
        "Eff_cov": _finite(result.coverage),
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
        row["Taxonomic_abundance"] = _finite(result.taxonomic_abundance)
        row["Sequence_abundance"] = _finite(result.sequence_abundance)
        row["kmers_reassigned"] = int(result.kmers_reassigned)
    elif mode != "query":
        raise ValueError(f"unknown mode {mode!r}")
    row["accession"] = accession
    row["genome_id"] = genome_id(
        {**(metadata or {}), "accession": accession, "Genome_file": genome_file},
        contig_name=genome.description,
    )

    if metadata is not None:
        selected = METADATA_COLUMNS if fields is None else tuple(fields)
        for column in selected:
            row[column] = metadata.get(column)
    return row


def _finite(value: object) -> float | None:
    """``value`` as a float, or ``None`` for sylph's ``inf``/``NaN``.

    With ``estimate_unknown`` a sketch without read statistics (one made from
    contigs) gets ``True_cov`` = inf and NaN abundances, as in ``sylph profile
    -u``; JSON has no such numbers, so they become ``null`` (an empty TSV cell).
    """
    number = float(value)  # type: ignore[arg-type]
    return number if math.isfinite(number) else None


def _pair(value: object) -> list[float] | None:
    if value is None:
        return None
    low, high = value  # type: ignore[misc]
    return [float(low), float(high)]


def sample_stats(
    value: object, count: int
) -> list[dict[str, int | None]]:
    """Validate ``params.samples``: per sketch ``{"name"?, "reads", "bases"}``.

    Returns one ``{"reads", "bases"}`` dict per sketch (``None`` for a value the
    client did not send).  Raises :class:`ProtocolError` for a bad shape.
    """
    if value is None:
        return [{"reads": None, "bases": None} for _ in range(count)]
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ProtocolError("'params.samples' must be a list of objects")
    if len(value) != count:
        raise ProtocolError(f"'params.samples' has {len(value)} entries for {count} sketches")
    stats: list[dict[str, int | None]] = []
    for index, item in enumerate(value):
        unknown = sorted(set(item) - {"name", "reads", "bases"})
        if unknown:
            raise ProtocolError(
                f"unknown key(s) in 'params.samples[{index}]': {', '.join(unknown)}"
            )
        if not isinstance(item.get("name", ""), str):
            raise ProtocolError(f"'params.samples[{index}].name' must be a string")
        entry: dict[str, int | None] = {}
        for key in ("reads", "bases"):
            raw = item.get(key)
            if raw is None:
                entry[key] = None
                continue
            if not isinstance(raw, int) or isinstance(raw, bool):
                raise ProtocolError(
                    f"'params.samples[{index}].{key}' must be a non-negative integer or null"
                )
            if raw < 0 or raw > MAX_INTEGER:
                raise ProtocolError(
                    f"'params.samples[{index}].{key}' must be a non-negative integer or null"
                )
            entry[key] = raw
        stats.append(entry)
    return stats


def table_columns(
    mode: str,
    params: ProfileParams | None = None,
    fields: Sequence[str] | None = None,
    *,
    metadata_columns: Sequence[str] | None = None,
) -> list[str]:
    """JSON table columns: :func:`columns_for_mode` plus ``Lambda_status`` before ``accession``."""
    columns = [*columns_for_mode(mode, params), "Lambda_status", *EXTRA_COLUMNS]
    if fields is not None:
        columns.extend(fields)
    elif metadata_columns is not None:
        columns.extend(metadata_columns)
    return columns


def result_document(
    mode: str,
    params: ProfileParams,
    fields: Sequence[str] | None,
    names: Sequence[str],
    samples: Sequence[Mapping[str, Any]],
    sample_info: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """The ``/query`` / ``/profile`` response for the finished ``samples``.

    ``samples`` are the per-sample results in request order (``rows``, optional
    ``caveats`` and ``taxprof``); ``names`` are all requested sample names. Rows are
    laid out in :func:`table_columns` order, the caveats are the distinct notes in
    first-seen order, and ``taxprof`` (by sample name) is present when any sample has one.
    """
    columns = table_columns(mode, params, fields)
    data: list[list[Any]] = []
    caveats: list[str] = []
    taxprof: dict[str, Any] = {}
    for sample in samples:
        for row in sample.get("rows") or ():
            data.append(
                [
                    row.get("Eff_cov")
                    if column == "True_cov" and "True_cov" not in row
                    else row.get(column)
                    for column in columns
                ]
            )
        for note in sample.get("caveats") or ():
            if note not in caveats:
                caveats.append(note)
        if "taxprof" in sample:
            taxprof[sample["name"]] = sample["taxprof"]
    document: dict[str, Any] = {
        "api_version": API_VERSION,
        "params": params.to_dict(),
        "samples": [
            {
                "name": name,
                "kmers": None,
                "mean_read_length": None,
                "paired": None,
                "reads": None,
                "bases": None,
                **(sample_info[index] if sample_info is not None else {}),
            }
            for index, name in enumerate(names)
        ],
        "table": {"columns": columns, "data": data},
        "caveats": caveats,
    }
    if taxprof:
        document["taxprof"] = taxprof
    return document


