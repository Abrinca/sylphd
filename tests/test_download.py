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
    DATABASES,
    MD5SUMS_FILE,
    Database,
    DownloadError,
    aria2c_command,
    aria2c_path,
    database_table,
    download_database,
    download_file,
    download_release,
    download_urls,
    lookup_database,
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
    with pytest.raises(DownloadError, match="checksum mismatch"):
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
# database catalogue
# ---------------------------------------------------------------------------


class TestCatalogue:
    def test_gtdb_r232_c200_dbv2(self) -> None:
        entry = lookup_database("gtdb-r232-c200-dbv2.syl2db")
        assert entry.filename == "gtdb-r232-c200-dbv2.syl2db"
        assert entry.size == 20044737329
        # Computed from a local copy of the file; matches the cloud mirror's
        # published object checksum.
        assert entry.md5 == "89ad2dbdd532be81c72e78f5d493d78d"
        assert entry.mirrors == (
            "http://faust.compbio.cs.cmu.edu/sylph-stuff/gtdb-r232-c200-dbv2.syl2db",
            "https://storage.googleapis.com/sylph-stuff/gtdb-r232-c200-dbv2.syl2db",
        )

    def test_entries_are_consistent(self) -> None:
        assert DATABASES
        for name, entry in DATABASES.items():
            assert entry.name == name
            assert entry.filename == name
            assert entry.size is None or entry.size > 0
            assert entry.md5 is None or len(entry.md5) == 32
            assert entry.mirrors
            for mirror in entry.mirrors:
                assert mirror.startswith(("http://", "https://"))
                assert mirror.endswith("/" + entry.filename)

    def test_table_lists_every_database(self) -> None:
        table = database_table()
        assert "NAME" in table and "SIZE" in table
        for name in DATABASES:
            assert name in table

    def test_unknown_database(self) -> None:
        with pytest.raises(ValueError, match="unknown database"):
            lookup_database("nope.syldb")
        with pytest.raises(ValueError, match="gtdb-r232-c200-dbv2"):
            lookup_database("nope.syldb")


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
        content_range: bool = True,
        records: list[str | None] | None = None,
        **kwargs: object,
    ) -> None:
        self.directory = Path(directory)
        self.honor_range = honor_range
        self.content_range = content_range
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
            if self.content_range:
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
        content_range: bool = True,
        records: list[str | None] | None = None,
    ) -> str:
        handler = functools.partial(
            _FileHandler,
            directory=str(root),
            honor_range=honor_range,
            content_range=content_range,
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


def _serve_database(
    root: Path, name: str, content: bytes, base_url: str, *, md5: str | None = None
) -> Database:
    """Publish ``content`` as ``name`` and describe it as a catalogue entry."""
    (root / name).write_bytes(content)
    return Database(
        name=name,
        filename=name,
        mirrors=(base_url + name,),
        size=len(content),
        md5=md5,
    )


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


def test_resume_without_content_range_restarts(tmp_path: Path, www: Path, serve) -> None:
    content = b"0123456789" * 1000
    (www / "db.syldb").write_bytes(content)
    records: list[str | None] = []
    base_url = serve(www, content_range=False, records=records)

    destination = tmp_path / "db.syldb"
    partial = destination.with_name("db.syldb.part")
    partial.write_bytes(b"x" * 3000)  # not a prefix of the served file

    download_urls([base_url + "db.syldb"], destination, expected_size=len(content))

    assert destination.read_bytes() == content
    assert records == ["bytes=3000-", None]  # bad 206, then a fresh full request


def test_http_does_not_resume_aria2c_partial(tmp_path: Path, www: Path, serve) -> None:
    content = b"0123456789" * 1000
    (www / "db.syldb").write_bytes(content)
    records: list[str | None] = []
    base_url = serve(www, records=records)

    destination = tmp_path / "db.syldb"
    partial = destination.with_name("db.syldb.part")
    control = destination.with_name("db.syldb.part.aria2")
    partial.write_bytes(content[:3000])
    control.write_bytes(b"control")

    download_urls([base_url + "db.syldb"], destination, expected_size=len(content))

    assert destination.read_bytes() == content
    assert not control.exists()
    assert records == [None]


def test_mirror_fallback(tmp_path: Path, www: Path, serve) -> None:
    content = b"second mirror has it"
    empty = tmp_path / "empty"
    empty.mkdir()
    (www / "db.syldb").write_bytes(content)
    records: list[str | None] = []
    first = serve(empty)  # 404
    second = serve(www, records=records)

    destination = tmp_path / "db.syldb"
    download_urls([first + "db.syldb", second + "db.syldb"], destination)
    assert destination.read_bytes() == content
    assert records == [None]  # the second mirror really served it


def test_checksum_mismatch_falls_back_to_next_mirror(
    tmp_path: Path, www: Path, serve
) -> None:
    content = b"good content"
    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    (corrupt / "db.syldb").write_bytes(b"evil content")  # same length
    (www / "db.syldb").write_bytes(content)
    first = serve(corrupt)
    second = serve(www)

    destination = tmp_path / "db.syldb"
    download_urls(
        [first + "db.syldb", second + "db.syldb"],
        destination,
        expected_size=len(content),
        expected_md5=hashlib.md5(content).hexdigest(),
    )
    assert destination.read_bytes() == content
    assert not destination.with_name("db.syldb.part").exists()


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
    with pytest.raises(DownloadError, match="checksum mismatch"):
        download_urls(
            [base_url + "db.syldb"], destination, expected_md5="0" * 32
        )
    assert not destination.exists()
    assert not destination.with_name("db.syldb.part").exists()


def test_download_database_skips_existing(tmp_path: Path, www: Path, serve) -> None:
    content = b"already here" * 100
    records: list[str | None] = []
    base_url = serve(www, records=records)
    entry = _serve_database(www, "db.syldb", content, base_url)

    destination = download_database(entry, tmp_path, use_aria2c=False)
    assert destination.read_bytes() == content
    assert records == [None]

    download_database(entry, tmp_path, use_aria2c=False)
    assert records == [None]  # not fetched again

    # A wrong-size file is replaced, not reused.
    destination.write_bytes(b"truncated")
    records.clear()
    download_database(entry, tmp_path, use_aria2c=False)
    assert destination.read_bytes() == content
    assert records == [None]


def test_download_database_force(tmp_path: Path, www: Path, serve) -> None:
    content = b"x" * 10
    base_url = serve(www)
    entry = _serve_database(www, "db.syldb", content, base_url)
    destination = tmp_path / "db.syldb"
    destination.write_bytes(content)
    download_database(entry, tmp_path, force=True, use_aria2c=False)
    assert destination.read_bytes() == content


def test_download_database_verify(tmp_path: Path, www: Path, serve) -> None:
    content = b"verify me" * 50
    digest = hashlib.md5(content).hexdigest()
    base_url = serve(www)
    entry = _serve_database(www, "db.syldb", content, base_url, md5=digest)

    destination = download_database(entry, tmp_path, verify=True, use_aria2c=False)
    assert destination.read_bytes() == content

    # A same-size corruption is caught by --verify and replaced.
    destination.write_bytes(b"y" * len(content))
    download_database(entry, tmp_path, verify=True, use_aria2c=False)
    assert destination.read_bytes() == content

    # Without --verify, the size check alone accepts the file.
    destination.write_bytes(b"z" * len(content))
    download_database(entry, tmp_path, use_aria2c=False)
    assert destination.read_bytes() == b"z" * len(content)


def test_download_database_unknown_name_with_url(tmp_path: Path, www: Path, serve) -> None:
    content = b"arbitrary"
    (www / "my.syldb").write_bytes(content)
    base_url = serve(www)
    destination = download_database(
        "my.syldb", tmp_path, urls=(base_url + "my.syldb",), use_aria2c=False
    )
    assert destination == tmp_path / "my.syldb"
    assert destination.read_bytes() == content

    with pytest.raises(ValueError, match="unknown database"):
        download_database("nope.syldb", tmp_path, use_aria2c=False)


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


def test_aria2c_failure_keeps_control_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(command: list[str], check: bool = False) -> _FakeCompleted:
        (tmp_path / "x.part").write_bytes(b"holes")
        (tmp_path / "x.part.aria2").write_bytes(b"control")
        return _FakeCompleted(7)

    monkeypatch.setattr(download.subprocess, "run", fake_run)
    with pytest.raises(DownloadError):
        download_urls(["http://a/x"], tmp_path / "x", use_aria2c=True)
    assert (tmp_path / "x.part").exists()
    assert (tmp_path / "x.part.aria2").exists()


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "sylphd", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_cli_lists_databases() -> None:
    for flag in ("--list-databases", "--database=list"):
        result = _run("download", flag)
        assert result.returncode == 0, result.stderr
        assert "gtdb-r232-c200-dbv2.syl2db" in result.stdout
        assert "18.7 GiB" in result.stdout


def test_cli_downloads_database_from_url(tmp_path: Path, www: Path, serve) -> None:
    content = b"cli database"
    (www / "db.syldb").write_bytes(content)
    base_url = serve(www)
    result = _run(
        "download",
        "--database",
        "db.syldb",
        "--url",
        base_url + "db.syldb",
        "--no-aria2c",
        "--outdir",
        str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(tmp_path / "db.syldb")
    assert (tmp_path / "db.syldb").read_bytes() == content


def test_cli_rejects_url_with_several_databases() -> None:
    result = _run(
        "download",
        "--database",
        "a.syldb",
        "--database",
        "b.syldb",
        "--url",
        "http://example.org/a.syldb",
    )
    assert result.returncode == 1
    assert "single --database" in result.stderr


def test_cli_downloads_several_urls(tmp_path: Path, www: Path, serve) -> None:
    (www / "a.syldb").write_bytes(b"aaa")
    (www / "b.syldb").write_bytes(b"bb")
    base_url = serve(www)
    result = _run(
        "download",
        "--url",
        base_url + "a.syldb",
        "--url",
        base_url + "b.syldb",
        "--no-aria2c",
        "--outdir",
        str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert sorted(result.stdout.split()) == sorted(
        [str(tmp_path / "a.syldb"), str(tmp_path / "b.syldb")]
    )
    assert (tmp_path / "a.syldb").read_bytes() == b"aaa"
    assert (tmp_path / "b.syldb").read_bytes() == b"bb"


def test_cli_rejects_urls_with_the_same_file_name(tmp_path: Path) -> None:
    result = _run(
        "download",
        "--url",
        "http://a.example/db.syldb",
        "--url",
        "http://b.example/db.syldb",
        "--outdir",
        str(tmp_path),
    )
    assert result.returncode == 1
    assert "same file: db.syldb" in result.stderr
    assert not list(tmp_path.iterdir())


def test_cli_release_network_error_is_a_cli_error(tmp_path: Path) -> None:
    result = _run(
        "download", "--base-url", "http://127.0.0.1:9/", "--outdir", str(tmp_path)
    )
    assert result.returncode == 1
    assert "error:" in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_unknown_database(tmp_path: Path) -> None:
    result = _run("download", "--database", "nope.syldb", "--outdir", str(tmp_path))
    assert result.returncode == 1
    assert "unknown database" in result.stderr
    assert "--list-databases" in result.stderr
