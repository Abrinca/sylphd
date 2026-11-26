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
from conftest import require_client_binary

from sylphd import __version__, clientbin, entry
from sylphd.clientbin import find_client_bin

CLIENT = require_client_binary()


def test_python_commands_are_the_server_and_admin_ones() -> None:
    assert list(entry.PYTHON_COMMANDS) == [
        "serve",
        "setup",
        "download",
        "taxmatrix",
        "diff",
    ]
    # The query commands are the binary's, and the two sets do not overlap.
    assert not set(entry.PYTHON_COMMANDS) & (clientbin.JSON_COMMANDS | {"render"})


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
    monkeypatch.setattr(entry, "find_client_bin", lambda: None)
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
    monkeypatch.setattr(entry, "find_client_bin", lambda: None)
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
    with pytest.raises(FileNotFoundError, match=str(tmp_path / "missing")):
        find_client_bin()
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
    done = subprocess.run([str(script), "--version"], check=False, capture_output=True, text=True)
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


def test_run_raises_when_the_binary_is_missing(monkeypatch) -> None:
    from sylphd import run

    monkeypatch.setattr(clientbin, "find_client_bin", lambda: None)
    with pytest.raises(FileNotFoundError, match="sylphd-client is not installed"):
        run("info")


def test_a_bad_override_names_the_path(monkeypatch, capsys) -> None:
    monkeypatch.setenv(clientbin.ENV_BIN, "/nonexistent/sylphd-client")
    assert entry.main(["info"]) == 2
    assert "/nonexistent/sylphd-client" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# sylphd.run and the entry point against a fake binary
# ---------------------------------------------------------------------------

posix_only = pytest.mark.skipif(os.name == "nt", reason="the fake binary is a #! script")


def _fake_binary(tmp_path: Path, monkeypatch, *, stdout: str, stderr: str = "", code: int = 0) -> Path:
    """A ``sylphd-client`` that records its argv in ``argv.json`` and prints what it is told."""
    record = tmp_path / "argv.json"
    script = tmp_path / "fake-client"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"json.dump(sys.argv, open({str(record)!r}, 'w'))\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({code})\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv(clientbin.ENV_BIN, str(script))
    return record


@posix_only
def test_run_adds_json_and_passes_the_arguments(tmp_path: Path, monkeypatch) -> None:
    from sylphd import run

    record = _fake_binary(tmp_path, monkeypatch, stdout=json.dumps(DOCUMENT))
    assert run("profile", "a.fq", "--fields", "none") == DOCUMENT
    argv = json.loads(record.read_text())
    assert argv[1:] == ["profile", "--json", "a.fq", "--fields", "none"]
    # `--json` given by the caller is not repeated
    run("query", "--json", "a.fq")
    assert json.loads(record.read_text())[1:] == ["query", "--json", "a.fq"]


@posix_only
def test_run_raises_on_a_failing_exit(tmp_path: Path, monkeypatch) -> None:
    from sylphd import ClientRunError, run

    _fake_binary(tmp_path, monkeypatch, stdout="", stderr="sylphd: error: no such file\n", code=2)
    with pytest.raises(ClientRunError) as caught:
        run("profile", "missing.fq")
    assert caught.value.returncode == 2
    assert caught.value.stderr == "sylphd: error: no such file\n"
    assert "no such file" in str(caught.value)


@posix_only
def test_run_raises_on_output_that_is_not_json(tmp_path: Path, monkeypatch) -> None:
    from sylphd import ClientRunError, run

    _fake_binary(tmp_path, monkeypatch, stdout="not json")
    with pytest.raises(ClientRunError, match="no JSON document"):
        run("info")


@pytest.mark.parametrize(
    "args",
    [("render", "doc.json"), ("profile", "a.fq", "-o", "out.tsv"), ("info", "--output=x"), ()],
)
def test_run_refuses_what_cannot_work(args) -> None:
    from sylphd import run

    with pytest.raises(ValueError):
        run(*args)


def test_exec_client_on_windows_runs_a_child(monkeypatch) -> None:
    calls: list[tuple[list[str], str | None]] = []

    def fake_call(argv, executable=None):
        calls.append((argv, executable))
        return 7

    monkeypatch.setattr(clientbin.os, "name", "nt")
    monkeypatch.setattr(clientbin.subprocess, "call", fake_call)
    assert clientbin.exec_client("C:/bin/sylphd-client.exe", ["profile", "a.fq"]) == 7
    assert calls == [(["sylphd", "profile", "a.fq"], "C:/bin/sylphd-client.exe")]


def test_exec_client_on_unix_replaces_the_process(monkeypatch) -> None:
    seen: list[tuple[str, list[str]]] = []

    def fake_execv(path, argv):
        seen.append((path, argv))
        raise SystemExit(0)

    monkeypatch.setattr(clientbin.os, "name", "posix")
    monkeypatch.setattr(clientbin.os, "execv", fake_execv)
    with pytest.raises(SystemExit):
        clientbin.exec_client("/usr/bin/sylphd-client", ["info"])
    assert seen == [("/usr/bin/sylphd-client", ["sylphd", "info"])]


def test_serve_without_the_server_packages_is_a_friendly_error(monkeypatch, capsys) -> None:
    import builtins

    real_import = builtins.__import__

    def refusing(name, *args, **kwargs):
        if name == "cli" and args[2:] == (("main",), 1):  # `from .cli import main`
            raise ImportError("No module named 'starlette'", name="starlette")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refusing)
    assert entry.main(["serve", "--db", "x.syldb"]) == 2
    err = capsys.readouterr().err
    assert "needs a package that is not installed (starlette)" in err
    assert "sylphd[server]" in err
    assert "Traceback" not in err
