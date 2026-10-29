"""Abuse limits for a public deployment: rate limits, queue caps, client IPs.

The server usually runs on a Unix socket on a machine you trust, so every
limit here is opt-in.  Behind a reverse proxy, ``--trusted-proxy`` names the
proxy addresses whose ``X-Forwarded-For`` header is believed; without it the
header is ignored entirely (a client could otherwise pick its own bucket).
"""

from __future__ import annotations

import math
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Callable, Iterator

import anyio

#: Identity used for peers that have no IP address, i.e. Unix sockets.
UNIX_CLIENT = "unix"

#: Methods limited by the rate limiter (only the expensive POST endpoints).
LIMITED_METHODS = frozenset({"POST"})

#: Value of ``Retry-After`` on a full queue, in seconds.
QUEUE_RETRY_SECONDS = 1


class QueueFullError(Exception):
    """More jobs are waiting for a job slot than ``--max-queue`` allows."""


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
    """A semaphore for profiling jobs plus a cap on the jobs waiting for it.

    A job is one sample of a ``/profile`` or ``/query`` request, or one
    ``/assembly`` upload.  ``max_queue=None`` means unlimited (the default).
    With a cap, at most ``max_jobs + max_queue`` jobs are admitted at a time;
    a request that does not fit is refused as a whole, so the queue cannot
    grow without bound.

    Usage: :meth:`check` early (before reading a body), then :meth:`admit`
    for all the jobs of a request, then :meth:`slot` around each job.
    """

    def __init__(self, max_jobs: int, max_queue: int | None = None) -> None:
        if max_jobs < 1:
            raise ValueError("max_jobs must be >= 1")
        self.max_jobs = max_jobs
        self.max_queue = max_queue
        self._admitted = 0
        self._semaphore = anyio.Semaphore(max_jobs)

    def check(self, count: int = 1) -> None:
        """Raise :class:`QueueFullError` unless ``count`` more jobs fit now."""
        if (
            self.max_queue is not None
            and self._admitted + count > self.max_jobs + self.max_queue
        ):
            raise QueueFullError(
                f"{self._admitted} job(s) are already running or waiting "
                f"(limit {self.max_jobs} + {self.max_queue} queued)"
            )

    @contextmanager
    def admit(self, count: int = 1) -> Iterator[None]:
        """Reserve room for ``count`` jobs, or raise :class:`QueueFullError`.

        Never awaits, so concurrent requests cannot both pass the check.
        """
        self.check(count)
        self._admitted += count
        try:
            yield
        finally:
            self._admitted -= count

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Wait for a free job slot (the job must have been admitted)."""
        async with self._semaphore:
            yield
