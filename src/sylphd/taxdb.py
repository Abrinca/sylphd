"""The NCBI taxonomy as one SQLite file: built from the taxdump, read without a server.

``sylphd taxdb build`` turns NCBI's ``new_taxdump.tar.gz`` into a single
indexed SQLite file; :class:`TaxDb` answers lookups on it.  The server
(``sylphd serve --taxonomy-db``) exposes the same methods as ``/taxonomy/*``
endpoints and :mod:`sylphd.ncbitax` wraps both in ``Taxon`` objects, so the
result shapes here (plain ``dict`` and ``list``, JSON-ready) are the wire
format.

What is kept of the taxdump: every node (rank, parent, division, the four
genetic codes, hidden flags, comments), every name with its class, the
merged and deleted taxids, and the division and genetic code tables.  Not
kept: the ``inherited`` flags, citations, host and type material files.

This module needs only the standard library (the download helper is imported
when a build downloads), so a client machine can use a file without the
server's dependencies.
"""

from __future__ import annotations

import difflib
import os
import re
import sqlite3
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

#: NCBI's current taxonomy dump.
TAXDUMP_URL = "https://ftp.ncbi.nlm.nih.gov/pub/taxonomy/new_taxdump/new_taxdump.tar.gz"

#: Version of the SQLite layout; a file with another one is refused.
SCHEMA_VERSION = 1

#: The taxdump members a build reads.
TAXDUMP_MEMBERS: tuple[str, ...] = (
    "nodes.dmp",
    "names.dmp",
    "merged.dmp",
    "delnodes.dmp",
    "division.dmp",
    "gencode.dmp",
)

#: Name classes the fuzzy search looks at (not authorities, "includes", ...).
FUZZY_CLASSES: tuple[str, ...] = (
    "scientific name",
    "synonym",
    "genbank synonym",
    "equivalent name",
    "common name",
    "genbank common name",
    "blast name",
)

#: Most ids or names a single call takes; the server enforces it too.
MAX_BATCH = 1000

#: Longest lineage followed (the deepest NCBI lineage has about 50 nodes).
MAX_DEPTH = 200

#: Most descendants one call returns (``truncated`` tells when it was cut).
MAX_DESCENDANTS = 100_000

#: Most hits of a search; the candidates the fuzzy search re-ranks; the
#: vocabulary words it weighs per query word.
MAX_SEARCH = 100
FUZZY_CANDIDATES = 600
FUZZY_WORDS = 1000

#: Lowest similarity (0-1) of a corrected query word to a vocabulary word.
FUZZY_WORD_CUTOFF = 0.7

#: Words up to this length also try every single-character edit (their few
#: trigrams find a misspelling poorly).
FUZZY_SHORT = 6

_LETTERS = "abcdefghijklmnopqrstuvwxyz0123456789"

#: Lowest similarity (0-1) a fuzzy hit may have.
FUZZY_CUTOFF = 0.7

