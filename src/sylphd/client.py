"""HTTP client for a running ``sylphd serve``.

The client speaks plain HTTP over a Unix socket (default) or TCP.  It only
sends sample sketches: sketching is done locally with the database's ``c``/``k``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx

from .protocol import ProfileParams


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

    Either ``socket_path`` (Unix socket) or ``base_url`` (``http://host:port``)
    must be given.
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
        self.base_url = base_url
        self.timeout = timeout if timeout is not None else default_timeout()
        transport = None
        if self.socket_path is not None:
            transport = httpx.HTTPTransport(uds=str(self.socket_path), retries=0)
        self._client = httpx.Client(
            transport=transport,
            base_url=base_url or "http://sylphd.local",
            timeout=self.timeout,
            headers={"user-agent": f"sylphd/{_version()}"},
        )

    # -- plumbing ---------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
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
        return self._request("GET", "/health").json()

    def info(self) -> dict[str, Any]:
        return self._request("GET", "/info").json()

    def profile(
        self,
        samples: Sequence[tuple[str, bytes]],
        *,
        params: ProfileParams | None = None,
        fields: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self._post("profile", samples, params, fields)

    def query(
        self,
        samples: Sequence[tuple[str, bytes]],
        *,
        params: ProfileParams | None = None,
        fields: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self._post("query", samples, params, fields)

    def _post(
        self,
        mode: str,
        samples: Sequence[tuple[str, bytes]],
        params: ProfileParams | None,
        fields: Sequence[str] | None,
    ) -> list[dict[str, Any]]:
        if not samples:
            raise ValueError("no samples to send")
        payload: dict[str, Any] = {
            **(params or ProfileParams()).to_dict(),
            "names": [name for name, _ in samples],
            "mode": mode,
        }
        if fields is not None:
            payload["fields"] = list(fields)
        files: list[tuple[str, tuple[str, bytes, str]]] = [
            (
                "sketch",
                (f"{name or 'sample'}.sylsp", data, "application/octet-stream"),
            )
            for name, data in samples
        ]
        response = self._request(
            "POST",
            f"/{mode}",
            data={"params": json.dumps(payload)},
            files=files,
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
    """Build a client for a socket path or an ``http://`` URL / ``host:port``."""
    http_timeout = default_timeout() if timeout is None else httpx.Timeout(timeout, connect=30.0)
    if socket_path is not None:
        return SylphClient(socket_path=socket_path, timeout=http_timeout)
    if url is None:
        raise ValueError("either a socket path or a URL is required")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return SylphClient(base_url=url, timeout=http_timeout)
