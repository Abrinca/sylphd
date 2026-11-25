"""Python transport to a running ``sylphd serve``: just what ``sylphd.ncbitax`` needs.

The query commands (``profile``, ``query``, ``assembly``, ``info``) run in the native
``sylphd-client``; this module only connects (a Unix socket, TCP or an ``http://`` /
``https://`` base URL), sends the token, retries 429/503 and talks JSON. Its address,
token and retry rules deliberately mirror ``rust/sylphd-client/src/{address,transport}.rs``
(``tests/test_address_cases.py`` and the Rust tests share one table of cases).
"""

from __future__ import annotations

import json
import os
import time
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit, urlunsplit

import httpx

#: Statuses that mean "try again shortly" (queue full, rate limited).
RETRY_STATUSES = frozenset({429, 503})

#: Longest wait between two attempts, in seconds.
MAX_RETRY_DELAY = 30.0


class TokenWarning(UserWarning):
    """The server does not accept the access token this client sends."""


def normalize_base_url(url: str) -> str:
    """Validate a base URL and strip its trailing slash.

    Accepts ``http://`` and ``https://`` URLs, with or without a path prefix
    (e.g. ``https://api.example.org/sylphd``).  A bare ``host:port`` gets
    ``http://`` prepended, as before.
    """
    text = url.strip()
    if not text:
        raise ValueError("empty server URL")
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise ValueError(
            f"unsupported URL scheme {parts.scheme!r}; use http:// or https://"
        )
    if not parts.hostname:
        raise ValueError(f"server URL has no host: {url!r}")
    try:
        _ = parts.port
    except ValueError as error:
        raise ValueError(f"invalid port in server URL {url!r}") from error
    if parts.query or parts.fragment:
        raise ValueError("server URL must not contain a query or fragment")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


class ClientError(RuntimeError):
    """Any failure talking to the server, including HTTP 4xx/5xx replies."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def default_timeout() -> httpx.Timeout:
    """No read timeout (profiling can be slow), 30 s to connect."""
    return httpx.Timeout(None, connect=30.0)


class SylphClient:
    """A small synchronous JSON client.

    Either ``socket_path`` (Unix socket) or ``base_url`` (``http://host:port``,
    optionally with a path prefix) must be given.
    """

    def __init__(
        self,
        *,
        socket_path: str | os.PathLike[str] | None = None,
        base_url: str | None = None,
        timeout: httpx.Timeout | float | None = None,
        retries: int = 0,
        token: str | None = None,
    ) -> None:
        if (socket_path is None) == (base_url is None):
            raise ValueError("exactly one of socket_path and base_url is required")
        self.socket_path = Path(socket_path) if socket_path is not None else None
        self.base_url = (
            normalize_base_url(base_url) if base_url is not None else None
        )
        self.timeout = timeout if timeout is not None else default_timeout()
        #: How often a 429/503 reply is retried (after its ``Retry-After``).
        self.retries = max(0, retries)
        self.token = token or None
        #: ``False`` once a response said the token is not accepted, ``True``
        #: once one said it is, ``None`` while nothing is known (or no token).
        self.token_valid: bool | None = None
        transport = None
        if self.socket_path is not None:
            transport = httpx.HTTPTransport(uds=str(self.socket_path), retries=0)
        # The trailing slash matters: httpx keeps the path prefix only when
        # request paths are relative ("info", not "/info").
        origin = f"{self.base_url}/" if self.base_url is not None else "http://sylphd.local/"
        headers = {"user-agent": f"sylphd/{_version()}"}
        if token:
            # A privileged token skips the server's rate limit; never send it
            # in the clear to a remote host.
            if self.base_url is not None and self.base_url.startswith("http://"):
                host = urlsplit(self.base_url).hostname or ""
                if host not in ("localhost", "127.0.0.1", "::1"):
                    raise ValueError("refusing to send a token over plain http")
            headers["authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            transport=transport,
            base_url=origin,
            timeout=self.timeout,
            headers=headers,
        )

    # -- plumbing ---------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return self._send(self._client.build_request(method, path, **kwargs))

    def _send(self, request: httpx.Request) -> httpx.Response:
        for attempt in range(self.retries + 1):
            try:
                response = self._client.send(request)
            except httpx.HTTPError as error:
                raise ClientError(self._connection_message(error)) from error
            if response.status_code not in RETRY_STATUSES or attempt == self.retries:
                break
            time.sleep(_retry_delay(response, attempt))
        self._note_token(response)
        if response.status_code >= 400:
            raise ClientError(_error_message(response), response.status_code)
        return response

    def _note_token(self, response: httpx.Response) -> None:
        """Warn (once) when the server says the token we send is not valid."""
        verdict = response.headers.get("x-token-valid")
        if verdict is None:
            return
        was = self.token_valid
        self.token_valid = verdict.strip().lower() == "true"
        if not self.token_valid and was is not False:
            where = self.base_url or f"the server on {self.socket_path}"
            warnings.warn(
                f"{where} does not accept this access token (mistyped, or replaced "
                "on the server?); you are treated as an anonymous client",
                TokenWarning,
                stacklevel=4,
            )

    def _connection_message(self, error: httpx.HTTPError) -> str:
        if self.socket_path is not None:
            return f"cannot talk to sylphd on {self.socket_path}: {error}"
        return f"cannot talk to sylphd at {self.base_url}: {error}"

    # -- endpoints --------------------------------------------------------
    def taxonomy(self, operation: str, payload: Mapping[str, Any] | None = None) -> Any:
        """Call a ``/taxonomy/<operation>`` endpoint: ``info`` is a GET, the rest POST JSON.

        Needs a server started with ``--taxonomy-db``; see :class:`sylphd.ncbitax.Taxonomy`
        for the friendly interface.
        """
        if operation == "info":
            return self._request("GET", "taxonomy/info").json()
        return self._request("POST", f"taxonomy/{operation}", json=dict(payload or {})).json()

    def health(self) -> dict[str, Any]:
        return self._request("GET", "health").json()

    def info(self) -> dict[str, Any]:
        return self._request("GET", "info").json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retry ``attempt + 1``: ``Retry-After``, but at
    least 1, 2, 4... s, and at most :data:`MAX_RETRY_DELAY`."""
    try:
        advised = float(response.headers.get("retry-after", 0))
    except ValueError:  # an HTTP date: not worth parsing
        advised = 0.0
    return min(MAX_RETRY_DELAY, max(advised, 2.0**attempt))


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        payload = None
    if isinstance(payload, Mapping) and "error" in payload:
        return f"server error ({response.status_code}): {payload['error']}"
    text = response.text.strip()
    return f"server error ({response.status_code}): {text or response.reason_phrase}"


def _version() -> str:
    from . import __version__

    return __version__


def connect(
    *,
    socket_path: str | os.PathLike[str] | None = None,
    url: str | None = None,
    timeout: float | None = None,
    retries: int = 0,
    token: str | None = None,
) -> SylphClient:
    """Build a client for a socket path or an HTTP(S) base URL / ``host:port``.

    The URL may carry a path prefix (``https://api.example.org/sylphd``);
    TLS certificates are verified as usual by httpx.
    """
    http_timeout = default_timeout() if timeout is None else httpx.Timeout(timeout, connect=30.0)
    if socket_path is not None:
        return SylphClient(
            socket_path=socket_path, timeout=http_timeout, retries=retries, token=token
        )
    if url is None:
        raise ValueError("either a socket path or a URL is required")
    return SylphClient(
        base_url=normalize_base_url(url),
        timeout=http_timeout,
        retries=retries,
        token=token,
    )
