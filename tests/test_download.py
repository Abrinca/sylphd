"""Tests for the GTDB metadata and sylph database downloaders."""

from __future__ import annotations

import functools
import hashlib
import http.server
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path

import pytest

from sylphd import download
from sylphd.download import (
    MD5SUMS_FILE,
    DownloadError,
    aria2c_command,
    aria2c_path,
    download_file,
    download_release,
    download_urls,
    parse_md5sums,
    release_url,
)


class TestParseMd5Sums:
    def test_prefixes_and_whitespace(self) -> None:
        text = (
            "0f51fe6f01b4ccbf0d9bab6c07df87af  ./ar53_metadata_r232.tsv.gz\n"
            "\n"
            "# a comment\n"
            "1f2a6d045e0f60df13849ef22be79115  bac120_metadata_r232.tsv.gz\n"
            "  8796f5c6240b857f718e356537efdf1c\t./ar53_r232.tree\n"
        )
        checksums = parse_md5sums(text)
        assert checksums["ar53_metadata_r232.tsv.gz"] == "0f51fe6f01b4ccbf0d9bab6c07df87af"
        assert checksums["bac120_metadata_r232.tsv.gz"] == "1f2a6d045e0f60df13849ef22be79115"
        assert checksums["ar53_r232.tree"] == "8796f5c6240b857f718e356537efdf1c"
        assert "ar53_r232.tree" in checksums

    def test_release_url(self) -> None:
        assert release_url(232).endswith("/releases/release232/232.0/")


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@pytest.fixture
def http_server(tmp_path: Path):
    root = tmp_path / "www"
    directory = root / "releases" / "release232" / "232.0"
    directory.mkdir(parents=True)
    handler = functools.partial(_QuietHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield directory, f"http://127.0.0.1:{server.server_port}/releases/release232/232.0/"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _publish(directory: Path, name: str, content: bytes) -> str:
    (directory / name).write_bytes(content)
    digest = hashlib.md5(content).hexdigest()
    checksums = directory / MD5SUMS_FILE
    existing = checksums.read_text() if checksums.exists() else ""
    checksums.write_text(existing + f"{digest}  ./{name}\n")
    return digest


def test_download_release(tmp_path: Path, http_server) -> None:
    directory, base_url = http_server
    content = b"accession\tgenome_size\nGCA_1\t1\n"
    _publish(directory, "ar53_metadata_r232.tsv.gz", content)

    out = tmp_path / "out"
    paths = download_release(
        232, out, domains=["ar53"], base_url=base_url, progress=None
    )
    assert paths == [out / "ar53_metadata_r232.tsv.gz"]
    assert paths[0].read_bytes() == content
    assert not list(out.glob("*.part"))


def test_checksum_mismatch_removes_partial(tmp_path: Path, http_server) -> None:
    directory, base_url = http_server
    content = b"good content"
    _publish(directory, "ar53_metadata_r232.tsv.gz", content)
    # Corrupt the served file, leaving the checksum from the good content.
    (directory / "ar53_metadata_r232.tsv.gz").write_bytes(b"corrupt")

    out = tmp_path / "out"
    with pytest.raises(ValueError, match="checksum mismatch"):
        download_release(232, out, domains=["ar53"], base_url=base_url)
    assert not (out / "ar53_metadata_r232.tsv.gz").exists()
    assert not list(out.glob("*.part"))


def test_existing_file_is_verified(tmp_path: Path, http_server) -> None:
    directory, base_url = http_server
    content = b"already here"
    _publish(directory, "ar53_metadata_r232.tsv.gz", content)
    out = tmp_path / "out"
    out.mkdir()
    destination = out / "ar53_metadata_r232.tsv.gz"
    destination.write_bytes(content)

    # A matching file is reused without downloading again.
    download_release(232, out, domains=["ar53"], base_url=base_url)
    assert destination.read_bytes() == content

    destination.write_bytes(b"stale")
    with pytest.raises(ValueError, match="does not match the expected checksum"):
        download_release(232, out, domains=["ar53"], base_url=base_url)
    download_release(232, out, domains=["ar53"], base_url=base_url, force=True)
    assert destination.read_bytes() == content


def test_unlisted_file(tmp_path: Path, http_server) -> None:
    directory, base_url = http_server
    (directory / MD5SUMS_FILE).write_text("")
    with pytest.raises(ValueError, match="not listed"):
        download_release(232, tmp_path, domains=["bac120"], base_url=base_url)


def test_download_file_without_checksum(tmp_path: Path, http_server) -> None:
    directory, base_url = http_server
    content = b"plain"
    (directory / "x.tsv.gz").write_bytes(content)
    target = download_file(base_url + "x.tsv.gz", tmp_path / "x.tsv.gz")
    assert target.read_bytes() == content

# ---------------------------------------------------------------------------
# transfer engine
# ---------------------------------------------------------------------------


class _FileHandler(http.server.BaseHTTPRequestHandler):
    """Serve files from ``directory``, optionally honouring ``Range``."""

    protocol_version = "HTTP/1.1"

    def __init__(
        self,
        *args: object,
        directory: str,
        honor_range: bool = True,
        records: list[str | None] | None = None,
        **kwargs: object,
    ) -> None:
        self.directory = Path(directory)
        self.honor_range = honor_range
        self.records = records
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:  # noqa: N802
        name = urllib.parse.unquote(self.path.split("?", 1)[0].lstrip("/"))
        path = self.directory / name
        if not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        range_header = self.headers.get("Range")
        if self.records is not None:
            self.records.append(range_header)
        start = 0
        if range_header and self.honor_range:
            start = int(range_header.split("=", 1)[1].split("-", 1)[0])
            body = data[start:]
            self.send_response(206)
            self.send_header(
                "Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}"
            )
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def serve(tmp_path: Path):
    """Start file servers; ``serve(root, honour_range=..., records=[...])``."""
    started: list[tuple[http.server.ThreadingHTTPServer, threading.Thread]] = []

    def _start(
        root: Path,
        *,
        honor_range: bool = True,
        records: list[str | None] | None = None,
    ) -> str:
        handler = functools.partial(
            _FileHandler,
            directory=str(root),
            honor_range=honor_range,
            records=records,
        )
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}/"

    yield _start
    for server, thread in started:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def www(tmp_path: Path) -> Path:
    root = tmp_path / "www"
    root.mkdir()
    return root


