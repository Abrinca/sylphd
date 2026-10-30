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

import functools
import json
import logging
import math
import os
import signal
import socket
import stat
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

import anyio
import pysylph
import uvicorn
from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import __version__, per_contig
from .about import About
from .assembly import INTERPRETATION_THRESHOLDS, SequenceError, SequenceTooLarge
from .limits import (
    QUEUE_RETRY_SECONDS,
    JobSlots,
    QueueFullError,
    RateLimiter,
    RateLimitMiddleware,
    client_identity,
)
from .metadata import MetadataDB
from .paths import SOCKET_MODE
from .profiling import (
    BadRequest,
    DatabaseSet,
    DatabaseSpec,
    check_sketch_header,
    run_assembly,
    run_assembly_sketch,
    run_contig_query,
    run_sample,
)
from .protocol import PROFILE_PARAM_KEYS, ProfileParams, ProtocolError
from .sketching import InputError, check_sample_name, sample_name_from_path
from .taxonomy import TaxonomyError, parse_taxonomies, resolve_taxonomy_columns

log = logging.getLogger("uvicorn.error")

#: Maximum number of sketches accepted in one ``/profile`` or ``/query``
#: request.
MAX_SKETCHES = 4096

#: Default of ``--max-contigs``: the maximum number of contigs accepted in one
#: ``/contigs`` request.  The whole request is one job (its contigs run
#: sequentially in that slot) and a contig can cost tens of milliseconds on a
#: multi-database server, so the operator can tighten it; ``/info`` advertises
#: the effective value as ``limits.max_contigs`` and clients batch by it.
DEFAULT_MAX_CONTIGS = MAX_SKETCHES

#: Largest non-file form field (``params``) Starlette accepts in one request.
#: ``/contigs`` repeats every contig name, length and k-mer count there, so the
#: limit is advertised in ``/info`` for clients that budget their requests.
MAX_PARAMS_BYTES = 1024 * 1024

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


class AccessLogMiddleware:
    """Tag each response with ``X-Request-Id`` and log one line per request.

    ``serve`` turns uvicorn's own access log off, so this is the only one.
    """

    def __init__(self, app: Any, trusted: frozenset[str] = frozenset()) -> None:
        self.app = app
        self.trusted = trusted

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex[:12]
        start = time.perf_counter()
        status = 500  # what the client sees if the app dies before responding

        async def tagged_send(message: dict) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [
                    *message.get("headers", ()),
                    (b"x-request-id", request_id.encode()),
                ]
            await send(message)

        try:
            await self.app(scope, receive, tagged_send)
        finally:
            log.info(
                "%s %s -> %d in %.1f ms client=%s request_id=%s",
                scope["method"],
                quote(scope["path"]),
                status,
                (time.perf_counter() - start) * 1000.0,
                client_identity(scope, self.trusted),
                request_id,
            )


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


