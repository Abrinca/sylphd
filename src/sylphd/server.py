"""Starlette application and uvicorn entry point for ``sylphd serve``.

Design notes
------------
* The database is loaded once, at startup, and shared by every request.
* Blocking ``pysylph`` calls run in worker threads through
  ``anyio.to_thread.run_sync``; a semaphore caps the number of concurrent
  profiling jobs (``--max-jobs``) and queues the rest, optionally up to
  ``--max-queue`` jobs (one job per sample, one job per ``/assembly`` upload,
  or one job per ``/contigs`` request however many contigs it carries).
* One ``Profiler`` per parameter set is cached and shared by all requests:
  with two or more databases its constructor scans every genome, and it is
  safe to use from several threads at once.
* One process, one worker: the whole point is sharing the in-memory index.
"""

from __future__ import annotations

import logging
import math
import os
import sys
import time
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import pysylph
from starlette.applications import Starlette
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import __version__, per_contig
from .about import About
from .assembly import INTERPRETATION_THRESHOLDS
from .limits import JobSlots, RateLimiter, RateLimitMiddleware
from .metadata import MetadataDB
from .paths import SOCKET_MODE
from .profiling import (
    DatabaseSet,
    DatabaseSpec,
    check_sketch_header,
    run_assembly,
    run_assembly_sketch,
    run_contig_query,
    run_sample,
)
from .protocol import PROFILE_PARAM_KEYS, ProfileParams
from .server_errors import RequestError, answers_errors, check_queue, job_errors
from .server_middleware import AccessLogMiddleware, BodyLimitMiddleware
from .server_upload import (
    MAX_PARAMS_BYTES,
    MAX_SKETCHES,
    read_contigs,
    read_upload,
    require_multipart,
)

log = logging.getLogger("uvicorn.error")

#: Default of ``--max-contigs``: the maximum number of contigs accepted in one
#: ``/contigs`` request.  The whole request is one job (its contigs run
#: sequentially in that slot) and a contig can cost tens of milliseconds on a
#: multi-database server, so the operator can tighten it; ``/info`` advertises
#: the effective value as ``limits.max_contigs`` and clients batch by it.
DEFAULT_MAX_CONTIGS = MAX_SKETCHES

#: Accepted ``Content-Type`` values of a raw-sequence ``/assembly`` upload.
SEQUENCE_CONTENT_TYPES = frozenset(
    {"text/plain", "application/octet-stream", "application/gzip"}
)

#: ``Content-Type`` of an ``/assembly`` upload that is a precomputed sketch
#: (the bytes of a ``.sylsp`` file).  Always accepted, with or without
#: ``--allow-sequences``.
SKETCH_CONTENT_TYPE = "application/vnd.sylph.sketch"

#: Upper bounds of the informational ``records`` / ``length`` query parameters
#: that accompany a sketch upload to ``/assembly``.
MAX_INFO_RECORDS = 10**9
MAX_INFO_LENGTH = 10**13

#: ``top`` range of ``POST /assembly``.
MIN_TOP, MAX_TOP = 1, 50

#: ``min_ani`` range of ``POST /assembly``.
MIN_ANI_RANGE = (70.0, 100.0)

#: Query-string parameters ``POST /assembly`` accepts, with their validator:
#: ``(kind, default, minimum, maximum)``.  The parser below and ``/info`` both
#: read this table, so the advertised list cannot drift from the validation.
ASSEMBLY_PARAMS: dict[str, tuple[str, Any, Any, Any]] = {
    "top": ("integer", 10, MIN_TOP, MAX_TOP),
    "min_ani": ("float", 80.0, *MIN_ANI_RANGE),
    "name": ("name", "assembly", None, 1024),
    "records": ("integer", None, 0, MAX_INFO_RECORDS),
    "length": ("integer", None, 0, MAX_INFO_LENGTH),
    "gc": ("float", None, 0.0, 100.0),
}

#: The keys of :data:`ASSEMBLY_PARAMS`, for ``/info``.
ASSEMBLY_PARAM_KEYS: tuple[str, ...] = tuple(ASSEMBLY_PARAMS)


