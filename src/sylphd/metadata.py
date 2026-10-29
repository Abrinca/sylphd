"""GTDB metadata and sylph-tax taxonomy sources: build, lookup, and update.

The metadata database keeps every GTDB genome (not only species
representatives), keyed by NCBI accession with the ``RS_``/``GB_`` prefix
stripped.  Repetitive text columns are dictionary-encoded into
``dict_<column>`` tables; the base table (``metadata_data``, ``WITHOUT ROWID``,
keyed by accession) stores integer ids and a view named ``metadata`` exposes
the original columns.  Taxonomy strings are stored as one column each; no
per-rank split happens here.

Format version 3 adds the ``taxonomy_*`` tables: the GTDB metadata is a
*virtual* source (its entries are ``metadata_data.accession`` and
``gtdb_taxonomy``, so no lineage is stored twice) and every imported sylph-tax
file is one ``taxonomy_source`` with its ``key -> lineage [-> host]`` rows in
``taxonomy_entry``.  ``taxonomy_overlap`` records, per pair of sources, how
many keys both have: importing overlapping sources is allowed (the one
imported later wins, as in sylph-tax) but never silent.  Version 2 files keep
loading and serving; a version 3 file still opens with a version 2 reader,
which ignores the new tables.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import logging
import os
import re
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Self, cast

from . import __version__
from .download import SYLPHTAX_VERSION

logger = logging.getLogger(__name__)
# A plain library warning would vanish in a process that configures no logging
# (``sylphd setup`` does not), and Python's ``lastResort`` handler would then
# print it *and* the explicit stderr copy below.  The null handler keeps the
# record silent for a reader and leaves the CLI's own handler working.
logger.addHandler(logging.NullHandler())

#: Prefix of the CLI's messages (``sylphd: error: ...``), reused for warnings
#: that must reach the terminal even when no logging is configured.
_PROG = "sylphd"

#: Schema version of the generated SQLite file.  Bump when the column layout
#: or the meaning of the ``info`` table changes.
METADATA_FORMAT_VERSION = "3"

#: Format versions this reader understands.  Version 2 has no taxonomy
#: sources; version 3 adds the ``taxonomy_*`` tables (a superset of 2).
READABLE_FORMAT_VERSIONS = ("2", "3")

#: Page size of the generated file.  8 KiB trims the b-tree page slack of the
#: dictionary-encoded layout (~2 % smaller, see scratch/sqlite-size/REPORT.md);
#: larger pages buy <1 % more and cost lookup time.
METADATA_PAGE_SIZE = 8192

#: Null markers used by GTDB for "no value".  Compared case-insensitively
#: after stripping whitespace.  ``not type material`` and friends are *values*.
NULL_MARKERS = frozenset({"", "none", "na"})


@dataclass(frozen=True)
class Column:
    """One column of the metadata table."""

    name: str
    kind: str  # "text" | "int" | "float" | "bool"

    @property
    def sql_type(self) -> str:
        return {
            "text": "TEXT",
            "int": "INTEGER",
            "float": "REAL",
            "bool": "INTEGER",
        }[self.kind]


#: Metadata columns kept from the GTDB files, in output order.  The accession
#: column comes first in the table and is handled separately.
COLUMNS: tuple[Column, ...] = (
    Column("gtdb_taxonomy", "text"),
    Column("gtdb_representative", "bool"),
    Column("gtdb_genome_representative", "text"),
    Column("gtdb_type_species_of_genus", "bool"),
    Column("gtdb_type_designation_ncbi_taxa", "text"),
    Column("ncbi_taxid", "int"),
    Column("ncbi_species_taxid", "int"),
    Column("ncbi_taxonomy", "text"),
    Column("ncbi_taxonomy_unfiltered", "text"),
    Column("ncbi_organism_name", "text"),
    Column("ncbi_strain_identifiers", "text"),
    Column("ncbi_isolate", "text"),
    Column("ncbi_genome_category", "text"),
    Column("ncbi_assembly_level", "text"),
    Column("ncbi_refseq_category", "text"),
    Column("ncbi_type_material_designation", "text"),
    Column("ncbi_biosample", "text"),
    Column("ncbi_bioproject", "text"),
    Column("checkm2_completeness", "float"),
    Column("checkm2_contamination", "float"),
    Column("genome_size", "int"),
    Column("gc_percentage", "float"),
    Column("contig_count", "int"),
    Column("n50_contigs", "int"),
    Column("mimag_high_quality", "bool"),
)

#: Names of the metadata columns in output order.
METADATA_COLUMNS: tuple[str, ...] = tuple(column.name for column in COLUMNS)

#: Repetitive text columns stored as integer ids into ``dict_<column>`` tables.
#: These are the long/low-cardinality strings measured in
#: scratch/sqlite-size/REPORT.md; they account for most of the file size.
DICT_ENCODED_COLUMNS: tuple[str, ...] = (
    "gtdb_taxonomy",
    "gtdb_genome_representative",
    "ncbi_taxonomy",
    "ncbi_taxonomy_unfiltered",
    "ncbi_organism_name",
    "ncbi_genome_category",
    "ncbi_assembly_level",
    "ncbi_refseq_category",
    "ncbi_type_material_designation",
    "ncbi_bioproject",
)

#: ``(column position in COLUMNS, column name)`` for dictionary-encoded columns.
_DICT_POSITIONS: tuple[tuple[int, str], ...] = tuple(
    (position, column.name)
    for position, column in enumerate(COLUMNS)
    if column.name in DICT_ENCODED_COLUMNS
)

_ACCESSION_PREFIX_RE = re.compile(r"^(?:RS_|GB_)", re.IGNORECASE)
#: ``GCA_964372435.1`` / ``GCF_000005845.2`` optionally with an ``RS_``/``GB_``
#: prefix.  Used to recover the accession from an arbitrary genome file name.
_ACCESSION_RE = re.compile(r"^(?:RS_|GB_)?(GC[AF]_\d+(?:\.\d+)?)", re.IGNORECASE)
_COMPRESSION_SUFFIXES = (".gz", ".bz2", ".xz", ".zst", ".zstd", ".bgz", ".bgzf")
_SEQUENCE_SUFFIXES = (
    ".fna",
    ".fa",
    ".fasta",
    ".fas",
    ".fsa",
    ".fnn",
    ".faa",
    ".ffn",
)


def normalize_accession(value: str) -> str:
    """Strip the GTDB ``RS_``/``GB_`` prefix and normalise case.

    >>> normalize_accession("RS_GCF_000005845.2")
    'GCF_000005845.2'
    >>> normalize_accession(" gb_gca_964372435.1 ")
    'GCA_964372435.1'
    """
    accession = _ACCESSION_PREFIX_RE.sub("", value.strip())
    return accession.upper()


def public_origin(origin: str | None) -> str | None:
    """The ``origin`` of a taxonomy source as ``/info`` may publish it.

    A catalogue download keeps the URL it came from; anything that is a local
    path is reduced to its base name, so a public ``/info`` never leaks the
    server's directory layout.  The full origin stays in the metadata file,
    and setup logs the paths it read.
    """
    if not origin:
        return None
    if origin.startswith(("http://", "https://")):
        return origin
    return os.path.basename(origin.replace("\\", "/")) or None


def accession_from_genome_name(name: str) -> str | None:
    """Map a sylph genome file name to a GTDB accession.

    Handles paths, compression suffixes, ``_genomic`` suffixes and
    ``_genomic.fna.gz`` style names, as well as NCBI-style names that carry the
    assembly name after the accession::

        /db/GCA_964372435.1_genomic.fna.gz -> GCA_964372435.1
        GCF_000005845.2_ASM584v2_genomic.fna.gz -> GCF_000005845.2

    Returns ``None`` when no accession can be found.
    """
    base = os.path.basename(name.replace("\\", "/")).strip()
    lowered = base.lower()
    for suffix in _COMPRESSION_SUFFIXES:
        if lowered.endswith(suffix):
            base = base[: -len(suffix)]
            lowered = lowered[: -len(suffix)]
            break
    for suffix in _SEQUENCE_SUFFIXES:
        if lowered.endswith(suffix):
            base = base[: -len(suffix)]
            lowered = lowered[: -len(suffix)]
            break
    if lowered.endswith("_genomic"):
        base = base[: -len("_genomic")]
    match = _ACCESSION_RE.match(base)
    if match is not None:
        return normalize_accession(match.group(1))
    return None


# ---------------------------------------------------------------------------
# sylph-tax probing: hit -> key
# ---------------------------------------------------------------------------

#: The order in which sylph-tax tries the probes of one pass, after the four
#: ``gf``/``gf + ".gz"``/``glob``/``contig`` probes: the suffix splits.
_PROBE_SPLITS: tuple[str, ...] = (".fa", ".fasta", ".fna")


def _sylphtax_gf_pass0(file_name: str) -> str:
    """Pass 0's ``gf`` probe: the basename, cut at ``_ASM`` or ``_genomic``.

    ``sylph_to_taxprof.genome_file_to_gcf_acc``: the ``"ASM"`` test looks at
    the **whole path**, the split at the basename.  A path with ``ASM`` in a
    directory therefore cuts the basename at ``_ASM`` even when the basename
    has no such part (the split then returns the basename unchanged).
    """
    if "ASM" in file_name:
        return file_name.split("/")[-1].split("_ASM")[0]
    return file_name.split("/")[-1].split("_genomic")[0]


def _probe_keys(gf: str, glob: str, contig: str) -> tuple[str, ...]:
    """One pass's probes in sylph-tax's order (``gf``, ``.gz``, ``glob``, ...)."""
    keys = [gf, gf + ".gz", glob, contig]
    keys.extend(gf.split(suffix)[0] for suffix in _PROBE_SPLITS)
    return tuple(keys)


@dataclass(frozen=True)
class SylphTaxProbes:
    """The keys sylph-tax probes for one hit, in its order and precedence.

    ``pass0`` is tried first, then ``pass1``; a pass-1 match **overrides** a
    pass-0 match (``sylph_to_taxprof.main`` has no ``break``).  ``genome_file``
    and ``contig_name`` are the pass-1 values, which sylph-tax uses to name an
    unresolved hit (``NO_TAXONOMY;t__<genome_file>:<contig_name>``).
    """

    pass0: tuple[str, ...]
    pass1: tuple[str, ...]
    genome_file: str
    contig_name: str

    def probe_keys(self) -> tuple[str, ...]:
        """Every probe key, both passes, without repeats (for one lookup)."""
        return tuple(dict.fromkeys((*self.pass0, *self.pass1)))

    @property
    def no_taxonomy(self) -> str:
        """sylph-tax's default lineage for a hit without any taxonomy."""
        return f"NO_TAXONOMY;t__{self.genome_file}:{self.contig_name}"


