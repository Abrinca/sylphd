"""Profiling work behind the HTTP endpoints: the loaded databases and the jobs.

Everything here is synchronous and blocking; ``server.py`` runs it in worker
threads.  Client mistakes surface as :class:`BadRequest`.

A server profiles against a :class:`DatabaseSet`, one or more sylph databases.
A single pooled :class:`pysylph.Profiler` serves every request of one parameter
set, because with two or more databases the constructor scans every genome.
``Profiler`` objects are read-only and safe to share between threads: ``query``
and ``profile`` take ``&self``, release the GIL, and the two-stage database
reads dense blocks with positional reads (no shared cursor, a mutex around its
cache), so one instance per parameter set is cached and used by all workers.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
import warnings
from collections import OrderedDict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pysylph

from .assembly import (
    DECOMPRESSION_FACTOR,
    classify_hits,
    gunzip_limited,
    is_gzip,
    parse_assembly,
)
from .metadata import METADATA_COLUMNS, MetadataDB, accession_from_genome_name
from .protocol import ProfileParams, result_row
from .sketchcheck import SketchFormatError, SketchHeader, parse_sketch_header

log = logging.getLogger("sylphd")

#: A database name may be given as ``NAME=PATH``; the name is an alias.
_NAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]+\Z")

#: How many ``Profiler`` objects one server keeps.  The parameters are few
#: (``/assembly`` varies only ``min_ani``); the limit only exists so that a
#: client cannot make the server hold unbounded state.
DEFAULT_PROFILER_CACHE_SIZE = 8

#: Databases loaded at once; both pysylph loaders release the GIL.
LOAD_WORKERS = 8


class BadRequest(ValueError):
    """A client error detected while running a job in a worker thread."""


@dataclass(frozen=True)
class DatabaseSpec:
    """One ``--db`` argument: a path and an optional display name."""

    path: Path
    name: str | None = None

    @classmethod
    def parse(cls, text: str) -> DatabaseSpec:
        """Parse ``PATH`` or ``NAME=PATH``.

        The name is only split off at the first ``=`` when the part before it
        looks like a name, so a path containing ``=`` still works.
        """
        name, separator, rest = text.partition("=")
        if separator and _NAME_PATTERN.match(name):
            if not rest:
                raise ValueError(f"database spec {text!r}: missing path after {name}=")
            return cls(Path(rest), name)
        return cls(Path(text))

    @property
    def display_name(self) -> str:
        """The name ``/info`` reports: the alias, else the file name."""
        return self.name if self.name is not None else self.path.name


class LoadedDatabase:
    """A loaded sylph database plus the identity reported by ``/info``."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        name: str | None = None,
        compute_hash: bool = False,
    ):
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"database not found: {self.path}")
        self.name = name if name is not None else self.path.name
        self.two_stage = self.path.name.endswith(".syl2db")
        if self.two_stage:
            self.db: Any = pysylph.TwoStageDatabase.load(str(self.path))
            self.k = int(self.db.k)
            self.c = int(self.db.c)
            self.screen_c: int | None = int(self.db.screen_c)
        else:
            self.db = pysylph.Database.load(str(self.path))
            if len(self.db) == 0:
                raise ValueError(f"database is empty: {self.path}")
            self.k, self.c = self._plain_parameters()
            self.screen_c = None
        self.genomes = len(self.db)
        self.size = self.path.stat().st_size
        self.sha256: str | None = None
        if compute_hash:
            self.sha256 = _sha256(self.path)

    def _plain_parameters(self) -> tuple[int, int]:
        """``k`` and the smallest ``c`` over the genomes of a plain database.

        Genomes of one ``.syldb`` may differ in ``c``; a sample must be sparser
        than *every* one of them, so the database's ``c`` is their minimum (the
        first genome's ``c`` would advertise more than every genome allows and
        every request would fail later, naming a genome).  A mixed ``k`` is
        refused here rather than by pysylph on the first request.  Reading
        ``c``/``k`` does not decode k-mers; only plain databases are scanned
        here, never a ``.syl2db`` (whose dense sketches are read on demand).
        """
        first = self.db[0]
        k = int(first.k)
        c = int(first.c)
        for index in range(1, len(self.db)):
            genome = self.db[index]
            if int(genome.k) != k:
                raise ValueError(
                    f"database {self.name}: its genomes were sketched with "
                    f"different k ({k} for {first.name!r} and "
                    f"{int(genome.k)} for {genome.name!r}); rebuild it with one k"
                )
            c = min(c, int(genome.c))
        return k, c

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "size": self.size,
            "sha256": self.sha256,
            "genomes": self.genomes,
            "c": self.c,
            "k": self.k,
            "screen_c": self.screen_c,
            "two_stage": self.two_stage,
        }

    def entry(self) -> dict[str, Any]:
        """This database for ``/info``'s ``databases`` list (``info()`` + file)."""
        return {
            "name": self.name,
            "file": self.path.name,
            "path": str(self.path),
            "size": self.size,
            "sha256": self.sha256,
            "genomes": self.genomes,
            "c": self.c,
            "k": self.k,
            "screen_c": self.screen_c,
            "two_stage": self.two_stage,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DatabaseSet:
    """The databases one server profiles against, loaded once at startup.

    ``/info`` reports the pooled view: ``c`` is the smallest ``c`` (the value
    samples must be sketched with), ``k`` the common ``k``, ``genomes`` the
    sum.  ``name``/``path`` of several databases are their names/paths joined
    with ``", "``; a single database reports exactly what it did before.
    """

    def __init__(
        self,
        databases: Sequence[LoadedDatabase],
        *,
        cache_size: int = DEFAULT_PROFILER_CACHE_SIZE,
    ) -> None:
        if not databases:
            raise ValueError("at least one database is required")
        self.databases = tuple(databases)
        self._check_names()
        self._check_k()
        self.c = min(database.c for database in self.databases)
        self.k = self.databases[0].k
        self.genomes = sum(database.genomes for database in self.databases)
        self.size = sum(database.size for database in self.databases)
        #: Pooled view: the paths/names joined, so ``info()`` keeps its shape.
        #: A single database reports its own name, which is the file name
        #: unless the operator gave ``--db NAME=PATH``.
        self.path = ", ".join(str(database.path) for database in self.databases)
        self.name = ", ".join(database.name for database in self.databases)
        self.two_stage = all(database.two_stage for database in self.databases)
        screen_cs = {database.screen_c for database in self.databases}
        self.screen_c = screen_cs.pop() if len(screen_cs) == 1 else None
        self.sha256 = (
            self.databases[0].sha256 if len(self.databases) == 1 else None
        )
        if len({database.c for database in self.databases}) > 1:
            log.warning(
                "databases differ in c (%s); samples are sketched at the smallest, c=%d",
                ", ".join(f"{database.name}: c={database.c}" for database in self.databases),
                self.c,
            )
        self._cache_size = cache_size
        self._profilers: OrderedDict[ProfileParams, Any] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._duplicates_logged = False
        #: Number of ``Profiler`` objects built so far (for logging and tests).
        self.profiler_builds = 0

    # -- loading ---------------------------------------------------------

    @classmethod
    def load(
        cls,
        specs: Sequence[DatabaseSpec],
        *,
        compute_hash: bool = False,
        cache_size: int = DEFAULT_PROFILER_CACHE_SIZE,
    ) -> DatabaseSet:
        """Load every database, in parallel, keeping the ``--db`` order."""
        if not specs:
            raise ValueError("at least one database is required")
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=min(LOAD_WORKERS, len(specs))) as pool:
            loaded = list(
                pool.map(
                    lambda spec: _load_one(spec, compute_hash=compute_hash), specs
                )
            )
        elapsed = time.perf_counter() - start
        databases = cls([database for database, _ in loaded], cache_size=cache_size)
        for database, seconds in loaded:
            log.info(
                "loaded %s: %s, %s genomes, c=%d, k=%d, %.2f s",
                database.name,
                "two-stage" if database.two_stage else "plain",
                f"{database.genomes:,}",
                database.c,
                database.k,
                seconds,
            )
        log.info(
            "loaded %d database(s): %s genomes, effective c=%d, k=%d, %.1f s",
            len(databases.databases),
            f"{databases.genomes:,}",
            databases.c,
            databases.k,
            elapsed,
        )
        return databases

    def _check_names(self) -> None:
        seen: dict[str, Path] = {}
        for database in self.databases:
            if database.name in seen:
                raise ValueError(
                    f"two databases are both named {database.name!r} "
                    f"({seen[database.name]} and {database.path}); "
                    "give one an alias with --db NAME=PATH"
                )
            seen[database.name] = database.path

    def _check_k(self) -> None:
        values = {database.k for database in self.databases}
        if len(values) > 1:
            listed = ", ".join(
                f"{database.name}: k={database.k}" for database in self.databases
            )
            raise ValueError(
                f"databases were sketched with different k ({listed}); "
                "all databases of one server must share k"
            )

    # -- description -----------------------------------------------------

    def info(self) -> dict[str, Any]:
        """The pooled view ``/info`` (and ``/assembly``) reports as ``database``."""
        return {
            "name": self.name,
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "genomes": self.genomes,
            "c": self.c,
            "k": self.k,
            "screen_c": self.screen_c,
            "two_stage": self.two_stage,
        }

    def entries(self) -> list[dict[str, Any]]:
        """One entry per database, for ``/info``'s ``databases`` list."""
        return [database.entry() for database in self.databases]

    # -- requests --------------------------------------------------------

    def check_sketch(self, header: SketchHeader, name: str) -> None:
        """Reject a sample sketch no database can profile, naming the culprit."""
        if header.k != self.k:
            if len(self.databases) == 1:
                raise BadRequest(
                    f"sample {name!r}: sketch k={header.k} does not match "
                    f"the database k={self.k}"
                )
            listed = ", ".join(f"{database.name}: k={database.k}" for database in self.databases)
            raise BadRequest(
                f"sample {name!r}: sketch k={header.k} does not match the "
                f"databases ({listed})"
            )
        if header.c > self.c:
            limiting = [
                database for database in self.databases if database.c == self.c
            ]
            if len(self.databases) == 1:
                raise BadRequest(
                    f"sample {name!r}: sketch c={header.c} is sparser than "
                    f"the database c={self.c}"
                )
            raise BadRequest(
                f"sample {name!r}: sketch c={header.c} is sparser than the c="
                f"{self.c} of {', '.join(database.name for database in limiting)}"
            )

    def profiler(self, params: ProfileParams) -> Any:
        """The ``Profiler`` for these parameters, built once and cached.

        With two or more databases the constructor scans every genome, so it
        is kept per parameter set.  Building happens outside the lock; if two
        threads race on a cold key both build, and the first insert wins (the
        second is dropped) so every caller gets the same object.
        """
        with self._cache_lock:
            cached = self._profilers.get(params)
            if cached is not None:
                self._profilers.move_to_end(params)
                return cached
        profiler = self._build_profiler(params)
        with self._cache_lock:
            existing = self._profilers.get(params)
            if existing is not None:
                self._profilers.move_to_end(params)
                return existing
            self._profilers[params] = profiler
            while len(self._profilers) > self._cache_size:
                self._profilers.popitem(last=False)
            return profiler

    def clear_profilers(self) -> None:
        """Drop the cached ``Profiler`` objects (for tests; nothing else needs it)."""
        with self._cache_lock:
            self._profilers.clear()

    def _build_profiler(self, params: ProfileParams) -> Any:
        """Build a :class:`pysylph.Profiler`, rejecting bad parameters as a 400.

        ``pysylph`` raises ``ValueError`` in the constructor for parameters or
        databases it cannot use (``k`` differing between databases, a parameter
        out of range).  Those are the client's request, not a server fault.
        """
        with self._cache_lock:
            self.profiler_builds += 1
        try:
            with warnings.catch_warnings(record=True) as caught:
                # pysylph warns (once per construction) when two databases hold
                # the same genome; sylph's reassignment then keeps the earlier
                # one.  Surface a short summary instead of losing the warning.
                warnings.simplefilter("always", UserWarning)
                profiler = pysylph.Profiler(
                    *(database.db for database in self.databases),
                    **params.profiler_kwargs(),
                )
        except ValueError as error:
            raise BadRequest(f"cannot profile against this database: {error}") from error
        self._log_duplicate_warning(caught)
        return profiler

    def _log_duplicate_warning(self, caught: Sequence[Any]) -> None:
        """Log pysylph's duplicate-genome warning once, with a bounded summary."""
        messages = [
            str(warning.message)
            for warning in caught
            if issubclass(warning.category, UserWarning)
        ]
        if not messages:
            return
        with self._cache_lock:
            if self._duplicates_logged:
                return
            self._duplicates_logged = True
        log.warning("pysylph: %s", _bounded_duplicate_summary(messages[0]))


