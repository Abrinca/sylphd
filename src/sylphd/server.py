"""Starlette application and uvicorn entry point for ``sylphd serve``.

Design notes
------------
* The database is loaded once, at startup, and shared by every request.
* Blocking ``pysylph`` calls run in worker threads through
  ``anyio.to_thread.run_sync``; an ``anyio`` semaphore caps the number of
  concurrent profiling jobs (``--max-jobs``) and queues the rest.
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
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import __version__
from .metadata import MetadataDB, accession_from_genome_name
from .paths import SOCKET_MODE, parse_tcp
from .protocol import ProfileParams, ProtocolError, result_row
from .sketchcheck import SketchFormatError, validate_sketch_bytes
from .sketching import sample_name_from_path

#: Maximum number of sketches accepted in one request.
MAX_SKETCHES = 4096


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
            },
            "endpoints": ["/health", "/info", "/profile", "/query"],
        }
    )


async def _index(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "service": "sylphd",
            "version": __version__,
            "endpoints": ["/health", "/info", "/profile", "/query"],
        }
    )


async def _profile(request: Request) -> JSONResponse:
    return await _handle(request, "profile")


async def _query(request: Request) -> JSONResponse:
    return await _handle(request, "query")


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
) -> None:
    async with state.semaphore:
        samples[index] = await anyio.to_thread.run_sync(
            _run_sample,
            state.database,
            state.metadata,
            mode,
            params,
            fields,
            name,
            payload,
        )


def _run_sample(
    database: LoadedDatabase,
    metadata: MetadataDB | None,
    mode: str,
    params: ProfileParams,
    fields: Sequence[str] | None,
    name: str,
    payload: bytes,
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
            rows.append(result_row(result, name, mode=mode))
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

    app = Starlette(
        routes=[
            Route("/", _index, methods=["GET"]),
            Route("/health", _health, methods=["GET"]),
            Route("/info", _info, methods=["GET"]),
            Route("/profile", _profile, methods=["POST"]),
            Route("/query", _query, methods=["POST"]),
        ],
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
    app.state.semaphore = anyio.Semaphore(config.max_jobs)
    app.add_middleware(
        BodyLimitMiddleware, max_bytes=config.max_upload_mb * 1024 * 1024
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