class RequestError(Exception):
    """A request to refuse: ``str(error)`` is the message, ``status`` the code.

    Raised anywhere below a handler and answered once by :func:`_answers_errors`.
    """

    def __init__(
        self, message: str, status: int = 400, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.headers = headers

    def response(self) -> JSONResponse:
        return JSONResponse(
            {"error": str(self)}, status_code=self.status, headers=self.headers
        )


def _answers_errors(handler: Callable[..., Any]) -> Callable[..., Any]:
    """Turn a :class:`RequestError` raised by ``handler`` into its response."""

    @functools.wraps(handler)
    async def wrapper(request: Request, *args: Any) -> JSONResponse:
        try:
            return await handler(request, *args)
        except RequestError as error:
            return error.response()

    return wrapper


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


@_answers_errors
async def _profile(request: Request) -> JSONResponse:
    return await _handle(request, "profile")


@_answers_errors
async def _query(request: Request) -> JSONResponse:
    return await _handle(request, "query")


@_answers_errors
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
    _check_queue(state)
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
    with _job_errors("assembly profiling"), state.jobs.admit():
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


@_answers_errors
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
    _require_multipart(request, "one 'sketch' part per contig")
    contigs, options, tax_sources, payloads = await _read_contigs(request)

    start = time.perf_counter()
    results: list[dict[str, Any] | None] = [None] * len(contigs)
    # The metadata columns to fetch are request-wide; resolve them once.
    row_fields = per_contig.classification_fields(
        options.fields, _metadata_columns(state.metadata)
    )
    with _job_errors("contig profiling"), state.jobs.admit():
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


def _too_many_contigs(limit: int) -> str:
    return (
        "too many contigs in one request; send at most "
        f"{limit} contigs per POST /contigs (one 'sketch' part each; the "
        "server's limits.max_contigs in GET /info) and batch the rest "
        "yourself, or use 'sylphd assembly --per-contig', which batches by "
        "that limit"
    )


def _is_too_many_files(message: str) -> bool:
    # Starlette stops at the first file beyond ``max_files``, so the count in
    # its message is that limit, not the number of parts the request carried.
    return message.startswith("Too many files")


def _require_multipart(request: Request, hint: str) -> None:
    """Refuse a non-multipart body, or any request while the queue is full."""
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise RequestError(f"expected a multipart/form-data body with {hint}", 415)
    _check_queue(request.app.state)


def _check_queue(state: Any) -> None:
    """Refuse the request now (before its body is read) if the queue is full."""
    try:
        state.jobs.check()
    except QueueFullError:
        raise _busy() from None


def _busy() -> RequestError:
    return RequestError(
        "the server is busy; retry shortly",
        503,
        {"Retry-After": str(QUEUE_RETRY_SECONDS)},
    )


#: Exceptions of a job that are the client's fault, with their status.
#: ``SequenceTooLarge`` is a ``SequenceError``, so it comes first.
_CLIENT_ERRORS: tuple[tuple[type[Exception], int], ...] = (
    (BadRequest, 400),
    (SequenceTooLarge, 413),
    (SequenceError, 400),
)


def _client_error(error: BaseException) -> RequestError | None:
    if isinstance(error, QueueFullError):
        return _busy()
    for kind, status in _CLIENT_ERRORS:
        if isinstance(error, kind):
            return RequestError(str(error), status)
    return None


@contextmanager
def _job_errors(what: str) -> Iterator[None]:
    """Run profiling jobs; raise a :class:`RequestError` for any failure.

    Client mistakes and a full queue keep their message and status; anything
    else is logged and answered with a generic 500 (``"<what> failed"``).
    """
    try:
        yield
    except BaseExceptionGroup as group:  # from the task group of /profile
        for error in group.exceptions:
            if (refusal := _client_error(error)) is not None:
                raise refusal from None
        log.exception("%s failed", what)
        raise RequestError(f"{what} failed", 500) from None
    except Exception as error:
        if (refusal := _client_error(error)) is not None:
            raise refusal from error
        log.exception("%s failed", what)
        raise RequestError(f"{what} failed", 500) from None


@asynccontextmanager
async def _read_form(
    request: Request, max_files: int, too_many: str | None = None
) -> AsyncIterator[Any]:
    """Open the multipart form, answering Starlette's parse errors.

    ``too_many`` replaces the message of Starlette's too-many-files error.  The
    form (and its temporary files) is closed on exit.
    """
    try:
        async with request.form(
            # Exactly the limit: the parser raises on the (N+1)th file part,
            # so N+1 uploads are not spooled to disk before the 400.
            max_files=max_files,
            max_fields=16,
            max_part_size=MAX_PARAMS_BYTES,
        ) as form:
            yield form
    except StarletteHTTPException as error:
        if too_many is not None and _is_too_many_files(str(error.detail)):
            raise RequestError(too_many) from None
        raise RequestError(str(error.detail), error.status_code) from None
    except MultiPartException as error:
        # Starlette re-raises this as a 400 HTTPException when the app is in
        # the scope; translating it here too keeps the message independent of
        # that. Other multipart errors (bad boundary, oversized part) keep
        # their own message.
        if too_many is not None and _is_too_many_files(error.message):
            raise RequestError(too_many) from None
        raise RequestError(error.message) from None


def _params_object(form: Any) -> dict[str, Any]:
    """The JSON object in the ``params`` field (empty when absent)."""
    raw_params = form.get("params")
    try:
        params_data: Any = {} if raw_params is None else json.loads(str(raw_params))
    except json.JSONDecodeError as error:
        raise RequestError(f"invalid JSON in 'params': {error}") from None
    if not isinstance(params_data, dict):
        raise RequestError("'params' must be a JSON object")
    return params_data


def _sketch_parts(form: Any) -> list[UploadFile]:
    sketches = form.getlist("sketch")
    if not sketches:
        raise RequestError("no 'sketch' part in the request")
    if not all(isinstance(part, UploadFile) for part in sketches):
        raise RequestError("'sketch' parts must be file uploads")
    return sketches


async def _read_payloads(sketches: Sequence[UploadFile]) -> list[bytes]:
    try:
        return [await part.read() for part in sketches]
    except Exception:
        log.exception("failed to read an uploaded sketch part")
        raise RequestError("failed to read a 'sketch' part", 500) from None


async def _read_contigs(
    request: Request,
) -> tuple[
    tuple[per_contig.ContigInput, ...],
    per_contig.ContigOptions,
    Sequence[str] | None,
    list[bytes],
]:
    """Parse the ``/contigs`` form, or raise the :class:`RequestError`."""
    state = request.app.state
    limit = state.max_contigs
    async with _read_form(request, limit, _too_many_contigs(limit)) as form:
        params_data = _params_object(form)
        sketches = _sketch_parts(form)
        if len(sketches) > limit:
            raise RequestError(_too_many_contigs(limit))
        try:
            contigs, options = per_contig.parse_params(
                params_data, k=state.database.k, count=len(sketches)
            )
        except ProtocolError as error:
            raise RequestError(str(error)) from None
        fields = _validate_fields(params_data.get("fields"), state.metadata)
        tax_sources = _validate_tax_sources(
            params_data.get("tax_sources"), state.metadata
        )
        payloads = await _read_payloads(sketches)
        return (
            contigs,
            replace(options, fields=tuple(fields or ())),
            tax_sources,
            payloads,
        )


@_answers_errors
async def _handle(request: Request, mode: str) -> JSONResponse:
    state = request.app.state
    _require_multipart(request, "one 'sketch' part per sample")
    params, fields, names, payloads, tax_sources, taxprof = await _read_upload(
        request, mode
    )

    samples: list[dict[str, Any] | None] = [None] * len(payloads)
    with _job_errors("profiling"), state.jobs.admit(len(payloads)):
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


async def _read_upload(
    request: Request, mode: str
) -> tuple[
    ProfileParams,
    Sequence[str] | None,
    list[str],
    list[bytes],
    Sequence[str] | None,
    Sequence[str] | None,
]:
    """Parse the form into ``(params, fields, names, payloads, tax_sources, taxprof)``.

    Raises the :class:`RequestError` when the request is invalid.
    """
    state = request.app.state
    async with _read_form(request, MAX_SKETCHES) as form:
        params_data = _params_object(form)
        try:
            params = ProfileParams.from_mapping(params_data)
        except ProtocolError as error:
            raise RequestError(str(error)) from None

        declared_mode = params_data.get("mode")
        if declared_mode is not None and declared_mode != mode:
            raise RequestError(
                f"params.mode is {declared_mode!r} but this is the /{mode} endpoint"
            )

        fields = _validate_fields(params_data.get("fields"), state.metadata)
        tax_sources = _validate_tax_sources(
            params_data.get("tax_sources"), state.metadata
        )
        taxprof = _validate_taxprof(params_data.get("taxprof"), state.metadata, mode)
        sketches = _sketch_parts(form)
        names = _resolve_names(params_data.get("names"), sketches)
        payloads = await _read_payloads(sketches)
        return params, fields, names, payloads, tax_sources, taxprof


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


def _validate_fields(value: Any, metadata: MetadataDB | None) -> Sequence[str] | None:
    """Validate ``params.fields``: metadata column names, in request order.

    Duplicates are removed (a repeated column must not duplicate the key of a
    hit or the work of the metadata join), and the result cannot be longer
    than the number of columns the server's metadata file has.
    """
    if value is None:
        return None if metadata is None else tuple(metadata.field_columns)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RequestError("'params.fields' must be a list of column names")
    if not value:
        return ()
    if metadata is None:
        raise RequestError(
            "this server has no metadata database; --fields is not available"
        )
    unknown = [column for column in value if column not in metadata.field_columns]
    if unknown:
        raise RequestError(f"unknown metadata column(s): {', '.join(unknown)}")
    columns = tuple(dict.fromkeys(value))
    if len(columns) > len(metadata.field_columns):
        raise RequestError(
            f"'params.fields' has {len(columns)} columns; this server's metadata "
            f"file has only {len(metadata.field_columns)}"
        )
    return columns


def _validate_tax_sources(
    value: Any, metadata: MetadataDB | None
) -> Sequence[str] | None:
    """Validate ``params.tax_sources``: source names of the metadata file.

    ``None`` means all sources (sylph-tax given every file).  A name the file
    does not have is a 400, since silently resolving against another source
    would produce a profile that looks right and is not.
    """
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RequestError("'params.tax_sources' must be a list of source names")
    if not value:  # an empty list is "no restriction", like an omitted field
        return None
    available = metadata.source_names() if metadata is not None else []
    if not available:
        raise RequestError(
            "this server has no sylph-tax taxonomy sources; "
            "'params.tax_sources' is not available"
        )
    unknown = [name for name in value if name not in available]
    if unknown:
        raise RequestError(
            "unknown taxonomy source(s): "
            + ", ".join(unknown)
            + f" (available: {', '.join(available)})"
        )
    return tuple(dict.fromkeys(value))


def _validate_taxprof(
    value: Any, metadata: MetadataDB | None, mode: str
) -> Sequence[str] | None:
    """Validate ``params.taxprof``: taxonomies the server can aggregate.

    The server is the only place that knows which metadata columns and
    sylph-tax sources its file has, so the request is checked against them
    (:func:`sylphd.taxonomy.resolve_taxonomy_columns`): a taxonomy the file
    cannot produce is a 400, not a silently empty table.
    """
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RequestError("'params.taxprof' must be a list of taxonomy names")
    if not value:
        return None
    if mode != "profile":
        raise RequestError("'params.taxprof' is only available on /profile")
    try:
        taxonomies = parse_taxonomies(value)
        summary = metadata.summary() if metadata is not None else None
        resolve_taxonomy_columns(taxonomies, summary)
    except (TaxonomyError, ProtocolError) as error:
        raise RequestError(str(error)) from None
    return taxonomies


def _resolve_names(value: Any, sketches: Sequence[Any]) -> list[str]:
    if value is None:
        names = []
        for index, part in enumerate(sketches):
            filename = getattr(part, "filename", None)
            name = sample_name_from_path(filename) if filename else ""
            names.append(name or f"sample-{index + 1}")
    elif not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RequestError("'params.names' must be a list of strings")
    elif len(value) != len(sketches):
        raise RequestError(
            f"'params.names' has {len(value)} entries for {len(sketches)} sketches"
        )
    else:
        names = value
    try:
        return [check_sample_name(name) for name in names]
    except InputError as error:
        raise RequestError(str(error)) from None


async def _json_http_error(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, StarletteHTTPException):
        return RequestError(str(exc.detail), exc.status_code).response()
    return RequestError(str(exc), 500).response()


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
        # create_app's AccessLogMiddleware logs each request (with its id).
        "access_log": False,
        # limits.client_identity() decides whom to trust; uvicorn must not
        # rewrite scope["client"] from X-Forwarded-For first.
        "proxy_headers": False,
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
