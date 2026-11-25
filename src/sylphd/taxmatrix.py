"""``sylphd taxmatrix``: merge taxonomic profiles into one clade x sample table.

Reads ``.sylphmpa`` files (as written by ``sylph-tax taxprof`` or
``sylphd profile --taxprof``) and saved ``POST /profile`` JSON documents (with
``taxprof``), and renders the wide tab-separated matrix of ``sylph-tax merge``
via :func:`sylphd.taxonomy.clade_matrix_lines`.

Only the standard library and :mod:`sylphd.taxonomy` are used, so the command
works in the base install without ``pysylph`` or any extra.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .entry import PYTHON_COMMANDS
from .taxonomy import (
    MATRIX_VALUES,
    CladeAbundance,
    TaxonomyError,
    clade_matrix_lines,
    clades_from_dict,
    parse_sylphmpa,
    write_taxmatrix,
)

#: Wire-protocol version of ``/profile`` JSON documents supported here.
SUPPORTED_API_VERSION: int = 2


def _pick_taxonomy(entry: Mapping[str, Any], requested: str) -> str | None:
    """Resolve ``--taxonomy`` against one sample's ``taxprof`` mapping."""
    if requested in entry:
        return requested
    lowered = requested.lower()
    if lowered in entry:
        return lowered
    if lowered.startswith("tax:"):
        normalised = "tax:" + requested[4:]
        if normalised in entry:
            return normalised
    return None


def _clades_from_payload(
    payload: Mapping[str, Any], *, label: str
) -> list[CladeAbundance]:
    """Extract clades from one ``taxprof[sample][taxonomy]`` object."""
    raw_clades = payload.get("clades")
    if isinstance(raw_clades, list):
        for index, row in enumerate(raw_clades):
            if (
                not isinstance(row, Mapping)
                or not isinstance(row.get("clade_name"), str)
                or not row["clade_name"]
            ):
                raise TaxonomyError(f"{label}: invalid clade entry at index {index}")
        return clades_from_dict(raw_clades)
    sylphmpa = payload.get("sylphmpa")
    if isinstance(sylphmpa, str):
        _, clades = parse_sylphmpa(sylphmpa, label=label)
        return clades
    raise TaxonomyError(f"{label}: taxprof entry has neither 'clades' nor 'sylphmpa'")


def _parse_profile_json(
    text: str, *, label: str, taxonomy: str | None = None
) -> tuple[str, list[tuple[str, list[CladeAbundance]]]]:
    """Extract ``(selected_taxonomy, [(sample_name, clades), ...])`` from a ``/profile`` JSON doc."""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as error:
        raise TaxonomyError(f"{label}: invalid JSON: {error}") from error
    if not isinstance(doc, Mapping):
        raise TaxonomyError(f"{label}: expected a /profile JSON object")

    version = doc.get("api_version")
    if version is None:
        raise TaxonomyError(
            f"{label}: not a /profile document (no api_version; this sylphd supports "
            f"API version {SUPPORTED_API_VERSION})"
        )
    if isinstance(version, bool) or version != SUPPORTED_API_VERSION:
        raise TaxonomyError(
            f"{label}: document API version {version!r}, this sylphd supports "
            f"{SUPPORTED_API_VERSION}: update sylphd"
        )

    taxprof = doc.get("taxprof")
    if not isinstance(taxprof, Mapping) or not taxprof:
        raise TaxonomyError(
            f"{label}: /profile document has no 'taxprof' data "
            "(profile with taxprof enabled to include taxonomic profiles)"
        )

    if "samples" in doc:
        raw_samples = doc["samples"]
        if not isinstance(raw_samples, list) or not raw_samples:
            raise TaxonomyError(f"{label}: /profile document has no samples")
        sample_names: list[str] = []
        for index, entry in enumerate(raw_samples):
            if (
                not isinstance(entry, Mapping)
                or not isinstance(entry.get("name"), str)
                or not entry["name"]
            ):
                raise TaxonomyError(f"{label}: invalid sample entry at index {index}")
            name = entry["name"]
            if name not in taxprof:
                raise TaxonomyError(
                    f"{label}: sample {name!r} is missing from 'taxprof'"
                )
            sample_names.append(name)
    else:
        sample_names = [str(name) for name in taxprof]

    per_sample: list[tuple[str, Mapping[str, Any]]] = []
    for name in sample_names:
        entry = taxprof[name]
        if not isinstance(entry, Mapping) or not entry:
            raise TaxonomyError(
                f"{label}: sample {name!r} has no taxonomies in 'taxprof'"
            )
        per_sample.append((name, entry))

    if taxonomy is None:
        all_taxonomies = list(
            dict.fromkeys(key for _, entry in per_sample for key in entry)
        )
        if len(all_taxonomies) > 1:
            raise TaxonomyError(
                f"{label}: document contains multiple taxonomies "
                f"({', '.join(all_taxonomies)}); select one with --taxonomy"
            )
        selected = all_taxonomies[0]
        chosen_keys = [selected] * len(per_sample)
    else:
        chosen_keys = []
        for name, entry in per_sample:
            key = _pick_taxonomy(entry, taxonomy)
            if key is None:
                available = ", ".join(entry)
                raise TaxonomyError(
                    f"{label}: sample {name!r} has no taxonomy {taxonomy!r} "
                    f"(available: {available})"
                )
            chosen_keys.append(key)
        selected = chosen_keys[0]

    samples: list[tuple[str, list[CladeAbundance]]] = []
    for (name, entry), key in zip(per_sample, chosen_keys):
        payload = entry[key]
        if not isinstance(payload, Mapping):
            raise TaxonomyError(
                f"{label}: invalid taxprof entry for sample {name!r}, taxonomy {key!r}"
            )
        clades = _clades_from_payload(payload, label=f"{label} ({name}, {key})")
        samples.append((name, clades))
    return selected, samples