#: The count in pysylph's duplicate-genome warning.
_DUPLICATE_COUNT = re.compile(r"(\d+)\s+genome\(s\) appear more than once")
#: The name list of that warning.
_DUPLICATE_NAMES = re.compile(r"contig name\): (.*?)\. This can cause", re.DOTALL)


def _bounded_duplicate_summary(message: str, limit: int = 200) -> str:
    """A one-line, length-bounded version of pysylph's duplicate warning.

    pysylph lists every duplicate; a large overlap would flood the log, so the
    count and the first names (up to ``limit`` characters) are enough.
    """
    count = _DUPLICATE_COUNT.search(message)
    if count is None:
        summary = " ".join(message.split())
    else:
        summary = (
            f"{count.group(1)} genome(s) appear more than once across the databases"
        )
        names = _DUPLICATE_NAMES.search(message)
        if names is not None:
            summary += f" (same file name and contig name), first: {names.group(1)}"
    summary = " ".join(summary.split())
    if len(summary) > limit:
        summary = summary[: limit - 1] + "…"
    return summary


def _load_one(
    spec: DatabaseSpec, *, compute_hash: bool
) -> tuple[LoadedDatabase, float]:
    start = time.perf_counter()
    database = LoadedDatabase(spec.path, name=spec.name, compute_hash=compute_hash)
    return database, time.perf_counter() - start


