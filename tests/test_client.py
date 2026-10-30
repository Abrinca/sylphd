"""Client base URLs: parsing, CLI precedence, path prefixes and an HTTP round trip."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from sylphd import cli
from sylphd.client import ClientError, SylphClient, connect, normalize_base_url

from conftest import ECOLI_TWO_STAGE


@pytest.fixture()
def recording_server():
    """A tiny HTTP server recording request paths; returns ``(port, paths)``."""
    paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            paths.append(self.path)
            body = json.dumps({"path": self.path}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield server.server_address[1], paths
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def http_server(tmp_path_factory):
    """A real ``sylphd serve --tcp`` process, addressed with an ``http://`` URL."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "sylphd",
            "serve",
            "--db",
            str(ECOLI_TWO_STAGE),
            "--tcp",
            f"127.0.0.1:{port}",
            "--log-level",
            "warning",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 120
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError("server exited before accepting connections")
            try:
                if httpx.get(f"{base}/health", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            raise AssertionError("server did not answer in time")
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


class TestNormalizeBaseUrl:
    def test_http_and_https(self) -> None:
        assert normalize_base_url("http://127.0.0.1:8765") == "http://127.0.0.1:8765"
        assert (
            normalize_base_url("https://api.example.org/sylphd")
            == "https://api.example.org/sylphd"
        )

    def test_path_prefix_keeps_trailing_slash_off(self) -> None:
        assert (
            normalize_base_url("https://api.example.org/sylphd/")
            == "https://api.example.org/sylphd"
        )
        assert normalize_base_url("http://example.org/") == "http://example.org"

    def test_bare_host_and_port_gets_http(self) -> None:
        assert normalize_base_url("127.0.0.1:8765") == "http://127.0.0.1:8765"
        assert (
            normalize_base_url("example.org/sylphd") == "http://example.org/sylphd"
        )

    @pytest.mark.parametrize(
        "url",
        ["", "ftp://example.org", "http://", "https://example.org/?x=1", "http://h/#x"],
    )
    def test_invalid_urls(self, url: str) -> None:
        with pytest.raises(ValueError):
            normalize_base_url(url)


class TestPathPrefix:
    def test_prefix_is_kept_for_requests(self, recording_server) -> None:
        port, paths = recording_server
        with connect(url=f"http://127.0.0.1:{port}/sylphd") as client:
            assert client.info() == {"path": "/sylphd/info"}
        assert paths == ["/sylphd/info"]


class TestCliAddressPrecedence:
    @staticmethod
    def _client(*argv: str) -> SylphClient:
        args = cli.build_parser().parse_args(["info", *argv])
        return cli._client_from_args(args)

    def test_env_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SYLPHD_URL", "http://env.example:1234/prefix")
        with self._client() as client:
            assert client.base_url == "http://env.example:1234/prefix"

    def test_url_flag_beats_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SYLPHD_URL", "http://env.example:1234")
        with self._client("--url", "https://flag.example/sylphd") as client:
            assert client.base_url == "https://flag.example/sylphd"
            assert client.socket_path is None

    def test_socket_flag_beats_env_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SYLPHD_URL", "http://env.example:1234")
        with self._client("--socket", "/tmp/x.sock") as client:
            assert client.socket_path is not None
            assert client.base_url is None

    @pytest.mark.parametrize(
        "flags",
        [
            ("--url", "http://example.org", "--socket", "/tmp/x.sock"),
            ("--url", "http://example.org", "--tcp", "127.0.0.1:8765"),
        ],
    )
    def test_address_flags_are_mutually_exclusive(self, flags: tuple[str, ...]) -> None:
        with pytest.raises(SystemExit) as caught:
            cli.build_parser().parse_args(["info", *flags])
        assert caught.value.code == 2


class TestHttpRoundTrip:
    def test_cli_info_over_http(self, http_server: str) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "sylphd", "info", "--url", http_server, "--json"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        info = json.loads(result.stdout)
        assert info["database"]["c"] == 200
        assert info["database"]["k"] == 31

    def test_cli_url_must_be_reachable(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "sylphd",
                "info",
                "--url",
                "http://127.0.0.1:9",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 1
        assert "cannot talk to sylphd at http://127.0.0.1:9" in result.stderr


class TestRetries:
    @staticmethod
    def _client(replies: list[httpx.Response], retries: int, sleeps: list[float], monkeypatch):
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return replies[min(len(calls), len(replies)) - 1]

        monkeypatch.setattr("sylphd.client.time.sleep", sleeps.append)
        client = SylphClient(base_url="http://example.test", retries=retries)
        client._client = httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://example.test/"
        )
        return client, calls

    def test_retries_503_after_retry_after_then_succeeds(self, monkeypatch) -> None:
        busy = httpx.Response(503, json={"error": "busy"}, headers={"Retry-After": "3"})
        sleeps: list[float] = []
        client, calls = self._client(
            [busy, busy, httpx.Response(200, json=[])], 2, sleeps, monkeypatch
        )
        assert client.profile([("a", b"sketch")]) == []
        assert len(calls) == 3
        assert sleeps == [3.0, 3.0]
        # the retried request carries the same multipart body
        assert calls[0].content == calls[2].content

    def test_gives_up_after_the_retries(self, monkeypatch) -> None:
        limited = httpx.Response(429, json={"error": "slow down"})
        sleeps: list[float] = []
        client, calls = self._client([limited], 2, sleeps, monkeypatch)
        with pytest.raises(ClientError, match="slow down") as raised:
            client.info()
        assert raised.value.status_code == 429
        assert len(calls) == 3
        assert sleeps == [1.0, 2.0]  # exponential without a Retry-After

    def test_delay_is_capped(self, monkeypatch) -> None:
        huge = httpx.Response(503, json={"error": "busy"}, headers={"Retry-After": "9999"})
        sleeps: list[float] = []
        client, _ = self._client([huge, httpx.Response(200, json={})], 1, sleeps, monkeypatch)
        client.info()
        assert sleeps == [30.0]

    def test_default_does_not_retry(self, monkeypatch) -> None:
        sleeps: list[float] = []
        client, calls = self._client(
            [httpx.Response(503, json={"error": "busy"})], 0, sleeps, monkeypatch
        )
        with pytest.raises(ClientError):
            client.info()
        assert len(calls) == 1 and sleeps == []

    def test_other_errors_are_not_retried(self, monkeypatch) -> None:
        sleeps: list[float] = []
        client, calls = self._client(
            [httpx.Response(400, json={"error": "bad"})], 3, sleeps, monkeypatch
        )
        with pytest.raises(ClientError):
            client.info()
        assert len(calls) == 1 and sleeps == []

    def test_cli_flag(self) -> None:
        args = cli.build_parser().parse_args(["info", "--retries", "4"])
        with cli._client_from_args(args) as client:
            assert client.retries == 4
        args = cli.build_parser().parse_args(["info"])
        with cli._client_from_args(args) as client:
            assert client.retries == 0