def sylphtax_probes(genome_file: str, contig_name: str) -> SylphTaxProbes:
    """The probe keys of one hit, exactly as ``sylph-to-taxprof`` builds them.

    Replicates ``sylph_to_taxprof.py`` (v1.9.2) rule for rule::

        pass 0  gf     basename, split at "_ASM" if "ASM" is in the full path,
                       else at "_genomic"
                glob   basename.split(".")[0]
                contig Contig_name.split(" ")[0].split("|")[0]   (space only)
        pass 1  gf     basename
                glob   gf
                contig Contig_name.split()[0]                   (any whitespace)

    and per pass the probes ``gf``, ``gf + ".gz"``, ``glob``, ``contig``, then
    ``gf.split(".fa")[0]``, ``gf.split(".fasta")[0]`` and
    ``gf.split(".fna")[0]``.  Keys are **not** normalised: sylph-tax stores
    them verbatim, so ``GCF_010093815.1_Psehy1`` and ``GCF_010093815.1`` are
    different keys.  The rules are verified against the real sylph-tax in
    ``tests/test_sylphtax_parity.py``.

    >>> probes = sylphtax_probes(
    ...     "gtdb_reps/database/GCA/047/199/115/GCA_047199115.1_genomic.fna.gz",
    ...     "GCA_047199115.1 contig 1",
    ... )
    >>> probes.pass0[0], probes.pass0[2]
    ('GCA_047199115.1', 'GCA_047199115')
    >>> sylphtax_probes("votus_full.fna.gz", "UHGV-0320751 species=vOTU-125334").pass1
    ('votus_full.fna.gz', 'votus_full.fna.gz.gz', 'votus_full.fna.gz',
     'UHGV-0320751', 'votus_full.fna.gz', 'votus_full.fna.gz',
     'votus_full')
    """
    basename = genome_file.split("/")[-1]
    gf0 = _sylphtax_gf_pass0(genome_file)
    contig0 = contig_name.split(" ")[0].split("|")[0]
    glob0 = basename.split(".")[0]

    # ``str.split()`` drops leading whitespace and returns [] for an empty or
    # blank name, where sylph-tax would raise IndexError; "no contig name" is a
    # miss, not a crash.
    words = contig_name.split()
    contig1 = words[0] if words else ""

    return SylphTaxProbes(
        pass0=_probe_keys(gf0, glob0, contig0),
        pass1=_probe_keys(basename, basename, contig1),
        genome_file=basename,
        contig_name=contig1,
    )


#: The columns a resolved hit carries (format 3).  They are *virtual*: they are
#: not in the ``metadata`` view, ``resolve`` fills them.
HIT_TAXONOMY_COLUMNS: tuple[str, ...] = (
    "tax_source",
    "tax_key",
    "tax_lineage",
    "tax_host",
)


@dataclass(frozen=True)
class HitTaxonomy:
    """The taxonomy ``resolve`` found for one hit (all ``None`` for a miss)."""

    source: str | None = None
    key: str | None = None
    lineage: str | None = None
    host: str | None = None

    def columns(self) -> dict[str, str | None]:
        """The four ``tax_*`` values, keyed like the metadata columns."""
        return {
            "tax_source": self.source,
            "tax_key": self.key,
            "tax_lineage": self.lineage,
            "tax_host": self.host,
        }


#: What ``resolve`` returns for a hit without any taxonomy.
NO_HIT_TAXONOMY = HitTaxonomy()


def _parse_value(raw: str, kind: str) -> object:
    """Convert a raw TSV cell to the SQLite representation."""
    value = raw.strip()
    if value.lower() in NULL_MARKERS:
        return None
    if kind == "text":
        return value
    if kind == "int":
        try:
            return int(value)
        except ValueError:
            return None
    if kind == "float":
        try:
            return float(value)
        except ValueError:
            return None
    if kind == "bool":
        lowered = value.lower()
        if lowered in {"t", "true", "1", "yes"}:
            return 1
        if lowered in {"f", "false", "0", "no"}:
            return 0
        return None
    raise ValueError(f"unknown column kind {kind!r}")


def open_text(path: str | os.PathLike[str]) -> IO[str]:
    """Open a possibly gzip-compressed TSV file for text reading."""
    with open(path, "rb") as handle:
        magic = handle.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rt", newline="", encoding="utf-8")
    return open(path, "rt", newline="", encoding="utf-8")


def _base_table_sql() -> str:
    """DDL for the base table.

    ``WITHOUT ROWID`` is only a win once the rows are dictionary-encoded: the
    accession becomes the b-tree key, saving the separate primary-key index.
    """
    columns = ",\n    ".join(
        ["accession TEXT PRIMARY KEY"]
        + [
            f"{column.name} "
            f"{'INTEGER' if column.name in DICT_ENCODED_COLUMNS else column.sql_type}"
            for column in COLUMNS
        ]
    )
    return f"CREATE TABLE metadata_data (\n    {columns}\n) WITHOUT ROWID"


def _dict_table_sql(name: str) -> str:
    # Values are unique by construction: ids are assigned from a dict of the
    # distinct strings seen in the input.  A ``UNIQUE`` declaration would add a
    # second b-tree that stores every string again (~67 MiB on r232), so it is
    # deliberately left out; ``NOT NULL`` documents that ids are never NULL.
    return f"CREATE TABLE dict_{name} (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"


def _view_sql() -> str:
    """DDL for the ``metadata`` view: the original columns in the original order.

    A missing id (``NULL`` cell) joins to no dictionary row, so the view yields
    ``NULL`` for it.
    """
    select = ["m.accession AS accession"]
    joins: list[str] = []
    for column in COLUMNS:
        if column.name in DICT_ENCODED_COLUMNS:
            select.append(f'dict_{column.name}.value AS "{column.name}"')
            joins.append(
                f"LEFT JOIN dict_{column.name} "
                f'ON dict_{column.name}.id = m."{column.name}"'
            )
        else:
            select.append(f'm."{column.name}" AS "{column.name}"')
    return (
        "CREATE VIEW metadata AS\nSELECT "
        + ",\n       ".join(select)
        + "\nFROM metadata_data AS m\n"
        + "\n".join(joins)
    )


#: Kinds of a taxonomy source.  ``metadata`` is the virtual GTDB source over
#: ``metadata_data``; ``sylphtax`` is one imported file.
TAXONOMY_KINDS = ("metadata", "sylphtax")