_SCHEMA = """
CREATE TABLE node (
    taxid INTEGER PRIMARY KEY,
    parent INTEGER NOT NULL,
    rank TEXT NOT NULL,
    name TEXT,
    unique_name TEXT,
    embl_code TEXT,
    division_id INTEGER,
    genetic_code_id INTEGER,
    mito_genetic_code_id INTEGER,
    plastid_genetic_code_id INTEGER,
    hydrogenosome_genetic_code_id INTEGER,
    genbank_hidden INTEGER,
    hidden_subtree_root INTEGER,
    specified_species INTEGER,
    comments TEXT
);
CREATE TABLE name_class (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE name (
    id INTEGER PRIMARY KEY,
    taxid INTEGER NOT NULL,
    name TEXT NOT NULL,
    name_lc TEXT NOT NULL,
    class_id INTEGER NOT NULL
);
CREATE TABLE unique_name (
    name_lc TEXT NOT NULL,
    taxid INTEGER NOT NULL,
    unique_name TEXT NOT NULL,
    name_id INTEGER NOT NULL
);
CREATE TABLE merged (old INTEGER PRIMARY KEY, new INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE deleted (taxid INTEGER PRIMARY KEY) WITHOUT ROWID;
CREATE TABLE division (id INTEGER PRIMARY KEY, code TEXT, name TEXT);
CREATE TABLE genetic_code (
    id INTEGER PRIMARY KEY,
    abbreviation TEXT,
    name TEXT,
    amino_acids TEXT,
    starts TEXT
);
CREATE TABLE info (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_INDEXES = """
CREATE INDEX node_by_parent ON node(parent);
CREATE INDEX name_by_taxid ON name(taxid);
CREATE INDEX name_by_lc ON name(name_lc);
CREATE INDEX unique_name_by_lc ON unique_name(name_lc);
"""

_WORD_INDEX = "CREATE INDEX word_by_w ON word(w)"

_NODE_COLUMNS = (
    "taxid, parent, rank, name, unique_name, embl_code, division_id, "
    "genetic_code_id, mito_genetic_code_id, plastid_genetic_code_id, "
    "hydrogenosome_genetic_code_id, genbank_hidden, hidden_subtree_root, "
    "specified_species, comments"
)


class TaxDbError(RuntimeError):
    """The taxonomy file is missing, not a taxonomy file, or has another layout."""


# -- building ---------------------------------------------------------------


def _fields(path: Path) -> Iterator[list[str]]:
    """The rows of a taxdump file: ``a\\t|\\tb\\t|\\t...\\t|`` split into fields."""
    with open(path, encoding="utf-8", newline="") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if line.endswith("\t|"):
                line = line[:-2]
            yield line.split("\t|\t")


def _int(value: str) -> int | None:
    value = value.strip()
    return int(value) if value else None


def _text(value: str) -> str | None:
    value = value.strip()
    return value or None


def _batches(rows: Iterable[tuple], size: int = 50_000) -> Iterator[list[tuple]]:
    batch: list[tuple] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _node_row(f: list[str]) -> tuple:
    def at(index: int) -> str:
        return f[index] if index < len(f) else ""

    return (
        int(f[0]),
        int(f[1]),
        f[2].strip(),
        None,
        None,
        _text(at(3)),
        _int(at(4)),
        _int(at(6)),
        _int(at(8)),
        _int(at(13)),
        _int(at(16)),
        _int(at(10)),
        _int(at(11)),
        _int(at(15)),
        _text(at(12)),
    )


def _extract(tarball: Path, directory: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    try:
        with tarfile.open(tarball, "r:gz") as archive:
            for member in archive:
                if member.name in TAXDUMP_MEMBERS:
                    source = archive.extractfile(member)
                    if source is None:
                        continue
                    target = directory / member.name
                    with open(target, "wb") as handle:
                        while chunk := source.read(1 << 20):
                            handle.write(chunk)
                    found[member.name] = target
    except (tarfile.TarError, EOFError, OSError) as error:
        raise TaxDbError(f"{tarball}: cannot read the taxdump: {error}") from error
    missing = [name for name in TAXDUMP_MEMBERS if name not in found]
    if missing:
        raise TaxDbError(f"{tarball}: no {', '.join(missing)}")
    return found


def build_taxdb(
    output: str | os.PathLike[str],
    *,
    taxdump: str | os.PathLike[str] | None = None,
    force: bool = False,
    fuzzy: bool = True,
    cache_dir: str | os.PathLike[str] | None = None,
    log: Callable[[str], None] | None = None,
    progress: Callable[[str, int, int | None], None] | None = None,
) -> dict[str, Any]:
    """Build the taxonomy file ``output`` from NCBI's ``new_taxdump.tar.gz``.

    ``taxdump`` is a local ``new_taxdump.tar.gz``; without it the current one
    is downloaded (into a temporary directory under ``cache_dir``, default
    next to ``output``, deleted afterwards).  The file is written next to
    ``output`` and renamed into place, so a running server never sees half
    of it.  ``fuzzy`` adds the trigram index for fuzzy search (about a third of
    the file).  Returns the file's ``info``.
    """
    output = Path(output)
    if output.exists() and not force:
        raise TaxDbError(f"{output} exists (use --force to replace it)")
    say = log or (lambda _message: None)
    output.parent.mkdir(parents=True, exist_ok=True)
    work_root = Path(cache_dir) if cache_dir is not None else output.parent
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work_root, prefix="taxdump-") as name:
        work = Path(name)
        if taxdump is None:
            from .download import download_urls, remote_size

            say(f"downloading {TAXDUMP_URL}")
            tarball = work / "new_taxdump.tar.gz"
            download_urls(
                [TAXDUMP_URL],
                tarball,
                expected_size=remote_size(TAXDUMP_URL),
                progress=progress,
            )
            source = TAXDUMP_URL
        else:
            tarball = Path(taxdump)
            source = tarball.name
        source_date = time.strftime(
            "%Y-%m-%d", time.gmtime(tarball.stat().st_mtime)
        )
        say("extracting")
        files = _extract(tarball, work)
        temporary = output.with_name(output.name + ".part")
        temporary.unlink(missing_ok=True)
        try:
            info = _write(temporary, files, source, source_date, fuzzy, say)
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
    return info


def _write(
    path: Path,
    files: dict[str, Path],
    source: str,
    source_date: str,
    fuzzy: bool,
    say: Callable[[str], None],
) -> dict[str, Any]:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; " + _SCHEMA
        )
        say("nodes")
        with connection:
            for batch in _batches(_node_row(f) for f in _fields(files["nodes.dmp"])):
                connection.executemany(
                    f"INSERT INTO node ({_NODE_COLUMNS}) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    batch,
                )
        say("names")
        _write_names(connection, files["names.dmp"])
        say("merged, deleted, divisions, genetic codes")
        with connection:
            connection.executemany(
                "INSERT OR REPLACE INTO merged VALUES (?,?)",
                ((int(f[0]), int(f[1])) for f in _fields(files["merged.dmp"])),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO deleted VALUES (?)",
                ((int(f[0]),) for f in _fields(files["delnodes.dmp"]) if f[0].strip()),
            )
            connection.executemany(
                "INSERT INTO division VALUES (?,?,?)",
                ((int(f[0]), f[1].strip(), f[2].strip()) for f in _fields(files["division.dmp"])),
            )
            connection.executemany(
                "INSERT INTO genetic_code VALUES (?,?,?,?,?)",
                (
                    (int(f[0]), _text(f[1]), f[2].strip(), f[3].strip(), f[4].strip())
                    for f in _fields(files["gencode.dmp"])
                ),
            )
        say("indexes")
        connection.executescript(_INDEXES)
        if fuzzy:
            say("fuzzy-search index")
            classes = ",".join(
                str(row[0])
                for row in connection.execute(
                    f"SELECT id FROM name_class WHERE name IN "
                    f"({','.join('?' * len(FUZZY_CLASSES))})",
                    FUZZY_CLASSES,
                )
            )
            connection.executescript(
                "CREATE VIRTUAL TABLE name_words USING fts5("
                "name_lc, content='', detail='none', "
                "tokenize='unicode61 remove_diacritics 2');"
                "INSERT INTO name_words(rowid, name_lc) "
                f"SELECT id, name_lc FROM name WHERE class_id IN ({classes});"
                "CREATE VIRTUAL TABLE name_vocab USING fts5vocab(name_words, 'row');"
                "CREATE TABLE word (id INTEGER PRIMARY KEY, w TEXT NOT NULL);"
                "INSERT INTO word(w) SELECT term FROM name_vocab "
                "WHERE length(term) >= 3 AND term GLOB '*[^0-9]*';"
                "CREATE VIRTUAL TABLE word_trigram USING fts5("
                "w, content='', tokenize='trigram');"
                "INSERT INTO word_trigram(rowid, w) SELECT id, w FROM word;"
                + _WORD_INDEX
            )
        counts = {
            "taxa": connection.execute("SELECT count(*) FROM node").fetchone()[0],
            "names": connection.execute("SELECT count(*) FROM name").fetchone()[0],
            "merged": connection.execute("SELECT count(*) FROM merged").fetchone()[0],
            "deleted": connection.execute("SELECT count(*) FROM deleted").fetchone()[0],
        }
        info = {
            "schema": SCHEMA_VERSION,
            "source": source,
            "source_date": source_date,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "fuzzy": fuzzy,
            **counts,
        }
        with connection:
            connection.executemany(
                "INSERT INTO info VALUES (?,?)",
                ((key, str(value)) for key, value in info.items()),
            )
        say("analyze")
        connection.execute("ANALYZE")
        connection.commit()
    finally:
        connection.close()
    return info


def _write_names(connection: sqlite3.Connection, path: Path) -> None:
    classes: dict[str, int] = {}
    scientific: list[tuple] = []

    def rows() -> Iterator[tuple]:
        for name_id, f in enumerate(_fields(path), 1):
            taxid, name, unique, klass = int(f[0]), f[1].strip(), f[2].strip(), f[3].strip()
            class_id = classes.setdefault(klass, len(classes) + 1)
            if klass == "scientific name":
                scientific.append((name, unique or None, taxid))
            yield (name_id, taxid, name, name.casefold(), class_id, unique)

    with connection:
        for batch in _batches(rows(), 100_000):
            connection.executemany(
                "INSERT INTO name (id, taxid, name, name_lc, class_id) VALUES (?,?,?,?,?)",
                [row[:5] for row in batch],
            )
            connection.executemany(
                "INSERT INTO unique_name VALUES (?,?,?,?)",
                [(row[5].casefold(), row[1], row[5], row[0]) for row in batch if row[5]],
            )
        connection.executemany(
            "INSERT INTO name_class VALUES (?,?)", [(v, k) for k, v in classes.items()]
        )
        for batch in _batches(iter(scientific), 100_000):
            connection.executemany(
                "UPDATE node SET name=?, unique_name=? WHERE taxid=?", batch
            )


# -- reading ----------------------------------------------------------------


def _brief(row: Sequence[Any]) -> dict[str, Any]:
    return {"taxid": row[0], "name": row[1], "rank": row[2], "parent": row[3]}


def _node(row: Sequence[Any]) -> dict[str, Any]:
    return {
        "taxid": row[0],
        "parent": row[1],
        "rank": row[2],
        "name": row[3],
        "unique_name": row[4] or row[3],
        "embl_code": row[5],
        "division_id": row[6],
        "genetic_code_id": row[7],
        "mito_genetic_code_id": row[8],
        "plastid_genetic_code_id": row[9],
        "hydrogenosome_genetic_code_id": row[10],
        "hidden": bool(row[11]),
        "hidden_subtree_root": bool(row[12]),
        "specified_species": bool(row[13]),
        "comments": row[14],
    }


def _marks(count: int) -> str:
    return ",".join("?" * count)


class TaxDb:
    """Lookups on a taxonomy file built by :func:`build_taxdb`.

    Safe to share between threads (each thread has its own read-only
    connection).  When the file is replaced (the build renames the new one
    into place) the next call opens the new one: no restart is needed.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._local = threading.local()
        self._info: dict[str, Any] = {}
        if not self.path.is_file():
            raise TaxDbError(f"taxonomy file {self.path} not found")
        self._connect()

    # -- connection -------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        stat = self.path.stat()
        identity = (stat.st_ino, stat.st_mtime_ns)
        connection = getattr(self._local, "connection", None)
        if connection is not None and self._local.identity == identity:
            return connection
        if connection is not None:
            connection.close()
        try:
            connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            connection.execute("PRAGMA query_only=ON")
        except sqlite3.Error as error:
            raise TaxDbError(f"cannot open taxonomy file {self.path}: {error}") from error
        try:
            self._info = self._read_info(connection)
        except TaxDbError:
            connection.close()
            raise
        self._local.connection = connection
        self._local.identity = identity
        return connection

    def _read_info(self, connection: sqlite3.Connection) -> dict[str, Any]:
        try:
            rows = dict(connection.execute("SELECT key, value FROM info"))
        except sqlite3.Error as error:
            raise TaxDbError(f"{self.path} is not a taxonomy file: {error}") from error
        if rows.get("schema") != str(SCHEMA_VERSION):
            raise TaxDbError(
                f"{self.path}: taxonomy file layout {rows.get('schema')!r}, "
                f"this sylphd reads {SCHEMA_VERSION}; rebuild it with `sylphd taxdb build`"
            )
        return {
            key: (int(value) if value.isdigit() else value == "True" if value in ("True", "False") else value)
            for key, value in rows.items()
        }

    def close(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None

    # -- helpers ----------------------------------------------------------
    def _resolve(self, taxids: Sequence[int]) -> dict[int, int]:
        """Map each requested taxid to the current one (merged ids followed)."""
        db = self._connect()
        current = {taxid: taxid for taxid in taxids}
        for _ in range(5):
            pending = sorted({v for v in current.values()})
            if not pending:
                break
            moved = dict(
                db.execute(
                    f"SELECT old, new FROM merged WHERE old IN ({_marks(len(pending))})",
                    pending,
                )
            )
            if not moved:
                break
            current = {k: moved.get(v, v) for k, v in current.items()}
        return current

    # -- queries ----------------------------------------------------------
    def info(self) -> dict[str, Any]:
        """File facts, plus the division and genetic code tables."""
        db = self._connect()
        info = dict(self._info)
        info["divisions"] = [
            {"id": i, "code": c, "name": n}
            for i, c, n in db.execute("SELECT id, code, name FROM division ORDER BY id")
        ]
        info["genetic_codes"] = [
            {"id": i, "abbreviation": a, "name": n, "amino_acids": aa, "starts": s}
            for i, a, n, aa, s in db.execute(
                "SELECT id, abbreviation, name, amino_acids, starts "
                "FROM genetic_code ORDER BY id"
            )
        ]
        return info

    def nodes(self, taxids: Sequence[int]) -> list[dict[str, Any] | None]:
        """The full record of each taxid, in order; ``None`` when unknown.

        A merged taxid gives its replacement's record plus ``merged_from``.
        """
        taxids = [int(t) for t in taxids]
        db = self._connect()
        current = self._resolve(taxids)
        wanted = sorted(set(current.values()))
        found: dict[int, dict[str, Any]] = {}
        for start in range(0, len(wanted), 500):
            chunk = wanted[start : start + 500]
            for row in db.execute(
                f"SELECT {_NODE_COLUMNS} FROM node WHERE taxid IN ({_marks(len(chunk))})",
                chunk,
            ):
                found[row[0]] = _node(row)
        result: list[dict[str, Any] | None] = []
        for taxid in taxids:
            record = found.get(current[taxid])
            if record is not None and current[taxid] != taxid:
                record = {**record, "merged_from": taxid}
            result.append(record)
        return result

    def lineage(self, taxids: Sequence[int]) -> list[list[dict[str, Any]] | None]:
        """For each taxid the path from the root to it: ``{taxid, name, rank}`` each."""
        db = self._connect()
        current = self._resolve([int(t) for t in taxids])
        out: list[list[dict[str, Any]] | None] = []
        for taxid in (int(t) for t in taxids):
            rows = db.execute(
                "WITH RECURSIVE up(taxid, parent, depth) AS ("
                " SELECT taxid, parent, 0 FROM node WHERE taxid=?"
                " UNION ALL"
                " SELECT n.taxid, n.parent, up.depth+1 FROM node n JOIN up ON n.taxid=up.parent"
                " WHERE up.parent != up.taxid AND up.depth < ?)"
                " SELECT n.taxid, n.name, n.rank FROM up JOIN node n ON n.taxid=up.taxid"
                " ORDER BY up.depth DESC",
                (current[taxid], MAX_DEPTH),
            ).fetchall()
            out.append(
                [{"taxid": r[0], "name": r[1], "rank": r[2]} for r in rows] or None
            )
        return out

    def children(
        self,
        taxid: int,
        *,
        descendants: bool = False,
        rank: str | None = None,
        limit: int = MAX_DESCENDANTS,
    ) -> dict[str, Any]:
        """Direct children (or all descendants) of ``taxid``, optionally of one rank.

        ``{"taxid": the current id, "taxa": [{taxid, name, rank, parent}], "truncated": bool}``;
        ``taxa`` is ``None`` for an unknown taxid.
        """
        db = self._connect()
        current = self._resolve([int(taxid)])[int(taxid)]
        if db.execute("SELECT 1 FROM node WHERE taxid=?", (current,)).fetchone() is None:
            return {"taxid": current, "taxa": None, "truncated": False}
        limit = max(1, min(int(limit), MAX_DESCENDANTS))
        where = "AND d.rank = ?" if rank else ""
        params: list[Any] = [current]
        if rank:
            params.append(rank)
        params.append(limit + 1)
        if descendants:
            sql = (
                "WITH RECURSIVE down(taxid) AS ("
                " SELECT taxid FROM node WHERE parent=? AND taxid != parent"
                " UNION ALL"
                " SELECT n.taxid FROM node n JOIN down ON n.parent=down.taxid"
                " WHERE n.taxid != n.parent)"
                f" SELECT d.taxid, d.name, d.rank, d.parent FROM down JOIN node d "
                f"ON d.taxid=down.taxid WHERE 1 {where} ORDER BY d.taxid LIMIT ?"
            )
        else:
            sql = (
                "SELECT d.taxid, d.name, d.rank, d.parent FROM node d "
                f"WHERE d.parent=? AND d.taxid != d.parent {where} ORDER BY d.taxid LIMIT ?"
            )
        rows = db.execute(sql, params).fetchall()
        return {
            "taxid": current,
            "taxa": [_brief(r) for r in rows[:limit]],
            "truncated": len(rows) > limit,
        }

    def names(self, taxids: Sequence[int]) -> list[list[dict[str, Any]] | None]:
        """Every name of each taxid (``name``, ``name_class``, ``unique_name``)."""
        db = self._connect()
        current = self._resolve([int(t) for t in taxids])
        out: list[list[dict[str, Any]] | None] = []
        for taxid in (int(t) for t in taxids):
            rows = db.execute(
                "SELECT n.name, c.name, u.unique_name FROM name n "
                "JOIN name_class c ON c.id=n.class_id "
                "LEFT JOIN unique_name u ON u.name_id=n.id "
                "WHERE n.taxid=? ORDER BY c.id, n.name",
                (current[taxid],),
            ).fetchall()
            out.append(
                [{"name": r[0], "name_class": r[1], "unique_name": r[2]} for r in rows]
                or None
            )
        return out

    def lookup(self, names: Sequence[str]) -> list[dict[str, Any]]:
        """Exact (case-insensitive) name to taxid, for each name in order.

        ``[{"query", "matches": [{taxid, name, name_class, scientific_name, rank}]}]``.
        A name that is a unique name (``Bacteria <bacteria>``) matches too.
        """
        db = self._connect()
        queries = [str(n) for n in names]
        keys = sorted({q.strip().casefold() for q in queries})
        matches: dict[str, list[dict[str, Any]]] = {}
        for start in range(0, len(keys), 500):
            chunk = keys[start : start + 500]
            marks = _marks(len(chunk))
            for key, taxid, name, klass, sci, rank in db.execute(
                f"SELECT n.name_lc, n.taxid, n.name, c.name, d.name, d.rank "
                f"FROM name n JOIN name_class c ON c.id=n.class_id "
                f"JOIN node d ON d.taxid=n.taxid WHERE n.name_lc IN ({marks}) "
                f"UNION "
                f"SELECT u.name_lc, u.taxid, u.unique_name, 'unique name', d.name, d.rank "
                f"FROM unique_name u JOIN node d ON d.taxid=u.taxid "
                f"WHERE u.name_lc IN ({marks})",
                [*chunk, *chunk],
            ):
                matches.setdefault(key, []).append(
                    {
                        "taxid": taxid,
                        "name": name,
                        "name_class": klass,
                        "scientific_name": sci,
                        "rank": rank,
                    }
                )
        return [
            {"query": q, "matches": sorted(matches.get(q.strip().casefold(), []), key=_match_order)}
            for q in queries
        ]

    def search(
        self,
        query: str,
        *,
        fuzzy: bool = False,
        rank: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Names starting with ``query`` (or, with ``fuzzy``, similar to it).

        Each hit is ``{taxid, name, name_class, scientific_name, rank, score}``
        (``score`` 1.0 for an exact name; for ``fuzzy`` the similarity 0-1),
        best first, at most one per taxid.  Fuzzy search needs the file to be
        built with the fuzzy index.
        """
        limit = max(1, min(int(limit), MAX_SEARCH))
        text = query.strip().casefold()
        if not text:
            return []
        if fuzzy:
            return self._fuzzy(text, rank, limit)
        return self._prefix(text, rank, limit)

    def _prefix(self, text: str, rank: str | None, limit: int) -> list[dict[str, Any]]:
        db = self._connect()
        upper = text[:-1] + chr(ord(text[-1]) + 1)
        extra = "AND d.rank = ?" if rank else ""
        params: list[Any] = [text, upper]
        if rank:
            params.append(rank)
        params.append(limit * 5)
        rows = db.execute(
            "SELECT n.taxid, n.name, c.name, d.name, d.rank, n.name_lc "
            "FROM name n JOIN name_class c ON c.id=n.class_id JOIN node d ON d.taxid=n.taxid "
            f"WHERE n.name_lc >= ? AND n.name_lc < ? {extra} ORDER BY n.name_lc LIMIT ?",
            params,
        ).fetchall()
        hits = [
            {
                "taxid": r[0],
                "name": r[1],
                "name_class": r[2],
                "scientific_name": r[3],
                "rank": r[4],
                "score": 1.0 if r[5] == text else round(len(text) / len(r[5]), 4),
            }
            for r in rows
        ]
        return _best_per_taxid(hits, limit)

    def _similar_words(self, word: str) -> list[str]:
        """The vocabulary words closest to ``word`` (itself first when it exists).

        Candidates come from two trigram queries: the words sharing many
        trigrams with it, and the words containing its left or its right half
        whole (a single typo spoils at most one half).  They are re-ranked by
        similarity.
        """
        db = self._connect()
        exact = db.execute("SELECT w FROM word WHERE w=?", (word,)).fetchone()
        grams = sorted({word[i : i + 3] for i in range(len(word) - 2)})
        n = len(word)
        halves = {word[: (n + 1) // 2 + 1], word[max(0, n // 2 - 1) :]}

        def quoted(parts: Iterable[str]) -> str:
            return " OR ".join('"' + p.replace('"', '""') + '"' for p in parts)

        candidates: set[str] = set()
        for expression, limit, order in (
            (quoted(grams), FUZZY_WORDS, "rank"),
            (quoted(halves), FUZZY_WORDS, f"abs(length(w.w) - {n})"),
        ):
            candidates.update(
                w
                for (w,) in db.execute(
                    "SELECT w.w FROM word w WHERE w.id IN "
                    "(SELECT rowid FROM word_trigram WHERE word_trigram MATCH ?"
                    + (" ORDER BY rank LIMIT ?)" if order == "rank" else ")")
                    + ("" if order == "rank" else f" ORDER BY {order} LIMIT ?"),
                    (expression, limit),
                )
            )
        if n <= FUZZY_SHORT:
            # Too few trigrams to find a typo'd short word: try every single edit.
            variants = _edits(word)
            for start in range(0, len(variants), 500):
                chunk = variants[start : start + 500]
                candidates.update(
                    w
                    for (w,) in db.execute(
                        f"SELECT w FROM word WHERE w IN ({_marks(len(chunk))})", chunk
                    )
                )
        candidates.discard(word)
        scored = sorted(
            ((difflib.SequenceMatcher(None, word, w).ratio(), w) for w in candidates),
            reverse=True,
        )
        close = [w for score, w in scored if score >= FUZZY_WORD_CUTOFF]
        # The exact word always counts; a typo's correction needs room next to it.
        return ([word] if exact else []) + close[: 4 if exact else 5]

    def _fuzzy(self, text: str, rank: str | None, limit: int) -> list[dict[str, Any]]:
        """Correct each query word against the vocabulary, find the names with
        those words, and rank them by similarity to the whole query."""
        db = self._connect()
        if not self._info.get("fuzzy"):
            raise TaxDbError(
                "this taxonomy file has no fuzzy-search index; rebuild it without --no-fuzzy"
            )
        groups: list[list[str]] = []
        for word in re.findall(r"[^\W_]+", text):
            options = self._similar_words(word) if len(word) >= 3 else []
            groups.append(options or [word])

        def quoted(word: str) -> str:
            return '"' + word.replace('"', '""') + '"'

        extra = "AND d.rank = ?" if rank else ""
        rows: list[Any] = []
        # All words first; if that finds nothing, any of them.
        for joiner in (" AND ", " OR "):
            expression = joiner.join(
                "(" + " OR ".join(quoted(w) for w in group) + ")" for group in groups
            )
            rows = db.execute(
                "SELECT n.taxid, n.name, c.name, d.name, d.rank, n.name_lc "
                "FROM (SELECT rowid FROM name_words WHERE name_words MATCH ?) t "
                "JOIN name n ON n.id=t.rowid JOIN name_class c ON c.id=n.class_id "
                f"JOIN node d ON d.taxid=n.taxid WHERE 1 {extra} "
                "ORDER BY abs(length(n.name_lc) - ?) LIMIT ?",
                [expression, *([rank] if rank else []), len(text), FUZZY_CANDIDATES],
            ).fetchall()
            if rows or len(groups) == 1:
                break
        hits = []
        sorted_text = " ".join(sorted(re.findall(r"[^\W_]+", text)))
        for taxid, name, klass, sci, node_rank, lc in rows:
            score = max(
                difflib.SequenceMatcher(None, text, lc).ratio(),
                # the same words in another order count as well
                difflib.SequenceMatcher(
                    None, sorted_text, " ".join(sorted(re.findall(r"[^\W_]+", lc)))
                ).ratio(),
            )
            if score >= FUZZY_CUTOFF:
                hits.append(
                    {
                        "taxid": taxid,
                        "name": name,
                        "name_class": klass,
                        "scientific_name": sci,
                        "rank": node_rank,
                        "score": round(score, 4),
                    }
                )
        return _best_per_taxid(hits, limit)


def _edits(word: str) -> list[str]:
    """Every string one deletion, substitution, insertion or swap away from ``word``."""
    splits = [(word[:i], word[i:]) for i in range(len(word) + 1)]
    edits = {a + b[1:] for a, b in splits if b}
    edits |= {a + c + b[1:] for a, b in splits if b for c in _LETTERS}
    edits |= {a + c + b for a, b in splits for c in _LETTERS}
    edits |= {a + b[1] + b[0] + b[2:] for a, b in splits if len(b) > 1}
    edits.discard(word)
    return sorted(edits)


def _match_order(match: dict[str, Any]) -> tuple[int, int]:
    return (0 if match["name_class"] == "scientific name" else 1, match["taxid"])


def _best_per_taxid(hits: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    hits.sort(key=lambda h: (-h["score"], h["name_class"] != "scientific name", h["taxid"]))
    seen: set[int] = set()
    out = []
    for hit in hits:
        if hit["taxid"] in seen:
            continue
        seen.add(hit["taxid"])
        out.append(hit)
        if len(out) >= limit:
            break
    return out
