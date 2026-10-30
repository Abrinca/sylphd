"""Writers of the metadata database: ``sylphd setup`` and ``setup --update``.

See :mod:`sylphd.metadata` for the file layout and :class:`MetadataDB` for the
reader.  Both writers build the file next to the target and replace it only on
success (:func:`_replace_on_success`), so a failure leaves an existing database
untouched.
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import stat
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from . import __version__
from .catalog import SYLPHTAX_VERSION
from .metadata import (
    _DICT_POSITIONS,
    COLUMNS,
    DICT_ENCODED_COLUMNS,
    METADATA_COLUMNS,
    METADATA_FORMAT_VERSION,
    METADATA_PAGE_SIZE,
    NULL_MARKERS,
    READABLE_FORMAT_VERSIONS,
    normalize_accession,
    open_text,
)
from .taxonomy_sources import (
    TaxonomyOverlap,
    TaxonomySource,
    _add_taxonomy_source,
    _backfill_overlaps,
    _Dictionary,
    _insert_source_row,
    _load_dictionary,
    _next_ord,
    _report_taxonomy_overlaps,
    is_redundant_gtdb_source,
)


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


@contextmanager
def _replace_on_success(output: Path, *, mode: int | None = None) -> Iterator[Path]:
    """Yield a sibling temporary file that replaces ``output`` if the block succeeds.

    Close every connection to the temporary inside the block: on any exception
    (``BaseException`` included, so Ctrl-C too) the temporary is removed and
    ``output`` is left untouched.
    """
    temporary = _new_temporary(output, mode=mode)
    try:
        yield temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    temporary.replace(output)


def _write_info(connection: sqlite3.Connection, info: dict[str, str]) -> None:
    connection.executemany(
        "INSERT OR REPLACE INTO info (key, value) VALUES (?, ?)", info.items()
    )


def _check_output(output: Path, *, replaceable: bool) -> None:
    if output.exists() and not replaceable:
        raise FileExistsError(f"{output} already exists (pass --force to overwrite it)")
    if not output.parent.exists():
        raise FileNotFoundError(f"output directory does not exist: {output.parent}")


def _tune_for_writing(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode = OFF")
    connection.execute("PRAGMA synchronous = OFF")
    connection.execute("PRAGMA cache_size = -200000")  # 200 MB


def _gtdb_row_count(connection: sqlite3.Connection) -> int:
    return connection.execute(
        "SELECT COUNT(*) FROM metadata_data WHERE gtdb_taxonomy IS NOT NULL"
    ).fetchone()[0]


def _insert_virtual_gtdb_source(
    connection: sqlite3.Connection, release: object | None, rows: int
) -> None:
    """Register the GTDB metadata as the virtual source: its entries are the
    accessions and their ``gtdb_taxonomy``, so no lineage is stored twice."""
    _insert_source_row(
        connection,
        name=_gtdb_source_name(release),
        kind="metadata",
        ord_value=0,
        origin="metadata_data",
        md5=None,
        rows=int(rows),
        has_host=False,
        built=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def _import_taxonomy(
    connection: sqlite3.Connection,
    sources: Sequence[TaxonomySource],
    *,
    release: str | None,
    has_virtual: bool,
    first_ord: int,
    lineages: _Dictionary,
    hosts: _Dictionary,
    progress: object,
) -> tuple[list[str], list[TaxonomyOverlap]]:
    """Add sylph-tax ``sources`` after the existing ones; return ``(skipped, overlaps)``.

    Only the catalogue file of the release whose metadata is already the
    virtual source is redundant (``is_redundant_gtdb_source``); it is skipped
    rather than stored twice.
    """
    skipped: list[str] = []
    overlaps: list[TaxonomyOverlap] = []
    for source in sources:
        if has_virtual and is_redundant_gtdb_source(source, release):
            skipped.append(source.name)
            continue
        imported = _add_taxonomy_source(
            connection,
            source,
            ord_value=max(_next_ord(connection), first_ord),
            lineages=lineages,
            hosts=hosts,
            progress=progress,
        )
        overlaps.extend(cast(list[TaxonomyOverlap], imported["overlaps"]))
    lineages.flush(connection, "dict_tax_lineage")
    hosts.flush(connection, "dict_tax_host")
    return skipped, overlaps


def _finish(connection: sqlite3.Connection, info: dict[str, str]) -> None:
    _write_info(connection, info)
    connection.commit()
    connection.execute("ANALYZE")
    connection.commit()


def _summary(
    output: Path,
    *,
    rows: int,
    sources: list[dict[str, object]],
    taxonomy_sources: list[dict[str, object]],
    skipped: list[str],
    overlaps: list[TaxonomyOverlap],
    release: str | None,
) -> dict[str, object]:
    """What ``build_metadata_db`` and ``update_metadata_db`` return."""
    _report_taxonomy_overlaps(overlaps)
    return {
        "path": str(output),
        "rows": rows,
        "sources": sources,
        "taxonomy_sources": taxonomy_sources,
        "skipped_taxonomy": skipped,
        "taxonomy_overlaps": [overlap.as_dict() for overlap in overlaps],
        "size": output.stat().st_size,
        "release": release,
        "format_version": METADATA_FORMAT_VERSION,
    }


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
    _check_output(output, replaceable=force)

    placeholders = ", ".join("?" * (1 + len(COLUMNS)))
    sources: list[dict[str, object]] = []
    seen = 0
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

    # Build next to the target and replace it only on success, so a failure
    # leaves an existing database untouched.
    with (
        _replace_on_success(output) as temporary,
        closing(sqlite3.connect(temporary)) as connection,
    ):
        connection.execute(f"PRAGMA page_size = {METADATA_PAGE_SIZE}")
        _tune_for_writing(connection)
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
                    _insert_batch(connection, placeholders, batch)
                    batch.clear()
                    if callback is not None:
                        callback(seen, path)
            if batch:
                _insert_batch(connection, placeholders, batch)
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

        if files:
            _insert_virtual_gtdb_source(connection, release, _gtdb_row_count(connection))
        skipped_taxonomy, overlaps = _import_taxonomy(
            connection,
            taxonomy,
            release=release_text,
            has_virtual=bool(files),
            first_ord=1,
            lineages=lineages,
            hosts=hosts,
            progress=callback,
        )
        taxonomy_source_rows = _taxonomy_source_list(connection)

        _finish(connection, {
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
        })

    return _summary(
        output,
        rows=rows_written,
        sources=sources,
        taxonomy_sources=taxonomy_source_rows,
        skipped=skipped_taxonomy,
        overlaps=overlaps,
        release=release_text,
    )


def _insert_batch(
    connection: sqlite3.Connection, placeholders: str, batch: list[tuple[object, ...]]
) -> None:
    # Identifiers come from the COLUMNS constants, never from input; values are bound.
    names = ", ".join(["accession", *(column.name for column in COLUMNS)])
    connection.executemany(
        f"INSERT OR REPLACE INTO metadata_data ({names}) VALUES ({placeholders})",
        batch,
    )


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
    _check_output(output_path, replaceable=force or same_file)
    # In-place updates read the source from the same directory, and the new
    # file gets the destination's permissions.
    mode = stat.S_IMODE(output_path.stat().st_mode) if output_path.exists() else None

    uri = f"{source_path.resolve().as_uri()}?mode=ro"
    with (
        _replace_on_success(output_path, mode=mode) as temporary,
        closing(sqlite3.connect(temporary)) as connection,
    ):
        reader = sqlite3.connect(uri, uri=True)
        # The copy keeps the page size, dictionaries and rows of the source.
        _tune_for_writing(connection)
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
        # --update must not replace the virtual source with the catalogue file.
        has_virtual = (
            connection.execute(
                "SELECT 1 FROM taxonomy_source WHERE kind = 'metadata' LIMIT 1"
            ).fetchone()
            is not None
        )
        skipped_taxonomy, overlaps = _import_taxonomy(
            connection,
            taxonomy,
            release=info.get("release") or None,
            has_virtual=has_virtual,
            first_ord=0,
            lineages=lineages,
            hosts=hosts,
            progress=progress,
        )
        sources = _taxonomy_source_list(connection)
        _finish(connection, {
            "taxonomy_sources": json.dumps(sources),
            "format_version": METADATA_FORMAT_VERSION,
            "sylphtax_rules": info.get("sylphtax_rules", SYLPHTAX_VERSION),
        })
        rows = connection.execute("SELECT COUNT(*) FROM metadata_data").fetchone()[0]

    return _summary(
        output_path,
        rows=int(rows),
        sources=[],
        taxonomy_sources=sources,
        skipped=skipped_taxonomy,
        overlaps=overlaps,
        release=info.get("release") or None,
    )


def _add_virtual_gtdb_source(
    connection: sqlite3.Connection, info: dict[str, str]
) -> None:
    """Register the GTDB rows of a version 2 file as the virtual source."""
    try:
        rows = _gtdb_row_count(connection)
    except sqlite3.OperationalError:
        return
    if rows:
        _insert_virtual_gtdb_source(connection, info.get("release") or None, rows)