def _taxonomy_schema_sql() -> tuple[str, ...]:
    """DDL for the format 3 taxonomy tables (in the order they are created).

    ``taxonomy_entry`` maps a source-local key to dictionary ids; the entry
    index on ``key`` is what phase 3's batched ``IN`` lookups use, because the
    primary key starts with ``source_id``.  ``taxonomy_overlap`` stores, for
    every pair of sources, how many keys they share; importing a source writes
    both directions, so any reader finds the counts of either source.
    """
    return (
        (
            "CREATE TABLE taxonomy_source (\n"
            "    id INTEGER PRIMARY KEY,\n"
            "    ord INTEGER NOT NULL,\n"
            "    name TEXT NOT NULL UNIQUE,\n"
            "    kind TEXT NOT NULL,\n"
            "    origin TEXT,\n"
            "    md5 TEXT,\n"
            "    rows INTEGER,\n"
            "    has_host INTEGER NOT NULL DEFAULT 0,\n"
            "    built TEXT\n"
            ")"
        ),
        (
            "CREATE TABLE taxonomy_entry (\n"
            "    source_id INTEGER NOT NULL,\n"
            "    key TEXT NOT NULL,\n"
            "    lineage INTEGER,\n"
            "    host INTEGER,\n"
            "    PRIMARY KEY (source_id, key)\n"
            ") WITHOUT ROWID"
        ),
        "CREATE INDEX taxonomy_entry_key ON taxonomy_entry (key)",
        "CREATE TABLE dict_tax_lineage (id INTEGER PRIMARY KEY, value TEXT NOT NULL)",
        "CREATE TABLE dict_tax_host (id INTEGER PRIMARY KEY, value TEXT NOT NULL)",
        _OVERLAP_TABLE_SQL,
    )


#: ``taxonomy_overlap``: ``(source, other) -> shared keys``.  A table of its
#: own rather than a column so that a version 3 file written before it existed
#: still reads, and gets it filled by the next ``--update``.
_OVERLAP_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS taxonomy_overlap (\n"
    "    source_id INTEGER NOT NULL,\n"
    "    other_id INTEGER NOT NULL,\n"
    "    keys INTEGER NOT NULL,\n"
    "    PRIMARY KEY (source_id, other_id)\n"
    ") WITHOUT ROWID"
)


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_base_table_sql())
    for name in DICT_ENCODED_COLUMNS:
        connection.execute(_dict_table_sql(name))
    connection.execute("CREATE TABLE info (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute(_view_sql())
    for statement in _taxonomy_schema_sql():
        connection.execute(statement)


def _iter_rows(path: Path) -> Iterator[tuple[str, list[object]]]:
    """Yield ``(accession, values)`` for each data row of a GTDB TSV file."""
    with open_text(path) as handle:
        reader = csv.reader(handle, delimiter="\t", quoting=csv.QUOTE_NONE)
        try:
            header = next(reader)
        except StopIteration:
            return
        header = [column.strip() for column in header]
        index = {name: position for position, name in enumerate(header)}
        missing = [name for name in ("accession",) + METADATA_COLUMNS if name not in index]
        if missing:
            raise ValueError(
                f"{path}: not a GTDB metadata file, missing column(s): "
                + ", ".join(missing)
            )
        try:
            for row in reader:
                if not row or (len(row) == 1 and not row[0].strip()):
                    continue
                if len(row) < len(header):
                    row = row + [""] * (len(header) - len(row))
                accession = normalize_accession(row[index["accession"]])
                if not accession:
                    continue
                values = [
                    _parse_value(row[index[column.name]], column.kind)
                    for column in COLUMNS
                ]
                yield accession, values
        except csv.Error as error:
            raise ValueError(f"{path}: line {reader.line_num}: {error}") from error


# ---------------------------------------------------------------------------
# taxonomy sources (format 3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaxonomySource:
    """One taxonomy file to import: ``key<TAB>lineage[<TAB>host]`` per line.

    ``name`` is the source name stored in the file (a sylph-tax name, or the
    file stem), ``origin`` where the bytes came from (a path or a URL; only
    informational).  ``from_catalogue`` says whether ``name`` came from the
    sylph-tax catalogue (``sylphd setup --taxonomy UHGV_default``) rather than
    from a local file: only a catalogue ``GTDB_r<release>`` name is the
    redundant duplicate of the virtual GTDB source (see
    ``is_redundant_gtdb_source``).
    """

    name: str
    path: Path
    origin: str | None = None
    from_catalogue: bool = False


@dataclass(frozen=True)
class TaxonomySourceRow:
    """One row of ``taxonomy_source`` as read by :class:`MetadataDB`."""

    id: int
    ord: int
    name: str
    kind: str
    origin: str | None
    md5: str | None
    rows: int | None
    has_host: bool


def iter_taxonomy_rows(
    path: str | os.PathLike[str],
) -> Iterator[tuple[str, str, str | None]]:
    """Yield ``(key, lineage, host)`` for each row of a sylph-tax file.

    Parsing is sylph-tax's (``sylph_to_taxprof.py``): ``line.rstrip().split("\\t")``
    with the key verbatim (case sensitive, not normalised), the lineage opaque
    and an optional third column as the host.  A header row, as IMG/VR has, is
    simply an entry whose key never matches, and an **empty key** is stored as
    sylph-tax stores it (only ``spl[1]`` is required there).

    A blank line or a line without a tab is an error, as in sylph-tax (where
    ``spl[1]`` raises ``IndexError``); here it is a ``ValueError`` naming the
    file and the line, so a broken source fails the build instead of being
    half-imported.  Because ``rstrip()`` happens before the split, a trailing
    tab (an empty third column) is *no* host column: the earlier host of that
    key is kept, exactly as in sylph-tax.
    """
    name = os.fspath(path)
    with open_text(path) as handle:
        for number, line in enumerate(handle, start=1):
            parts = line.rstrip().split("\t")
            if len(parts) < 2:
                if not line.strip():
                    raise ValueError(f"{name}: line {number}: blank line")
                raise ValueError(
                    f"{name}: line {number}: expected 'key<TAB>lineage[<TAB>host]', "
                    "found no tab"
                )
            host = parts[2].rstrip() if len(parts) > 2 else None
            yield parts[0], parts[1].rstrip(), host


def _md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _Dictionary:
    """``value -> id`` for one ``dict_tax_*`` table, continuing an existing one."""

    def __init__(self, values: Sequence[str] = ()) -> None:
        self.values: list[str] = list(values)
        self.ids: dict[str, int] = {
            value: position for position, value in enumerate(self.values, start=1)
        }
        self.stored = len(self.values)

    def encode(self, value: str) -> int:
        identifier = self.ids.get(value)
        if identifier is None:
            identifier = len(self.values) + 1
            self.ids[value] = identifier
            self.values.append(value)
        return identifier

    def flush(self, connection: sqlite3.Connection, table: str) -> None:
        """Insert the values added since the last flush (readers see them at commit)."""
        connection.executemany(
            f"INSERT INTO {table} (id, value) VALUES (?, ?)",
            enumerate(self.values[self.stored :], start=self.stored + 1),
        )
        self.stored = len(self.values)


def _load_dictionary(connection: sqlite3.Connection, table: str) -> _Dictionary:
    return _Dictionary(
        [row[0] for row in connection.execute(f"SELECT value FROM {table} ORDER BY id")]
    )


def _next_ord(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT MAX(ord) FROM taxonomy_source").fetchone()
    return 0 if row is None or row[0] is None else int(row[0]) + 1


def _insert_source_row(
    connection: sqlite3.Connection,
    *,
    name: str,
    kind: str,
    ord_value: int,
    origin: str | None,
    md5: str | None,
    rows: int | None,
    has_host: bool,
    built: str,
) -> int:
    cursor = connection.execute(
        "INSERT INTO taxonomy_source "
        "(ord, name, kind, origin, md5, rows, has_host, built) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ord_value, name, kind, origin, md5, rows, 1 if has_host else 0, built),
    )
    return int(cursor.lastrowid)


def _add_taxonomy_source(
    connection: sqlite3.Connection,
    source: TaxonomySource,
    *,
    ord_value: int,
    lineages: _Dictionary,
    hosts: _Dictionary,
    progress: object = None,
) -> dict[str, object]:
    """Insert one sylph-tax file as a source, replacing a source of the same name.

    The returned mapping describes the source and carries the list of
    :class:`TaxonomyOverlap` records for the keys it shares with the sources
    already in the file (empty when it is disjoint).
    """
    path = Path(source.path)
    if not path.is_file():
        raise FileNotFoundError(f"taxonomy file not found: {path}")
    existing = connection.execute(
        "SELECT id, ord, kind FROM taxonomy_source WHERE name = ?", (source.name,)
    ).fetchone()
    if existing is not None and existing[2] == "metadata":
        raise ValueError(
            f"taxonomy source {source.name!r} is the name of the virtual GTDB "
            "source of this file; give the local file another name "
            "(NAME=PATH, e.g. GTDB_local=...)"
        )
    if existing is not None:
        # Replacing a source keeps its place in the resolution order.
        ord_value = int(existing[1])
        connection.execute(
            "DELETE FROM taxonomy_entry WHERE source_id = ?", (existing[0],)
        )
        connection.execute(
            "DELETE FROM taxonomy_overlap WHERE source_id = ? OR other_id = ?",
            (existing[0], existing[0]),
        )
        connection.execute("DELETE FROM taxonomy_source WHERE id = ?", (existing[0],))
    built = datetime.now(UTC).isoformat(timespec="seconds")
    source_id = _insert_source_row(
        connection,
        name=source.name,
        kind="sylphtax",
        ord_value=ord_value,
        origin=source.origin or str(path),
        md5=_md5_file(path),
        rows=None,
        has_host=False,
        built=built,
    )

    callback = progress if callable(progress) else None
    rows = 0
    has_host = False
    batch: list[tuple[int, str, int, int | None]] = []
    # Duplicate keys inside this source: the upsert of _insert_taxonomy_batch
    # reproduces sylph-tax's dict semantics (the last lineage wins; the host is
    # only replaced by a row that has one).
    # Across sources sylph-tax behaves differently, and phase 3 must copy it:
    # it reads every file into *one* pair of dicts (genome_to_taxonomy and
    # genome_to_additional_data are created before the file loop), so a key in
    # several files takes its lineage from the last file and its host from the
    # last file *that had a host column* — possibly two different files.  Here
    # each source keeps its own row, so resolution must pick the lineage and
    # the host by `ord` independently (see docs/project/multi-db-design.md B3).
    for key, lineage, host in iter_taxonomy_rows(path):
        host_id = hosts.encode(host) if host is not None else None
        has_host = has_host or host is not None
        batch.append((source_id, key, lineages.encode(lineage), host_id))
        rows += 1
        if len(batch) >= 20_000:
            _insert_taxonomy_batch(connection, batch)
            batch.clear()
            if callback is not None:
                callback(rows, path)
    if batch:
        _insert_taxonomy_batch(connection, batch)

    # ``rows`` counts rows read; duplicate keys within a source collapse, so
    # report the stored count.
    stored = connection.execute(
        "SELECT COUNT(*) FROM taxonomy_entry WHERE source_id = ?", (source_id,)
    ).fetchone()[0]
    connection.execute(
        "UPDATE taxonomy_source SET rows = ?, has_host = ? WHERE id = ?",
        (stored, 1 if has_host else 0, source_id),
    )
    if callback is not None:
        callback(rows, path)
    # Importing overlapping files is allowed but must not pass silently: count
    # the keys this source shares with the others (the virtual GTDB source
    # included) and record them for `info`.
    overlaps = _record_overlaps(
        connection, source_id=source_id, name=source.name, ord_value=ord_value
    )
    return {
        "name": source.name,
        "kind": "sylphtax",
        "origin": source.origin or str(path),
        "md5": connection.execute(
            "SELECT md5 FROM taxonomy_source WHERE id = ?", (source_id,)
        ).fetchone()[0],
        "rows": int(stored),
        "has_host": has_host,
        "ord": ord_value,
        "overlaps": overlaps,
    }


def _insert_taxonomy_batch(
    connection: sqlite3.Connection, batch: list[tuple[int, str, int, int | None]]
) -> None:
    """Store one batch of entries; a repeated key keeps the first host it had.

    sylph-tax keeps two dicts while reading (``sylph_to_taxprof.py``): the
    lineage is assigned on every row, so the last row of a key wins, while the
    host is assigned only when the row *has* a third column
    (``if len(spl) > 2``).  A later row without a host therefore leaves the
    earlier host of that key in place — hence the upsert below updates
    ``lineage`` always but ``host`` only when the new row carries one.  Within
    a batch (the 20,000-row boundary is an implementation detail) and across
    batches SQLite applies the statements in file order, so this matches
    sylph-tax's single dict pass exactly.
    """
    connection.executemany(
        "INSERT INTO taxonomy_entry (source_id, key, lineage, host) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (source_id, key) DO UPDATE SET "
        "lineage = excluded.lineage, "
        "host = COALESCE(excluded.host, taxonomy_entry.host)",
        batch,
    )


#: How many example keys a warning lists.
OVERLAP_EXAMPLES = 5


@dataclass(frozen=True)
class TaxonomyOverlapPair:
    """One pair of sources that share keys, as ``/info`` reports it.

    ``sources`` is in ``ord`` order (the later one wins for these keys) and the
    pair appears once, even though ``taxonomy_overlap`` stores both directions.
    """

    sources: tuple[str, str]
    keys: int
    examples: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "sources": list(self.sources),
            "keys": self.keys,
            "examples": list(self.examples),
        }


@dataclass(frozen=True)
class TaxonomyOverlap:
    """Keys that one imported source shares with another source.

    Importing overlapping sources is allowed (the source imported later wins,
    as in sylph-tax) but must never be silent, so one of these is produced and
    warned about per pair of sources.
    """

    source: str
    """The source just imported."""

    other: str
    """The source it collides with (another import or the virtual GTDB source)."""

    keys: int
    """Number of keys both contain."""

    examples: tuple[str, ...]
    """The first ``OVERLAP_EXAMPLES`` shared keys, sorted."""

    later: str
    """The name whose lineages win: the source with the larger ``ord``."""

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "other": self.other,
            "keys": self.keys,
            "examples": list(self.examples),
            "later": self.later,
        }

    def message(self) -> str:
        """The warning text: both names, the count, examples, and what happens."""
        examples = ", ".join(self.examples)
        return (
            f"taxonomy sources {self.other!r} and {self.source!r} share "
            f"{self.keys:,} key(s) (examples: {examples}): the source imported "
            f"later ({self.later}) wins for these keys; import only one of "
            "them, or rebuild without one"
        )


