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
``taxonomy_entry``.  Version 2 files keep loading and serving; a version 3 file
still opens with a version 2 reader, which ignores the new tables.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Iterable, Iterator, Sequence

from . import __version__
from .download import SYLPHTAX_VERSION

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
    primary key starts with ``source_id``.
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
    """Insert one sylph-tax file as a source, replacing a source of the same name."""
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
    if output.exists():
        if not force:
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
            _add_taxonomy_source(
                connection,
                source,
                ord_value=ord_value,
                lineages=lineages,
                hosts=hosts,
                progress=callback,
            )
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

    return {
        "path": str(output),
        "rows": rows_written,
        "sources": sources,
        "taxonomy_sources": taxonomy_source_rows,
        "skipped_taxonomy": skipped_taxonomy,
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
    columns = ("name", "kind", "ord", "origin", "md5", "rows", "has_host", "built")
    sources = []
    for row in connection.execute(
        "SELECT name, kind, ord, origin, md5, rows, has_host, built "
        "FROM taxonomy_source ORDER BY ord"
    ):
        record = dict(zip(columns, row))
        record["has_host"] = bool(record["has_host"])
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
    as they are, so no GTDB TSV files are needed.  The copy is built next to
    ``output`` and replaces it only on success, like ``setup --force``; a
    failure leaves ``output`` untouched.
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
            _add_taxonomy_source(
                connection,
                entry,
                ord_value=_next_ord(connection),
                lineages=lineages,
                hosts=hosts,
                progress=progress,
            )
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

    return {
        "path": str(output_path),
        "rows": int(rows),
        "sources": [],
        "taxonomy_sources": sources,
        "skipped_taxonomy": skipped_taxonomy,
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

    def taxonomy_sources(self) -> list[dict[str, object]]:
        """The taxonomy sources of the file, in ``ord`` order (empty for v2)."""
        if not self.has_taxonomy:
            return []
        with self._lock:
            return [
                {
                    "name": row["name"],
                    "kind": row["kind"],
                    "origin": row["origin"],
                    "md5": row["md5"],
                    "rows": row["rows"],
                    "has_host": bool(row["has_host"]),
                }
                for row in self._connection.execute(
                    "SELECT name, kind, origin, md5, rows, has_host "
                    "FROM taxonomy_source ORDER BY ord"
                )
            ]

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

    def summary(self) -> dict[str, object]:
        """Return a JSON-friendly description of this metadata database."""
        summary: dict[str, object] = {
            "path": str(self.path),
            "size": self.path.stat().st_size,
            "columns": list(self.columns),
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
            summary["sylphtax_rules"] = self.info.get("sylphtax_rules") or None
        return summary

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "MetadataDB":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
