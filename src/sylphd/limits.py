"""Abuse limits for a public deployment: rate limits, queue caps, client IPs.

The server usually runs on a Unix socket on a machine you trust, so every
limit here is opt-in.  Behind a reverse proxy, ``--trusted-proxy`` names the
proxy addresses whose ``X-Forwarded-For`` header is believed; without it the
header is ignored entirely (a client could otherwise pick its own bucket).
"""

from __future__ import annotations

import math
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

import anyio

#: Identity used for peers that have no IP address, i.e. Unix sockets.
UNIX_CLIENT = "unix"

#: Methods limited by the rate limiter (only the expensive POST endpoints).
LIMITED_METHODS = frozenset({"POST"})

#: Value of ``Retry-After`` on a full queue, in seconds.
QUEUE_RETRY_SECONDS = 1


class QueueFullError(Exception):
    """More requests are waiting for a job slot than ``--max-queue`` allows."""


class RateLimiter:
    """In-memory sliding-window rate limiter, keyed by client identity."""

    def __init__(
        self,
        count: int,
        window: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if count < 1:
            raise ValueError("rate limit count must be >= 1")
        if window <= 0:
            raise ValueError("rate limit window must be > 0")
        self.count = count
        self.window = window
        self._clock = clock
        self._hits: dict[str, list[float]] = {}

    def check(self, key: str) -> int | None:
        """Record a request for ``key``.

        Returns ``None`` when the request is allowed, otherwise the number of
        seconds (rounded up, at least 1) to wait before retrying.
        """
        now = self._clock()
        cutoff = now - self.window
        hits = self._hits.setdefault(key, [])
        while hits and hits[0] <= cutoff:
            hits.pop(0)
        if len(hits) >= self.count:
            retry = self.window - (now - hits[0])
            return max(1, math.ceil(retry))
        hits.append(now)
        if len(self._hits) > 10_000:
            self._prune(cutoff)
        return None

    def _prune(self, cutoff: float) -> None:
        for key in [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[key]


def parse_rate_limit(text: str) -> tuple[int, float]:
    """Parse ``N/min`` or ``N/s`` into ``(count, window_seconds)``."""
    raw = text.strip().lower()
    count_text, _, unit = raw.partition("/")
    if not count_text.isdigit():
        raise ValueError(f"invalid rate limit {text!r}: expected N/min or N/s")
    count = int(count_text)
    if count < 1:
        raise ValueError("rate limit must allow at least one request")
    if unit in {"s", "sec", "second", "seconds"}:
        window = 1.0
    elif unit in {"min", "minute", "minutes"}:
        window = 60.0
    else:
        raise ValueError(f"invalid rate limit unit {unit!r}: use /min or /s")
    return count, window


def peer_identity(scope: dict) -> str:
    """The socket peer of an ASGI scope, as a string (``"unix"`` for UDS)."""
    client = scope.get("client")
    if not client or not client[0]:
        return UNIX_CLIENT
    return str(client[0])


def client_identity(scope: dict, trusted: frozenset[str] | set[str]) -> str:
    """The client address to rate limit, honouring ``X-Forwarded-For``.

    Only a trusted peer (an IP in ``trusted``, or ``"unix"`` when that is
    trusted) may set ``X-Forwarded-For``.  The rightmost address that is not
    itself a trusted proxy is used; the rest of the chain is ignored because a
    client controls everything to the left of the proxy that appended its
    address.
    """
    peer = peer_identity(scope)
    if peer not in trusted:
        return peer
    raw = None
    for key, value in scope.get("headers", ()):
        if key.lower() == b"x-forwarded-for":
            raw = value.decode("latin-1")
    if not raw:
        return peer
    for address in reversed([part.strip() for part in raw.split(",")]):
        if not address or address in trusted:
            continue
        return address
    return peer


class RateLimitMiddleware:
    """Answer 429 for POSTs that exceed the limiter, before reading the body."""

    def __init__(
        self,
        app: Any,
        *,
        limiter: RateLimiter,
        paths: frozenset[str] | set[str],
        trusted: frozenset[str] | set[str] = frozenset(),
    ) -> None:
        self.app = app
        self.limiter = limiter
        self.paths = frozenset(paths)
        self.trusted = frozenset(trusted)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") not in LIMITED_METHODS
            or scope.get("path") not in self.paths
        ):
            await self.app(scope, receive, send)
            return
        key = client_identity(scope, self.trusted)
        retry = self.limiter.check(key)
        if retry is None:
            await self.app(scope, receive, send)
            return
        from starlette.responses import JSONResponse

        response = JSONResponse(
            {"error": f"rate limit exceeded; retry in {retry} s"},
            status_code=429,
            headers={"Retry-After": str(retry)},
        )
        await response(scope, receive, send)


class JobSlots:
    """A semaphore for profiling jobs plus a cap on the waiting queue.

    ``max_queue=None`` means unlimited (the default, as before).  With a cap,
    a request that would have to wait when ``max_queue`` requests (or jobs)
    already wait is refused instead, so the queue cannot grow without bound.
    """

    def __init__(self, max_jobs: int, max_queue: int | None = None) -> None:
        if max_jobs < 1:
            raise ValueError("max_jobs must be >= 1")
        self.max_jobs = max_jobs
        self.max_queue = max_queue
        self.running = 0
        self.waiting = 0
        self._semaphore = anyio.Semaphore(max_jobs)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        if (
            self.max_queue is not None
            and self.running >= self.max_jobs
            and self.waiting >= self.max_queue
        ):
            raise QueueFullError(
                f"more than {self.max_queue} request(s) are already waiting"
            )
        self.waiting += 1
        try:
            await self._semaphore.acquire()
        finally:
            self.waiting -= 1
        self.running += 1
        try:
            yield
        finally:
            self.running -= 1
            self._semaphore.release()
