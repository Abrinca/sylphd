"""``--name`` and ``--about``: the operator's description in ``GET /info``."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from sylphd.about import MAX_ABOUT_BYTES, About, AboutError, check_name
from sylphd.server import ServerConfig, create_app

from conftest import ECOLI_TWO_STAGE


def _app(name: str | None = None, about_path: Path | None = None):
    config = ServerConfig(db_path=ECOLI_TWO_STAGE, name=check_name(name), about=About(about_path))
    return create_app(config)


def _info(**config) -> dict:
    app = _app(**config)
    with TestClient(app) as client:
        return client.get("/info").json()


def test_absent_when_not_configured() -> None:
    info = _info()
    assert "name" not in info and "about" not in info


def test_name_and_about(tmp_path: Path) -> None:
    about = tmp_path / "about.md"
    about.write_text("This server holds **GTDB**.\n\n[docs](https://example.org)\n", encoding="utf-8")
    info = _info(name="  My server ", about_path=about)
    assert info["name"] == "My server"
    assert info["about"] == {
        "format": "markdown",
        "text": "This server holds **GTDB**.\n\n[docs](https://example.org)",
    }


def test_name_only_and_about_only(tmp_path: Path) -> None:
    info = _info(name="x")
    assert info["name"] == "x" and "about" not in info
    about = tmp_path / "a.md"
    about.write_text("hello", encoding="utf-8")
    info = _info(about_path=about)
    assert "name" not in info and info["about"]["text"] == "hello"


def test_empty_file_means_no_about(tmp_path: Path) -> None:
    about = tmp_path / "a.md"
    about.write_text("  \n", encoding="utf-8")
    assert "about" not in _info(about_path=about)


def test_utf8_and_bom(tmp_path: Path) -> None:
    about = tmp_path / "a.md"
    about.write_bytes("\ufeffGenome — Δ ✓".encode("utf-8"))
    assert _info(about_path=about)["about"]["text"] == "Genome — Δ ✓"


def test_size_limit(tmp_path: Path) -> None:
    about = tmp_path / "a.md"
    about.write_text("x" * MAX_ABOUT_BYTES, encoding="utf-8")
    assert len(_info(about_path=about)["about"]["text"]) == MAX_ABOUT_BYTES
    about.write_text("x" * (MAX_ABOUT_BYTES + 1), encoding="utf-8")
    with pytest.raises(AboutError, match="maximum"):
        About(about)


def test_bad_files_fail_at_startup(tmp_path: Path) -> None:
    with pytest.raises(AboutError, match="cannot read"):
        About(tmp_path / "missing.md")
    binary = tmp_path / "b.md"
    binary.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(AboutError, match="UTF-8"):
        About(binary)
    with pytest.raises(AboutError, match="cannot read"):
        About(tmp_path)  # a directory


@pytest.mark.parametrize("name", ["x" * 81, "two\nlines", "tab\there"])
def test_bad_names(name: str) -> None:
    with pytest.raises(AboutError):
        check_name(name)


def test_name_limits() -> None:
    assert check_name("x" * 80) == "x" * 80
    assert check_name("   ") is None and check_name(None) is None


def test_reload_on_change_and_keep_last_good(tmp_path: Path, caplog) -> None:
    about = tmp_path / "about.md"
    about.write_text("first", encoding="utf-8")
    app = _app(about_path=about)
    with TestClient(app) as client:
        assert client.get("/info").json()["about"]["text"] == "first"
        about.write_text("second, longer", encoding="utf-8")
        assert client.get("/info").json()["about"]["text"] == "second, longer"
        # same size, newer mtime
        stat = about.stat()
        about.write_text("SECOND, LONGER", encoding="utf-8")
        os.utime(about, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        assert client.get("/info").json()["about"]["text"] == "SECOND, LONGER"
        # too large: the last good text stays, with a warning, logged once
        about.write_text("y" * (MAX_ABOUT_BYTES + 1), encoding="utf-8")
        with caplog.at_level("WARNING"):
            for _ in range(3):
                assert client.get("/info").json()["about"]["text"] == "SECOND, LONGER"
        assert len([r for r in caplog.records if "keeping the previous" in r.message]) == 1
        # removed: still the last good text
        about.unlink()
        assert client.get("/info").json()["about"]["text"] == "SECOND, LONGER"
        # fixed again
        about.write_text("third", encoding="utf-8")
        assert client.get("/info").json()["about"]["text"] == "third"


def test_cli_rejects_bad_about(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "sylphd", "serve", "--db", str(ECOLI_TWO_STAGE),
         "--tcp", "127.0.0.1:8799", "--about", str(tmp_path / "nope.md")],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0
    assert "about file" in result.stderr
