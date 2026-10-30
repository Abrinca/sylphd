"""Running ``sylphd serve``: the Unix socket and the uvicorn entry point."""

from __future__ import annotations

import os
import signal
import socket
import stat
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette

from .paths import SOCKET_MODE
from .server import ServerConfig, create_app


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
