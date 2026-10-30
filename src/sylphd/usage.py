"""Opt-in usage statistics of ``sylphd serve`` (``--usage-db``).

What is counted, and nothing else: each successful request to ``/assembly``,
``/contigs``, ``/profile`` or ``/query`` adds to the counters of one row, keyed
by a ten-minute window, the endpoint, the kind of client and (for the command
line client) its version.  No address, name, result, taxon or parameter is
ever stored, and no per-user data of any kind: a row cannot be traced back to
a person, a file or a request.

Counters accumulate in memory and a background thread writes them to SQLite
about once a minute (and on shutdown), so a request never waits on the file
and a crash loses at most that interval.  Once a day, windows older than
:data:`KEEP_DAYS` days merge into monthly rows, which are kept for good.  Job
time and queue wait are fixed-bucket histograms, so medians and 95th
percentiles can be read from them and monthly rows stay exact.

Bump :data:`POLICY` whenever what is tracked changes: ``/info`` advertises it
and the web page asks for agreement again.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("uvicorn.error")

#: Version of what is tracked; ``/info`` advertises it as ``usage.policy``.
POLICY = 1

#: Length of one counting window, in minutes.
WINDOW_MINUTES = 10

#: Seconds between two writes of the counters to the database.
FLUSH_SECONDS = 60.0

#: Windows older than this many days merge into monthly rows.
KEEP_DAYS = 92

#: How long a ``/usage`` answer is reused, in seconds.
REPORT_SECONDS = 60.0

#: Endpoints that are counted.
ENDPOINTS = frozenset({"/assembly", "/contigs", "/profile", "/query"})

#: Upper bounds (seconds) of the job time and queue wait histograms; one more
#: bucket takes everything above the last bound.
BUCKET_BOUNDS = (
    0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0, 3600.0,
)

#: Counters that add up; ``peak_queue_depth`` takes the maximum instead.
COUNTERS = ("requests", "contigs", "samples", "bases", "sketch_bytes")

#: The numeric columns of a row, in :meth:`Counts.values` order.
_COLUMNS = ", ".join((*COUNTERS, "peak_queue_depth", "job_hist", "wait_hist"))

_CLI_VERSION = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS {table} (
    period TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    client TEXT NOT NULL,
    version TEXT NOT NULL,
    requests INTEGER NOT NULL,
    contigs INTEGER NOT NULL,
    samples INTEGER NOT NULL,
    bases INTEGER NOT NULL,
    sketch_bytes INTEGER NOT NULL,
    peak_queue_depth INTEGER NOT NULL,
    job_hist TEXT NOT NULL,
    wait_hist TEXT NOT NULL,
    PRIMARY KEY (period, endpoint, client, version)
) WITHOUT ROWID
"""

#: ``(period, endpoint, client, version)``; ``period`` is the window start
#: (``2026-10-30T12:10Z``) in ``windows`` and the month (``2026-10``) in ``months``.
Key = tuple[str, str, str, str]


def _empty_histogram() -> list[int]:
    return [0] * (len(BUCKET_BOUNDS) + 1)


@dataclass
class Counts:
    """The numbers of one row."""

    requests: int = 0
    contigs: int = 0
    samples: int = 0
    bases: int = 0
    sketch_bytes: int = 0
    peak_queue_depth: int = 0
    job_hist: list[int] = field(default_factory=_empty_histogram)
    wait_hist: list[int] = field(default_factory=_empty_histogram)

    def merge(self, other: Counts) -> None:
        for name in COUNTERS:
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.peak_queue_depth = max(self.peak_queue_depth, other.peak_queue_depth)
        for mine, theirs in ((self.job_hist, other.job_hist), (self.wait_hist, other.wait_hist)):
            for index, count in enumerate(theirs):
                mine[index] += count

    def values(self) -> tuple[Any, ...]:
        return (
            *(getattr(self, name) for name in COUNTERS),
            self.peak_queue_depth,
            json.dumps(self.job_hist),
            json.dumps(self.wait_hist),
        )

    @classmethod
    def from_values(cls, values: Iterable[Any]) -> Counts:
        *numbers, job_hist, wait_hist = values
        return cls(*numbers, json.loads(job_hist), json.loads(wait_hist))


