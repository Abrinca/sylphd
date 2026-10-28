"""Starlette application and uvicorn entry point for ``sylphd serve``.

Design notes
------------
* The database is loaded once, at startup, and shared by every request.
* Blocking ``pysylph`` calls run in worker threads through
  ``anyio.to_thread.run_sync``; a semaphore caps the number of concurrent
  profiling jobs (``--max-jobs``) and queues the rest, optionally up to
  ``--max-queue`` requests.
* A fresh ``Profiler`` is built per request: it holds references only.
* One process, one worker: the whole point is sharing the in-memory index.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import stat
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import anyio
import pysylph
import uvicorn
from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import __version__
from .assembly import (
    DECOMPRESSION_FACTOR,
    INTERPRETATION_THRESHOLDS,
    SequenceError,
    SequenceTooLarge,
    gunzip_limited,
    interpret_ani,
    is_gzip,
    parse_assembly,
)
from .limits import (
    QUEUE_RETRY_SECONDS,
    JobSlots,
    QueueFullError,
    RateLimiter,
    RateLimitMiddleware,
)
from .metadata import METADATA_COLUMNS, MetadataDB, accession_from_genome_name
from .paths import SOCKET_MODE, parse_tcp
from .protocol import ProfileParams, ProtocolError, result_row
from .sketchcheck import SketchFormatError, validate_sketch_bytes
from .sketching import sample_name_from_path

#: Maximum number of sketches accepted in one request.
MAX_SKETCHES = 4096

#: Accepted ``Content-Type`` values of an ``/assembly`` upload.
SEQUENCE_CONTENT_TYPES = frozenset(
    {"text/plain", "application/octet-stream", "application/gzip"}
)

#: ``top`` range of ``POST /assembly``.
MIN_TOP, MAX_TOP = 1, 50

#: ``min_ani`` range of ``POST /assembly``.
MIN_ANI_RANGE = (70.0, 100.0)


class BadRequest(ValueError):
    """A client error detected while running a job in a worker thread."""


@dataclass
class ServerConfig:
    """Everything ``sylphd serve`` needs."""

    db_path: Path
    meta_path: Path | None = None
    max_jobs: int = 2
    max_upload_mb: int = 512
    compute_hash: bool = False
    #: Serve ``POST /assembly`` (raw sequences instead of sketches).
    allow_sequences: bool = False
    #: Upload size limit of ``/assembly`` (compressed bytes).
    max_sequence_mb: int = 20
    #: ``(count, window_seconds)`` for the per-client rate limit, or ``None``.
    rate_limit: tuple[int, float] | None = None
    #: How many requests may wait for a job slot; ``None`` is unlimited.
    max_queue: int | None = None
    #: Peers whose ``X-Forwarded-For`` header is trusted (IPs or ``"unix"``).
    trusted_proxies: tuple[str, ...] = ()
    #: Allowed CORS origins; none by default.
    cors_origins: tuple[str, ...] = ()
    #: Set when serving on a Unix socket, so the app can adjust its mode.
    socket_path: Path | None = None


class LoadedDatabase:
    """A loaded sylph database plus the identity reported by ``/info``."""

    def __init__(self, path: str | os.PathLike[str], *, compute_hash: bool = False):
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"database not found: {self.path}")
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
            self.k = int(self.db[0].k)
            self.c = int(self.db[0].c)
            self.screen_c = None
        self.genomes = len(self.db)
        self.size = self.path.stat().st_size
        self.sha256: str | None = None
        if compute_hash:
            self.sha256 = _sha256(self.path)

    @property
    def name(self) -> str:
        return self.path.name

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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class BodyLimitMiddleware:
    """Reject request bodies larger than ``max_bytes``.

    The check uses ``Content-Length`` when present, and otherwise counts the
    received body chunks as they arrive.
    """

    def __init__(self, app: Any, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                if int(raw_length) > self.max_bytes:
                    await _too_large(send, self.max_bytes)
                    return
            except ValueError:
                pass

        received = 0

        async def limited_receive() -> dict:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await _too_large(send, self.max_bytes)


class _BodyTooLarge(Exception):
    pass


async def _too_large(send: Any, max_bytes: int) -> None:
    response = JSONResponse(
        {"error": f"request body exceeds the {max_bytes // (1024 * 1024)} MB limit"},
        status_code=413,
    )
    await response({"type": "http"}, _never_receive, send)


async def _never_receive() -> dict:
    return {"type": "http.disconnect"}


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


async def _health(request: Request) -> JSONResponse:
    state = request.app.state
    return JSONResponse(
        {
            "status": "ok",
            "service": "sylphd",
            "version": __version__,
            "uptime_seconds": round(time.monotonic() - state.started, 3),
        }
    )


async def _info(request: Request) -> JSONResponse:
    state = request.app.state
    metadata = state.metadata
    return JSONResponse(
        {
            "service": "sylphd",
            "version": __version__,
            "pysylph_version": pysylph.__version__,
            "python_version": ".".join(str(part) for part in sys.version_info[:3]),
            "database": state.database.info(),
            "metadata": metadata.summary() if metadata is not None else None,
            "limits": {
                "max_jobs": state.max_jobs,
                "max_upload_mb": state.max_upload_mb,
                "max_sketches": MAX_SKETCHES,
                "allow_sequences": state.allow_sequences,
                "max_sequence_mb": state.max_sequence_mb if state.allow_sequences else None,
                "rate_limit": _rate_limit_dict(state.rate_limit),
                "max_queue": state.max_queue,
            },
            "endpoints": _endpoints(state),
        }
    )


def _endpoints(state: Any) -> list[str]:
    endpoints = ["/health", "/info", "/profile", "/query"]
    if state.allow_sequences:
        endpoints.append("/assembly")
    return endpoints


def _rate_limit_dict(rate_limit: tuple[int, float] | None) -> dict[str, Any] | None:
    if rate_limit is None:
        return None
    count, window = rate_limit
    return {"count": count, "window_seconds": window}


async def _index(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "service": "sylphd",
            "version": __version__,
            "endpoints": _endpoints(request.app.state),
        }
    )


async def _profile(request: Request) -> JSONResponse:
    return await _handle(request, "profile")


async def _query(request: Request) -> JSONResponse:
    return await _handle(request, "query")


async def _assembly(request: Request) -> JSONResponse:
    """Profile one uploaded assembly against the database (opt-in)."""
    state = request.app.state
    if not state.allow_sequences:  # route is not registered; safety net only
        return _error("not found", 404)

    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type and media_type not in SEQUENCE_CONTENT_TYPES:
        return _error(
            "expected raw FASTA or FASTQ with Content-Type text/plain, "
            "application/octet-stream or application/gzip",
            415,
        )

    try:
        top = _query_integer(request, "top", 10, MIN_TOP, MAX_TOP)
        min_ani = _query_float(request, "min_ani", 80.0, *MIN_ANI_RANGE)
    except ValueError as error:
        return _error(str(error), 400)

    name = request.query_params.get("name") or "assembly"
    if len(name) > 1024:
        return _error("'name' is too long (max 1024 characters)", 400)

    limit = state.max_sequence_mb * 1024 * 1024
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                return _too_large_response(limit)
        except ValueError:
            pass
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            return _too_large_response(limit)
        chunks.append(chunk)
    payload = b"".join(chunks)
    if not payload:
        return _error("empty sequence upload", 400)

    start = time.perf_counter()
    try:
        async with state.jobs.slot():
            result = await anyio.to_thread.run_sync(
                _run_assembly,
                state.database,
                state.metadata,
                name,
                payload,
                top,
                min_ani,
                limit,
            )
    except QueueFullError:
        return _busy()
    except SequenceTooLarge as error:
        return _error(str(error), 413)
    except SequenceError as error:
        return _error(str(error), 400)
    except Exception as error:  # pragma: no cover - defensive
        # Never echo the payload: the type is enough to debug a failure.
        return _error(f"assembly profiling failed ({type(error).__name__})", 500)

    elapsed_ms = round((time.perf_counter() - start) * 1000.0, 1)
    database = state.database
    return JSONResponse(
        {
            "query": result["query"],
            "database": {
                "name": database.name,
                "genomes": database.genomes,
                "c": database.c,
                "k": database.k,
            },
            "params": {"top": top, "min_ani": min_ani},
            "thresholds": dict(INTERPRETATION_THRESHOLDS),
            "elapsed_ms": elapsed_ms,
            "hits": result["hits"],
        }
    )


def _too_large_response(limit: int) -> JSONResponse:
    return _error(
        f"request body exceeds the {limit // (1024 * 1024)} MB sequence limit", 413
    )


def _query_integer(
    request: Request, key: str, default: int, minimum: int, maximum: int
) -> int:
    raw = request.query_params.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"'{key}' must be an integer") from None
    if value < minimum or value > maximum:
        raise ValueError(f"'{key}' must be between {minimum} and {maximum}")
    return value


def _query_float(
    request: Request, key: str, default: float, minimum: float, maximum: float
) -> float:
    raw = request.query_params.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"'{key}' must be a number") from None
    if not value == value or value in (float("inf"), float("-inf")):
        raise ValueError(f"'{key}' must be a finite number")
    if value < minimum or value > maximum:
        raise ValueError(f"'{key}' must be between {minimum:g} and {maximum:g}")
    return value


def _run_assembly(
    database: LoadedDatabase,
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

    sketcher = pysylph.Sketcher(c=database.c, k=database.k)
    sketch = sketcher.sketch_single(name, list(parsed.sequences))
    profiler = pysylph.Profiler(
        database.db,
        no_adjust=True,
        minimum_ani=min_ani,
        screen_ani=min(min_ani, 85.0),
    )
    results = sorted(profiler.query(sketch), key=lambda result: float(result.ani), reverse=True)
    results = results[:top]

    columns = tuple(metadata.columns) if metadata is not None else METADATA_COLUMNS
    accessions = [
        accession_from_genome_name(result.genome_sketch.name) for result in results
    ]
    found = (
        metadata.lookup(accession for accession in accessions if accession)
        if metadata is not None
        else {}
    )

    hits: list[dict[str, Any]] = []
    for rank, result in enumerate(results, start=1):
        genome = result.genome_sketch
        accession = accession_from_genome_name(genome.name)
        contained, total = (int(value) for value in result.containment)
        row: dict[str, Any] = {
            "rank": rank,
            "ani": round(float(result.ani), 2),
            "containment": [contained, total],
            "containment_fraction": round(contained / total, 3) if total else 0.0,
            "interpretation": interpret_ani(float(result.ani)),
            "genome_file": genome.name,
            "accession": accession,
        }
        record = found.get(accession, {}) if accession else {}
        for column in columns:
            row[column] = record.get(column)
        hits.append(row)

    return {
        "query": {
            "name": name,
            "records": parsed.records,
            "length": parsed.length,
            "gc": parsed.gc,
            "sketch_kmers": len(sketch.kmer_counts),
        },
        "hits": hits,
    }


async def _handle(request: Request, mode: str) -> JSONResponse:
    state = request.app.state
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        return _error(
            "expected a multipart/form-data body with one 'sketch' part per sample",
            415,
        )

    try:
        form = await request.form(
            max_files=MAX_SKETCHES,
            max_fields=16,
            max_part_size=1024 * 1024,
        )
    except StarletteHTTPException as error:
        return _error(str(error.detail), error.status_code)

    raw_params = form.get("params")
    try:
        params_data: Any = {} if raw_params is None else json.loads(str(raw_params))
    except json.JSONDecodeError as error:
        return _error(f"invalid JSON in 'params': {error}", 400)
    if not isinstance(params_data, dict):
        return _error("'params' must be a JSON object", 400)

    try:
        params = ProfileParams.from_mapping(params_data)
    except ProtocolError as error:
        return _error(str(error), 400)

    declared_mode = params_data.get("mode")
    if declared_mode is not None and declared_mode != mode:
        return _error(
            f"params.mode is {declared_mode!r} but this is the /{mode} endpoint", 400
        )

    fields = _validate_fields(params_data.get("fields"), state.metadata)
    if isinstance(fields, JSONResponse):
        return fields

    sketches = form.getlist("sketch")
    if not sketches:
        return _error("no 'sketch' part in the request", 400)
    if len(sketches) > MAX_SKETCHES:
        return _error(f"more than {MAX_SKETCHES} sketches in one request", 413)
    for part in sketches:
        if not isinstance(part, UploadFile):
            return _error("'sketch' parts must be file uploads", 400)

    names = _resolve_names(params_data.get("names"), sketches)
    if isinstance(names, JSONResponse):
        return names

    try:
        payloads = []
        for part in sketches:
            payloads.append(await part.read())
    except Exception as error:
        return _error(f"failed to read a 'sketch' part: {error}", 500)

    samples: list[dict[str, Any] | None] = [None] * len(sketches)
    try:
        async with anyio.create_task_group() as task_group:
            for index, (name, payload) in enumerate(zip(names, payloads)):
                task_group.start_soon(
                    _run_slot, state, mode, params, fields, name, payload, samples, index
                )
    except BadRequest as error:
        return _error(str(error), 400)
    except BaseExceptionGroup as group:
        for error in group.exceptions:
            if isinstance(error, BadRequest):
                return _error(str(error), 400)
        return _error(f"profiling failed: {group}", 500)
    except Exception as error:  # pragma: no cover - defensive
        return _error(f"profiling failed: {error}", 500)
    if any(sample is _QUEUE_FULL for sample in samples):
        return _busy()
    return JSONResponse(samples)


#: Marker stored for a sample whose job was refused because the queue was full.
_QUEUE_FULL = object()


def _busy() -> JSONResponse:
    return JSONResponse(
        {"error": "the server is busy; retry shortly"},
        status_code=503,
        headers={"Retry-After": str(QUEUE_RETRY_SECONDS)},
    )


async def _run_slot(
    state: Any,
    mode: str,
    params: ProfileParams,
    fields: Sequence[str] | None,
    name: str,
    payload: bytes,
    samples: list[dict[str, Any] | None],
    index: int,
) -> None:
    try:
        async with state.jobs.slot():
            samples[index] = await anyio.to_thread.run_sync(
                _run_sample,
                state.database,
                state.metadata,
                mode,
                params,
                fields,
                name,
                payload,
                params.no_adjust,
            )
    except QueueFullError:
        samples[index] = _QUEUE_FULL


def _run_sample(
    database: LoadedDatabase,
    metadata: MetadataDB | None,
    mode: str,
    params: ProfileParams,
    fields: Sequence[str] | None,
    name: str,
    payload: bytes,
    hide_ani_ci: bool = False,
) -> dict[str, Any]:
    """Deserialize, validate and profile one sketch.  Runs in a worker thread."""
    # Check the fixed header before decoding: sylph's deserializer allocates
    # from the k-mer count prefix, and a huge count aborts the whole process.
    # This also lets us reject a c/k mismatch without deserializing anything.
    try:
        c, k = validate_sketch_bytes(payload)
    except SketchFormatError as error:
        raise BadRequest(f"sample {name!r}: not a valid sample sketch ({error})") from error
    if k != database.k:
        raise BadRequest(
            f"sample {name!r}: sketch k={k} does not match the database k={database.k}"
        )
    if c > database.c:
        raise BadRequest(
            f"sample {name!r}: sketch c={c} is sparser than the database c={database.c}"
        )
    try:
        sketch = pysylph.SampleSketch.from_bytes(payload)
    except (ValueError, OSError) as error:
        raise BadRequest(f"sample {name!r}: not a valid sample sketch ({error})") from error

    profiler = pysylph.Profiler(database.db, **params.profiler_kwargs())
    results = profiler.profile(sketch) if mode == "profile" else profiler.query(sketch)

    rows: list[dict[str, Any]] = []
    if metadata is None:
        for result in results:
            rows.append(result_row(result, name, mode=mode, hide_ani_ci=hide_ani_ci))
    else:
        accessions = [accession_from_genome_name(result.genome_sketch.name) for result in results]
        found = metadata.lookup(accession for accession in accessions if accession)
        for result, accession in zip(results, accessions):
            rows.append(
                result_row(
                    result,
                    name,
                    mode=mode,
                    metadata=found.get(accession, {}) if accession else {},
                    fields=fields,
                    hide_ani_ci=hide_ani_ci,
                )
            )
    return {"name": name, "kind": mode, "rows": rows}


def _validate_fields(value: Any, metadata: MetadataDB | None) -> Sequence[str] | None | JSONResponse:
    if value is None:
        return None if metadata is None else tuple(metadata.columns)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return _error("'params.fields' must be a list of column names", 400)
    if not value:
        return ()
    if metadata is None:
        return _error("this server has no metadata database; --fields is not available", 400)
    unknown = [column for column in value if column not in metadata.columns]
    if unknown:
        return _error(f"unknown metadata column(s): {', '.join(unknown)}", 400)
    return tuple(value)


def _resolve_names(value: Any, sketches: Sequence[Any]) -> list[str] | JSONResponse:
    if value is None:
        names = []
        for index, part in enumerate(sketches):
            filename = getattr(part, "filename", None)
            name = sample_name_from_path(filename) if filename else ""
            names.append(name or f"sample-{index + 1}")
        return names
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return _error("'params.names' must be a list of strings", 400)
    if len(value) != len(sketches):
        return _error(
            f"'params.names' has {len(value)} entries for {len(sketches)} sketches", 400
        )
    if any(len(name) > 1024 for name in value):
        return _error("sample name too long (max 1024 characters)", 400)
    return list(value)


async def _json_http_error(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, StarletteHTTPException):
        return _error(str(exc.detail), exc.status_code)
    return _error(str(exc), 500)


def _lifespan(config: ServerConfig) -> Any:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: Starlette):
        if config.socket_path is not None:
            try:
                os.chmod(config.socket_path, SOCKET_MODE)
            except FileNotFoundError:
                pass
        try:
            yield
        finally:
            metadata = app.state.metadata
            if metadata is not None:
                metadata.close()

    return lifespan


def create_app(
    config: ServerConfig,
    *,
    database: LoadedDatabase | None = None,
    metadata: MetadataDB | None = None,
) -> Starlette:
    """Build the ASGI application (loading the database unless provided)."""
    loaded = database if database is not None else LoadedDatabase(
        config.db_path, compute_hash=config.compute_hash
    )
    metadata_store = metadata
    if metadata_store is None and config.meta_path is not None:
        metadata_store = MetadataDB(config.meta_path)

    routes = [
        Route("/", _index, methods=["GET"]),
        Route("/health", _health, methods=["GET"]),
        Route("/info", _info, methods=["GET"]),
        Route("/profile", _profile, methods=["POST"]),
        Route("/query", _query, methods=["POST"]),
    ]
    if config.allow_sequences:
        routes.append(Route("/assembly", _assembly, methods=["POST"]))
    app = Starlette(
        routes=routes,
        exception_handlers={
            StarletteHTTPException: _json_http_error,
            Exception: _json_http_error,
        },
        lifespan=_lifespan(config),
    )
    app.state.database = loaded
    app.state.metadata = metadata_store
    app.state.started = time.monotonic()
    app.state.max_jobs = config.max_jobs
    app.state.max_upload_mb = config.max_upload_mb
    app.state.allow_sequences = config.allow_sequences
    app.state.max_sequence_mb = config.max_sequence_mb
    app.state.rate_limit = config.rate_limit
    app.state.max_queue = config.max_queue
    app.state.jobs = JobSlots(config.max_jobs, config.max_queue)
    # Starlette middleware: the last added is the outermost, so CORS wraps
    # rate-limit and body-limit errors too.
    app.add_middleware(
        BodyLimitMiddleware, max_bytes=config.max_upload_mb * 1024 * 1024
    )
    if config.rate_limit is not None:
        count, window = config.rate_limit
        app.add_middleware(
            RateLimitMiddleware,
            limiter=RateLimiter(count, window),
            paths=frozenset(("/profile", "/query")) | (
                {"/assembly"} if config.allow_sequences else set()
            ),
            trusted=frozenset(config.trusted_proxies),
        )
    if config.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(config.cors_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
            expose_headers=["Retry-After"],
            max_age=600,
        )
    return app


def _socket_identity(path: Path) -> tuple[int, int] | None:
    """Return the ``(st_dev, st_ino)`` identity of ``path``, or ``None``."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _unlink_socket_if_owned(path: Path, identity: tuple[int, int] | None) -> None:
    """Unlink ``path`` only if it is still the socket this process bound."""
    if identity is None:
        return
    try:
        st = path.stat()
    except OSError:
        return
    if (st.st_dev, st.st_ino) != identity:
        return
    if not stat.S_ISSOCK(st.st_mode):
        return
    try:
        path.unlink()
    except OSError:
        pass


