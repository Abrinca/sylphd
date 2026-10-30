"""Taxonomic aggregation for ``sylphd profile``.

The server returns one row per ANI hit, with the metadata taxonomy strings
(``gtdb_taxonomy``/``ncbi_taxonomy``) or the resolved sylph-tax taxonomy
(``tax_lineage``/``tax_key``/``tax_source``) joined in, and folds those rows
into the clade tables that ``sylph-tax`` writes (``taxprof``); the CLI only
writes them out.  The aggregation lives here:

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

Everything here is pure: rows in, lines out.  File handling is limited to
:func:`write_taxmatrix`.

Lineage rules
-------------
``gtdb`` and ``ncbi`` split the string on ``;`` and keep their ``d__``...``s__``
prefixes.  GTDB strings name every rank; NCBI strings may have empty ranks in
the middle (``o__;f__;g__Name``) or at the end (``g__;s__``).  Therefore:

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

The ``tax`` taxonomy
--------------------
``--taxonomy tax`` is **exact sylph-tax** over all imported sources of the
metadata file (``sylph-tax taxprof -t A B C``): lineages are opaque tokens,
split on ``;`` exactly as stored; an empty token becomes ``UNKNOWN``; the label
of the last level is ``t__<tax_key>``, the key sylph-tax matched in its
dictionary; a hit without any taxonomy becomes ``NO_TAXONOMY;t__<file>:<contig>``
(pass-1 probes, as sylph-tax writes it); rows are ordered by number of levels
and then by decreasing relative abundance (stable); empty tokens / no
``unclassified X`` filling.  Nothing is normalised here — the server did the
resolution and this module only aggregates.
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

#: sylph-tax's placeholder for an empty lineage token.
UNKNOWN = "UNKNOWN"

#: sylph-tax's default lineage when no taxonomy file matches a hit.
NO_TAXONOMY = "NO_TAXONOMY"

#: The taxonomy name for the exact-sylph-tax merged profile.
SYLPHTAX = "tax"

#: Metadata column holding the lineage for each rank-based taxonomy.
TAXONOMY_COLUMNS: dict[str, str] = {
    "gtdb": "gtdb_taxonomy",
    "ncbi": "ncbi_taxonomy",
}

#: Supported taxonomy identifiers, in default order.
TAXONOMIES: tuple[str, ...] = (*TAXONOMY_COLUMNS, SYLPHTAX)

#: Columns the ``tax`` profile needs from the server.  ``tax_host`` is
#: resolved server-side too, but the sixth output column is a later phase, so
#: it is deliberately not requested.
SYLPHTAX_PROFILE_COLUMNS: tuple[str, ...] = ("tax_source", "tax_key", "tax_lineage")

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
    #: Whether the five-column line carries the strain statistics.  ``None``
    #: means "the last level is ``t__``", which is sylphd's rule; the ``tax``
    #: profile sets it from sylph-tax's own (substring) rule.
    strain_stats: bool | None = None

    @property
    def depth(self) -> int:
        """Number of levels in the clade path (``d__X`` is 1)."""
        return self.clade.count("|") + 1

    @property
    def is_strain(self) -> bool:
        return self.clade.rsplit("|", 1)[-1].startswith("t__")

    @property
    def has_strain_stats(self) -> bool:
        return self.is_strain if self.strain_stats is None else self.strain_stats


# ---------------------------------------------------------------------------
# flags
# ---------------------------------------------------------------------------


def parse_taxonomies(text: str | Sequence[str] | None) -> tuple[str, ...]:
    """Parse ``--taxonomy`` (``"gtdb,ncbi"``, ``"gtdb"``, ``"tax"``, ...).

    ``None`` is the rank-based default (``gtdb,ncbi``); the command line
    decides separately whether ``tax`` belongs to the default, because that
    depends on the server (see :func:`default_taxonomies`).  Unknown names
    raise :class:`TaxonomyError`; duplicates are collapsed, first mention wins.
    """
    if text is None:
        return tuple(TAXONOMY_COLUMNS)
    parts = text.split(",") if isinstance(text, str) else [str(part) for part in text]
    selected: list[str] = []
    for part in parts:
        name = part.strip().lower()
        if not name:
            continue
        if name not in TAXONOMIES:
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


def has_taxonomy_sources(metadata: Mapping[str, Any] | None) -> bool:
    """True when the server's metadata has sylph-tax taxonomy sources."""
    return bool((metadata or {}).get("taxonomy_sources"))


