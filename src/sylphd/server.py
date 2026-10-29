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
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import anyio
import pysylph
import uvicorn
from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException
from starlette.middleware.cors import CORSMiddleware
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
            "endpoints": _endpoints(state),
            "params": _params_info(),
        }
    )


def _endpoints(state: Any) -> list[str]:
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
            "endpoints": _endpoints(request.app.state),
        }
    )


async def _profile(request: Request) -> JSONResponse:
    return await _handle(request, "profile")


async def _query(request: Request) -> JSONResponse:
    return await _handle(request, "query")


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
            return _error(
                "this server does not accept raw sequences (it was started "
                "without --allow-sequences); send a sketch with Content-Type "
                f"{SKETCH_CONTENT_TYPE}",
                404,
            )
        if media_type and media_type not in SEQUENCE_CONTENT_TYPES:
            return _error(
                "expected raw FASTA or FASTQ with Content-Type text/plain, "
                "application/octet-stream or application/gzip, or a sketch "
                f"with Content-Type {SKETCH_CONTENT_TYPE}",
                415,
            )

    try:
        parsed = _parse_assembly_params(request)
    except ValueError as error:
        return _error(str(error), 400)
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
            if int(declared) > limit:
                return _too_large_response(limit, is_sketch)
        except ValueError:
            pass
    try:
        state.jobs.check()
    except QueueFullError:
        return _busy()
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            return _too_large_response(limit, is_sketch)
        chunks.append(chunk)
    payload = b"".join(chunks)
    if not payload:
        return _error("empty sketch upload" if is_sketch else "empty sequence upload", 400)

    start = time.perf_counter()
    try:
        with state.jobs.admit():
            async with state.jobs.slot():
                if is_sketch:
                    result = await anyio.to_thread.run_sync(
                        run_assembly_sketch,
                        state.database,
                        state.metadata,
                        name,
                        payload,
                        top,
                        min_ani,
                        stats,
                    )
                else:
                    result = await anyio.to_thread.run_sync(
                        run_assembly,
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
    except BadRequest as error:
        return _error(str(error), 400)
    except SequenceTooLarge as error:
        return _error(str(error), 413)
    except SequenceError as error:
        return _error(str(error), 400)
    except Exception:  # pragma: no cover - defensive
        log.exception("assembly profiling failed")
        return _error("assembly profiling failed", 500)

    elapsed_ms = round((time.perf_counter() - start) * 1000.0, 1)
    database = state.database
    return JSONResponse(
        {
            "query": result["query"],
            "assignment": result["assignment"],
            "closest": result["closest"],
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


def _too_large_response(limit: int, is_sketch: bool = False) -> JSONResponse:
    kind = "upload" if is_sketch else "sequence"
    return _error(
        f"request body exceeds the {limit // (1024 * 1024)} MB {kind} limit", 413
    )


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
                raise ValueError(f"'{key}' is too long (max {maximum} characters)")
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
        raise ValueError(f"'{key}' must be an integer") from None
    if value < minimum or value > maximum:
        raise ValueError(f"'{key}' must be between {minimum} and {maximum}")
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
        raise ValueError(f"'{key}' must be a number") from None
    if not math.isfinite(value):
        raise ValueError(f"'{key}' must be a finite number")
    if value < minimum or value > maximum:
        raise ValueError(f"'{key}' must be between {minimum:g} and {maximum:g}")
    return value


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
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        return _error(
            "expected a multipart/form-data body with one 'sketch' part per contig",
            415,
        )

    try:
        state.jobs.check()
    except QueueFullError:
        return _busy()

    upload = await _read_contigs(request)
    if isinstance(upload, JSONResponse):
        return upload
    contigs, options, tax_sources, payloads = upload

    start = time.perf_counter()
    results: list[dict[str, Any] | None] = [None] * len(contigs)
    # The metadata columns to fetch are request-wide; resolve them once.
    row_fields = per_contig.classification_fields(
        options.fields, _metadata_columns(state.metadata)
    )
    try:
        with state.jobs.admit():
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
    except QueueFullError:
        return _busy()
    except BadRequest as error:
        return _error(str(error), 400)
    except Exception:  # pragma: no cover - defensive
        log.exception("contig profiling failed")
        return _error("contig profiling failed", 500)

    elapsed_ms = round((time.perf_counter() - start) * 1000.0, 1)
    contig_results = [result for result in results if result is not None]
    database = state.database
    return JSONResponse(
        {
            "contigs": contig_results,
            "summary": per_contig.summarize(contig_results),
            "caveats": per_contig.caveats(contig_results),
            "database": {
                "name": database.name,
                "genomes": database.genomes,
                "c": database.c,
                "k": database.k,
            },
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


async def _read_contigs(
    request: Request,
) -> tuple[
    tuple[per_contig.ContigInput, ...],
    per_contig.ContigOptions,
    Sequence[str] | None,
    list[bytes],
] | JSONResponse:
    """Parse the ``/contigs`` form, or return the error response.

    The form (and its temporary files) is closed before returning.
    """
    state = request.app.state
    limit = state.max_contigs
    try:
        async with request.form(
            # Exactly the limit: the parser raises on the (N+1)th file part,
            # so N+1 uploads are not spooled to disk before the 400 below.
            max_files=limit,
            max_fields=16,
            max_part_size=MAX_PARAMS_BYTES,
        ) as form:
            raw_params = form.get("params")
            try:
                params_data: Any = (
                    {} if raw_params is None else json.loads(str(raw_params))
                )
            except json.JSONDecodeError as error:
                return _error(f"invalid JSON in 'params': {error}", 400)
            if not isinstance(params_data, dict):
                return _error("'params' must be a JSON object", 400)

            sketches = form.getlist("sketch")
            if not sketches:
                return _error("no 'sketch' part in the request", 400)
            if len(sketches) > limit:
                return _error(_too_many_contigs(limit), 400)
            for part in sketches:
                if not isinstance(part, UploadFile):
                    return _error("'sketch' parts must be file uploads", 400)

            try:
                contigs, options = per_contig.parse_params(
                    params_data, k=state.database.k, count=len(sketches)
                )
            except ProtocolError as error:
                return _error(str(error), 400)

            fields = _validate_fields(params_data.get("fields"), state.metadata)
            if isinstance(fields, JSONResponse):
                return fields
            tax_sources = _validate_tax_sources(
                params_data.get("tax_sources"), state.metadata
            )
            if isinstance(tax_sources, JSONResponse):
                return tax_sources

            try:
                payloads = [await part.read() for part in sketches]
            except Exception:
                log.exception("failed to read an uploaded sketch part")
                return _error("failed to read a 'sketch' part", 500)
            return (
                contigs,
                replace(options, fields=tuple(fields or ())),
                tax_sources,
                payloads,
            )
    except StarletteHTTPException as error:
        if _is_too_many_files(str(error.detail)):
            return _error(_too_many_contigs(limit), 400)
        return _error(str(error.detail), error.status_code)
    except MultiPartException as error:
        # Starlette re-raises this as a 400 HTTPException when the app is in
        # the scope; translating it here too keeps the message independent of
        # that. Other multipart errors (bad boundary, oversized part) keep
        # their own message.
        if _is_too_many_files(error.message):
            return _error(_too_many_contigs(limit), 400)
        return _error(error.message, 400)


async def _handle(request: Request, mode: str) -> JSONResponse:
    state = request.app.state
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        return _error(
            "expected a multipart/form-data body with one 'sketch' part per sample",
            415,
        )

    try:
        state.jobs.check()
    except QueueFullError:
        return _busy()

    upload = await _read_upload(request, mode)
    if isinstance(upload, JSONResponse):
        return upload
    params, fields, names, payloads, tax_sources, taxprof = upload

    samples: list[dict[str, Any] | None] = [None] * len(payloads)
    try:
        with state.jobs.admit(len(payloads)):
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
    except QueueFullError:
        return _busy()
    except BadRequest as error:
        return _error(str(error), 400)
    except BaseExceptionGroup as group:
        for error in group.exceptions:
            if isinstance(error, BadRequest):
                return _error(str(error), 400)
        log.exception("profiling failed")
        return _error("profiling failed", 500)
    except Exception:  # pragma: no cover - defensive
        log.exception("profiling failed")
        return _error("profiling failed", 500)
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
] | JSONResponse:
    """Parse the form into ``(params, fields, names, payloads, tax_sources, taxprof)``.

    Returns the error response instead when the request is invalid.  The form
    (and its temporary files) is closed before returning.
    """
    state = request.app.state
    try:
        async with request.form(
            max_files=MAX_SKETCHES,
            max_fields=16,
            max_part_size=MAX_PARAMS_BYTES,
        ) as form:
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
                    f"params.mode is {declared_mode!r} but this is the /{mode} endpoint",
                    400,
                )

            fields = _validate_fields(params_data.get("fields"), state.metadata)
            if isinstance(fields, JSONResponse):
                return fields

            tax_sources = _validate_tax_sources(
                params_data.get("tax_sources"), state.metadata
            )
            if isinstance(tax_sources, JSONResponse):
                return tax_sources

            taxprof = _validate_taxprof(
                params_data.get("taxprof"), state.metadata, mode
            )
            if isinstance(taxprof, JSONResponse):
                return taxprof

            sketches = form.getlist("sketch")
            if not sketches:
                return _error("no 'sketch' part in the request", 400)
            for part in sketches:
                if not isinstance(part, UploadFile):
                    return _error("'sketch' parts must be file uploads", 400)

            names = _resolve_names(params_data.get("names"), sketches)
            if isinstance(names, JSONResponse):
                return names

            try:
                payloads = [await part.read() for part in sketches]
            except Exception:
                log.exception("failed to read an uploaded sketch part")
                return _error("failed to read a 'sketch' part", 500)
            return params, fields, names, payloads, tax_sources, taxprof
    except StarletteHTTPException as error:
        return _error(str(error.detail), error.status_code)


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


def _validate_fields(value: Any, metadata: MetadataDB | None) -> Sequence[str] | None | JSONResponse:
    """Validate ``params.fields``: metadata column names, in request order.

    Duplicates are removed (a repeated column must not duplicate the key of a
    hit or the work of the metadata join), and the result cannot be longer
    than the number of columns the server's metadata file has.
    """
    if value is None:
        return None if metadata is None else tuple(metadata.field_columns)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return _error("'params.fields' must be a list of column names", 400)
    if not value:
        return ()
    if metadata is None:
        return _error("this server has no metadata database; --fields is not available", 400)
    unknown = [column for column in value if column not in metadata.field_columns]
    if unknown:
        return _error(f"unknown metadata column(s): {', '.join(unknown)}", 400)
    columns = tuple(dict.fromkeys(value))
    if len(columns) > len(metadata.field_columns):
        return _error(
            f"'params.fields' has {len(columns)} columns; this server's metadata "
            f"file has only {len(metadata.field_columns)}",
            400,
        )
    return columns


def _validate_tax_sources(
    value: Any, metadata: MetadataDB | None
) -> Sequence[str] | None | JSONResponse:
    """Validate ``params.tax_sources``: source names of the metadata file.

    ``None`` means all sources (sylph-tax given every file).  A name the file
    does not have is a 400, since silently resolving against another source
    would produce a profile that looks right and is not.
    """
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return _error("'params.tax_sources' must be a list of source names", 400)
    if not value:  # an empty list is "no restriction", like an omitted field
        return None
    available = metadata.source_names() if metadata is not None else []
    if not available:
        return _error(
            "this server has no sylph-tax taxonomy sources; "
            "'params.tax_sources' is not available",
            400,
        )
    unknown = [name for name in value if name not in available]
    if unknown:
        return _error(
            "unknown taxonomy source(s): "
            + ", ".join(unknown)
            + f" (available: {', '.join(available)})",
            400,
        )
    return tuple(dict.fromkeys(value))


def _validate_taxprof(
    value: Any, metadata: MetadataDB | None, mode: str
) -> Sequence[str] | None | JSONResponse:
    """Validate ``params.taxprof``: taxonomies the server can aggregate.

    The server is the only place that knows which metadata columns and
    sylph-tax sources its file has, so the request is checked against them
    (:func:`sylphd.taxonomy.resolve_taxonomy_columns`): a taxonomy the file
    cannot produce is a 400, not a silently empty table.
    """
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return _error(
            "'params.taxprof' must be a list of taxonomy names", 400
        )
    if not value:
        return None
    if mode != "profile":
        return _error("'params.taxprof' is only available on /profile", 400)
    try:
        taxonomies = parse_taxonomies(value)
        summary = metadata.summary() if metadata is not None else None
        resolve_taxonomy_columns(taxonomies, summary)
    except (TaxonomyError, ProtocolError) as error:
        return _error(str(error), 400)
    return taxonomies


def _resolve_names(value: Any, sketches: Sequence[Any]) -> list[str] | JSONResponse:
    if value is None:
        names = []
        for index, part in enumerate(sketches):
            filename = getattr(part, "filename", None)
            name = sample_name_from_path(filename) if filename else ""
            names.append(name or f"sample-{index + 1}")
    elif not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return _error("'params.names' must be a list of strings", 400)
    elif len(value) != len(sketches):
        return _error(
            f"'params.names' has {len(value)} entries for {len(sketches)} sketches", 400
        )
    else:
        names = value
    try:
        return [check_sample_name(name) for name in names]
    except InputError as error:
        return _error(str(error), 400)


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
            expose_headers=["Retry-After"],
            max_age=600,
        )
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
        "access_log": log_level != "warning",
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