def prepare_unix_socket(path: Path) -> None:
    """Remove a stale socket file, refusing to steal a live one."""
    if not path.exists() and not path.is_symlink():
        return
    if not path.is_socket():
        raise RuntimeError(f"{path} exists and is not a socket; refusing to remove it")
    # A server that has just bound but not yet called listen() also gets
    # ECONNREFUSED.  Retry briefly before declaring the socket stale.
    last_refused = False
    for attempt in range(3):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(1.0)
        try:
            probe.connect(str(path))
        except FileNotFoundError:
            return
        except ConnectionRefusedError:
            last_refused = True
            if attempt < 2:
                time.sleep(0.1)
                continue
        except OSError as error:
            raise RuntimeError(
                f"cannot check whether {path} is in use: {error}"
            ) from error
        else:
            raise RuntimeError(f"another server is already listening on {path}")
        finally:
            probe.close()
    if last_refused:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def bind_unix_socket(path: Path) -> socket.socket:
    """Bind a Unix socket ourselves, so its mode is ``SOCKET_MODE``.

    ``uvicorn.run(uds=...)`` forces ``0o666`` on the socket file, which is
    world-writable.  Binding here and handing uvicorn the descriptor instead
    keeps the stricter ``0o660``.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
        os.chmod(path, SOCKET_MODE)
    except BaseException:
        sock.close()
        raise
    return sock


def serve(
    config: ServerConfig,
    *,
    socket_path: Path | None = None,
    tcp: tuple[str, int] | None = None,
    log_level: str = "info",
    app: Starlette | None = None,
) -> None:
    """Run the server until interrupted.  ``socket_path``/``tcp`` are exclusive."""
    if (socket_path is None) == (tcp is None):
        raise ValueError("exactly one of socket_path and tcp must be given")
    application = app if app is not None else create_app(config)
    kwargs: dict[str, Any] = {
        "log_level": log_level,
        "access_log": log_level != "warning",
    }
    sock: socket.socket | None = None
    socket_identity: tuple[int, int] | None = None
    if socket_path is not None:
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        prepare_unix_socket(socket_path)
        sock = bind_unix_socket(socket_path)
        socket_identity = _socket_identity(socket_path)
        kwargs["fd"] = sock.fileno()
    else:
        host, port = tcp  # type: ignore[misc]
        kwargs["host"] = host
        kwargs["port"] = port

    def cleanup() -> None:
        if sock is not None:
            sock.close()
        if socket_path is not None:
            _unlink_socket_if_owned(socket_path, socket_identity)

    previous_sigterm = None
    if socket_path is not None and threading.current_thread() is threading.main_thread():
        # uvicorn re-raises SIGTERM after its graceful shutdown, which would
        # kill the process before the ``finally`` below runs.  Clean up first
        # and exit normally instead.
        def handle_sigterm(signum: int, frame: Any) -> None:
            cleanup()
            raise SystemExit(0)

        previous_sigterm = signal.signal(signal.SIGTERM, handle_sigterm)

    try:
        uvicorn.run(application, **kwargs)
    finally:
        cleanup()
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