def default_taxonomies(metadata: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The taxonomies to write when ``--taxonomy`` is not given.

    ``gtdb,ncbi`` as always, plus ``tax`` when the server has sylph-tax
    sources (``docs/project/multi-db-design.md`` B4).
    """
    names = list(TAXONOMY_COLUMNS)
    if has_taxonomy_sources(metadata):
        names.append(SYLPHTAX)
    return tuple(names)


def resolve_taxonomy_columns(
    taxonomies: Sequence[str], metadata: Mapping[str, Any] | None
) -> list[str]:
    """Return the metadata columns needed for ``taxonomies``.

    Raises :class:`TaxonomyError` when the server has no metadata database or
    when its metadata lacks one of the columns (e.g. a database built from
    files without ``ncbi_taxonomy``).  ``tax`` needs the ``tax_*`` columns,
    which only a metadata file with sylph-tax sources carries.
    """
    if metadata is None:
        raise TaxonomyError(
            "the server has no metadata database; --taxprof/--taxmatrix need "
            "'gtdb_taxonomy'/'ncbi_taxonomy' (start it with 'sylphd serve --meta')"
        )
    if SYLPHTAX in taxonomies and not has_taxonomy_sources(metadata):
        raise TaxonomyError(
            "the server has no sylph-tax taxonomy sources, so there is nothing "
            "for '--taxonomy tax' to aggregate; add sources with "
            "'sylphd setup --taxonomy' and restart the server"
        )
    available = [str(column) for column in metadata.get("columns") or ()]
    columns: list[str] = []
    missing: list[str] = []
    for taxonomy in taxonomies:
        needed: Sequence[str] = (
            SYLPHTAX_PROFILE_COLUMNS
            if taxonomy == SYLPHTAX
            else (TAXONOMY_COLUMNS[taxonomy],)
        )
        for column in needed:
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
    decreasing relative abundance, keeping first-seen order for ties.  ``tax``
    is aggregated by :func:`aggregate_sylphtax_rows`, which is sylph-tax's own
    rule (opaque tokens, ``UNKNOWN``, ``NO_TAXONOMY``).
    """
    if taxonomy == SYLPHTAX:
        return aggregate_sylphtax_rows(rows)
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


def hit_taxonomy_string(row: Mapping[str, Any]) -> str:
    """The lineage string sylph-tax builds for one hit.

    Resolved hits are ``<tax_lineage>;t__<tax_key>`` — the key is what
    sylph-tax looked up in its dictionary, so it is also the ``t__`` label.
    A hit without a taxonomy is ``NO_TAXONOMY;t__<genome file>:<contig>`` with
    sylph-tax's **pass-1** probe values.
    """
    lineage = row.get("tax_lineage")
    if isinstance(lineage, str):
        key = row.get("tax_key")
        return f"{lineage};t__{'' if key is None else key}"

    # Local import: keeps this module importable without sylphd's HTTP stack
    # (the golden scripts run with the sylph-tax venv, which lacks httpx).
    from .metadata import sylphtax_probes

    probes = sylphtax_probes(
        str(row.get("Genome_file") or ""), str(row.get("Contig_name") or "")
    )
    return probes.no_taxonomy


def aggregate_sylphtax_rows(rows: Iterable[Mapping[str, Any]]) -> list[CladeAbundance]:
    """Aggregate one sample exactly as ``sylph-tax taxprof`` does.

    Differs from :func:`aggregate_rows` on purpose, because sylph-tax does:

    * the lineage is opaque, split on ``;`` as stored; no rank prefixes are
      interpreted and nothing is dropped or filled in;
    * an **empty** token becomes ``UNKNOWN`` (a token such as ``o__`` is not
      empty and stays as it is);
    * the last level is ``t__<tax_key>`` (or the ``NO_TAXONOMY`` default);
    * a clade carries ANI and coverage when ``"t__"`` occurs anywhere in its
      path — sylph-tax's substring test, normally exactly the strain rows;
    * rows are ordered by number of levels, then by decreasing relative
      abundance (stable), so a short strain sorts before a deeper clade.
    """
    relative: dict[str, float] = {}
    sequence: dict[str, float] = {}
    strain_stats: dict[str, tuple[float | None, float | None]] = {}

    for row in rows:
        tax_str = hit_taxonomy_string(row)
        row_relative = _number(row.get("Taxonomic_abundance"))
        row_sequence = _number(row.get("Sequence_abundance"))
        coverage = row.get("Eff_cov")
        if coverage is None:
            coverage = row.get("True_cov")
        stats = (_optional_number(row.get("Adjusted_ANI")), _optional_number(coverage))

        clade = ""
        for token in tax_str.split(";"):
            token = token if token else UNKNOWN
            clade = token if not clade else f"{clade}|{token}"
            relative[clade] = relative.get(clade, 0.0) + row_relative
            sequence[clade] = sequence.get(clade, 0.0) + row_sequence
            if "t__" in clade:
                strain_stats[clade] = stats

    clades = [
        CladeAbundance(
            clade=clade,
            relative=value,
            sequence=sequence[clade],
            ani=strain_stats.get(clade, (None, None))[0],
            coverage=strain_stats.get(clade, (None, None))[1],
            strain_stats=clade in strain_stats,
        )
        for clade, value in relative.items()
    ]
    clades.sort(key=lambda item: (item.depth, -item.relative))
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
    name: str,
    taxonomy: str,
    clades: Sequence[CladeAbundance],
    *,
    taxonomies_used: Sequence[str] | None = None,
) -> Iterator[str]:
    """Yield one ``.sylphmpa`` document, as ``sylph-tax taxprof`` writes it.

    ``taxonomies_used`` is the list in the ``Taxonomies_used:`` comment.  For
    ``gtdb``/``ncbi`` it is the taxonomy name; for ``tax`` it is the names of
    the metadata file's sources, as sylph-tax prints the files it was given.
    """
    used = [taxonomy] if taxonomies_used is None else list(taxonomies_used)
    yield f"#SampleID\t{name}\tTaxonomies_used:{used!r}"
    yield "\t".join(TAXPROF_COLUMNS)
    for item in clades:
        if item.has_strain_stats:
            yield (
                f"{item.clade}\t{_cell(item.relative)}\t{_cell(item.sequence)}\t"
                f"{_cell(item.ani)}\t{_cell(item.coverage)}"
            )
        else:
            yield f"{item.clade}\t{_cell(item.relative)}\t{_cell(item.sequence)}\tNA\tNA"


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