@dataclass
class ServerConfig:
    """Everything ``sylphd serve`` needs."""

    #: ``--db`` of a single-database server, the historical spelling, still
    #: usable positionally.  Do not set it together with :attr:`db_paths`.
    db_path: Path | None = None
    #: The databases to load, in order (``--db`` repeated).  Keyword-only, so
    #: the positional arguments keep their historical meaning.
    db_paths: tuple[DatabaseSpec, ...] = field(default=(), kw_only=True)
    meta_path: Path | None = None
    max_jobs: int = 2
    max_upload_mb: int = 512
    compute_hash: bool = False
    #: Accept raw FASTA/FASTQ uploads on ``POST /assembly``.  Sketch uploads
    #: to ``/assembly`` need no opt-in: the server never sees sequences then.
    allow_sequences: bool = False
    #: Upload size limit of ``/assembly`` (compressed bytes).
    max_sequence_mb: int = 20
    #: ``(count, window_seconds)`` for the per-client rate limit, or ``None``.
    rate_limit: tuple[int, float] | None = None
    #: How many jobs (samples) may wait for a job slot; ``None`` is unlimited.
    max_queue: int | None = None
    #: Peers whose ``X-Forwarded-For`` header is trusted (IPs or ``"unix"``).
    trusted_proxies: tuple[str, ...] = ()
    #: Allowed CORS origins; none by default.
    cors_origins: tuple[str, ...] = ()
    #: Display name of the instance, reported by ``/info``; as returned by
    #: :func:`sylphd.about.check_name`.
    name: str | None = None
    #: The operator's about text (``/info`` ``about``); the caller builds it,
    #: so a bad file fails before the database is loaded.
    about: About = field(default_factory=lambda: About(None))
    #: Set when serving on a Unix socket, so the app can adjust its mode.
    socket_path: Path | None = None
    #: Contigs accepted in one ``POST /contigs`` request (``--max-contigs``);
    #: kept after the historical positional fields so their order is unchanged.
    max_contigs: int = DEFAULT_MAX_CONTIGS

    def __post_init__(self) -> None:
        if self.max_contigs < 1:
            raise ValueError("max_contigs must be at least 1")
        if self.db_paths:
            if self.db_path is not None:
                raise ValueError(
                    "give either db_path (one database) or db_paths (several), "
                    "not both"
                )
            self.db_path = self.db_paths[0].path
        elif self.db_path is None:
            raise ValueError("at least one database is required")
        else:
            self.db_paths = (DatabaseSpec(Path(self.db_path)),)


async def _health(request: Request) -> JSONResponse:
    state = request.app.state
    return JSONResponse(
        {
            "status": "ok",
            "service": "sylphd",
            "version": __version__,
            "uptime_seconds": round(time.monotonic() - state.started, 3),
            "jobs_running": state.jobs.running,
            "jobs_queued": state.jobs.queued,
        }
    )


async def _info(request: Request) -> JSONResponse:
    state = request.app.state
    metadata = state.metadata
    description: dict[str, Any] = {}
    if state.name:
        description["name"] = state.name
    about = state.about.text
    if about:
        description["about"] = {"format": "markdown", "text": about}
    return JSONResponse(
        {
            "service": "sylphd",
            "version": __version__,
            **description,
            "pysylph_version": pysylph.__version__,
            "python_version": ".".join(str(part) for part in sys.version_info[:3]),
            "database": state.database.info(),
            "databases": state.database.entries(),
            "metadata": metadata.summary() if metadata is not None else None,
            "limits": {
                "max_jobs": state.max_jobs,
                "max_upload_mb": state.max_upload_mb,
                "max_sketches": MAX_SKETCHES,
                "max_contigs": state.max_contigs,
                "max_params_bytes": MAX_PARAMS_BYTES,
                "allow_sequences": state.allow_sequences,
                "max_sequence_mb": state.max_sequence_mb if state.allow_sequences else None,
                "rate_limit": _rate_limit_dict(state.rate_limit),
                "max_queue": state.max_queue,
            },
            "endpoints": _endpoints(),
            "params": _params_info(),
        }
    )


def _endpoints() -> list[str]:
    return ["/health", "/info", "/profile", "/query", "/assembly", "/contigs"]