def load_inputs(
    paths: Sequence[str | os.PathLike[str]],
    *,
    taxonomy: str | None = None,
) -> list[tuple[str, list[CladeAbundance]]]:
    """Load samples from ``.sylphmpa`` files and/or saved ``/profile`` JSON documents."""
    if not paths:
        raise TaxonomyError("at least one input file is required")

    samples: list[tuple[str, list[CladeAbundance]]] = []
    seen_samples: dict[str, str] = {}
    json_taxonomies: list[str] = []
    saw_json = False

    for raw_path in paths:
        path = Path(raw_path)
        label = str(raw_path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise TaxonomyError(f"cannot read {label}: {error}") from error

        stripped = text.lstrip()
        if text.startswith("#SampleID"):
            file_samples = [parse_sylphmpa(text, label=label)]
        elif stripped.startswith("{"):
            saw_json = True
            selected, file_samples = _parse_profile_json(
                text, label=label, taxonomy=taxonomy
            )
            if selected not in json_taxonomies:
                json_taxonomies.append(selected)
        else:
            raise TaxonomyError(
                f"{label}: not a .sylphmpa file (first line '#SampleID') "
                "nor a /profile JSON document"
            )

        for name, clades in file_samples:
            if name in seen_samples:
                raise TaxonomyError(
                    f"duplicate sample name {name!r} "
                    f"(in {seen_samples[name]} and {label})"
                )
            seen_samples[name] = label
            samples.append((name, clades))

    if taxonomy is not None and not saw_json:
        raise TaxonomyError(
            "--taxonomy only applies to /profile JSON documents, not .sylphmpa files"
        )
    if taxonomy is None and len(json_taxonomies) > 1:
        raise TaxonomyError(
            "input JSON documents have different taxonomies "
            f"({', '.join(json_taxonomies)}); select one with --taxonomy"
        )
    return samples


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "files",
        nargs="+",
        metavar="FILE",
        help=".sylphmpa files or saved /profile JSON documents",
    )
    parser.add_argument(
        "--value",
        choices=MATRIX_VALUES,
        default="relative",
        help=(
            "value to tabulate: 'relative' (Taxonomic_abundance), "
            "'sequence' (Sequence_abundance), 'ani' or 'coverage' (strain "
            "rows only, like 'sylph-tax merge --column ANI|Coverage') "
            "[default: relative]"
        ),
    )
    parser.add_argument(
        "--taxonomy",
        default=None,
        metavar="NAME",
        help=(
            "taxonomy to extract from /profile JSON documents (required when "
            "a document holds more than one, e.g. gtdb, ncbi, tax, tax:SOURCE)"
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        metavar="FILE",
        help="write the matrix TSV to FILE [default: stdout]",
    )


def run(args: argparse.Namespace) -> int:
    """Run ``sylphd taxmatrix``: 0 on success, 2 on usage/input/output error."""
    try:
        samples = load_inputs(args.files, taxonomy=args.taxonomy)
    except TaxonomyError as error:
        print(f"sylphd taxmatrix: {error}", file=sys.stderr)
        return 2

    if args.output is None or args.output == "-":
        for line in clade_matrix_lines(samples, args.value):
            sys.stdout.write(line + "\n")
        sys.stdout.flush()
        return 0

    try:
        write_taxmatrix(args.output, samples, args.value)
    except OSError as error:
        print(f"sylphd taxmatrix: cannot write {args.output}: {error}", file=sys.stderr)
        return 2
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sylphd taxmatrix",
        description=PYTHON_COMMANDS["taxmatrix"],
    )
    add_arguments(parser)
    try:
        args = parser.parse_args(None if argv is None else list(argv))
    except SystemExit as error:
        return int(error.code or 0)
    return run(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
