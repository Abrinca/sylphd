"""Per-rank taxonomic aggregation for ``sylphd profile``.

The server returns one row per ANI hit, with the metadata taxonomy strings
(``gtdb_taxonomy``/``ncbi_taxonomy``) joined in.  This module folds those rows
into the clade tables that ``sylph-tax`` writes, without any server support:

* :func:`aggregate_rows` sums ``Taxonomic_abundance`` and
  ``Sequence_abundance`` up every lineage, so each ancestor clade gets the sum
  of its descendants;
* :func:`taxprof_lines` renders one ``*.sylphmpa`` file per sample, in
  ``sylph-tax taxprof``'s format (header comment, five columns, rows ordered
  rank by rank with t__ last, abundances sorted descending within a rank);
* :func:`clade_matrix_lines` renders the wide table of ``sylph-tax merge``:
  one row per clade, one column per sample, missing clades filled with ``0.0``,
  clades sorted as pandas sorts the outer join (textually, for two or more
  samples).

Everything here is pure: rows in, lines out.  File handling is limited to the
two small ``write_*`` helpers.

Lineage rules
-------------
Taxonomy strings are split on ``;`` and keep their ``d__``...``s__`` prefixes.
GTDB strings name every rank; NCBI strings may have empty ranks in the middle
(``o__;f__;g__Name``) or at the end (``g__;s__``).  Therefore:

* leading/trailing whitespace is stripped and tokens without a known rank
  prefix (``t__``, ``b__``, ...) are ignored;
* trailing empty ranks are dropped, so the lineage ends at the last named
  rank and the strain hangs directly below it;
* an empty rank *before* a named one is filled with
  ``{rank}__unclassified {nearest named ancestor}`` (e.g.
  ``o__unclassified Clostridia``); with no named ancestor the name is
  ``root``;
* a row without usable taxonomy ends up in a single ``unclassified`` clade.

The strain level is ``t__<accession>`` when the row has an accession (the
server derives it from ``Genome_file``), otherwise ``t__<file name>``.
``sylph-tax`` uses the accessions from its taxonomy files, which is the same
thing for GTDB.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

#: Rank prefixes of the GTDB/NCBI lineages, in lineage order.
RANKS: tuple[str, ...] = ("d", "p", "c", "o", "f", "g", "s")

#: Name of the pseudo-clade used for rows without any taxonomy.
UNCLASSIFIED = "unclassified"

#: Metadata column holding the lineage for each supported taxonomy.
TAXONOMY_COLUMNS: dict[str, str] = {
    "gtdb": "gtdb_taxonomy",
    "ncbi": "ncbi_taxonomy",
}

#: Supported taxonomy identifiers, in default order.
TAXONOMIES: tuple[str, ...] = tuple(TAXONOMY_COLUMNS)

#: Column headers written by ``sylph-tax taxprof`` (without virus annotation).
TAXPROF_COLUMNS: tuple[str, ...] = (
    "clade_name",
    "relative_abundance",
    "sequence_abundance",
    "ANI (if strain-level)",
    "Coverage (if strain-level)",
)

#: Values accepted by ``--taxmatrix-value``, as ``sylph-tax merge --column``.
MATRIX_VALUES: tuple[str, ...] = ("relative", "sequence", "ani", "coverage")

#: Values that only strain rows carry (``sylph-tax merge --column ANI|Coverage``).
STRAIN_MATRIX_VALUES: tuple[str, ...] = ("ani", "coverage")


class TaxonomyError(ValueError):
    """Bad taxonomy flag, or a server that cannot provide a taxonomy."""


@dataclass(frozen=True)
class CladeAbundance:
    """One clade of one sample: summed abundances and strain-level stats."""

    clade: str
    relative: float
    sequence: float
    ani: float | None = None
    coverage: float | None = None

    @property
    def depth(self) -> int:
        """Number of levels in the clade path (``d__X`` is 1)."""
        return self.clade.count("|") + 1

    @property
    def is_strain(self) -> bool:
        return self.clade.rsplit("|", 1)[-1].startswith("t__")


# ---------------------------------------------------------------------------
# flags
# ---------------------------------------------------------------------------


def parse_taxonomies(text: str | Sequence[str] | None) -> tuple[str, ...]:
    """Parse ``--taxonomy`` (``"gtdb,ncbi"``, ``"gtdb"``, ...).

    The default (``None``) is both taxonomies.  Unknown names raise
    :class:`TaxonomyError`; duplicates are collapsed, first mention wins.
    """
    if text is None:
        return TAXONOMIES
    parts = text.split(",") if isinstance(text, str) else [str(part) for part in text]
    selected: list[str] = []
    for part in parts:
        name = part.strip().lower()
        if not name:
            continue
        if name not in TAXONOMY_COLUMNS:
            raise TaxonomyError(
                f"unknown taxonomy {part.strip()!r} "
                f"(choose from: {', '.join(TAXONOMIES)})"
            )
        if name not in selected:
            selected.append(name)
    if not selected:
        raise TaxonomyError(
            f"--taxonomy needs at least one of: {', '.join(TAXONOMIES)}"
        )
    return tuple(selected)


def resolve_taxonomy_columns(
    taxonomies: Sequence[str], metadata: Mapping[str, Any] | None
) -> list[str]:
    """Return the metadata columns needed for ``taxonomies``.

    Raises :class:`TaxonomyError` when the server has no metadata database or
    when its metadata lacks one of the columns (e.g. a database built from
    files without ``ncbi_taxonomy``).
    """
    if metadata is None:
        raise TaxonomyError(
            "the server has no metadata database; --taxprof/--taxmatrix need "
            "'gtdb_taxonomy'/'ncbi_taxonomy' (start it with 'sylphd serve --meta')"
        )
    available = [str(column) for column in metadata.get("columns") or ()]
    columns: list[str] = []
    missing: list[str] = []
    for taxonomy in taxonomies:
        column = TAXONOMY_COLUMNS[taxonomy]
        if column not in available:
            missing.append(f"{column} ({taxonomy})")
        elif column not in columns:
            columns.append(column)
    if missing:
        raise TaxonomyError(
            "the server metadata has no "
            + ", ".join(missing)
            + "; available columns: "
            + (", ".join(available) or "(none)")
        )
    return columns


# ---------------------------------------------------------------------------
# lineage parsing
# ---------------------------------------------------------------------------


def _rank_levels(taxonomy: str | None) -> dict[str, str]:
    """Map rank prefix to name (possibly empty) for one taxonomy string."""
    levels: dict[str, str] = {}
    for token in (taxonomy or "").split(";"):
        token = token.strip()
        if not token:
            continue
        prefix, separator, name = token.partition("__")
        if not separator:
            continue
        rank = prefix.strip().lower()
        if rank not in RANKS:
            # t__ from a pre-annotated string, b__/k__ from other taxonomies.
            continue
        name = name.strip()
        # First mention wins, but let a later named token fill an empty one.
        if rank not in levels or (not levels[rank] and name):
            levels[rank] = name
    return levels


def parse_lineage(taxonomy: str | None) -> tuple[str, ...]:
    """Turn a ``d__..;p__..;s__..`` string into its cumulative clade names.

    Returns an empty tuple when the string names no rank at all (``None``,
    ``""``, ``"none"``, ``"unclassified"``, ...), which the callers render as
    the :data:`UNCLASSIFIED` clade.

    For example ``d__Bacteria;p__Bacillota;c__Clostridia;o__;f__;``
    ``g__Candidatus Avispirillum;s__`` becomes ``d__Bacteria``,
    ``p__Bacillota``, ``c__Clostridia``, ``o__unclassified Clostridia``,
    ``f__unclassified Clostridia`` and ``g__Candidatus Avispirillum``.
    """
    levels = _rank_levels(taxonomy)
    last_named = -1
    for index, rank in enumerate(RANKS):
        if levels.get(rank):
            last_named = index
    if last_named < 0:
        return ()

    clades: list[str] = []
    ancestor = "root"
    for rank in RANKS[: last_named + 1]:
        name = levels.get(rank, "")
        if name:
            clades.append(f"{rank}__{name}")
            ancestor = name
        else:
            clades.append(f"{rank}__unclassified {ancestor}")
    return tuple(clades)


def strain_label(row: Mapping[str, Any]) -> str:
    """Name the ``t__`` level of a row: accession, else genome file basename."""
    accession = row.get("accession")
    if isinstance(accession, str) and accession.strip():
        return accession.strip()
    genome = row.get("Genome_file")
    base = os.path.basename(str(genome or "").replace("\\", "/")).strip()
    return base or "unknown"


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------


def _number(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _optional_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def aggregate_rows(
    rows: Iterable[Mapping[str, Any]], taxonomy: str = "gtdb"
) -> list[CladeAbundance]:
    """Sum one sample's rows over every clade of ``taxonomy``.

    Abundances are summed as sylph reported them; nothing is renormalised, so
    with ``--estimate-unknown`` the root clade can stay below 100.

    The order mirrors ``sylph-tax taxprof``: rank by rank, and within a rank by
    decreasing relative abundance, keeping first-seen order for ties.
    """
    try:
        column = TAXONOMY_COLUMNS[taxonomy]
    except KeyError:
        raise TaxonomyError(
            f"unknown taxonomy {taxonomy!r} (choose from: {', '.join(TAXONOMIES)})"
        ) from None

    relative: dict[str, float] = {}
    sequence: dict[str, float] = {}
    strain_stats: dict[str, tuple[float | None, float | None]] = {}

    for row in rows:
        lineage = parse_lineage(row.get(column))
        strain = f"t__{strain_label(row)}"
        parts = (*lineage, strain) if lineage else (UNCLASSIFIED, strain)
        row_relative = _number(row.get("Taxonomic_abundance"))
        row_sequence = _number(row.get("Sequence_abundance"))

        clade = ""
        for part in parts:
            clade = part if not clade else f"{clade}|{part}"
            relative[clade] = relative.get(clade, 0.0) + row_relative
            sequence[clade] = sequence.get(clade, 0.0) + row_sequence

        coverage = row.get("Eff_cov")
        if coverage is None:
            coverage = row.get("True_cov")
        strain_stats[clade] = (
            _optional_number(row.get("Adjusted_ANI")),
            _optional_number(coverage),
        )

    clades = [
        CladeAbundance(
            clade=clade,
            relative=value,
            sequence=sequence[clade],
            ani=strain_stats.get(clade, (None, None))[0],
            coverage=strain_stats.get(clade, (None, None))[1],
        )
        for clade, value in relative.items()
    ]
    # Strains (t__) are the last rank, however short their lineage was.
    clades.sort(
        key=lambda item: (len(RANKS) + 1 if item.is_strain else item.depth, -item.relative)
    )
    return clades


def aggregate_samples(
    samples: Iterable[Mapping[str, Any]], taxonomy: str = "gtdb"
) -> list[tuple[str, list[CladeAbundance]]]:
    """Aggregate every returned sample, keeping the response order."""
    return [
        (sample_name(sample), aggregate_rows(sample.get("rows") or (), taxonomy))
        for sample in samples
    ]


def sample_name(sample: Mapping[str, Any]) -> str:
    """The sample name used in file names and matrix headers."""
    name = sample.get("name")
    if not name:
        rows = sample.get("rows") or ()
        for row in rows:
            name = row.get("Sample_file")
            if name:
                break
    return str(name or "sample")


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _cell(value: float | None) -> str:
    """Format a number the way sylph-tax does (full float precision)."""
    if value is None:
        return "NA"
    return str(float(value))


def _safe_name(name: str) -> str:
    """Keep a sample name from escaping the output prefix directory."""
    return name.replace("/", "_").replace("\\", "_") or "sample"


def taxprof_lines(
    name: str, taxonomy: str, clades: Sequence[CladeAbundance]
) -> Iterator[str]:
    """Yield one ``.sylphmpa`` document, as ``sylph-tax taxprof`` writes it."""
    yield f"#SampleID\t{name}\tTaxonomies_used:{[taxonomy]!r}"
    yield "\t".join(TAXPROF_COLUMNS)
    for item in clades:
        if item.is_strain:
            yield (
                f"{item.clade}\t{_cell(item.relative)}\t{_cell(item.sequence)}\t"
                f"{_cell(item.ani)}\t{_cell(item.coverage)}"
            )
        else:
            yield f"{item.clade}\t{_cell(item.relative)}\t{_cell(item.sequence)}\tNA\tNA"


def write_taxprof(
    path: str | os.PathLike[str],
    name: str,
    taxonomy: str,
    clades: Sequence[CladeAbundance],
) -> Path:
    """Write one ``.sylphmpa`` file and return its path."""
    output = Path(path)
    with open(output, "w", encoding="utf-8", newline="\n") as handle:
        for line in taxprof_lines(name, taxonomy, clades):
            handle.write(line + "\n")
    return output


def clade_matrix_lines(
    samples: Sequence[tuple[str, Sequence[CladeAbundance]]],
    value: str = "relative",
) -> Iterator[str]:
    """Yield the wide ``sylph-tax merge`` table for already aggregated samples.

    This reproduces ``merge_sylph_taxprof.merge_data`` exactly: rows are all
    clades seen in any sample (outer join), a clade missing from a sample is
    ``0.0``, and the clade names are sorted lexicographically as soon as there
    are two or more samples (pandas sorts the index of an outer join; a single
    file keeps the profile's order).  For ``ani`` and ``coverage`` only the
    strain rows carry a value, so clades without one in *any* sample are
    dropped (pandas' ``dropna(how="all")``) and a clade with a value in one
    sample but not another gets ``0``, not ``NA``.
    """
    if value not in MATRIX_VALUES:
        raise TaxonomyError(
            f"unknown matrix value {value!r} (choose from: {', '.join(MATRIX_VALUES)})"
        )

    order: list[str] = []
    cells: dict[str, list[float | None]] = {}
    for index, (_, clades) in enumerate(samples):
        for item in clades:
            if item.clade not in cells:
                cells[item.clade] = [None] * len(samples)
                order.append(item.clade)
            cells[item.clade][index] = getattr(item, value)

    if value in STRAIN_MATRIX_VALUES:
        order = [clade for clade in order if any(c is not None for c in cells[clade])]
    if len(samples) >= 2:
        order.sort()

    yield "\t".join(["clade_name", *(name for name, _ in samples)])
    for clade in order:
        yield "\t".join(
            [clade, *(_cell(0.0 if cell is None else cell) for cell in cells[clade])]
        )


def write_taxmatrix(
    path: str | os.PathLike[str],
    samples: Sequence[tuple[str, Sequence[CladeAbundance]]],
    value: str = "relative",
) -> Path:
    """Write one wide matrix file and return its path."""
    output = Path(path)
    with open(output, "w", encoding="utf-8", newline="\n") as handle:
        for line in clade_matrix_lines(samples, value):
            handle.write(line + "\n")
    return output


def taxprof_path(prefix: str, name: str, taxonomy: str) -> Path:
    return Path(f"{prefix}{_safe_name(name)}.{taxonomy}.sylphmpa")


def taxmatrix_path(prefix: str, taxonomy: str) -> Path:
    return Path(f"{prefix}{taxonomy}.tsv")


def write_outputs(
    samples: Sequence[Mapping[str, Any]],
    *,
    taxonomies: Sequence[str],
    taxprof_prefix: str | None = None,
    taxmatrix_prefix: str | None = None,
    matrix_value: str = "relative",
) -> list[Path]:
    """Write the requested taxonomy files for a profile response.

    Files are named ``{taxprof_prefix}{sample}.{taxonomy}.sylphmpa`` and
    ``{taxmatrix_prefix}{taxonomy}.tsv``; a ``None`` prefix skips that output.
    A prefix that is an existing directory is treated as ``directory/``.
    Returns the paths written, taxonomy by taxonomy (each taxonomy writes its
    per-sample files first, then its matrix).
    """
    if taxprof_prefix is None and taxmatrix_prefix is None:
        return []

    # A prefix naming an existing directory writes into it, like one ending in "/".
    if taxprof_prefix is not None and os.path.isdir(taxprof_prefix):
        taxprof_prefix += "/"
    if taxmatrix_prefix is not None and os.path.isdir(taxmatrix_prefix):
        taxmatrix_prefix += "/"

    if taxprof_prefix is not None:
        # Sanitising can map different names to one file; check before writing.
        owners: dict[Path, str] = {}
        for sample in samples:
            name = sample_name(sample)
            path = taxprof_path(taxprof_prefix, name, taxonomies[0])
            if path in owners:
                raise TaxonomyError(
                    f"sample names {owners[path]!r} and {name!r} would write the "
                    f"same file {path.name}"
                )
            owners[path] = name

    written: list[Path] = []
    for taxonomy in taxonomies:
        table = aggregate_samples(samples, taxonomy)
        if taxprof_prefix is not None:
            for name, clades in table:
                written.append(
                    write_taxprof(
                        taxprof_path(taxprof_prefix, name, taxonomy),
                        name,
                        taxonomy,
                        clades,
                    )
                )
        if taxmatrix_prefix is not None:
            written.append(
                write_taxmatrix(
                    taxmatrix_path(taxmatrix_prefix, taxonomy), table, matrix_value
                )
            )
    return written