def _params_info() -> dict[str, list[str]]:
    """Accepted ``params`` keys per POST endpoint, derived from the validators.

    ``/query`` and ``/profile`` share :data:`PROFILE_PARAM_KEYS` (the
    :class:`ProfileParams` fields plus ``names``, ``fields``, ``mode`` and
    ``tax_sources``); ``/contigs`` advertises
    :data:`sylphd.per_contig.PARAM_KEYS`; ``/assembly`` takes query-string
    parameters, listed in :data:`ASSEMBLY_PARAM_KEYS`.
    """
    profile_keys = [key for key in PROFILE_PARAM_KEYS if key != "taxprof"]
    return {
        # taxprof is /profile only: query rows have no abundances to aggregate.
        "/profile": [*profile_keys, "taxprof"],
        "/query": profile_keys,
        "/contigs": sorted(per_contig.PARAM_KEYS),
        "/assembly": list(ASSEMBLY_PARAM_KEYS),
    }


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
            "endpoints": _endpoints(),
        }
    )


@answers_errors
async def _profile(request: Request) -> JSONResponse:
    return await _handle(request, "profile")


@answers_errors
async def _query(request: Request) -> JSONResponse:
    return await _handle(request, "query")


@answers_errors
async def _assembly(request: Request) -> JSONResponse:
    """Find the closest genomes of one assembly.

    The body is either a precomputed sketch (``Content-Type:
    application/vnd.sylph.sketch``; always accepted) or raw FASTA/FASTQ, which
    the server sketches itself (only with ``--allow-sequences``).
    """
    state = request.app.state
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    is_sketch = media_type == SKETCH_CONTENT_TYPE

    if not is_sketch:
        if not state.allow_sequences:
            raise RequestError(
                "this server does not accept raw sequences (it was started "
                "without --allow-sequences); send a sketch with Content-Type "
                f"{SKETCH_CONTENT_TYPE}",
                404,
            )
        if media_type and media_type not in SEQUENCE_CONTENT_TYPES:
            raise RequestError(
                "expected raw FASTA or FASTQ with Content-Type text/plain, "
                "application/octet-stream or application/gzip, or a sketch "
                f"with Content-Type {SKETCH_CONTENT_TYPE}",
                415,
            )

    parsed = _parse_assembly_params(request)
    top = parsed["top"]
    min_ani = parsed["min_ani"]
    stats = (
        {
            "records": parsed["records"],
            "length": parsed["length"],
            "gc": parsed["gc"],
        }
        if is_sketch
        else None
    )
    name = parsed["name"]

    # Raw sequences have their own (small) limit; a sketch is bounded by the
    # general upload limit, which BodyLimitMiddleware already enforces.
    limit = (
        state.max_upload_mb if is_sketch else state.max_sequence_mb
    ) * 1024 * 1024
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError:
            raise RequestError("invalid Content-Length header") from None
        if size > limit:
            raise _too_large_error(limit, is_sketch)
    check_queue(state)
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            raise _too_large_error(limit, is_sketch)
        chunks.append(chunk)
    payload = b"".join(chunks)
    if not payload:
        raise RequestError("empty sketch upload" if is_sketch else "empty sequence upload")

    start = time.perf_counter()
    run, last = (run_assembly_sketch, stats) if is_sketch else (run_assembly, limit)
    with job_errors("assembly profiling"), state.jobs.admit():
        async with state.jobs.slot():
            result = await anyio.to_thread.run_sync(
                run,
                state.database,
                state.metadata,
                name,
                payload,
                top,
                min_ani,
                last,
            )

    elapsed_ms = round((time.perf_counter() - start) * 1000.0, 1)
    return JSONResponse(
        {
            "query": result["query"],
            "assignment": result["assignment"],
            "closest": result["closest"],
            "database": _database_summary(state.database),
            "params": {"top": top, "min_ani": min_ani},
            "thresholds": dict(INTERPRETATION_THRESHOLDS),
            "elapsed_ms": elapsed_ms,
            "hits": result["hits"],
        }
    )


def _too_large_error(limit: int, is_sketch: bool = False) -> RequestError:
    kind = "upload" if is_sketch else "sequence"
    return RequestError(
        f"request body exceeds the {limit // (1024 * 1024)} MB {kind} limit", 413
    )


def _database_summary(database: DatabaseSet) -> dict[str, Any]:
    """The ``database`` member of ``/assembly`` and ``/contigs`` responses."""
    return {
        "name": database.name,
        "genomes": database.genomes,
        "c": database.c,
        "k": database.k,
    }


