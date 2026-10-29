"""Frozen copy of the format 2 metadata reader from commit ``e72b807``.

Source: ``src/sylphd/metadata.py`` at ``e72b807`` ("serve: several databases
per server (multi-db phase 1)"), i.e. the last release that knew only format
version 2.  It is trimmed to what reading needs — the ``MetadataDB`` class and
its imports — so it does not drift with the code under test: the compatibility
test must keep checking format 3 files against exactly these bytes, not against
``HEAD`` (which would make the test vacuous as soon as format 3 is committed).

Do not modernise this file.  It is a fixture, not part of sylphd.  Only the
import block (and one annotation) follows today's lint rules; the reading code
is unchanged from ``e72b807``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path


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
        return summary

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> MetadataDB:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