def run_assembly(
    databases: DatabaseSet,
    metadata: MetadataDB | None,
    name: str,
    payload: bytes,
    top: int,
    min_ani: float,
    max_sequence_bytes: int,
) -> dict[str, Any]:
    """Parse, sketch and profile one assembly.  Runs in a worker thread."""
    data = payload
    if is_gzip(data):
        data = gunzip_limited(data, max_sequence_bytes * DECOMPRESSION_FACTOR)
    parsed = parse_assembly(data)

    sketcher = pysylph.Sketcher(c=databases.c, k=databases.k)
    sketch = sketcher.sketch_single(name, list(parsed.sequences))
    result = _assembly_hits(databases, metadata, sketch, top, min_ani)
    result["query"] = {
        "name": name,
        "records": parsed.records,
        "length": parsed.length,
        "gc": parsed.gc,
        "sketch_kmers": sketch.n_kmers,
        "input": "sequence",
    }
    return result


def run_assembly_sketch(
    databases: DatabaseSet,
    metadata: MetadataDB | None,
    name: str,
    payload: bytes,
    top: int,
    min_ani: float,
    stats: dict[str, Any],
) -> dict[str, Any]:
    """Profile a precomputed assembly sketch.  Runs in a worker thread.

    The sketch is validated exactly like ``/profile`` uploads; everything after
    that is shared with the raw-sequence path.  The server cannot know the
    number of records, the length or the GC content of an assembly it never
    saw, so those come from the optional (informational) query parameters of
    the client, else ``null``.
    """
    sketch, header = _decode_sketch(databases, name, payload)
    result = _assembly_hits(databases, metadata, sketch, top, min_ani)
    result["query"] = {
        "name": name,
        "records": stats["records"],
        "length": stats["length"],
        "gc": stats["gc"],
        "sketch_kmers": header.kmer_count,
        "input": "sketch",
    }
    return result


