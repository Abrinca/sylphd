"""HTTP client for a running ``sylphd serve``.

The client speaks HTTP over a Unix socket (default), TCP, or an ``http://`` /
``https://`` base URL.  Sketches are made locally with the database's
``c``/``k``.  ``/assembly`` takes either such a sketch (``assembly_sketch``,
the default of ``sylphd assembly``: the sequence never leaves the machine) or
raw sequences (``assembly``, only if the server runs with ``--allow-sequences``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx

from .protocol import ProfileParams, params_json

#: ``Content-Type`` of an ``/assembly`` body that is a sketch (see the server).
SKETCH_CONTENT_TYPE = "application/vnd.sylph.sketch"


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
        parts.port
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
    """A small synchronous client.

    Either ``socket_path`` (Unix socket) or ``base_url`` (``http://host:port``,
    optionally with a path prefix) must be given.
    """

    def __init__(
        self,
        *,
        socket_path: str | os.PathLike[str] | None = None,
        base_url: str | None = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> None:
        if (socket_path is None) == (base_url is None):
            raise ValueError("exactly one of socket_path and base_url is required")
        self.socket_path = Path(socket_path) if socket_path is not None else None
        self.base_url = (
            normalize_base_url(base_url) if base_url is not None else None
        )
        self.timeout = timeout if timeout is not None else default_timeout()
        transport = None
        if self.socket_path is not None:
            transport = httpx.HTTPTransport(uds=str(self.socket_path), retries=0)
        # The trailing slash matters: httpx keeps the path prefix only when
        # request paths are relative ("info", not "/info").
        origin = f"{self.base_url}/" if self.base_url is not None else "http://sylphd.local/"
        self._client = httpx.Client(
            transport=transport,
            base_url=origin,
            timeout=self.timeout,
            headers={"user-agent": f"sylphd/{_version()}"},
        )

    # -- plumbing ---------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return self._send(self._client.build_request(method, path, **kwargs))

    def _send(self, request: httpx.Request) -> httpx.Response:
        try:
            response = self._client.send(request)
        except httpx.HTTPError as error:
            raise ClientError(self._connection_message(error)) from error
        if response.status_code >= 400:
            raise ClientError(_error_message(response), response.status_code)
        return response

    def _connection_message(self, error: httpx.HTTPError) -> str:
        if self.socket_path is not None:
            return f"cannot talk to sylphd on {self.socket_path}: {error}"
        return f"cannot talk to sylphd at {self.base_url}: {error}"

    # -- endpoints --------------------------------------------------------
    def health(self) -> dict[str, Any]:
        return self._request("GET", "health").json()

    def info(self) -> dict[str, Any]:
        return self._request("GET", "info").json()

    def profile(
        self,
        samples: Sequence[tuple[str, bytes]],
        *,
        params: ProfileParams | None = None,
        fields: Sequence[str] | None = None,
        tax_sources: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self._post("profile", samples, params, fields, tax_sources)

    def query(
        self,
        samples: Sequence[tuple[str, bytes]],
        *,
        params: ProfileParams | None = None,
        fields: Sequence[str] | None = None,
        tax_sources: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self._post("query", samples, params, fields, tax_sources)

    def assembly(
        self,
        payload: bytes,
        *,
        top: int = 10,
        min_ani: float = 80.0,
        name: str | None = None,
        content_type: str = "application/octet-stream",
        extra_params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send one raw FASTA/FASTQ assembly to ``POST /assembly``.

        The server must run with ``--allow-sequences``.
        """
        return self._assembly(
            payload, top=top, min_ani=min_ani, name=name, content_type=content_type,
            extra_params=extra_params,
        )

    def assembly_sketch(
        self,
        sketch: bytes,
        *,
        top: int = 10,
        min_ani: float = 80.0,
        name: str | None = None,
        records: int | None = None,
        length: int | None = None,
        gc: float | None = None,
    ) -> dict[str, Any]:
        """Send the sketch (``.sylsp`` bytes) of one assembly to ``POST /assembly``.

        Works on every server.  ``records``, ``length`` and ``gc`` are
        informational: the server echoes them in ``query`` since it never sees
        the sequence.
        """
        extra = {
            key: value
            for key, value in (("records", records), ("length", length), ("gc", gc))
            if value is not None
        }
        return self._assembly(
            sketch, top=top, min_ani=min_ani, name=name,
            content_type=SKETCH_CONTENT_TYPE, extra_params=extra,
        )

    def _assembly(
        self,
        payload: bytes,
        *,
        top: int,
        min_ani: float,
        name: str | None,
        content_type: str,
        extra_params: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"top": top, "min_ani": min_ani}
        if name:
            params["name"] = name
        params.update(extra_params or {})
        response = self._request(
            "POST",
            "assembly",
            params=params,
            content=payload,
            headers={"content-type": content_type},
        )
        result = response.json()
        if not isinstance(result, dict) or "hits" not in result:
            raise ClientError("unexpected response from /assembly")
        return result

    def query_size(
        self,
        samples: Sequence[tuple[str, bytes]],
        *,
        params: ProfileParams | None = None,
        fields: Sequence[str] | None = None,
    ) -> int:
        """Exact size in bytes of the multipart body :meth:`query` would send.

        This is the prepared request's ``Content-Length`` (boundary, form
        headers, JSON ``params`` and file names included), so a caller can
        compare it with a server's upload limit instead of estimating.
        """
        request = self._prepare_post("query", samples, params, fields)
        length = request.headers.get("content-length")
        if length is None:  # pragma: no cover - bytes payloads always have one
            raise ClientError("cannot determine the request size")
        return int(length)

    def _prepare_post(
        self,
        mode: str,
        samples: Sequence[tuple[str, bytes]],
        params: ProfileParams | None,
        fields: Sequence[str] | None,
        tax_sources: Sequence[str] | None = None,
    ) -> httpx.Request:
        """Build the multipart request ``_post`` sends (shared with ``query_size``)."""
        names = [name for name, _ in samples]
        files: list[tuple[str, tuple[str, bytes, str]]] = [
            (
                "sketch",
                (f"{name or 'sample'}.sylsp", data, "application/octet-stream"),
            )
            for name, data in samples
        ]
        return self._client.build_request(
            "POST",
            mode,
            data={"params": params_json(mode, params, names, fields, tax_sources)},
            files=files,
        )

    def _post(
        self,
        mode: str,
        samples: Sequence[tuple[str, bytes]],
        params: ProfileParams | None,
        fields: Sequence[str] | None,
        tax_sources: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not samples:
            raise ValueError("no samples to send")
        response = self._send(
            self._prepare_post(mode, samples, params, fields, tax_sources)
        )
        result = response.json()
        if not isinstance(result, list):
            raise ClientError(f"unexpected response from /{mode}: not a JSON list")
        return result

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SylphClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


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
) -> SylphClient:
    """Build a client for a socket path or an HTTP(S) base URL / ``host:port``.

    The URL may carry a path prefix (``https://api.example.org/sylphd``);
    TLS certificates are verified as usual by httpx.
    """
    http_timeout = default_timeout() if timeout is None else httpx.Timeout(timeout, connect=30.0)
    if socket_path is not None:
        return SylphClient(socket_path=socket_path, timeout=http_timeout)
    if url is None:
        raise ValueError("either a socket path or a URL is required")
    return SylphClient(base_url=normalize_base_url(url), timeout=http_timeout)
