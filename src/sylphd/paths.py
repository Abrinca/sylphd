"""Shared path and address helpers for the server and the client."""

from __future__ import annotations

import os
from pathlib import Path

#: Socket permissions used by ``sylphd serve``: the owner and their group may
#: connect.  Adjust with ``chgrp``/``chmod`` if a different group is needed.
SOCKET_MODE = 0o660

#: Longest path ``AF_UNIX`` accepts (sun_path is 108 bytes including NUL).
MAX_SOCKET_PATH_BYTES = 107


def default_socket_path() -> Path:
    """``$XDG_RUNTIME_DIR/sylphd.sock``, falling back to ``/tmp/sylphd-$UID.sock``."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "sylphd.sock"
    return Path("/tmp") / f"sylphd-{os.getuid()}.sock"


def check_socket_path(path: str | os.PathLike[str]) -> Path:
    """Return ``path`` as a ``Path``, rejecting paths too long for a socket."""
    result = Path(path).expanduser()
    encoded = os.fsencode(result)
    if len(encoded) > MAX_SOCKET_PATH_BYTES:
        raise ValueError(
            f"socket path is {len(encoded)} bytes, the limit is "
            f"{MAX_SOCKET_PATH_BYTES}: {result}"
        )
    return result


def parse_tcp(text: str) -> tuple[str, int]:
    """Parse ``HOST:PORT``; ``[::1]:9000`` and ``:9000`` work too."""
    host, separator, port_text = text.rpartition(":")
    if not separator or not port_text:
        raise ValueError(f"expected HOST:PORT, got {text!r}")
    try:
        port = int(port_text)
    except ValueError as error:
        raise ValueError(f"invalid port in {text!r}") from error
    if not 0 < port < 65536:
        raise ValueError(f"invalid port in {text!r}")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host:
        host = "127.0.0.1"
    return host, port


def format_tcp(host: str, port: int) -> str:
    if ":" in host:
        return f"[{host}]:{port}"
    return f"{host}:{port}"