def _decode_sketch(
    databases: DatabaseSet, name: str, payload: bytes
) -> tuple[Any, Any]:
    """Validate sketch bytes against the databases and decode them.

    Checks the fixed header before decoding: sylph's deserializer allocates
    from the k-mer count prefix, and a huge count aborts the whole process.
    This also lets a c/k mismatch be rejected without deserializing anything.
    """
    try:
        header = parse_sketch_header(payload)
    except SketchFormatError as error:
        raise BadRequest(f"sample {name!r}: not a valid sample sketch ({error})") from error
    databases.check_sketch(header, name)
    try:
        sketch = pysylph.SampleSketch.from_bytes(payload)
    except (ValueError, OSError) as error:
        raise BadRequest(f"sample {name!r}: not a valid sample sketch ({error})") from error
    return sketch, header


def _run_profiler(profiler: Any, sketch: Any, mode: str) -> list[Any]:
    """Run ``profiler`` on ``sketch``, turning its validation errors into 400s.

    ``pysylph`` raises ``ValueError`` from ``profile``/``query`` when the
    sample sketch does not fit the database (a ``k`` mismatch, or a database
    genome denser than the sample sketch), which sylph cannot profile either.
    The scope is deliberately narrow: only these two calls translate errors,
    so an unrelated ``ValueError`` elsewhere stays a 500.
    """
    try:
        return profiler.profile(sketch) if mode == "profile" else profiler.query(sketch)
    except ValueError as error:
        raise BadRequest(f"cannot profile this sample: {error}") from error