def _parse_assembly_params(request: Request) -> dict[str, Any]:
    """Validate every ``/assembly`` query parameter from :data:`ASSEMBLY_PARAMS`."""
    values: dict[str, Any] = {}
    for key, (kind, default, minimum, maximum) in ASSEMBLY_PARAMS.items():
        if kind == "integer":
            values[key] = _query_integer(request, key, default, minimum, maximum)
        elif kind == "float":
            values[key] = _query_float(request, key, default, minimum, maximum)
        else:  # "name"
            raw = request.query_params.get(key) or default
            if len(raw) > maximum:
                raise RequestError(f"'{key}' is too long (max {maximum} characters)")
            values[key] = raw
    return values


def _query_integer(
    request: Request, key: str, default: int | None, minimum: int, maximum: int
) -> Any:
    raw = request.query_params.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RequestError(f"'{key}' must be an integer") from None
    if value < minimum or value > maximum:
        raise RequestError(f"'{key}' must be between {minimum} and {maximum}")
    return value


def _query_float(
    request: Request, key: str, default: float | None, minimum: float, maximum: float
) -> Any:
    raw = request.query_params.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise RequestError(f"'{key}' must be a number") from None
    if not math.isfinite(value):
        raise RequestError(f"'{key}' must be a finite number")
    if value < minimum or value > maximum:
        raise RequestError(f"'{key}' must be between {minimum:g} and {maximum:g}")
    return value


@answers_errors
async def _contigs(request: Request) -> JSONResponse:
    """Classify every contig of one fragmented assembly.

    The body is ``multipart/form-data``: one ``sketch`` part per contig (the
    contig sketched as a single-end sample) and a ``params`` field holding each
    contig's measurements and the options.  The whole request is **one job**,
    however many contigs it carries; its contigs run one after the other inside
    that one job slot, so ``--max-jobs``/``--max-queue`` bound requests rather
    than contigs.  The per-request bound is ``--max-contigs`` (4096 by default,
    advertised as ``limits.max_contigs``); on a server whose plain databases are
    scanned per contig a contig can cost ~0.6 s, so that bound is what keeps one
    upload from occupying a job slot for an hour.  The contig-relative ANI,
    ranking, classification and consensus are computed by
    :mod:`sylphd.per_contig`, so every client gets the same answer.
    """
    state = request.app.state
    require_multipart(request, "one 'sketch' part per contig")
    contigs, options, tax_sources, payloads = await read_contigs(request)

    start = time.perf_counter()
    results: list[dict[str, Any] | None] = [None] * len(contigs)
    # The metadata columns to fetch are request-wide; resolve them once.
    row_fields = per_contig.classification_fields(
        options.fields, _metadata_columns(state.metadata)
    )
    with job_errors("contig profiling"), state.jobs.admit():
        async with state.jobs.slot():
            for index, (contig, payload) in enumerate(zip(contigs, payloads)):
                results[index] = await anyio.to_thread.run_sync(
                    _contig_job,
                    state.database,
                    state.metadata,
                    contig,
                    payload,
                    options,
                    tax_sources,
                    row_fields,
                )

    elapsed_ms = round((time.perf_counter() - start) * 1000.0, 1)
    contig_results = [result for result in results if result is not None]
    return JSONResponse(
        {
            "contigs": contig_results,
            "summary": per_contig.summarize(contig_results),
            "caveats": per_contig.caveats(contig_results),
            "database": _database_summary(state.database),
            "params": {
                "top": options.top,
                "min_ani": options.min_ani,
                "consensus_ratio": options.consensus_ratio,
                "consensus_z": options.consensus_z,
                "fields": list(options.fields),
            },
            "thresholds": dict(INTERPRETATION_THRESHOLDS),
            "elapsed_ms": elapsed_ms,
        }
    )


def _contig_job(
    database: DatabaseSet,
    metadata: MetadataDB | None,
    contig: per_contig.ContigInput,
    payload: bytes,
    options: per_contig.ContigOptions,
    tax_sources: Sequence[str] | None,
    row_fields: Sequence[str],
) -> dict[str, Any]:
    """Query **and** score one contig; runs in a worker thread.

    The server calls this through ``anyio.to_thread.run_sync`` while it holds
    the job slot, so both the synchronous pysylph query and the scoring /
    classification / consensus of :mod:`sylphd.per_contig` stay off the event
    loop: a /contigs request with many contigs must not delay ``/health``.

    The sketch header is validated first.  A sketch below sylph's sample k-mer
    floor (or a contig with no genome-style k-mer) cannot produce an ANI, so it
    is reported as ``too_few_kmers`` **without** running the query: on a
    multi-database server a query is the expensive part, and a fragmented
    assembly may carry thousands of such records.
    """
    header = check_sketch_header(database, contig.name, payload)
    k = database.k
    if (
        header.kmer_count < per_contig.QUERY_PARAMS.min_number_kmers
        or contig.contig_kmers == 0
    ):
        return per_contig.contig_result(
            contig, sketch_kmers=header.kmer_count, rows=(), options=options, k=k
        )
    query = run_contig_query(
        database,
        metadata,
        contig.name,
        payload,
        per_contig.QUERY_PARAMS,
        row_fields,
        tax_sources,
    )
    return per_contig.contig_result(
        contig,
        sketch_kmers=int(query["sketch_kmers"]),
        rows=query["rows"],
        options=options,
        k=k,
    )