def classify(headers: Mapping[str, str]) -> tuple[str, str]:
    """The ``(client kind, client version)`` of a request.

    ``web`` when the browser sent an ``Origin`` header, ``cli`` when the
    user agent is ``sylphd/<version>`` and ``other`` otherwise.  The version is
    kept only for ``cli`` and only when it is a plain ``x.y.z``.
    """
    if headers.get("origin"):
        return "web", ""
    agent = headers.get("user-agent", "")
    if agent.startswith("sylphd/"):
        version = agent[len("sylphd/"):]
        return "cli", version if _CLI_VERSION.fullmatch(version) else ""
    return "other", ""


def window_start(moment: datetime) -> str:
    minute = moment.minute // WINDOW_MINUTES * WINDOW_MINUTES
    return moment.replace(minute=minute, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%MZ")


def quantile(histogram: list[int], fraction: float) -> float | None:
    """Upper bound (seconds) of the bucket holding the ``fraction`` quantile."""
    total = sum(histogram)
    if total == 0:
        return None
    wanted = math.ceil(total * fraction)
    seen = 0
    for index, count in enumerate(histogram):
        seen += count
        if seen >= wanted:
            return BUCKET_BOUNDS[min(index, len(BUCKET_BOUNDS) - 1)]
    return BUCKET_BOUNDS[-1]  # pragma: no cover - the loop always reaches `wanted`


def summarize(rows: Iterable[Counts]) -> dict[str, Any]:
    """Totals of ``rows`` with the median and 95th percentile of the histograms."""
    total = Counts()
    for row in rows:
        total.merge(row)
    return {
        **{name: getattr(total, name) for name in COUNTERS},
        "peak_queue_depth": total.peak_queue_depth,
        "job_seconds": _distribution(total.job_hist),
        "queue_wait_seconds": _distribution(total.wait_hist),
    }


def _distribution(histogram: list[int]) -> dict[str, Any]:
    return {
        "count": sum(histogram),
        "median": quantile(histogram, 0.5),
        "p95": quantile(histogram, 0.95),
    }


class Usage:
    """The counters, their database and the thread that writes them."""

    def __init__(
        self, path: Path, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[Key, Counts] = {}
        self._rolled_on = ""
        self._report: tuple[float, bytes] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        try:
            with closing(self._connect()) as db, db:
                for table in ("windows", "months"):
                    db.execute(_SCHEMA.format(table=table))
        except sqlite3.Error as error:
            raise ValueError(f"cannot use usage database {self.path}: {error}") from None

    # -- recording --------------------------------------------------------
    def record(
        self,
        endpoint: str,
        headers: Mapping[str, str],
        *,
        jobs: Iterable[Any] = (),
        contigs: int = 0,
        samples: int = 0,
        bases: int = 0,
        sketch_bytes: int = 0,
    ) -> None:
        """Count one successful request; ``jobs`` are its :class:`~sylphd.limits.JobTimes`."""
        counts = Counts(
            requests=1,
            contigs=contigs,
            samples=samples,
            bases=bases,
            sketch_bytes=sketch_bytes,
        )
        for job in jobs:
            counts.peak_queue_depth = max(counts.peak_queue_depth, job.backlog)
            counts.job_hist[bisect.bisect_left(BUCKET_BOUNDS, job.ran)] += 1
            counts.wait_hist[bisect.bisect_left(BUCKET_BOUNDS, job.waited)] += 1
        client, version = classify(headers)
        period = window_start(datetime.fromtimestamp(self._clock(), UTC))
        with self._lock:
            self._pending.setdefault(
                (period, endpoint, client, version), Counts()
            ).merge(counts)

    # -- writing ----------------------------------------------------------
    def start(self) -> None:
        """Start the thread that writes the counters about once a minute."""
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="usage", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Stop the thread and write what is left."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        self.flush()

    def _run(self) -> None:
        while not self._stop.wait(FLUSH_SECONDS):
            self.flush()

    def flush(self) -> None:
        """Write the pending counters; once a day also merge old windows into months."""
        with self._lock:
            pending, self._pending = self._pending, {}
        now = datetime.fromtimestamp(self._clock(), UTC)
        try:
            with closing(self._connect()) as db, db:
                self._add(db, "windows", pending)
                if self._rolled_on != now.strftime("%Y-%m-%d"):
                    self._roll_up(db, now)
                    self._rolled_on = now.strftime("%Y-%m-%d")
        except sqlite3.Error:
            log.exception("could not write the usage statistics; keeping them in memory")
            with self._lock:
                for key, counts in pending.items():
                    self._pending.setdefault(key, Counts()).merge(counts)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10.0)

    @staticmethod
    def _add(db: sqlite3.Connection, table: str, rows: Mapping[Key, Counts]) -> None:
        for key, counts in rows.items():
            found = db.execute(
                f"SELECT {_COLUMNS} FROM {table} "
                "WHERE period = ? AND endpoint = ? AND client = ? AND version = ?",
                key,
            ).fetchone()
            if found is not None:
                counts.merge(Counts.from_values(found))
            db.execute(
                f"INSERT OR REPLACE INTO {table} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*key, *counts.values()),
            )

    def _roll_up(self, db: sqlite3.Connection, now: datetime) -> None:
        cutoff = window_start(now - timedelta(days=KEEP_DAYS))
        months: dict[Key, Counts] = {}
        for key, counts in self._rows(db, "windows", "WHERE period < ?", (cutoff,)):
            month = (key[0][:7], *key[1:])
            months.setdefault(month, Counts()).merge(counts)
        self._add(db, "months", months)
        db.execute("DELETE FROM windows WHERE period < ?", (cutoff,))

    @staticmethod
    def _rows(
        db: sqlite3.Connection, table: str, where: str = "", args: tuple[Any, ...] = ()
    ) -> list[tuple[Key, Counts]]:
        found = db.execute(
            f"SELECT period, endpoint, client, version, {_COLUMNS} FROM {table} "
            f"{where} ORDER BY period, endpoint, client, version",
            args,
        )
        return [(tuple(row[:4]), Counts.from_values(row[4:])) for row in found]

    # -- reading ----------------------------------------------------------
    def report_json(self) -> bytes:
        """The ``GET /usage`` body, reused for :data:`REPORT_SECONDS`."""
        now = time.monotonic()
        cached = self._report
        if cached is not None and now - cached[0] < REPORT_SECONDS:
            return cached[1]
        body = json.dumps(self.report(), separators=(",", ":")).encode()
        self._report = (now, body)
        return body

    def report(self) -> dict[str, Any]:
        """Everything written so far, aggregated; nothing that identifies anyone."""
        with closing(self._connect()) as db:
            windows = self._rows(db, "windows")
            months = self._rows(db, "months")
        every = [counts for _, counts in (*windows, *months)]
        periods = [key[0] + "-01T00:00Z" if len(key[0]) == 7 else key[0] for key, _ in (*months, *windows)]
        return {
            "policy": POLICY,
            "window_minutes": WINDOW_MINUTES,
            "generated_at": datetime.fromtimestamp(self._clock(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "since": min(periods, default=None),
            "bucket_bounds_seconds": list(BUCKET_BOUNDS),
            "totals": summarize(every),
            "by_endpoint": self._grouped(windows, months, 1),
            "by_client": self._grouped(windows, months, 2),
            "windows": {
                "columns": ["start", "endpoint", "client", "version", *COUNTERS, "peak_queue_depth"],
                "rows": [
                    [*key, *(getattr(c, name) for name in COUNTERS), c.peak_queue_depth]
                    for key, c in windows
                ],
            },
            "months": [
                {
                    "month": key[0],
                    "endpoint": key[1],
                    "client": key[2],
                    "version": key[3],
                    **{name: getattr(c, name) for name in COUNTERS},
                    "peak_queue_depth": c.peak_queue_depth,
                    "job_histogram": c.job_hist,
                    "queue_wait_histogram": c.wait_hist,
                }
                for key, c in months
            ],
        }

    @staticmethod
    def _grouped(
        windows: list[tuple[Key, Counts]], months: list[tuple[Key, Counts]], index: int
    ) -> dict[str, dict[str, Any]]:
        groups: dict[str, list[Counts]] = {}
        for key, counts in (*windows, *months):
            groups.setdefault(key[index], []).append(counts)
        return {name: summarize(rows) for name, rows in sorted(groups.items())}