def assembly_params(min_ani: float) -> ProfileParams:
    """The profiler parameters of ``/assembly``: naive ANI, one screen."""
    return ProfileParams(
        no_adjust=True,
        minimum_ani=min_ani,
        screen_ani=min(min_ani, 85.0),
    )


def _assembly_hits(
    databases: DatabaseSet,
    metadata: MetadataDB | None,
    sketch: Any,
    top: int,
    min_ani: float,
) -> dict[str, Any]:
    """Query ``sketch`` (naive ANI), join metadata and classify the hits."""
    profiler = databases.profiler(assembly_params(min_ani))
    results = sorted(
        _run_profiler(profiler, sketch, "query"),
        key=lambda result: float(result.ani),
        reverse=True,
    )
    results = results[:top]

    columns = tuple(metadata.field_columns) if metadata is not None else METADATA_COLUMNS
    accessions = [
        accession_from_genome_name(result.genome_sketch.name) for result in results
    ]
    found = (
        metadata.lookup(accession for accession in accessions if accession)
        if metadata is not None
        else {}
    )
    resolved = (
        metadata.resolve(
            [
                (result.genome_sketch.name, result.genome_sketch.description)
                for result in results
            ]
        )
        if metadata is not None
        else []
    )

    hits: list[dict[str, Any]] = []
    for rank, result in enumerate(results, start=1):
        genome = result.genome_sketch
        accession = accession_from_genome_name(genome.name)
        contained, total = (int(value) for value in result.containment)
        row: dict[str, Any] = {
            "rank": rank,
            "ani": float(result.ani),
            "containment": [contained, total],
            "containment_fraction": round(contained / total, 3) if total else 0.0,
            "genome_file": genome.name,
            "accession": accession,
        }
        record: dict[str, Any] = dict(found.get(accession, {})) if accession else {}
        if resolved:
            record.update(resolved[rank - 1].columns())
        for column in columns:
            row[column] = record.get(column)
        hits.append(row)

    assignment, closest = classify_hits(hits)
    return {"assignment": assignment, "closest": closest, "hits": hits}


