"""The ``sylphd`` command: Python commands in Python, everything else in the binary.

The forwarding itself is tested end to end in ``tests/test_client_binary.py`` (a live
server); this file covers the dispatch table, the help text, the friendly message when
the binary is missing, and ``sylphd.run`` parsing a document.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sylphd import __version__, clientbin, entry
from sylphd.clientbin import find_client_bin

from conftest import require_client_binary

CLIENT = require_client_binary()


def test_python_commands_are_the_server_and_admin_ones() -> None:
    assert list(entry.PYTHON_COMMANDS) == [
        "serve",
        "setup",
        "download",
        "taxdb",
        "taxonomy",
        "taxmatrix",
        "diff",
    ]
    # The query commands are the binary's, and the two sets do not overlap.
    assert clientbin.CLIENT_COMMANDS == {"info", "profile", "query", "assembly", "render"}
    assert not set(entry.PYTHON_COMMANDS) & set(clientbin.CLIENT_COMMANDS)


def test_help_lists_python_commands_then_the_binarys(capsys) -> None:
    assert entry.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "Python commands" in out
    assert "Client commands" in out
    for command in entry.PYTHON_COMMANDS:
        assert command in out
    assert out.index("serve") < out.index("Client commands")
    assert "info" in out and "profile" in out and "render" in out
    assert out.index("Client commands") < out.index("options:")
    assert "usage: sylphd" in out
    assert "-V, --version" in out


def test_help_without_the_binary(monkeypatch, capsys) -> None:
    monkeypatch.setenv(clientbin.ENV_BIN, "/nonexistent/sylphd-client")
    assert entry.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "sylphd-client is not installed" in out
    assert "pip install 'sylphd[client]'" in out


def test_version(capsys) -> None:
    assert entry.main(["--version"]) == 0
    assert capsys.readouterr().out == f"sylphd {__version__}\n"
    assert entry.main(["-V"]) == 0
    assert capsys.readouterr().out == f"sylphd {__version__}\n"


def test_no_arguments_shows_the_help(capsys) -> None:
    assert entry.main([]) == 0
    assert "Python commands" in capsys.readouterr().out


def test_a_missing_binary_is_a_friendly_error(monkeypatch, capsys) -> None:
    monkeypatch.setenv(clientbin.ENV_BIN, "/nonexistent/sylphd-client")
    assert entry.main(["profile", "reads.fq"]) == 2
    err = capsys.readouterr().err
    assert "sylphd-client" in err
    assert "uv tool install 'sylphd[client]'" in err
    assert "Traceback" not in err


def test_python_commands_do_not_need_the_binary(monkeypatch, capsys) -> None:
    monkeypatch.setenv(clientbin.ENV_BIN, "/nonexistent/sylphd-client")
    # `diff` is pure standard library: it must not look for the binary at all.
    assert entry.main(["diff", "--help"]) == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_find_client_bin_honours_the_override(tmp_path, monkeypatch) -> None:
    assert find_client_bin()  # the dev venv / cargo build has it
    monkeypatch.setenv(clientbin.ENV_BIN, str(tmp_path / "missing"))
    assert find_client_bin() is None
    monkeypatch.setenv(clientbin.ENV_BIN, str(CLIENT))
    assert find_client_bin() == str(CLIENT)


def test_find_client_bin_falls_back_to_the_venv(monkeypatch) -> None:
    monkeypatch.delenv(clientbin.ENV_BIN, raising=False)
    found = find_client_bin()
    # Either the venv script (uv sync) or PATH; both are the same binary here.
    assert found is not None
    assert Path(found).name.startswith("sylphd-client")


def test_the_console_script_is_installed() -> None:
    """`uv sync` installs `sylphd`, the entry point of `[project.scripts]`."""
    script = Path(sys.executable).with_name("sylphd")
    if not script.is_file():
        pytest.skip("not running from an installed environment")
    done = subprocess.run([str(script), "--version"], capture_output=True, text=True)
    assert done.returncode == 0
    assert done.stdout.strip() == f"sylphd {__version__}"


# ---------------------------------------------------------------------------
# sylphd.run against a fake server: arguments, --json, parsing
# ---------------------------------------------------------------------------

DOCUMENT = {
    "api_version": 1,
    "params": {},
    "samples": [{"name": "sample"}],
    "table": {"columns": ["Sample_file", "Genome_file"], "data": [["sample", "g.fna"]]},
    "caveats": [],
}


def _info_document() -> dict:
    return {
        "api_version": 1,
        "service": "sylphd",
        "version": __version__,
        "pysylph_version": "test",
        "python_version": "3.14",
        "database": {
            "name": "fake",
            "size": 1,
            "sha256": None,
            "genomes": 1,
            "c": 200,
            "k": 31,
            "screen_c": None,
            "two_stage": False,
        },
        "databases": [],
        "metadata": None,
        "limits": {
            "max_jobs": 1,
            "max_upload_mb": 512,
            "max_sketches": 4096,
            "max_contigs": 4096,
            "max_params_bytes": 1048576,
            "allow_sequences": False,
            "max_sequence_mb": None,
            "rate_limit": None,
            "max_queue": None,
        },
        "usage": {"enabled": False},
        "endpoints": ["/info", "/messages", "/profile", "/query"],
    }


@contextmanager
def _fake_server(documents: list[str]):
    """Answers /info and /messages, then one of ``documents`` per POST."""
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def _send(self, status: int, body: str) -> None:
            payload = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            if self.path == "/info":
                self._send(200, json.dumps(_info_document()))
            elif self.path == "/messages":
                self._send(200, '{"messages": []}')
            else:
                self._send(404, '{"error": "not found"}')

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            self.rfile.read(length)
            self._send(200, documents[min(len(seen), len(documents) - 1)])
            seen.append(self.path)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=10)


def test_run_parses_the_document(tmp_path: Path) -> None:
    from sylphd import run

    reads = tmp_path / "reads.fq"
    reads.write_text("@r\nACGT\n+\nIIII\n")
    with _fake_server([json.dumps(DOCUMENT)]) as url:
        document = run("profile", reads, "--url", url)
    assert document == DOCUMENT


def test_run_passes_the_flags_through(tmp_path: Path) -> None:
    from sylphd import run

    reads = tmp_path / "reads.fq"
    reads.write_text("@r\nACGT\n+\nIIII\n")
    with _fake_server([json.dumps(DOCUMENT)]) as url:
        run("profile", reads, "--url", url, "--fields", "none")
    # (the fake server accepts anything; the point is that run() does not eat flags)


def test_run_raises_when_the_binary_is_missing(monkeypatch) -> None:
    from sylphd import run

    monkeypatch.setenv(clientbin.ENV_BIN, "/nonexistent/sylphd-client")
    with pytest.raises(FileNotFoundError, match="sylphd-client is not installed"):
        run("info")
