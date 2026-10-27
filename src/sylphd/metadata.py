"""GTDB metadata: download, SQLite build, and lookup.

The metadata table keeps every GTDB genome (not only species representatives),
keyed by NCBI accession with the ``RS_``/``GB_`` prefix stripped.  Taxonomy
strings are stored as one column each; no per-rank split happens here.
"""

from __future__ import annotations

import csv
import gzip
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Iterable, Iterator, Sequence

from . import __version__

#: Schema version of the generated SQLite file.  Bump when the column layout
#: or the meaning of the ``info`` table changes.
METADATA_FORMAT_VERSION = "1"

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


def _table_sql() -> str:
    columns = ",\n    ".join(
        [f"accession TEXT PRIMARY KEY"]
        + [f"{column.name} {column.sql_type}" for column in COLUMNS]
    )
    return f"CREATE TABLE metadata (\n    {columns}\n)"


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_table_sql())
    connection.execute("CREATE TABLE info (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute(
        "CREATE INDEX metadata_gtdb_representative ON metadata (gtdb_representative)"
    )
    connection.execute(
        "CREATE INDEX metadata_gtdb_genome_representative "
        "ON metadata (gtdb_genome_representative)"
    )


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
        for line_number, row in enumerate(reader, start=2):
            if not row or (len(row) == 1 and not row[0].strip()):
                continue
            if len(row) < len(header):
                row = row + [""] * (len(header) - len(row))
            accession = normalize_accession(row[index["accession"]])
            if not accession:
                continue
            values = [_parse_value(row[index[column.name]], column.kind) for column in COLUMNS]
            yield accession, values


def build_metadata_db(
    files: Sequence[str | os.PathLike[str]],
    output: str | os.PathLike[str],
    *,
    release: int | str | None = None,
    force: bool = False,
    progress: object = None,
) -> dict[str, object]:
    """Build a SQLite metadata database from GTDB metadata TSV files.

    ``files`` are read in order; later files overwrite earlier rows with the
    same accession.  ``progress``, when given, is called as
    ``progress(rows_seen, path)`` roughly every 50,000 rows.
    """
    if not files:
        raise ValueError("at least one metadata file is required")
    output = Path(output)
    if output.exists():
        if not force:
            raise FileExistsError(
                f"{output} already exists (pass --force to overwrite it)"
            )
        output.unlink()
    if not output.parent.exists():
        raise FileNotFoundError(f"output directory does not exist: {output.parent}")

    placeholders = ", ".join("?" * (1 + len(COLUMNS)))
    sources: list[dict[str, object]] = []
    seen = 0
    rows_written = 0
    callback = progress if callable(progress) else None

    connection = sqlite3.connect(str(output))
    try:
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute("PRAGMA cache_size = -200000")  # 200 MB
        _create_schema(connection)

        for file in files:
            path = Path(file)
            file_rows = 0
            batch: list[tuple[object, ...]] = []
            for accession, values in _iter_rows(path):
                batch.append((accession, *values))
                file_rows += 1
                seen += 1
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

        rows_written = connection.execute("SELECT COUNT(*) FROM metadata").fetchone()[0]
        info = {
            "format_version": METADATA_FORMAT_VERSION,
            "sylphd_version": __version__,
            "release": "" if release is None else str(release),
            "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sources": json.dumps(sources),
            "rows": str(rows_written),
            "columns": json.dumps(list(METADATA_COLUMNS)),
        }
        connection.executemany(
            "INSERT OR REPLACE INTO info (key, value) VALUES (?, ?)", info.items()
        )
        connection.commit()
        connection.execute("ANALYZE")
        connection.commit()
    except BaseException:
        connection.close()
        try:
            output.unlink()
        except FileNotFoundError:
            pass
        raise
    else:
        connection.close()

    return {
        "path": str(output),
        "rows": rows_written,
        "sources": sources,
        "size": output.stat().st_size,
        "release": None if release is None else str(release),
    }


def _insert_batch(
    connection: sqlite3.Connection, placeholders: str, batch: list[tuple[object, ...]]
) -> int:
    cursor = connection.executemany(
        f"INSERT OR REPLACE INTO metadata VALUES ({placeholders})", batch
    )
    return cursor.rowcount


class MetadataDB:
    """Read-only handle on a metadata SQLite file built by ``sylphd setup``."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"metadata database not found: {self.path}")
        uri = f"file:{self.path.resolve().as_posix()}?mode=ro"
        self._connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        tables = {
            row[0]
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
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
        return summary

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "MetadataDB":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