def run_sample(
    databases: DatabaseSet,
    metadata: MetadataDB | None,
    mode: str,
    params: ProfileParams,
    fields: Sequence[str] | None,
    name: str,
    payload: bytes,
    hide_ani_ci: bool = False,
    tax_sources: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Deserialize, validate and profile one sketch.  Runs in a worker thread.

    ``tax_sources`` restricts hit resolution to those taxonomy sources
    (``--tax-source``); it is ignored without a metadata database.
    """
    sketch, _ = _decode_sketch(databases, name, payload)

    profiler = databases.profiler(params)
    results = _run_profiler(profiler, sketch, mode)
    rows = result_rows(
        metadata,
        results,
        name,
        mode=mode,
        fields=fields,
        hide_ani_ci=hide_ani_ci,
        tax_sources=tax_sources,
    )
    return {"name": name, "kind": mode, "rows": rows}


def run_contig_query(
    databases: DatabaseSet,
    metadata: MetadataDB | None,
    name: str,
    payload: bytes,
    params: ProfileParams,
    fields: Sequence[str] | None,
    tax_sources: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Query one contig sketch for ``POST /contigs``.  Runs in a worker thread.

    This is the identity-only half of the endpoint: it validates and decodes
    the sketch, runs the query with ``params`` (the per-contig permissive set)
    and joins the metadata.  The contig-relative ANI, the ranking and the
    classification happen in :mod:`sylphd.per_contig`, which needs the contig's
    genome-style k-mer count and so cannot live here.
    """
    sketch, header = _decode_sketch(databases, name, payload)
    profiler = databases.profiler(params)
    results = _run_profiler(profiler, sketch, "query")
    return {
        "sketch_kmers": header.kmer_count,
        "rows": result_rows(
            metadata,
            results,
            name,
            mode="query",
            fields=fields,
            hide_ani_ci=True,
            tax_sources=tax_sources,
        ),
    }


def result_rows(
    metadata: MetadataDB | None,
    results: Sequence[Any],
    name: str,
    *,
    mode: str = "query",
    fields: Sequence[str] | None = None,
    hide_ani_ci: bool = False,
    tax_sources: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Convert ``pysylph`` results into JSON rows, joining the metadata.

    Without a metadata database the rows are sylph's own columns only.  With
    one, the hit's metadata record and the taxonomy a sylph-tax source
    resolved for it are merged first, so ``fields`` selects from both.
    """
    if metadata is None:
        return [
            result_row(result, name, mode=mode, hide_ani_ci=hide_ani_ci)
            for result in results
        ]
    accessions = [
        accession_from_genome_name(result.genome_sketch.name) for result in results
    ]
    found = metadata.lookup(accession for accession in accessions if accession)
    resolved = metadata.resolve(
        [
            (result.genome_sketch.name, result.genome_sketch.description)
            for result in results
        ],
        sources=tax_sources,
    )
    rows: list[dict[str, Any]] = []
    for result, accession, taxonomy in zip(results, accessions, resolved):
        record: dict[str, Any] = dict(found.get(accession, {})) if accession else {}
        record.update(taxonomy.columns())
        rows.append(
            result_row(
                result,
                name,
                mode=mode,
                metadata=record,
                fields=fields,
                hide_ani_ci=hide_ani_ci,
            )
        )
    return rows