def _metadata_columns(metadata: MetadataDB | None) -> tuple[str, ...]:
    return tuple(metadata.field_columns) if metadata is not None else ()



@answers_errors
async def _handle(request: Request, mode: str) -> JSONResponse:
    state = request.app.state
    require_multipart(request, "one 'sketch' part per sample")
    params, fields, names, payloads, tax_sources, taxprof = await read_upload(
        request, mode
    )

    samples: list[dict[str, Any] | None] = [None] * len(payloads)
    with job_errors("profiling"), state.jobs.admit(len(payloads)):
        async with anyio.create_task_group() as task_group:
            for index, (name, payload) in enumerate(zip(names, payloads)):
                task_group.start_soon(
                    _run_slot,
                    state,
                    mode,
                    params,
                    fields,
                    name,
                    payload,
                    samples,
                    index,
                    tax_sources,
                    taxprof,
                )
    return JSONResponse(samples)



async def _run_slot(
    state: Any,
    mode: str,
    params: ProfileParams,
    fields: Sequence[str] | None,
    name: str,
    payload: bytes,
    samples: list[dict[str, Any] | None],
    index: int,
    tax_sources: Sequence[str] | None = None,
    taxprof: Sequence[str] | None = None,
) -> None:
    async with state.jobs.slot():
        samples[index] = await anyio.to_thread.run_sync(
            run_sample,
            state.database,
            state.metadata,
            mode,
            params,
            fields,
            name,
            payload,
            params.no_adjust,
            tax_sources,
            taxprof,
        )


async def _json_http_error(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, StarletteHTTPException):
        return RequestError(str(exc.detail), exc.status_code).response()
    return RequestError(str(exc), 500).response()


def _lifespan(config: ServerConfig) -> Any:
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
    database: DatabaseSet | None = None,
    metadata: MetadataDB | None = None,
) -> Starlette:
    """Build the ASGI application (loading the databases unless provided)."""
    loaded = database if database is not None else DatabaseSet.load(
        config.db_paths, compute_hash=config.compute_hash
    )
    metadata_store = metadata
    if metadata_store is None and config.meta_path is not None:
        metadata_store = MetadataDB(config.meta_path)
    loaded.annotate_gtdb(metadata_store)

    routes = [
        Route("/", _index, methods=["GET"]),
        Route("/health", _health, methods=["GET"]),
        Route("/info", _info, methods=["GET"]),
        Route("/profile", _profile, methods=["POST"]),
        Route("/query", _query, methods=["POST"]),
        Route("/assembly", _assembly, methods=["POST"]),
        Route("/contigs", _contigs, methods=["POST"]),
    ]
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
    app.state.max_contigs = config.max_contigs
    app.state.max_upload_mb = config.max_upload_mb
    app.state.allow_sequences = config.allow_sequences
    app.state.max_sequence_mb = config.max_sequence_mb
    app.state.rate_limit = config.rate_limit
    app.state.max_queue = config.max_queue
    app.state.name = config.name
    app.state.about = config.about
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
            paths=frozenset(("/profile", "/query", "/assembly", "/contigs")),
            trusted=frozenset(config.trusted_proxies),
        )
    if config.cors_origins:
        origins = [o for o in config.cors_origins if o != "local"]
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_origin_regex=(
                LOCAL_ORIGIN_REGEX if "local" in config.cors_origins else None
            ),
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
            expose_headers=["Retry-After", "X-Request-Id"],
            max_age=600,
        )
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(AccessLogMiddleware, trusted=frozenset(config.trusted_proxies))
    return app


#: Origins accepted by ``--cors-origin local``: loopback hosts on any port,
#: for developing the web page against a local server.
LOCAL_ORIGIN_REGEX = r"https?://(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:\d+)?"
