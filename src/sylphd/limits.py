"""Abuse limits for a public deployment: rate limits, queue caps, client IPs.

The server usually runs on a Unix socket on a machine you trust, so every
limit here is opt-in.  Behind a reverse proxy, ``--trusted-proxy`` names the
proxy addresses whose ``X-Forwarded-For`` header is believed; without it the
header is ignored entirely (a client could otherwise pick its own bucket).
"""

from __future__ import annotations

import hashlib
import hmac
import math
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from dataclasses import dataclass
from typing import Any

import anyio

#: Identity used for peers that have no IP address, i.e. Unix sockets.
UNIX_CLIENT = "unix"

#: Methods limited by the rate limiter, on the paths it is given (the
#: expensive POST endpoints and ``GET /usage``).
LIMITED_METHODS = frozenset({"POST", "GET"})

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
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str) -> int | None:
        """Record a request for ``key``.

        Returns ``None`` when the request is allowed, otherwise the number of
        seconds (rounded up, at least 1) to wait before retrying.
        """
        now = self._clock()
        cutoff = now - self.window
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= cutoff:
            hits.popleft()
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


#: Shortest accepted privileged token, in characters.
MIN_TOKEN_LENGTH = 16


def load_token_digests(path: str | Path) -> tuple[bytes, ...]:
    """Read bearer tokens from ``path``: one per line, blank lines and ``#`` comments ignored.

    Only the SHA-256 digests are kept in memory.  A token shorter than
    :data:`MIN_TOKEN_LENGTH` is refused (``ValueError``), as is a file with no
    token at all, so a typo cannot silently turn privileged access off or open
    it to a guessable secret.  Generate one with ``openssl rand -hex 32``.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(f"cannot read token file {path}: {error}") from error
    digests = []
    for line in text.splitlines():
        token = line.strip()
        if not token or token.startswith("#"):
            continue
        if len(token) < MIN_TOKEN_LENGTH:
            raise ValueError(
                f"token in {path} is shorter than {MIN_TOKEN_LENGTH} characters "
                "(try: openssl rand -hex 32)"
            )
        digests.append(hashlib.sha256(token.encode("utf-8")).digest())
    if not digests:
        raise ValueError(f"token file {path} contains no token")
    return tuple(digests)


def bearer_token(scope: dict) -> str | None:
    """The token of an ``Authorization: Bearer <token>`` header, or ``None``."""
    supplied = None
    for key, value in scope.get("headers", ()):
        if key.lower() == b"authorization":
            supplied = value.decode("latin-1")
    if supplied is None:
        return None
    scheme, _, token = supplied.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def is_privileged(scope: dict, digests: tuple[bytes, ...]) -> bool:
    """Whether the request carries ``Authorization: Bearer <token>`` for a known token.

    Digests are compared in constant time, and every digest is always
    compared, so timing reveals neither a match nor its position.
    """
    token = bearer_token(scope)
    if not digests or token is None:
        return False
    candidate = hashlib.sha256(token.encode("utf-8")).digest()
    matched = False
    for digest in digests:
        matched |= hmac.compare_digest(candidate, digest)
    return matched


#: Response header that says whether the bearer token of the request is
#: accepted; sent only when the request carried one.
TOKEN_VALID_HEADER = "X-Token-Valid"

#: ASGI scope key under which :class:`TokenMiddleware` leaves its verdict
#: (``True``/``False``, absent when no token was sent), for ``GET /info``.
TOKEN_SCOPE_KEY = "sylphd.token_valid"

#: Paths a server that requires a token still answers without one, so a
#: client can find out that it needs one.
OPEN_PATHS = frozenset({"/", "/health", "/info"})


class TokenMiddleware:
    """Report whether a request's bearer token is valid; optionally require one.

    A request that carries a token gets ``X-Token-Valid: true|false`` on its
    response, whatever the status, so a client can warn about a mistyped or
    rotated-out token instead of seeing only a rate limit.  With ``require``
    every path outside :data:`OPEN_PATHS` answers ``401`` without a valid
    token (CORS preflights are answered by the CORS middleware outside this
    one and never get here).
    """

    def __init__(
        self,
        app: Any,
        *,
        digests: tuple[bytes, ...],
        require: bool = False,
        open_paths: frozenset[str] | set[str] = OPEN_PATHS,
    ) -> None:
        self.app = app
        self.digests = tuple(digests)
        self.require = require
        self.open_paths = frozenset(open_paths)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        sent = bearer_token(scope) is not None
        valid = sent and is_privileged(scope, self.digests)
        if sent:
            scope[TOKEN_SCOPE_KEY] = valid
        header = (TOKEN_VALID_HEADER.lower().encode(), b"true" if valid else b"false")

        async def send_with_verdict(message: dict) -> None:
            if sent and message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", ()), header]}
            await send(message)

        if (
            self.require
            and not valid
            and scope.get("method") != "OPTIONS"
            and scope.get("path") not in self.open_paths
        ):
            from starlette.responses import JSONResponse

            response = JSONResponse(
                {
                    "error": (
                        "this server requires an access token: send "
                        "'Authorization: Bearer <token>'"
                        + (" (the token you sent is not accepted)" if sent else "")
                    )
                },
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send_with_verdict)
            return
        await self.app(scope, receive, send_with_verdict)


class RateLimitMiddleware:
    """Answer 429 for requests to ``paths`` that exceed the limiter, before reading the body.

    A request with a valid privileged bearer token (``privileged``: token
    digests) skips the limiter and is not counted.  The job queue limits still
    apply to it.
    """

    def __init__(
        self,
        app: Any,
        *,
        limiter: RateLimiter,
        paths: frozenset[str] | set[str],
        trusted: frozenset[str] | set[str] = frozenset(),
        privileged: tuple[bytes, ...] = (),
    ) -> None:
        self.app = app
        self.privileged = tuple(privileged)
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
        if is_privileged(scope, self.privileged):
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

    A job is one sample of a ``/profile`` or ``/query`` request, one
    ``/assembly`` upload, or one whole ``/contigs`` request (however many
    contigs it carries).  ``max_queue=None`` means unlimited (the default).
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

    @property
    def running(self) -> int:
        """Jobs holding a slot right now."""
        return self.max_jobs - self._semaphore.value

    @property
    def queued(self) -> int:
        """Admitted jobs that have not got a slot yet."""
        return max(0, self._admitted - self.running)

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
    async def slot(self) -> AsyncIterator[JobTimes]:
        """Wait for a free job slot (the job must have been admitted).

        Yields the job's :class:`JobTimes`; ``ran`` is set when the slot is left.
        """
        job = JobTimes(backlog=max(0, self._admitted - self.max_jobs))
        asked = time.perf_counter()
        async with self._semaphore:
            began = time.perf_counter()
            job.waited = began - asked
            try:
                yield job
            finally:
                job.ran = time.perf_counter() - began


@dataclass
class JobTimes:
    """How one job fared: the jobs beyond capacity when it asked for a slot
    (``backlog``), and the seconds it ``waited`` for the slot and ``ran`` in it."""

    backlog: int = 0
    waited: float = 0.0
    ran: float = 0.0
