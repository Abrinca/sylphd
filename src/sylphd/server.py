"""Starlette application and uvicorn entry point for ``sylphd serve``.

Design notes
------------
* The database is loaded once, at startup, and shared by every request.
* Blocking ``pysylph`` calls run in worker threads through
  ``anyio.to_thread.run_sync``; a semaphore caps the number of concurrent
  profiling jobs (``--max-jobs``) and queues the rest, optionally up to
  ``--max-queue`` jobs (one job per sample).
* A fresh ``Profiler`` is built per request: it holds references only.
* One process, one worker: the whole point is sharing the in-memory index.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import stat
import sys
import threading
import time
from dataclasses import dataclass, field
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
from .paths import SOCKET_MODE, parse_tcp
from .profiling import (
    BadRequest,
    LoadedDatabase,
    run_assembly,
    run_assembly_sketch,
    run_sample,
)
from .protocol import ProfileParams, ProtocolError
from .sketching import InputError, check_sample_name, sample_name_from_path

log = logging.getLogger("uvicorn.error")

#: Maximum number of sketches accepted in one request.
MAX_SKETCHES = 4096

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


@dataclass
class ServerConfig:
    """Everything ``sylphd serve`` needs."""

    db_path: Path
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
    return ["/health", "/info", "/profile", "/query", "/assembly"]


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
        top = _query_integer(request, "top", 10, MIN_TOP, MAX_TOP)
        min_ani = _query_float(request, "min_ani", 80.0, *MIN_ANI_RANGE)
        stats = (
            {
                "records": _query_integer(request, "records", None, 0, MAX_INFO_RECORDS),
                "length": _query_integer(request, "length", None, 0, MAX_INFO_LENGTH),
                "gc": _query_float(request, "gc", None, 0.0, 100.0),
            }
            if is_sketch
            else None
        )
    except ValueError as error:
        return _error(str(error), 400)

    name = request.query_params.get("name") or "assembly"
    if len(name) > 1024:
        return _error("'name' is too long (max 1024 characters)", 400)

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
    if not value == value or value in (float("inf"), float("-inf")):
        raise ValueError(f"'{key}' must be a finite number")
    if value < minimum or value > maximum:
        raise ValueError(f"'{key}' must be between {minimum:g} and {maximum:g}")
    return value


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
    params, fields, names, payloads = upload

    samples: list[dict[str, Any] | None] = [None] * len(payloads)
    try:
        with state.jobs.admit(len(payloads)):
            async with anyio.create_task_group() as task_group:
                for index, (name, payload) in enumerate(zip(names, payloads)):
                    task_group.start_soon(
                        _run_slot, state, mode, params, fields, name, payload, samples, index
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
) -> tuple[ProfileParams, Sequence[str] | None, list[str], list[bytes]] | JSONResponse:
    """Parse the multipart form into ``(params, fields, names, payloads)``.

    Returns the error response instead when the request is invalid.  The form
    (and its temporary files) is closed before returning.
    """
    state = request.app.state
    try:
        async with request.form(
            max_files=MAX_SKETCHES,
            max_fields=16,
            max_part_size=1024 * 1024,
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
            return params, fields, names, payloads
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
        )


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
        Route("/assembly", _assembly, methods=["POST"]),
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
            paths=frozenset(("/profile", "/query", "/assembly")),
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