def test_resume_with_range(tmp_path: Path, www: Path, serve) -> None:
    content = b"0123456789" * 1000
    (www / "db.syldb").write_bytes(content)
    records: list[str | None] = []
    base_url = serve(www, records=records)

    destination = tmp_path / "out" / "db.syldb"
    destination.parent.mkdir()
    partial = destination.with_name("db.syldb.part")
    partial.write_bytes(content[:3000])

    download_urls([base_url + "db.syldb"], destination, expected_size=len(content))

    assert destination.read_bytes() == content
    assert not partial.exists()
    assert records == ["bytes=3000-"]


def test_server_ignoring_range_restarts(tmp_path: Path, www: Path, serve) -> None:
    content = b"fresh content" * 500
    (www / "db.syldb").write_bytes(content)
    records: list[str | None] = []
    base_url = serve(www, honor_range=False, records=records)

    destination = tmp_path / "db.syldb"
    partial = destination.with_name("db.syldb.part")
    partial.write_bytes(b"garbage from another file")

    download_urls([base_url + "db.syldb"], destination, expected_size=len(content))

    assert destination.read_bytes() == content
    assert not partial.exists()
    assert records == ["bytes=25-"]  # asked, was ignored, started over


def test_mirror_fallback(tmp_path: Path, www: Path, serve) -> None:
    content = b"second mirror has it"
    first = serve(www)  # empty: 404
    (www / "db.syldb").write_bytes(content)
    second = serve(www)

    destination = tmp_path / "db.syldb"
    download_urls([first + "db.syldb", second + "db.syldb"], destination)
    assert destination.read_bytes() == content


def test_all_mirrors_fail(tmp_path: Path, www: Path, serve) -> None:
    first = serve(www)
    second = serve(www)
    with pytest.raises(DownloadError) as caught:
        download_urls(
            [first + "db.syldb", second + "db.syldb"], tmp_path / "db.syldb"
        )
    message = str(caught.value)
    assert first in message and second in message
    assert not (tmp_path / "db.syldb").exists()


def test_size_mismatch(tmp_path: Path, www: Path, serve) -> None:
    (www / "db.syldb").write_bytes(b"short")
    base_url = serve(www)
    destination = tmp_path / "db.syldb"
    with pytest.raises(DownloadError, match="expected 100 bytes, got 5"):
        download_urls([base_url + "db.syldb"], destination, expected_size=100)
    assert not destination.exists()
    assert not destination.with_name("db.syldb.part").exists()


def test_md5_mismatch_removes_file(tmp_path: Path, www: Path, serve) -> None:
    (www / "db.syldb").write_bytes(b"contents")
    base_url = serve(www)
    destination = tmp_path / "db.syldb"
    with pytest.raises(ValueError, match="checksum mismatch"):
        download_urls(
            [base_url + "db.syldb"], destination, expected_md5="0" * 32
        )
    assert not destination.exists()
    assert not destination.with_name("db.syldb.part").exists()

# ---------------------------------------------------------------------------
# aria2c
# ---------------------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode


def test_aria2c_command() -> None:
    command = aria2c_command("aria2c", ["http://a/x", "http://b/x"], Path("d/x.part"))
    assert command[:11] == [
        "aria2c", "-s", "16", "-x", "8", "-k", "5M", "-c", "--file-allocation=none",
        "-d", "d",
    ]
    assert command[11:13] == ["-o", "x.part"]
    assert command[-2:] == ["http://a/x", "http://b/x"]


def test_aria2c_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download.shutil, "which", lambda name: "/usr/bin/aria2c")
    assert aria2c_path() == "/usr/bin/aria2c"
    monkeypatch.setattr(download.shutil, "which", lambda name: None)
    assert aria2c_path() is None


def test_download_urls_with_aria2c(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"aria2c fetched this"
    destination = tmp_path / "db.syldb"
    commands: list[list[str]] = []

    def fake_run(command: list[str], check: bool = False) -> _FakeCompleted:
        commands.append(command)
        partial = tmp_path / (Path(command[command.index("-o") + 1]).name)
        partial.write_bytes(content)
        return _FakeCompleted()

    monkeypatch.setattr(download.subprocess, "run", fake_run)
    download_urls(
        ["http://a/x", "http://b/x"], destination, expected_size=len(content), use_aria2c=True
    )
    assert destination.read_bytes() == content
    assert len(commands) == 1
    assert commands[0].count("http://a/x") == 1
    assert "http://b/x" in commands[0]
    assert "-d" in commands[0]
    assert str(tmp_path) in commands[0]


def test_download_urls_without_aria2c(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download.shutil, "which", lambda name: None)
    with pytest.raises(DownloadError, match="not on PATH"):
        download_urls(["http://a/x"], tmp_path / "x", use_aria2c=True)


def test_aria2c_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        download.subprocess, "run", lambda command, check=False: _FakeCompleted(9)
    )
    with pytest.raises(DownloadError, match="aria2c failed with exit status 9"):
        download_urls(["http://a/x"], tmp_path / "x", use_aria2c=True)