def _record_overlaps(
    connection: sqlite3.Connection, *, source_id: int, name: str, ord_value: int
) -> list[TaxonomyOverlap]:
    """Count the keys ``source_id`` shares with every other source; store them.

    The keys of an imported source are ``taxonomy_entry.key`` verbatim; the
    virtual GTDB source's keys are ``metadata_data.accession``, which is what
    :meth:`MetadataDB.resolve` looks up for it (only rows with a lineage), so
    both are counted in the form resolution uses.  The work is done in SQL over
    the ``taxonomy_entry(key)`` index after the insert, so a 170k-key file costs
    one index pass per source instead of a Python set of every key.
    """
    started = time.perf_counter()
    connection.execute("DROP TABLE IF EXISTS temp._overlap_keys")
    connection.execute(
        "CREATE TEMP TABLE _overlap_keys (key TEXT PRIMARY KEY) WITHOUT ROWID"
    )
    try:
        connection.execute(
            "INSERT OR IGNORE INTO _overlap_keys (key) "
            "SELECT key FROM taxonomy_entry WHERE source_id = ?",
            (source_id,),
        )
        counts: dict[int, int] = dict(
            connection.execute(
                "SELECT e.source_id, COUNT(*) FROM taxonomy_entry AS e "
                "JOIN _overlap_keys AS k ON k.key = e.key "
                "WHERE e.source_id <> ? GROUP BY e.source_id",
                (source_id,),
            ).fetchall()
        )
        # One row per virtual source (there is at most one in a file).
        counts.update(
            dict(
                connection.execute(
                    "SELECT s.id, COUNT(*) FROM metadata_data AS m "
                    "JOIN _overlap_keys AS k ON k.key = m.accession "
                    "JOIN taxonomy_source AS s ON s.kind = 'metadata' "
                    "WHERE m.gtdb_taxonomy IS NOT NULL GROUP BY s.id"
                ).fetchall()
            )
        )
        others = {
            int(row[0]): (str(row[1]), int(row[2]), str(row[3]))
            for row in connection.execute(
                "SELECT id, name, ord, kind FROM taxonomy_source"
            )
        }
        overlaps: list[TaxonomyOverlap] = []
        for other_id, keys in sorted(
            (item for item in counts.items() if item[1]),
            key=lambda item: others[item[0]][1],
        ):
            if other_id == source_id:
                continue
            other_name, other_ord, other_kind = others[other_id]
            if other_kind == "metadata":
                examples = [
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT k.key FROM _overlap_keys AS k "
                        "JOIN metadata_data AS m ON m.accession = k.key "
                        "WHERE m.gtdb_taxonomy IS NOT NULL "
                        "ORDER BY k.key LIMIT ?",
                        (OVERLAP_EXAMPLES,),
                    )
                ]
            else:
                examples = [
                    row[0]
                    for row in connection.execute(
                        "SELECT key FROM taxonomy_entry "
                        "WHERE source_id = ? AND key IN (SELECT key FROM _overlap_keys) "
                        "ORDER BY key LIMIT ?",
                        (other_id, OVERLAP_EXAMPLES),
                    )
                ]
            connection.executemany(
                "INSERT OR REPLACE INTO taxonomy_overlap (source_id, other_id, keys) "
                "VALUES (?, ?, ?)",
                [(source_id, other_id, keys), (other_id, source_id, keys)],
            )
            overlaps.append(
                TaxonomyOverlap(
                    source=name,
                    other=other_name,
                    keys=int(keys),
                    examples=tuple(str(example) for example in examples),
                    later=name if ord_value >= other_ord else other_name,
                )
            )
    finally:
        connection.execute("DROP TABLE IF EXISTS temp._overlap_keys")
    logger.debug(
        "taxonomy overlap check for %s: %.2f s", name, time.perf_counter() - started
    )
    return overlaps


