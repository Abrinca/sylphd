"""Tests for the GTDB metadata downloader."""

from __future__ import annotations

import functools
import hashlib
import http.server
import threading
from pathlib import Path

import pytest

from sylphd.download import (
    MD5SUMS_FILE,
    download_file,
    download_release,
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
