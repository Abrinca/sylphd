"""Opt-in usage statistics: what is counted, how it is stored, what is public."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import ECOLI_TWO_STAGE, MIX_SKETCH
from starlette.testclient import TestClient

from sylphd import __version__, cli
from sylphd.limits import JobTimes
from sylphd.protocol import contigs_json
from sylphd.server import ServerConfig, create_app
from sylphd.usage import BUCKET_BOUNDS, POLICY, Usage, classify, quantile

NOON = datetime(2026, 5, 17, 12, 34, 56, tzinfo=UTC)

SECRET_NAME = "patient-zero-sample"
SECRET_ADDRESS = "203.0.113.77"


class Clock:
    """A settable clock for :class:`Usage`."""

    def __init__(self, moment: datetime = NOON) -> None:
        self.moment = moment

    def __call__(self) -> float:
        return self.moment.timestamp()


def _app(tmp_path: Path, **config) -> tuple[Usage, TestClient]:
    usage = Usage(tmp_path / "usage.sqlite")
    app = create_app(ServerConfig(db_path=ECOLI_TWO_STAGE, usage=usage, **config))
    return usage, TestClient(app, client=(SECRET_ADDRESS, 4242))


def _query(client: TestClient, name: str = SECRET_NAME, **headers: str):
    return client.post(
        "/query",
        data={"params": json.dumps({"names": [name]})},
        files=[("sketch", ("x.sylsp", MIX_SKETCH.read_bytes()))],
        headers=headers,
    )


def _rows(path: Path, table: str) -> list[tuple]:
    with sqlite3.connect(path) as db:
        return db.execute(f"SELECT * FROM {table} ORDER BY period, endpoint").fetchall()


class TestClassify:
    @pytest.mark.parametrize(
        ("headers", "expected"),
        [
            ({"origin": "https://sylph.example"}, ("web", "")),
            ({"origin": "https://x", "user-agent": "sylphd/0.3.1"}, ("web", "")),
            ({"user-agent": "sylphd/0.3.1"}, ("cli", "0.3.1")),
            ({"user-agent": "sylphd/12.0.100"}, ("cli", "12.0.100")),
            ({"user-agent": "sylphd/0.3.1.dev4"}, ("cli", "")),
            ({"user-agent": "sylphd/0.3"}, ("cli", "")),
            ({"user-agent": "sylphd/0.3.1 (evil)"}, ("cli", "")),
            ({"user-agent": "sylphd/١.٢.٣"}, ("cli", "")),
            ({"user-agent": "sylphd/1.2.99999"}, ("cli", "")),
            ({"user-agent": "curl/8.0"}, ("other", "")),
            ({"user-agent": "Mozilla/5.0 sylphd/0.3.1"}, ("other", "")),
            ({}, ("other", "")),
            ({"origin": ""}, ("other", "")),
        ],
    )
    def test_kind_and_version(self, headers, expected) -> None:
        assert classify(headers) == expected

    def test_the_client_identifies_itself(self) -> None:
        from sylphd.client import SylphClient

        with SylphClient(base_url="http://localhost:1") as client:
            agent = client._client.headers["user-agent"]
        assert agent == f"sylphd/{__version__}"
        assert classify({"user-agent": agent}) == ("cli", __version__)


class TestDisabled:
    def test_nothing_is_recorded_or_served(self) -> None:
        app = create_app(ServerConfig(db_path=ECOLI_TWO_STAGE))
        assert app.state.usage is None
        with TestClient(app) as client:
            assert _query(client).status_code == 200
            assert client.get("/usage").status_code == 404
            info = client.get("/info").json()
        assert info["usage"] == {"enabled": False}
        assert "/usage" not in info["endpoints"]


class TestEnabled:
    def test_info_advertises_the_policy(self, tmp_path: Path) -> None:
        _, client = _app(tmp_path)
        info = client.get("/info").json()
        assert info["usage"] == {"enabled": True, "policy": POLICY, "window_minutes": 10}
        assert "/usage" in info["endpoints"]

    def test_only_successful_endpoint_requests_count(self, tmp_path: Path) -> None:
        usage, client = _app(tmp_path)
        with client:
            client.get("/health")
            client.get("/info")
            client.get("/usage")
            assert client.post("/query", content=b"x").status_code == 415
            assert client.post(
                "/query",
                data={"params": "{"},
                files=[("sketch", ("x.sylsp", b"x"))],
            ).status_code == 400
            assert _query(client).status_code == 200
        usage.flush()
        report = usage.report()
        assert report["totals"]["requests"] == 1
        assert list(report["by_endpoint"]) == ["/query"]

    def test_metrics_of_each_endpoint(self, tmp_path: Path) -> None:
        usage, client = _app(tmp_path)
        sketch = MIX_SKETCH.read_bytes()
        with client:
            _query(client)
            client.post(
                "/profile",
                data={"params": json.dumps({"names": ["a", "b"]})},
                files=[("sketch", ("a.sylsp", sketch)), ("sketch", ("b.sylsp", sketch))],
            )
            contigs = [
                {"name": "c1", "contig_kmers": 5000, "length": 120_000, "gc": 50.0},
                {"name": "c2", "contig_kmers": 5000, "length": 30_000, "gc": 50.0},
            ]
            assert client.post(
                "/contigs",
                data={"params": contigs_json(contigs)},
                files=[("sketch", ("c1.sylsp", sketch)), ("sketch", ("c2.sylsp", sketch))],
            ).status_code == 200
        usage.flush()
        by = usage.report()["by_endpoint"]
        assert by["/query"]["samples"] == 1
        assert by["/query"]["sketch_bytes"] == len(sketch)
        assert by["/profile"]["samples"] == 2
        assert by["/profile"]["sketch_bytes"] == 2 * len(sketch)
        assert by["/profile"]["job_seconds"]["count"] == 2
        assert by["/contigs"]["requests"] == 1
        assert by["/contigs"]["contigs"] == 2
        assert by["/contigs"]["bases"] == 150_000
        assert by["/contigs"]["job_seconds"]["count"] == 1

    def test_client_kinds(self, tmp_path: Path) -> None:
        usage, client = _app(tmp_path)
        with client:
            _query(client, origin="https://sylph.example")
            _query(client, **{"user-agent": "sylphd/0.3.1"})
            _query(client, **{"user-agent": "sylphd/0.3.1"})
            _query(client, **{"user-agent": "sylphd/9.9.9 injected"})
            _query(client, **{"user-agent": "curl/8"})
        usage.flush()
        report = usage.report()
        assert {kind: v["requests"] for kind, v in report["by_client"].items()} == {
            "cli": 3,
            "other": 1,
            "web": 1,
        }
        columns = report["windows"]["columns"]
        keys = {(row[columns.index("client")], row[columns.index("version")]) for row in report["windows"]["rows"]}
        assert keys == {("web", ""), ("cli", "0.3.1"), ("cli", ""), ("other", "")}

    def test_usage_is_public_cached_and_not_counted(self, tmp_path: Path) -> None:
        usage, client = _app(tmp_path, cors_origins=("https://sylph.example",))
        with client:
            response = client.get("/usage", headers={"origin": "https://sylph.example"})
            assert response.status_code == 200
            assert response.headers["access-control-allow-origin"] == "https://sylph.example"
            assert response.headers["content-type"] == "application/json"
            assert client.get("/usage").content == response.content  # cached
        usage.flush()
        assert usage.report()["totals"]["requests"] == 0

    def test_usage_is_rate_limited(self, tmp_path: Path) -> None:
        _, client = _app(tmp_path, rate_limit=(1, 60.0))
        assert client.get("/usage").status_code == 200
        assert client.get("/usage").status_code == 429
        assert client.get("/info").status_code == 200

    def test_flushed_on_shutdown(self, tmp_path: Path) -> None:
        usage, client = _app(tmp_path)
        with client:
            _query(client)
            assert _rows(usage.path, "windows") == []  # requests never write
        assert len(_rows(usage.path, "windows")) == 1

    def test_access_log_has_no_client_address(self, tmp_path: Path, caplog) -> None:
        _, client = _app(tmp_path)
        with caplog.at_level("INFO", logger="uvicorn.error"), client:
            _query(client, **{"x-forwarded-for": "198.51.100.5"})
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert "POST /query -> 200" in logged
        assert SECRET_ADDRESS not in logged
        assert "198.51.100.5" not in logged


class TestPrivacy:
    def test_nothing_identifying_is_stored_or_served(self, tmp_path: Path) -> None:
        usage, client = _app(tmp_path)
        with client:
            _query(client, **{"x-forwarded-for": "198.51.100.5", "user-agent": "sylphd/0.3.1"})
            body = client.get("/usage").text
        stored = "\n".join(sqlite3.connect(usage.path).iterdump())
        for text in (body, stored):
            for secret in (SECRET_NAME, SECRET_ADDRESS, "198.51.100.5", "testclient"):
                assert secret not in text
        keys = set(json.loads(body))
        assert keys == {
            "policy", "window_minutes", "generated_at", "since", "bucket_bounds_seconds",
            "totals", "by_endpoint", "by_client", "windows", "months",
        }

    def test_the_tables_hold_only_counters(self, tmp_path: Path) -> None:
        usage = Usage(tmp_path / "u.sqlite")
        with sqlite3.connect(usage.path) as db:
            for table in ("windows", "months"):
                columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
                assert columns == [
                    "period", "endpoint", "client", "version", "requests", "contigs",
                    "samples", "bases", "sketch_bytes", "peak_queue_depth",
                    "job_hist", "wait_hist",
                ]


class TestWindows:
    def test_ten_minute_windows(self, tmp_path: Path) -> None:
        clock = Clock()
        usage = Usage(tmp_path / "u.sqlite", clock=clock)
        headers = {"user-agent": "sylphd/0.3.1"}
        usage.record("/query", headers, samples=1)  # 12:34 -> 12:30
        clock.moment = NOON.replace(minute=39, second=59)
        usage.record("/query", headers, samples=1)  # still 12:30
        clock.moment = NOON.replace(minute=40, second=0)
        usage.record("/query", headers, samples=1)  # 12:40
        usage.flush()
        rows = _rows(usage.path, "windows")
        assert [(row[0], row[4], row[6]) for row in rows] == [
            ("2026-05-17T12:30Z", 2, 2),
            ("2026-05-17T12:40Z", 1, 1),
        ]

    def test_idle_windows_write_nothing(self, tmp_path: Path) -> None:
        usage = Usage(tmp_path / "u.sqlite", clock=Clock())
        usage.flush()
        usage.flush()
        assert _rows(usage.path, "windows") == []
        assert usage.report()["since"] is None

    def test_flush_adds_to_the_stored_row(self, tmp_path: Path) -> None:
        usage = Usage(tmp_path / "u.sqlite", clock=Clock())
        usage.record("/query", {}, samples=2, jobs=[JobTimes(backlog=3, ran=0.2)])
        usage.flush()
        usage.record("/query", {}, samples=1, jobs=[JobTimes(backlog=1, ran=0.2)])
        usage.flush()
        (row,) = _rows(usage.path, "windows")
        assert (row[4], row[6], row[9]) == (2, 3, 3)  # requests, samples, peak depth
        assert sum(json.loads(row[10])) == 2

    def test_restart_keeps_and_extends_the_numbers(self, tmp_path: Path) -> None:
        path = tmp_path / "u.sqlite"
        first = Usage(path, clock=Clock())
        first.record("/assembly", {}, bases=10)
        first.close()
        second = Usage(path, clock=Clock())
        assert second.report()["totals"]["bases"] == 10
        second.record("/assembly", {}, bases=5)
        second.close()
        assert Usage(path, clock=Clock()).report()["totals"]["bases"] == 15

    def test_background_thread_flushes(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("sylphd.usage.FLUSH_SECONDS", 0.05)
        usage = Usage(tmp_path / "u.sqlite", clock=Clock())
        usage.start()
        usage.record("/query", {}, samples=1)
        for _ in range(100):
            if _rows(usage.path, "windows"):
                break
            usage._stop.wait(0.05)
        usage.close()
        assert len(_rows(usage.path, "windows")) == 1

    def test_failed_write_is_kept_for_the_next_flush(self, tmp_path: Path, monkeypatch) -> None:
        usage = Usage(tmp_path / "u.sqlite", clock=Clock())
        usage.record("/query", {}, samples=1)
        monkeypatch.setattr(usage, "_connect", lambda: sqlite3.connect("/nonexistent/dir/x"))
        usage.flush()
        monkeypatch.undo()
        usage.flush()
        assert usage.report()["totals"]["samples"] == 1

    def test_a_bad_path_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="cannot use usage database"):
            Usage(tmp_path / "missing" / "u.sqlite")

    def test_serve_refuses_a_bad_path(self, tmp_path: Path, capsys) -> None:
        code = cli.main(
            ["serve", "--db", str(ECOLI_TWO_STAGE), "--tcp", "127.0.0.1:0",
             "--usage-db", str(tmp_path / "missing" / "u.sqlite")]
        )
        assert code == 2
        assert "cannot use usage database" in capsys.readouterr().err


class TestHistograms:
    def test_bucketing_and_quantiles(self, tmp_path: Path) -> None:
        usage = Usage(tmp_path / "u.sqlite", clock=Clock())
        jobs = [JobTimes(ran=0.2, waited=0.0)] * 9 + [JobTimes(ran=40.0, waited=7.0)]
        usage.record("/profile", {}, samples=10, jobs=jobs)
        usage.flush()
        totals = usage.report()["totals"]
        assert totals["job_seconds"] == {"count": 10, "median": 0.25, "p95": 60.0}
        assert totals["queue_wait_seconds"] == {"count": 10, "median": 0.01, "p95": 10.0}

    def test_quantile_edges(self) -> None:
        assert quantile([0] * (len(BUCKET_BOUNDS) + 1), 0.5) is None
        overflow = [0] * (len(BUCKET_BOUNDS) + 1)
        overflow[-1] = 1
        assert quantile(overflow, 0.95) == BUCKET_BOUNDS[-1]


class TestMonthlyRollup:
    def _fill(self, usage: Usage, clock: Clock) -> None:
        for days, jobs in ((100, [JobTimes(ran=0.2, backlog=2)]), (99, [JobTimes(ran=9.0)])):
            clock.moment = NOON - timedelta(days=days)
            usage.record("/query", {"user-agent": "sylphd/0.3.1"}, samples=1, bases=7, jobs=jobs)
            usage.record("/assembly", {}, bases=3, sketch_bytes=11, jobs=jobs)
        clock.moment = NOON - timedelta(days=1)
        usage.record("/query", {}, samples=1, jobs=[JobTimes(ran=0.2)])

    def test_old_windows_merge_into_months_exactly(self, tmp_path: Path) -> None:
        clock = Clock()
        usage = Usage(tmp_path / "u.sqlite", clock=clock)
        self._fill(usage, clock)
        clock.moment = NOON
        usage.flush()  # the daily roll-up happens at the first flush of the day
        report = usage.report()
        assert [(m["month"], m["endpoint"]) for m in report["months"]] == [
            ("2026-02", "/assembly"),
            ("2026-02", "/query"),
        ]
        assert [row[0] for row in report["windows"]["rows"]] == ["2026-05-16T12:30Z"]
        assert report["since"] == "2026-02-01T00:00Z"

    def test_totals_and_histograms_are_unchanged_by_the_roll_up(self, tmp_path: Path) -> None:
        clock = Clock()
        unrolled = Usage(tmp_path / "a.sqlite", clock=clock)
        rolled = Usage(tmp_path / "b.sqlite", clock=clock)
        self._fill(unrolled, clock)
        clock.moment = NOON - timedelta(days=100)  # the roll-up is due only once a day
        unrolled.flush()  # nothing is older than the cutoff yet
        assert unrolled.report()["months"] == []
        self._fill(rolled, clock)
        clock.moment = NOON
        rolled.flush()
        assert rolled.report()["months"]
        for key in ("totals", "by_endpoint", "by_client"):
            assert rolled.report()[key] == unrolled.report()[key]

    def test_monthly_rows_keep_the_histograms_and_the_peak(self, tmp_path: Path) -> None:
        clock = Clock()
        usage = Usage(tmp_path / "u.sqlite", clock=clock)
        self._fill(usage, clock)
        clock.moment = NOON
        usage.flush()
        months = {(m["month"], m["endpoint"], m["client"]): m for m in usage.report()["months"]}
        query = months[("2026-02", "/query", "cli")]  # NOON - 100 days is in February
        assert query["version"] == "0.3.1"
        assert query["requests"] == 2
        assert query["bases"] == 14
        assert query["peak_queue_depth"] == 2
        assert sum(query["job_histogram"]) == 2
        assert query["job_histogram"][BUCKET_BOUNDS.index(0.25)] == 1
        assert query["job_histogram"][BUCKET_BOUNDS.index(10.0)] == 1

    def test_later_roll_ups_add_to_the_month(self, tmp_path: Path) -> None:
        clock = Clock()
        usage = Usage(tmp_path / "u.sqlite", clock=clock)
        clock.moment = NOON - timedelta(days=100)
        usage.record("/query", {}, samples=1, jobs=[JobTimes(ran=0.2)])
        clock.moment = NOON
        usage.flush()
        clock.moment = NOON - timedelta(days=100) + timedelta(hours=3)
        usage.record("/query", {}, samples=1, jobs=[JobTimes(ran=0.2)])
        clock.moment = NOON + timedelta(days=1)  # next day: the roll-up runs again
        usage.flush()
        (month,) = usage.report()["months"]
        assert month["requests"] == 2
        assert sum(month["job_histogram"]) == 2
        assert usage.report()["windows"]["rows"] == []
        assert usage.report()["since"] == f"{month['month']}-01T00:00Z"


class TestCommandLine:
    class _Client:
        def __init__(self, base_url: str | None) -> None:
            self.base_url = base_url

    ON = {"usage": {"enabled": True, "policy": 1, "window_minutes": 10}}

    def test_note_names_the_usage_url(self, capsys) -> None:
        cli._note_usage(self._Client("https://sylph.example/api"), self.ON)
        assert capsys.readouterr().err == (
            "sylphd: note: this server keeps usage statistics, "
            "see https://sylph.example/api/usage\n"
        )

    def test_note_on_a_socket(self, capsys) -> None:
        cli._note_usage(self._Client(None), self.ON)
        assert "see GET /usage" in capsys.readouterr().err

    def test_no_note_when_off_or_quiet(self, capsys) -> None:
        cli._note_usage(self._Client("http://x"), {"usage": {"enabled": False}})
        cli._note_usage(self._Client("http://x"), self.ON, quiet=True)
        assert capsys.readouterr().err == ""

    def test_info_shows_the_state(self, capsys) -> None:
        cli._print_usage({"enabled": False})
        cli._print_usage(self.ON["usage"])
        assert capsys.readouterr().out.splitlines() == [
            "usage statistics: off",
            "usage statistics: on (policy 1, 10-minute windows, public at /usage)",
        ]
