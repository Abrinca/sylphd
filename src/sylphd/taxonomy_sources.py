"""Taxonomy sources (format 3): sylph-tax files, overlaps, and their import.

A :class:`TaxonomySource` is one ``key<TAB>lineage[<TAB>host]`` file to store
as a ``taxonomy_source`` of the metadata database.  This module parses such
files, inserts them with sylph-tax's duplicate-key semantics, and records (and
reports) the keys that two sources share.  The writers that use it are in
:mod:`sylphd.metadata_build`; the reader is :class:`sylphd.metadata.MetadataDB`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .metadata import open_text

logger = logging.getLogger(__name__)
# A plain library warning would vanish in a process that configures no logging
# (``sylphd setup`` does not), and Python's ``lastResort`` handler would then
# print it *and* the explicit stderr copy below.  The null handler keeps the
# record silent for a reader and leaves the CLI's own handler working.
logger.addHandler(logging.NullHandler())

#: Prefix of the CLI's messages (``sylphd: error: ...``), reused for warnings
#: that must reach the terminal even when no logging is configured.
_PROG = "sylphd"


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
