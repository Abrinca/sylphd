"""End-to-end tests of the native ``sylphd-client`` binary.

The binary is located from ``$SYLPHD_CLIENT_BIN`` or
``rust/target/{release,debug}/sylphd-client``; the module is skipped when it has
not been built. The Rust unit tests cover the address table, retry parsing and
CLI parsing; these tests run the real binary against a live server on a
temporary Unix socket (and a fake HTTP server for the api_version and JSON
passthrough cases).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pysylph
import pytest

from conftest import CLODF13_FASTA, K12_R1

REPO = Path(__file__).resolve().parent.parent


def _find_binary() -> Path | None:
    override = os.environ.get("SYLPHD_CLIENT_BIN")
    if override:
        path = Path(override)
        return path if path.is_file() else None
    for profile in ("release", "debug"):
        path = REPO / "rust" / "target" / profile / "sylphd-client"
        if path.is_file():
            return path
    return None


BIN = _find_binary()

pytestmark = pytest.mark.skipif(
    BIN is None,
    reason="sylphd-client is not built (cargo build -p sylphd-client)",
)


def _run(*args: str | Path, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(BIN), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run_python(*args: str | Path, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "sylphd", *map(str, args)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _wait_for_socket(path: Path, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_socket():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.2)
            try:
                probe.connect(str(path))
            except (FileNotFoundError, ConnectionRefusedError):
                pass
            else:
                return
            finally:
                probe.close()
        time.sleep(0.1)
    raise AssertionError(f"server did not create {path} in time")


@pytest.fixture(scope="module")
def server(tmp_path_factory, tiny_database: Path, tiny_metadata: Path):
    directory = tmp_path_factory.mktemp("client-binary-server")
    socket_path = directory / "sylphd.sock"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "sylphd",
            "serve",
            "--db",
            str(tiny_database),
            "--meta",
            str(tiny_metadata),
            "--socket",
            str(socket_path),
            "--log-level",
            "warning",
            "--allow-sequences",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_socket(socket_path)
        yield socket_path
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_info_text_matches_python(server: Path) -> None:
    rust = _run("info", "--socket", server)
    python = _run_python("info", "--socket", server)
    assert rust.returncode == python.returncode == 0
    assert rust.stdout == python.stdout
    assert rust.stderr == python.stderr == ""

    rust_json = _run("info", "--json", "--socket", server)
    python_json = _run_python("info", "--json", "--socket", server)
    assert rust_json.returncode == python_json.returncode == 0
    assert json.loads(rust_json.stdout) == json.loads(python_json.stdout)
    assert json.loads(rust_json.stdout)["messages"] == []


def test_profile_tsv_is_byte_identical_to_the_python_cli(server: Path) -> None:
    rust = _run("profile", "--socket", server, CLODF13_FASTA)
    python = _run_python("profile", "--socket", server, CLODF13_FASTA)
    assert rust.returncode == python.returncode == 0, rust.stderr
    assert rust.stdout == python.stdout
    assert rust.stderr == python.stderr == ""


def test_query_tsv_and_sylsp_input(server: Path, tmp_path: Path) -> None:
    # A pre-made .sylsp sketch made from the tiny database's only genome.
    import dnaio

    with dnaio.open(CLODF13_FASTA) as reader:
        sequences = [record.sequence for record in reader]
    sketch = pysylph.Sketcher(c=200, k=31).sketch_single("clodf13", sequences)
    sketch_path = tmp_path / "clodf13.sylsp"
    sketch_path.write_bytes(sketch.to_bytes())

    rust = _run("query", "--socket", server, sketch_path)
    python = _run_python("query", "--socket", server, sketch_path)
    assert rust.returncode == python.returncode == 0, rust.stderr
    assert rust.stdout == python.stdout
    assert "GCA_000000001.1" in rust.stdout


def test_json_is_a_document_and_renders_identically(server: Path, tmp_path: Path) -> None:
    output = tmp_path / "profile.json"
    rust = _run("profile", "--socket", server, CLODF13_FASTA, "--json", "-o", output)
    tsv = _run("profile", "--socket", server, CLODF13_FASTA)
    assert rust.returncode == 0, rust.stderr
    document = json.loads(output.read_text())
    assert document["api_version"] == 1
    assert isinstance(document["table"]["data"], list)
    assert [sample["name"] for sample in document["samples"]] == ["clodf13"]
    rendered = _run("render", output)
    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stdout == tsv.stdout


def test_taxprof_files_match_the_python_cli(server: Path, tmp_path: Path) -> None:
    rust_dir = tmp_path / "rust"
    python_dir = tmp_path / "python"
    rust_dir.mkdir()
    python_dir.mkdir()
    rust = _run(
        "profile",
        "--socket",
        server,
        CLODF13_FASTA,
        "--taxonomy",
        "gtdb",
        "--taxprof",
        str(rust_dir) + "/",
    )
    python = _run_python(
        "profile",
        "--socket",
        server,
        CLODF13_FASTA,
        "--taxonomy",
        "gtdb",
        "--taxprof",
        str(python_dir) + "/",
    )
    assert rust.returncode == python.returncode == 0, rust.stderr
    assert rust.stdout == python.stdout
    rust_files = sorted(rust_dir.glob("*.sylphmpa"))
    python_files = sorted(python_dir.glob("*.sylphmpa"))
    assert [path.name for path in rust_files] == ["clodf13.gtdb.sylphmpa"]
    assert [path.name for path in python_files] == ["clodf13.gtdb.sylphmpa"]
    assert rust_files[0].read_bytes() == python_files[0].read_bytes()
    assert f"wrote {rust_dir}/clodf13.gtdb.sylphmpa" in rust.stderr
    assert f"wrote {python_dir}/clodf13.gtdb.sylphmpa" in python.stderr


def test_assembly_tsv_and_summary_match_python(server: Path, tmp_path: Path) -> None:
    rust = _run("assembly", "--socket", server, CLODF13_FASTA)
    python = _run_python("assembly", "--socket", server, CLODF13_FASTA)
    assert rust.returncode == python.returncode == 0, rust.stderr
    assert rust.stdout == python.stdout
    assert rust.stderr == python.stderr
    assert "assigned to species Test" in rust.stderr

    # --no-local-sketch uploads the raw (gzip) sequence; the hits and summary
    # are the same as for the locally made sketch.
    raw = _run("assembly", "--socket", server, CLODF13_FASTA, "--no-local-sketch")
    assert raw.returncode == 0, raw.stderr
    assert raw.stdout == rust.stdout
    assert raw.stderr == rust.stderr

    output = tmp_path / "assembly.json"
    saved = _run("assembly", "--socket", server, CLODF13_FASTA, "--json", "-o", output)
    assert saved.returncode == 0, saved.stderr
    assert saved.stderr == ""
    rendered = _run("render", output)
    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stdout == rust.stdout
    assert rendered.stderr == rust.stderr


def test_per_contig_tsv_and_summary_match_python(server: Path, tmp_path: Path) -> None:
    # --min-contig-length 1 so the single 9957 bp record is actually classified.
    args = [
        "assembly",
        "--socket",
        server,
        CLODF13_FASTA,
        "--per-contig",
        "--min-contig-length",
        "1",
        "--fields",
        "none",
    ]
    rust = _run(*args)
    python = _run_python(*args)
    assert rust.returncode == python.returncode == 0, rust.stderr
    assert rust.stdout == python.stdout
    assert rust.stderr == python.stderr
    assert "per-contig: 1 of 1 contig(s)" in rust.stderr

    output = tmp_path / "contigs.json"
    saved = _run(
        "assembly",
        "--socket",
        server,
        CLODF13_FASTA,
        "--per-contig",
        "--min-contig-length",
        "1",
        "--fields",
        "none",
        "--json",
        "-o",
        output,
    )
    assert saved.returncode == 0, saved.stderr
    document = json.loads(output.read_text())
    assert document["api_version"] == 1
    assert document["summary"]["queried_contigs"] == 1
    assert document["summary"]["min_contig_length"] == 1
    assert list(document["contigs"]) == ["X04466.1"]
    rendered = _run("render", output)
    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stdout == rust.stdout
    assert rendered.stderr == rust.stderr


def test_exit_codes(server: Path, tmp_path: Path) -> None:
    # 2: no input samples (usage/input error).
    missing = _run("profile", "--socket", server)
    assert missing.returncode == 2
    assert "sylphd: error: no input samples given" in missing.stderr

    # 2: --per-contig always sketches locally.
    per_contig = _run(
        "assembly", "--socket", server, CLODF13_FASTA, "--per-contig", "--no-local-sketch"
    )
    assert per_contig.returncode == 2
    assert "--no-local-sketch cannot be used" in per_contig.stderr

    # 1: no server at that socket.
    unreachable = _run("info", "--socket", tmp_path / "missing.sock")
    assert unreachable.returncode == 1
    assert "cannot talk to sylphd on" in unreachable.stderr


# ---------------------------------------------------------------------------
# fake HTTP server: api_version check and --json passthrough
# ---------------------------------------------------------------------------

GOLDEN_PROFILE = json.loads(
    (REPO / "rust" / "sylphd-core" / "tests" / "data" / "profile_golden.json").read_text()
)
PASSTHROUGH_DOCUMENT = json.dumps(GOLDEN_PROFILE["cases"][0]["document"])


def _info_document(api_version: int = 1) -> dict:
    return {
        "api_version": api_version,
        "service": "sylphd",
        "version": "0.7.0",
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
def _fake_server(info_document: dict, response_document: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # keep pytest output clean
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
                self._send(200, json.dumps(info_document))
            elif self.path == "/messages":
                self._send(200, '{"messages": []}')
            else:
                self._send(404, '{"error": "not found"}')

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            self.rfile.read(length)
            self._send(200, response_document)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=10)


def test_json_passthrough_is_the_server_document() -> None:
    with _fake_server(_info_document(), PASSTHROUGH_DOCUMENT) as url:
        result = _run("profile", "--url", url, K12_R1, "--json")
    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip("\n") == PASSTHROUGH_DOCUMENT


def test_api_version_mismatch_fails_before_sketching() -> None:
    with _fake_server(_info_document(api_version=2), PASSTHROUGH_DOCUMENT) as url:
        result = _run("profile", "--url", url, K12_R1)
    assert result.returncode == 1
    assert "server API version 2, this client supports 1" in result.stderr
    assert "update sylphd" in result.stderr
    # Nothing was sketched, so the profile response was never requested:
    assert result.stdout == ""


def test_render_golden_documents(tmp_path: Path) -> None:
    golden = json.loads(
        (REPO / "rust" / "sylphd-core" / "tests" / "data" / "assembly_golden.json").read_text()
    )
    case = golden["cases"][0]
    path = tmp_path / "assembly.json"
    path.write_text(json.dumps(case["document"]))
    result = _run("render", path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == case["expected_tsv"]
    assert result.stderr == case["expected_stderr"]

    # A document from another API version is refused.
    document = dict(case["document"])
    document["api_version"] = 99
    path.write_text(json.dumps(document))
    mismatch = _run("render", path)
    assert mismatch.returncode == 1
    assert "server API version 99" in mismatch.stderr
