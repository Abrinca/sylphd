"""ASGI middleware of ``sylphd serve``: request body limit and access log."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any
from urllib.parse import quote

from starlette.responses import JSONResponse

log = logging.getLogger("uvicorn.error")


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

    ``serve`` turns uvicorn's own access log off, so this is the only one.  A
    line carries no client address.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

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
                "%s %s -> %d in %.1f ms request_id=%s",
                scope["method"],
                quote(scope["path"]),
                status,
                (time.perf_counter() - start) * 1000.0,
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
