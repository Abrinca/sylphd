"""``sylphd.taxonomy()``: an omnitax taxonomy served by a sylphd server (``--taxonomy-db``) under ``/taxonomy``."""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from urllib.parse import urlsplit

from .paths import default_socket_path

MOUNT = "taxonomy"


def resolve_address(url: str | None = None, socket: str | os.PathLike[str] | None = None) -> tuple[str, str]:
    """``("url" | "socket", where)`` by the precedence of docs/commands/address.md: explicit URL or socket,
    ``$SYLPHD_URL``, ``$SYLPHD_SOCKET``, the default socket. A URL is validated and loses its trailing slash."""
    if url is not None and socket is not None:
        raise ValueError("give at most one of url and socket")
    if url is None and socket is None:
        url = os.environ.get("SYLPHD_URL") or None
        socket = None if url else (os.environ.get("SYLPHD_SOCKET") or default_socket_path())
    if socket is not None:
        return "socket", str(socket)
    from omnitax.client import normalize_base_url

    return "url", normalize_base_url(url)


def check_token_transport(url: str) -> None:
    """Refuse to send a token over plain http to a host that is not this machine."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host.lower() == "localhost"
    if parts.scheme == "http" and not local:
        raise ValueError("refusing to send a token over plain http to a remote host; use https:// (or a Unix socket)")


def resolve_token(token: str | None = None, token_file: str | os.PathLike[str] | None = None) -> str | None:
    """The explicit token, else the contents of ``token_file``, else ``$SYLPHD_TOKEN``."""
    if token is None and token_file is not None:
        token = Path(token_file).expanduser().read_text().strip()
    return token or os.environ.get("SYLPHD_TOKEN") or None


def taxonomy(
    name: str | None = None,
    *,
    url: str | None = None,
    socket: str | os.PathLike[str] | None = None,
    token: str | None = None,
    token_file: str | os.PathLike[str] | None = None,
    timeout: float | None = None,
    retries: int = 0,
):
    """The taxonomy ``name`` (``"ncbi"``, ``"gtdb:232"``; none: the only one) of a sylphd server, as an
    :class:`omnitax.Taxonomy`. The server is found as the command line finds it (``url`` or ``socket``,
    ``$SYLPHD_URL``, ``$SYLPHD_SOCKET``, the default socket) and the token as well (``token``, ``token_file``,
    ``$SYLPHD_TOKEN``); a privileged token lifts the taxonomy rate limit."""
    try:
        import omnitax
    except ImportError as error:
        raise ImportError("sylphd.taxonomy() needs omnitax: pip install 'sylphd[client]'") from error
    kind, where = resolve_address(url, socket)
    secret = resolve_token(token, token_file)
    if secret and kind == "url":
        check_token_transport(where)
    headers = {"Authorization": f"Bearer {secret}"} if secret else None
    if kind == "url":
        return omnitax.connect(url=f"{where}/{MOUNT}", taxonomy=name, timeout=timeout, retries=retries, headers=headers)
    # omnitax has no path prefix for a socket: point its client at the mount.
    from omnitax.api import _wrap
    from omnitax.client import Client
    from omnitax.remote import RemoteStore

    client = Client(socket=where, timeout=timeout, retries=retries, headers=headers)
    client._client.base_url = f"http://omnitax.local/{MOUNT}/"
    try:
        return _wrap(RemoteStore(client, name))
    except BaseException:
        client.close()
        raise