def clade_dict(item: CladeAbundance) -> dict[str, Any]:
    """One clade as the ``/profile`` JSON carries it."""
    return {
        "clade_name": item.clade,
        "relative_abundance": item.relative,
        "sequence_abundance": item.sequence,
        "ani": item.ani,
        "coverage": item.coverage,
    }


def clades_from_dict(rows: Iterable[Mapping[str, Any]]) -> list[CladeAbundance]:
    """Rebuild :class:`CladeAbundance` values from the ``/profile`` JSON.

    Used by the CLI for ``--taxmatrix`` (and by tests): the server has already
    done the aggregation, so only the matrix rendering needs the objects.
    """
    return [
        CladeAbundance(
            clade=str(row["clade_name"]),
            relative=_number(row.get("relative_abundance")),
            sequence=_number(row.get("sequence_abundance")),
            ani=_optional_number(row.get("ani")),
            coverage=_optional_number(row.get("coverage")),
        )
        for row in rows
    ]


def taxprof_payload(
    name: str,
    taxonomy: str,
    clades: Sequence[CladeAbundance],
    *,
    taxonomies_used: Sequence[str] | None = None,
) -> dict[str, Any]:
    """One sample's profile for one taxonomy, as ``POST /profile`` returns it.

    Carries the structured clades (for charts and matrices) **and** the exact
    ``.sylphmpa`` text (so neither the CLI nor the web page formats anything).
    """
    used = [taxonomy] if taxonomies_used is None else list(taxonomies_used)
    text = "\n".join(
        taxprof_lines(name, taxonomy, clades, taxonomies_used=used)
    ) + "\n"
    return {
        "columns": list(TAXPROF_COLUMNS),
        "taxonomies_used": used,
        "clades": [clade_dict(item) for item in clades],
        "sylphmpa": text,
    }


def build_taxprof(
    name: str,
    taxonomies: Sequence[str],
    rows: Iterable[Mapping[str, Any]],
    *,
    tax_sources: Sequence[str] = (),
) -> dict[str, Any]:
    """Aggregate one sample's ``/profile`` rows for every requested taxonomy."""
    materialized = list(rows)
    payload: dict[str, Any] = {}
    for taxonomy in taxonomies:
        used = list(tax_sources) if taxonomy == SYLPHTAX else [taxonomy]
        payload[taxonomy] = taxprof_payload(
            name,
            taxonomy,
            aggregate_rows(materialized, taxonomy),
            taxonomies_used=used,
        )
    return payload


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
    tax_sources: Sequence[str] = (),
) -> list[Path]:
    """Write the requested taxonomy files for a profile response.

    Files are named ``{taxprof_prefix}{sample}.{taxonomy}.sylphmpa`` and
    ``{taxmatrix_prefix}{taxonomy}.tsv``; a ``None`` prefix skips that output.
    A prefix that is an existing directory is treated as ``directory/``.
    Returns the paths written, taxonomy by taxonomy (each taxonomy writes its
    per-sample files first, then its matrix).

    ``tax_sources`` names the taxonomy sources the ``tax`` profile was resolved
    against, in ``ord`` order; it becomes the ``Taxonomies_used:`` list of a
    ``tax`` ``.sylphmpa``, so it must be given when ``tax`` is written.
    """
    if taxprof_prefix is None and taxmatrix_prefix is None:
        return []
    if SYLPHTAX in taxonomies and not tax_sources:
        raise TaxonomyError(
            "the 'tax' profile needs the names of the taxonomy sources it was "
            "resolved against"
        )

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
        # The server aggregates (`/profile` `taxprof`): the CLI must not fold rows itself.
        payloads = [(sample_name(sample), sample["taxprof"][taxonomy]) for sample in samples]
        if taxprof_prefix is not None:
            for name, payload in payloads:
                path = taxprof_path(taxprof_prefix, name, taxonomy)
                with open(path, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(payload["sylphmpa"])
                written.append(path)
        table = [(name, clades_from_dict(payload["clades"])) for name, payload in payloads]
        if taxmatrix_prefix is not None:
            written.append(
                write_taxmatrix(
                    taxmatrix_path(taxmatrix_prefix, taxonomy), table, matrix_value
                )
            )
    return written