def _backfill_overlaps(connection: sqlite3.Connection) -> None:
    """Fill ``taxonomy_overlap`` for the sources of a file that lacks the table.

    Files written before the table existed (all version 3 files of the first
    release) have the entries but no counts; ``--update`` adds the table, so it
    recomputes the pairs once.  Only the counts are recorded: those sources are
    not being imported now, so nothing is warned about.
    """
    connection.execute("DELETE FROM taxonomy_overlap")
    for source_id, name, ord_value in connection.execute(
        "SELECT id, name, ord FROM taxonomy_source WHERE kind = 'sylphtax' ORDER BY ord"
    ).fetchall():
        _record_overlaps(
            connection,
            source_id=int(source_id),
            name=str(name),
            ord_value=int(ord_value),
        )


def _report_taxonomy_overlaps(overlaps: Sequence[TaxonomyOverlap]) -> None:
    """Warn about every pair of sources that share keys (logger *and* stderr).

    ``setup`` configures no logging, and a warning about conflicting taxonomy
    files is exactly what the owner must see, so it is written to stderr too
    (``-q`` silences progress, not conflicts).  One warning per pair.
    """
    for overlap in overlaps:
        message = overlap.message()
        logger.warning(message)
        print(f"{_PROG}: warning: {message}", file=sys.stderr)


def is_redundant_gtdb_source(source: TaxonomySource, release: str | None) -> bool:
    """True when ``source`` merely repeats the virtual ``-m`` source.

    Only the **catalogue** name ``GTDB_r<release>`` of the *same* release is
    redundant: that file holds the species representatives of the release whose
    full metadata ``-m`` already stored (as the virtual source over
    ``metadata_data``), so importing it would store every lineage twice.

    Everything else is imported normally: a local file (``NAME=PATH`` or
    ``PATH``), a custom name that merely starts with ``GTDB_r``, and a
    catalogue file for a *different* release (``GTDB_r214`` next to r232
    metadata is a different taxonomy).  A local file whose name is exactly the
    virtual source's is not skipped here but refused by ``_add_taxonomy_source``
    (importing it under that name would replace the virtual source).  Without
    ``--release`` the virtual source is called ``GTDB``, so no catalogue name
    can be matched and nothing is skipped.
    """
    if not source.from_catalogue or release in (None, ""):
        return False
    return source.name == f"GTDB_r{release}"


def _gtdb_source_name(release: object | None) -> str:
    return "GTDB" if release in (None, "") else f"GTDB_r{release}"


def _new_temporary(output: Path, *, mode: int | None = None) -> Path:
    """Create an empty, uniquely named sibling of ``output``; return its path.

    The file is created exclusively (``O_EXCL``) under a random name, so two
    concurrent builds cannot collide and no pre-existing file is ever removed
    — a fixed ``<output>.tmp`` used to be unlinked first, which could delete a
    user's file or the input of an in-place update.  Creating it like an
    ordinary file (mode 0o666, masked by the umask) keeps a fresh build's
    umask-derived permissions; ``mode`` overrides that for an in-place update,
    which must keep the destination's permissions (a ``mkstemp`` file would be
    0600).  Only this path is ever unlinked on failure.
    """
    for _ in range(100):
        name = f"{output.name}.{os.urandom(8).hex()}.tmp"
        temporary = output.parent / name
        try:
            # O_EXCL: never reuse or shorten somebody else's file.
            descriptor = os.open(
                temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666
            )
        except FileExistsError:
            continue
        os.close(descriptor)
        if mode is not None:
            os.chmod(temporary, mode)
        return temporary
    raise FileExistsError(
        f"could not create a temporary file next to {output} (100 names tried)"
    )


def _write_info(connection: sqlite3.Connection, info: dict[str, str]) -> None:
    connection.executemany(
        "INSERT OR REPLACE INTO info (key, value) VALUES (?, ?)", info.items()
    )


def build_metadata_db(
    files: Sequence[str | os.PathLike[str]],
    output: str | os.PathLike[str],
    *,
    release: int | str | None = None,
    force: bool = False,
    reps_only: bool = False,
    progress: object = None,
    taxonomy: Sequence[TaxonomySource] = (),
) -> dict[str, object]:
    """Build a SQLite metadata database from GTDB metadata TSV files.

    ``files`` are read in order; later files overwrite earlier rows with the
    same accession.  With ``reps_only``, only species representatives
    (``gtdb_representative`` true) are stored.  ``progress``, when given, is
    called as ``progress(rows_seen, path)`` roughly every 20,000 rows.

    ``taxonomy`` adds sylph-tax files as sources in the given order (their
    ``ord`` follows the virtual GTDB source).  The GTDB metadata itself becomes
    the virtual source ``GTDB_r<release>``; the catalogue file of that same
    release (``GTDB_r232`` with r232 metadata) is skipped and reported in
    ``skipped_taxonomy`` instead of being stored twice, while a local file or a
    different release is imported (``is_redundant_gtdb_source``).
    """
    if not files and not taxonomy:
        raise ValueError("at least one metadata file or taxonomy source is required")
    output = Path(output)
    if output.exists() and not force:
        raise FileExistsError(
            f"{output} already exists (pass --force to overwrite it)"
        )
    if not output.parent.exists():
        raise FileNotFoundError(f"output directory does not exist: {output.parent}")
    # Build next to the target and replace it only on success, so a failure
    # leaves an existing database untouched.
    temporary = _new_temporary(output)

    placeholders = ", ".join("?" * (1 + len(COLUMNS)))
    sources: list[dict[str, object]] = []
    seen = 0
    rows_written = 0
    callback = progress if callable(progress) else None
    # value -> id, plus values in id order, for each dictionary-encoded column.
    dictionaries: dict[str, dict[str, int]] = {
        name: {} for name in DICT_ENCODED_COLUMNS
    }
    dictionary_values: dict[str, list[str]] = {
        name: [] for name in DICT_ENCODED_COLUMNS
    }
    representative_position = METADATA_COLUMNS.index("gtdb_representative")
    release_text = None if release is None else str(release)
    lineages = _Dictionary()
    hosts = _Dictionary()
    taxonomy_source_rows: list[dict[str, object]] = []
    skipped_taxonomy: list[str] = []
    overlaps: list[TaxonomyOverlap] = []

    def encode(accession: str, values: list[object]) -> tuple[object, ...]:
        for position, name in _DICT_POSITIONS:
            value = values[position]
            # These columns are text: anything but a non-NULL string stays NULL.
            if not isinstance(value, str):
                continue
            ids = dictionaries[name]
            identifier = ids.get(value)
            if identifier is None:
                identifier = len(ids) + 1
                ids[value] = identifier
                dictionary_values[name].append(value)
            values[position] = identifier
        return (accession, *values)

    connection = sqlite3.connect(str(temporary))
    try:
        connection.execute(f"PRAGMA page_size = {METADATA_PAGE_SIZE}")
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute("PRAGMA cache_size = -200000")  # 200 MB
        _create_schema(connection)

        for file in files:
            path = Path(file)
            file_rows = 0
            batch: list[tuple[object, ...]] = []
            for accession, values in _iter_rows(path):
                file_rows += 1
                seen += 1
                if reps_only and values[representative_position] != 1:
                    continue
                batch.append(encode(accession, values))
                if len(batch) >= 20_000:
                    rows_written += _insert_batch(connection, placeholders, batch)
                    batch.clear()
                    if callback is not None:
                        callback(seen, path)
            if batch:
                rows_written += _insert_batch(connection, placeholders, batch)
            sources.append({"file": path.name, "rows": file_rows})
            if callback is not None:
                callback(seen, path)

        for name in DICT_ENCODED_COLUMNS:
            connection.executemany(
                f"INSERT INTO dict_{name} (id, value) VALUES (?, ?)",
                enumerate(dictionary_values[name], start=1),
            )

        rows_written = connection.execute(
            "SELECT COUNT(*) FROM metadata_data"
        ).fetchone()[0]

        # The GTDB metadata is a virtual source: its entries are the accessions
        # and their gtdb_taxonomy, so no lineage is stored twice.
        if files:
            virtual_rows = connection.execute(
                "SELECT COUNT(*) FROM metadata_data WHERE gtdb_taxonomy IS NOT NULL"
            ).fetchone()[0]
            _insert_source_row(
                connection,
                name=_gtdb_source_name(release),
                kind="metadata",
                ord_value=0,
                origin="metadata_data",
                md5=None,
                rows=int(virtual_rows),
                has_host=False,
                built=datetime.now(UTC).isoformat(timespec="seconds"),
            )

        ord_value = 1
        for source in taxonomy:
            if files and is_redundant_gtdb_source(source, release_text):
                skipped_taxonomy.append(source.name)
                continue
            imported = _add_taxonomy_source(
                connection,
                source,
                ord_value=ord_value,
                lineages=lineages,
                hosts=hosts,
                progress=callback,
            )
            overlaps.extend(cast(list[TaxonomyOverlap], imported["overlaps"]))
            ord_value += 1
        lineages.flush(connection, "dict_tax_lineage")
        hosts.flush(connection, "dict_tax_host")
        taxonomy_source_rows = _taxonomy_source_list(connection)

        info = {
            "format_version": METADATA_FORMAT_VERSION,
            "sylphd_version": __version__,
            "release": "" if release is None else str(release),
            "built": datetime.now(UTC).isoformat(timespec="seconds"),
            "sources": json.dumps(sources),
            "rows": str(rows_written),
            "columns": json.dumps(list(METADATA_COLUMNS)),
            "reps_only": "true" if reps_only else "false",
            "sylphtax_rules": SYLPHTAX_VERSION,
            "taxonomy_sources": json.dumps(taxonomy_source_rows),
        }
        _write_info(connection, info)
        connection.commit()
        connection.execute("ANALYZE")
        connection.commit()
    except BaseException:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise
    else:
        connection.close()
        temporary.replace(output)
    _report_taxonomy_overlaps(overlaps)

    return {
        "path": str(output),
        "rows": rows_written,
        "sources": sources,
        "taxonomy_sources": taxonomy_source_rows,
        "skipped_taxonomy": skipped_taxonomy,
        "taxonomy_overlaps": [overlap.as_dict() for overlap in overlaps],
        "size": output.stat().st_size,
        "release": None if release is None else str(release),
        "format_version": METADATA_FORMAT_VERSION,
    }


def _insert_batch(
    connection: sqlite3.Connection, placeholders: str, batch: list[tuple[object, ...]]
) -> int:
    # Identifiers come from the COLUMNS constants, never from input; values are bound.
    names = ", ".join(["accession", *(column.name for column in COLUMNS)])
    cursor = connection.executemany(
        f"INSERT OR REPLACE INTO metadata_data ({names}) VALUES ({placeholders})",
        batch,
    )
    return cursor.rowcount


def _taxonomy_source_list(connection: sqlite3.Connection) -> list[dict[str, object]]:
    """The sources in ``ord`` order, each with its ``overlaps`` counts.

    ``overlaps`` maps an other source's name to the number of keys both have;
    it is empty when the file has no ``taxonomy_overlap`` table (a version 3
    file written before it existed) or no overlap.
    """
    columns = ("name", "kind", "ord", "origin", "md5", "rows", "has_host", "built")
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    overlaps: dict[int, dict[str, int]] = {}
    if "taxonomy_overlap" in tables:
        for source_id, other, keys in connection.execute(
            "SELECT o.source_id, s.name, o.keys FROM taxonomy_overlap AS o "
            "JOIN taxonomy_source AS s ON s.id = o.other_id ORDER BY s.ord"
        ):
            overlaps.setdefault(int(source_id), {})[str(other)] = int(keys)
    sources = []
    for row in connection.execute(
        "SELECT id, name, kind, ord, origin, md5, rows, has_host, built "
        "FROM taxonomy_source ORDER BY ord"
    ):
        record = dict(zip(columns, row[1:]))
        record["has_host"] = bool(record["has_host"])
        record["overlaps"] = overlaps.get(int(row[0]), {})
        sources.append(record)
    return sources


def update_metadata_db(
    source: str | os.PathLike[str],
    output: str | os.PathLike[str],
    *,
    taxonomy: Sequence[TaxonomySource] = (),
    force: bool = False,
    progress: object = None,
) -> dict[str, object]:
    """Copy an existing metadata database, add taxonomy sources, write ``output``.

    This is ``setup --update``: it adds the ``taxonomy_*`` tables to a version
    2 file (and the virtual GTDB source), bumps ``format_version`` to 3, and
    appends (or replaces, by name) the given sources.  The GTDB rows are copied
    as they are, so no GTDB TSV files are needed.  A version 3 file written
    before ``taxonomy_overlap`` existed gets it (and its counts) here.  The copy
    is built next to ``output`` and replaces it only on success, like
    ``setup --force``; a failure leaves ``output`` untouched.
    """
    source_path = Path(source)
    output_path = Path(output)
    if not source_path.is_file():
        raise FileNotFoundError(f"metadata database not found: {source_path}")
    same_file = output_path.exists() and source_path.resolve() == output_path.resolve()
    if output_path.exists() and not force and not same_file:
        raise FileExistsError(
            f"{output_path} already exists (pass --force to overwrite it)"
        )
    if not output_path.parent.exists():
        raise FileNotFoundError(f"output directory does not exist: {output_path.parent}")
    # A fresh sibling temp, created exclusively: in-place updates read the
    # source from the same directory, so nothing near the output may be
    # unlinked, and the new file gets the destination's permissions.
    temporary = _new_temporary(
        output_path,
        mode=None
        if not output_path.exists()
        else stat.S_IMODE(output_path.stat().st_mode),
    )

    skipped_taxonomy: list[str] = []
    overlaps: list[TaxonomyOverlap] = []
    uri = f"{source_path.resolve().as_uri()}?mode=ro"
    reader = sqlite3.connect(uri, uri=True)
    connection = sqlite3.connect(str(temporary))
    try:
        # The copy keeps the page size, dictionaries and rows of the source.
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute("PRAGMA cache_size = -200000")
        try:
            reader.backup(connection)
        finally:
            reader.close()

        connection.row_factory = sqlite3.Row
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        if "metadata" not in tables:
            raise ValueError(f"{source_path} is not a sylphd metadata database")
        info: dict[str, str] = {}
        if "info" in tables:
            info = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM info")
            }
        version = info.get("format_version", "2")
        if version not in READABLE_FORMAT_VERSIONS:
            raise ValueError(
                f"{source_path} has metadata format version {version}; "
                f"this sylphd reads {', '.join(READABLE_FORMAT_VERSIONS)}"
            )
        if "taxonomy_source" not in tables:
            for statement in _taxonomy_schema_sql():
                connection.execute(statement)
            # A version 2 file has no sources to count; a version 3 file written
            # before ``taxonomy_overlap`` existed has them, so fill it now (once).
            needs_backfill = False
        elif "taxonomy_overlap" not in tables:
            connection.execute(_OVERLAP_TABLE_SQL)
            needs_backfill = True
        else:
            needs_backfill = False
        if needs_backfill:
            _backfill_overlaps(connection)
        if version == "2":
            _add_virtual_gtdb_source(connection, info)
            info["format_version"] = METADATA_FORMAT_VERSION
            info["sylphtax_rules"] = SYLPHTAX_VERSION
            _write_info(
                connection,
                {
                    "format_version": METADATA_FORMAT_VERSION,
                    "sylphtax_rules": SYLPHTAX_VERSION,
                },
            )

        lineages = _load_dictionary(connection, "dict_tax_lineage")
        hosts = _load_dictionary(connection, "dict_tax_host")
        # Only the catalogue file of the release whose metadata is already the
        # virtual source is redundant; --update must not replace the virtual
        # source with it (is_redundant_gtdb_source covers both paths).
        virtual_source = connection.execute(
            "SELECT 1 FROM taxonomy_source WHERE kind = 'metadata' LIMIT 1"
        ).fetchone()
        release = info.get("release") or None
        for entry in taxonomy:
            if virtual_source is not None and is_redundant_gtdb_source(entry, release):
                skipped_taxonomy.append(entry.name)
                continue
            imported = _add_taxonomy_source(
                connection,
                entry,
                ord_value=_next_ord(connection),
                lineages=lineages,
                hosts=hosts,
                progress=progress,
            )
            overlaps.extend(cast(list[TaxonomyOverlap], imported["overlaps"]))
        lineages.flush(connection, "dict_tax_lineage")
        hosts.flush(connection, "dict_tax_host")
        _write_info(
            connection,
            {
                "taxonomy_sources": json.dumps(_taxonomy_source_list(connection)),
                "format_version": METADATA_FORMAT_VERSION,
                "sylphtax_rules": info.get("sylphtax_rules", SYLPHTAX_VERSION),
            },
        )
        connection.commit()
        connection.execute("ANALYZE")
        connection.commit()
        rows = connection.execute("SELECT COUNT(*) FROM metadata_data").fetchone()[0]
        sources = _taxonomy_source_list(connection)
    except BaseException:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise
    else:
        connection.close()
        temporary.replace(output_path)
    _report_taxonomy_overlaps(overlaps)

    return {
        "path": str(output_path),
        "rows": int(rows),
        "sources": [],
        "taxonomy_sources": sources,
        "skipped_taxonomy": skipped_taxonomy,
        "taxonomy_overlaps": [overlap.as_dict() for overlap in overlaps],
        "size": output_path.stat().st_size,
        "release": info.get("release") or None,
        "format_version": METADATA_FORMAT_VERSION,
    }


def _add_virtual_gtdb_source(
    connection: sqlite3.Connection, info: dict[str, str]
) -> None:
    """Register the GTDB rows of a version 2 file as the virtual source."""
    try:
        rows = connection.execute(
            "SELECT COUNT(*) FROM metadata_data WHERE gtdb_taxonomy IS NOT NULL"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return
    if not rows:
        return
    _insert_source_row(
        connection,
        name=_gtdb_source_name(info.get("release") or None),
        kind="metadata",
        ord_value=0,
        origin="metadata_data",
        md5=None,
        rows=int(rows),
        has_host=False,
        built=datetime.now(UTC).isoformat(timespec="seconds"),
    )


class MetadataDB:
    """Read-only handle on a metadata SQLite file built by ``sylphd setup``."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"metadata database not found: {self.path}")
        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        self._connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        tables = {
            row[0] for row in self._connection.execute("SELECT name FROM sqlite_master")
        }
        if "metadata" not in tables:
            self._connection.close()
            raise ValueError(f"{self.path} is not a sylphd metadata database")
        self.columns: tuple[str, ...] = tuple(
            row[1]
            for row in self._connection.execute("PRAGMA table_info(metadata)")
            if row[1] != "accession"
        )
        self.info: dict[str, str] = {}
        if "info" in tables:
            self.info = {
                row[0]: row[1]
                for row in self._connection.execute("SELECT key, value FROM info")
            }
        version = self.info.get("format_version", "2")
        if version not in READABLE_FORMAT_VERSIONS:
            self._connection.close()
            raise ValueError(
                f"{self.path} has metadata format version {version}; "
                f"this sylphd reads {', '.join(READABLE_FORMAT_VERSIONS)}"
            )
        self.format_version: str = version
        #: True when the file carries the format 3 taxonomy tables.
        self.has_taxonomy: bool = "taxonomy_source" in tables
        #: The sources, in ``ord`` order (empty for a version 2 file).
        self._sources: tuple[TaxonomySourceRow, ...] = self._read_sources()
        #: The de-duplicated overlap pairs, in ``ord`` order; ``()`` for a file
        #: written before ``taxonomy_overlap`` existed.
        self._overlaps: tuple[TaxonomyOverlapPair, ...] = self._read_overlaps(
            "taxonomy_overlap" in tables
        )
        #: The virtual GTDB source, if the file has one.
        self._virtual_source: TaxonomySourceRow | None = next(
            (source for source in self._sources if source.kind == "metadata"), None
        )

    def _read_sources(self) -> tuple[TaxonomySourceRow, ...]:
        if not self.has_taxonomy:
            return ()
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, ord, name, kind, origin, md5, rows, has_host "
                "FROM taxonomy_source ORDER BY ord, id"
            ).fetchall()
        return tuple(
            TaxonomySourceRow(
                id=int(row["id"]),
                ord=int(row["ord"]),
                name=str(row["name"]),
                kind=str(row["kind"]),
                origin=row["origin"],
                md5=row["md5"],
                rows=row["rows"],
                has_host=bool(row["has_host"]),
            )
            for row in rows
        )

    def _read_overlaps(self, has_table: bool) -> tuple[TaxonomyOverlapPair, ...]:
        """The de-duplicated overlap pairs, each source pair listed once.

        ``taxonomy_overlap`` stores both directions of a pair; the pair is
        reported once, with the names in ``ord`` order (the later one wins).
        The first shared keys are read here, once at startup, because the
        metadata file only stores the counts.
        """
        if not has_table:
            return ()
        sources = {source.id: source for source in self._sources}
        sources_by_name = {source.name: source for source in self._sources}
        with self._lock:
            rows = self._connection.execute(
                "SELECT source_id, other_id, keys FROM taxonomy_overlap"
            ).fetchall()
        pairs: list[TaxonomyOverlapPair] = []
        seen: set[tuple[int, int]] = set()
        for row in rows:
            first_id = int(row["source_id"])
            second_id = int(row["other_id"])
            if first_id == second_id:
                continue
            key = (min(first_id, second_id), max(first_id, second_id))
            if key in seen:
                continue
            seen.add(key)
            first = sources.get(first_id)
            second = sources.get(second_id)
            if first is None or second is None:
                continue
            if (second.ord, second.id) < (first.ord, first.id):
                first, second = second, first
            pairs.append(
                TaxonomyOverlapPair(
                    sources=(first.name, second.name),
                    keys=int(row["keys"]),
                    examples=self._overlap_examples(first, second),
                )
            )
        pairs.sort(
            key=lambda pair: (
                sources_by_name[pair.sources[0]].ord,
                sources_by_name[pair.sources[1]].ord,
            )
        )
        return tuple(pairs)

    def _overlap_examples(
        self, first: TaxonomySourceRow, second: TaxonomySourceRow, limit: int = 5
    ) -> tuple[str, ...]:
        """The first ``limit`` keys two sources share, sorted.

        One of the two may be the virtual GTDB source, whose keys are
        ``metadata_data.accession`` rows that have a lineage (exactly what
        ``resolve`` looks up); the other keys are ``taxonomy_entry.key``.
        """
        with self._lock:
            if first.kind == "metadata" or second.kind == "metadata":
                other = second if first.kind == "metadata" else first
                rows = self._connection.execute(
                    "SELECT DISTINCT e.key AS key FROM taxonomy_entry AS e "
                    "JOIN metadata_data AS m ON m.accession = e.key "
                    "WHERE e.source_id = ? AND m.gtdb_taxonomy IS NOT NULL "
                    "ORDER BY e.key LIMIT ?",
                    (other.id, limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT e.key AS key FROM taxonomy_entry AS e "
                    "JOIN taxonomy_entry AS o ON o.key = e.key AND o.source_id = ? "
                    "WHERE e.source_id = ? ORDER BY e.key LIMIT ?",
                    (second.id, first.id, limit),
                ).fetchall()
        return tuple(str(row["key"]) for row in rows)

    @property
    def taxonomy_columns(self) -> tuple[str, ...]:
        """The ``tax_*`` columns a row can carry (empty without sources)."""
        return HIT_TAXONOMY_COLUMNS if self._sources else ()

    @property
    def field_columns(self) -> tuple[str, ...]:
        """Every column a client may request: metadata plus ``tax_*``."""
        return self.columns + self.taxonomy_columns

    def taxonomy_sources(self) -> list[dict[str, object]]:
        """The taxonomy sources of the file, in ``ord`` order (empty for v2).

        ``keys`` is the number of stored keys of the source.  Keys shared with
        another source are reported once per pair at the metadata level
        (:meth:`taxonomy_overlaps`), not per source.
        """
        return [
            {
                "name": source.name,
                "kind": source.kind,
                "origin": public_origin(source.origin),
                "md5": source.md5,
                "keys": source.rows,
                "has_host": source.has_host,
            }
            for source in self._sources
        ]

    def taxonomy_overlaps(self) -> list[dict[str, object]]:
        """The pairs of sources that share keys, each pair once, in ord order."""
        return [pair.as_dict() for pair in self._overlaps]

    def source_names(self) -> list[str]:
        """Names of the taxonomy sources, in ``ord`` order."""
        return [source.name for source in self._sources]

    def _chunked_lookup(self, accessions: Sequence[str]) -> dict[str, dict[str, object]]:
        unique = list(dict.fromkeys(accession for accession in accessions if accession))
        found: dict[str, dict[str, object]] = {}
        # 500 parameters per statement stays well below SQLite's variable limit.
        for start in range(0, len(unique), 500):
            chunk = unique[start : start + 500]
            placeholders = ", ".join("?" * len(chunk))
            query = f"SELECT * FROM metadata WHERE accession IN ({placeholders})"
            with self._lock:
                for row in self._connection.execute(query, chunk):
                    record = dict(row)
                    found[record.pop("accession")] = record
        return found

    def lookup(self, accessions: Iterable[str]) -> dict[str, dict[str, object]]:
        """Return metadata rows keyed by accession, for the accessions found."""
        return self._chunked_lookup(list(accessions))

    def contains_accessions(self, accessions: Iterable[str]) -> bool:
        """Whether any of ``accessions`` is a genome of this metadata file.

        Used to tell a GTDB database from one whose genome names merely look
        like accessions (fungal RefSeq genomes are ``GCF_`` too, but they are
        not in GTDB's metadata).  Stopping at the first match keeps the query
        small even for a batch of sampled names.
        """
        unique = list(dict.fromkeys(value for value in accessions if value))
        for start in range(0, len(unique), 500):
            chunk = unique[start : start + 500]
            placeholders = ", ".join("?" * len(chunk))
            with self._lock:
                row = self._connection.execute(
                    f"SELECT 1 FROM metadata_data WHERE accession IN ({placeholders}) "
                    "LIMIT 1",
                    chunk,
                ).fetchone()
            if row is not None:
                return True
        return False

    # -- hit -> taxonomy -------------------------------------------------

    def resolve(
        self,
        hits: Iterable[tuple[str, str]],
        *,
        sources: Sequence[str] | None = None,
    ) -> list[HitTaxonomy]:
        """Map each ``(Genome_file, Contig_name)`` hit to its taxonomy.

        Replicates ``sylph-tax taxprof`` (see :func:`sylphtax_probes`): the
        probe keys of both passes are fetched in one batch, and per hit the
        first key of pass 1 wins over the first key of pass 0.  ``sources``
        restricts the search to those source names (``--tax-source``), as if
        sylph-tax had been given only those files: a key that exists only in an
        excluded source is a miss.

        For a key found in several sources the **lineage** comes from the
        largest ``ord`` and the **host** from the largest ``ord`` whose row has
        one — two different sources, possibly, exactly like sylph-tax's two
        dicts (docs/project/multi-db-design.md B3).  The returned
        :class:`HitTaxonomy` is all ``None`` for a hit without any taxonomy.
        """
        pairs = [
            (str(genome or ""), str(contig or "")) for genome, contig in hits
        ]
        if not pairs:
            return []
        if not self._sources:
            return [NO_HIT_TAXONOMY] * len(pairs)
        allowed = None if sources is None else set(sources)
        probes = [sylphtax_probes(genome, contig) for genome, contig in pairs]
        keys = {key for probe in probes for key in probe.probe_keys()}
        entries = self._taxonomy_entries(keys, allowed)

        resolved: list[HitTaxonomy] = []
        for probe in probes:
            taxonomy = NO_HIT_TAXONOMY
            for pass_keys in (probe.pass0, probe.pass1):
                for key in pass_keys:
                    entry = entries.get(key)
                    if entry is not None:
                        taxonomy = HitTaxonomy(
                            source=entry[0], key=key, lineage=entry[1], host=entry[2]
                        )
                        break
            resolved.append(taxonomy)
        return resolved

    def _taxonomy_entries(
        self, keys: Iterable[str], allowed: set[str] | None
    ) -> dict[str, tuple[str, str, str | None]]:
        """``key -> (source name, lineage, host)`` merged across sources by ``ord``.

        Two dictionaries, as sylph-tax has: the lineage from the largest
        ``ord`` that has the key, the host from the largest ``ord`` that has the
        key *and* a host.
        """
        # The empty key is a valid key: setup stores it (sylph-tax accepts it)
        # and a pass-0 contig probe is "" for a contig such as "|extra".
        unique = list(dict.fromkeys(keys))
        lineages: dict[str, tuple[int, str, str]] = {}
        hosts: dict[str, tuple[int, str]] = {}

        def add(key: str, ord_value: int, name: str, lineage: str, host: str | None,
                ) -> None:
            best = lineages.get(key)
            if best is None or ord_value >= best[0]:
                lineages[key] = (ord_value, name, lineage)
            if host is not None:
                previous = hosts.get(key)
                if previous is None or ord_value >= previous[0]:
                    hosts[key] = (ord_value, host)

        # 500 parameters per statement stays well below SQLite's variable limit.
        for start in range(0, len(unique), 500):
            chunk = unique[start : start + 500]
            placeholders = ", ".join("?" * len(chunk))
            query = (
                "SELECT e.key AS key, s.ord AS ord, s.name AS name, "
                "l.value AS lineage, h.value AS host "
                "FROM taxonomy_entry AS e "
                "JOIN taxonomy_source AS s ON s.id = e.source_id "
                "LEFT JOIN dict_tax_lineage AS l ON l.id = e.lineage "
                "LEFT JOIN dict_tax_host AS h ON h.id = e.host "
                f"WHERE e.key IN ({placeholders})"
            )
            with self._lock:
                rows = self._connection.execute(query, chunk).fetchall()
            for row in rows:
                name = str(row["name"])
                if allowed is not None and name not in allowed:
                    continue
                lineage = row["lineage"]
                if lineage is None:  # pragma: no cover - the builder always sets it
                    continue
                add(str(row["key"]), int(row["ord"]), name, str(lineage), row["host"])

        virtual = self._virtual_source
        if virtual is not None and (allowed is None or virtual.name in allowed):
            for start in range(0, len(unique), 500):
                chunk = unique[start : start + 500]
                placeholders = ", ".join("?" * len(chunk))
                query = (
                    "SELECT m.accession AS key, d.value AS lineage "
                    "FROM metadata_data AS m "
                    "JOIN dict_gtdb_taxonomy AS d ON d.id = m.gtdb_taxonomy "
                    f"WHERE m.accession IN ({placeholders})"
                )
                with self._lock:
                    rows = self._connection.execute(query, chunk).fetchall()
                for row in rows:
                    add(
                        str(row["key"]),
                        virtual.ord,
                        virtual.name,
                        str(row["lineage"]),
                        None,
                    )

        return {
            key: (name, lineage, hosts[key][1] if key in hosts else None)
            for key, (_, name, lineage) in lineages.items()
        }

    def summary(self) -> dict[str, object]:
        """Return a JSON-friendly description of this metadata database.

        This is what ``GET /info`` publishes, so it names the file without its
        directory: the absolute path is not exposed to clients.  Likewise a
        taxonomy source's ``origin`` is reduced to its base name (or kept as
        the URL it was downloaded from), never a local path.
        """
        summary: dict[str, object] = {
            "file": self.path.name,
            "size": self.path.stat().st_size,
            "columns": list(self.field_columns),
            "rows": None,
            "release": self.info.get("release") or None,
            "built": self.info.get("built") or None,
            "format_version": self.info.get("format_version") or None,
            "reps_only": self.info.get("reps_only") == "true",
            "sources": [],
        }
        try:
            summary["rows"] = int(self.info.get("rows", ""))
        except ValueError:
            with self._lock:
                row = self._connection.execute("SELECT COUNT(*) FROM metadata").fetchone()
            summary["rows"] = int(row[0])
        try:
            summary["sources"] = json.loads(self.info.get("sources", "[]"))
        except json.JSONDecodeError:
            summary["sources"] = []
        if self.has_taxonomy:
            # Additive: a format 2 file reports exactly what it always did.
            summary["taxonomy_sources"] = self.taxonomy_sources()
            summary["taxonomy_overlaps"] = self.taxonomy_overlaps()
            summary["sylphtax_rules"] = self.info.get("sylphtax_rules") or None
        return summary

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
