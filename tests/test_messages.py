"""``--messages``: operator messages, ``GET /messages``, the CLI notice and the ``/info`` licence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from sylphd import cli
from sylphd.catalog import DATABASES
from sylphd.client import SylphClient
from sylphd.messages import (
    MAX_MESSAGE_CHARS,
    MAX_MESSAGES,
    MAX_TITLE_CHARS,
    Messages,
    MessagesError,
    parse_messages,
)
from sylphd.server import ServerConfig, create_app

from conftest import ECOLI_TWO_STAGE

INFO = {"level": "info", "title": "Licence", "message": "CC BY-SA, [terms](https://example.org)"}
WARNING = {"level": "warning", "title": "Slow", "message": "Expect **delays**."}
DANGER = {"level": "danger", "title": "Down", "message": "Maintenance tonight."}


def _write(path: Path, items) -> Path:
    path.write_text(json.dumps(items), encoding="utf-8")
    return path


def _app(path: Path | None = None, **config):
    return create_app(
        ServerConfig(db_path=ECOLI_TWO_STAGE, messages=Messages(path), **config)
    )


# -- the endpoint ---------------------------------------------------------


def test_empty_without_the_option() -> None:
    with TestClient(_app()) as client:
        response = client.get("/messages")
        assert response.status_code == 200
        assert response.json() == {"messages": []}
        assert "/messages" in client.get("/info").json()["endpoints"]


def test_valid_file(tmp_path: Path) -> None:
    path = _write(tmp_path / "m.json", [INFO, WARNING, DANGER])
    with TestClient(_app(path)) as client:
        assert client.get("/messages").json() == {"messages": [INFO, WARNING, DANGER]}


def test_reload_on_change_and_keep_last_good(tmp_path: Path, caplog) -> None:
    path = _write(tmp_path / "m.json", [INFO])
    with TestClient(_app(path)) as client:
        assert client.get("/messages").json()["messages"] == [INFO]
        _write(path, [INFO, DANGER])
        assert client.get("/messages").json()["messages"] == [INFO, DANGER]
        # invalid at runtime: the last good list stays, one warning however often asked
        path.write_text("{not json", encoding="utf-8")
        with caplog.at_level("WARNING"):
            for _ in range(3):
                assert client.get("/messages").json()["messages"] == [INFO, DANGER]
        assert len([r for r in caplog.records if "keeping the previous" in r.message]) == 1
        path.unlink()
        assert client.get("/messages").json()["messages"] == [INFO, DANGER]
        _write(path, [])
        assert client.get("/messages").json() == {"messages": []}


def test_cors_and_open_with_required_token(tmp_path: Path) -> None:
    import hashlib

    path = _write(tmp_path / "m.json", [INFO])
    app = _app(
        path,
        cors_origins=("https://page.example",),
        privileged_tokens=(hashlib.sha256(b"secret").digest(),),
        require_token=True,
        rate_limit=(1, 60.0),
    )
    with TestClient(app) as client:
        for _ in range(3):  # no rate limit, like /info
            response = client.get("/messages", headers={"origin": "https://page.example"})
            assert response.status_code == 200
            assert response.headers["access-control-allow-origin"] == "https://page.example"


# -- validation -------------------------------------------------------------


@pytest.mark.parametrize(
    ("document", "needle"),
    [
        ("nope", "valid JSON"),
        ('{"level": "info"}', "list"),
        ([1], "must be an object"),
        ([{"level": "info", "title": "t"}], "lacks message"),
        ([{**INFO, "id": 1}], "unknown keys: id"),
        ([{**INFO, "level": "error"}], "level must be one of"),
        ([{**INFO, "level": None}], "level must be one of"),
        ([{**INFO, "title": 3}], "title must be a string"),
        ([{**INFO, "message": ["x"]}], "message must be a string"),
        ([{**INFO, "title": "  "}], "title must not be empty"),
        ([{**INFO, "title": "a\nb"}], "single line"),
        ([{**INFO, "title": "t" * (MAX_TITLE_CHARS + 1)}], "title is"),
        ([{**INFO, "message": "m" * (MAX_MESSAGE_CHARS + 1)}], "message is"),
        ([INFO] * (MAX_MESSAGES + 1), "maximum"),
    ],
)
def test_validation(document, needle: str) -> None:
    text = document if isinstance(document, str) else json.dumps(document)
    with pytest.raises(MessagesError, match=needle):
        parse_messages(text)


def test_limits_are_inclusive() -> None:
    item = {**INFO, "title": "t" * MAX_TITLE_CHARS, "message": "m" * MAX_MESSAGE_CHARS}
    assert len(parse_messages(json.dumps([item] * MAX_MESSAGES))) == MAX_MESSAGES


def test_startup_errors(tmp_path: Path) -> None:
    with pytest.raises(MessagesError, match="cannot read"):
        Messages(tmp_path / "missing.json")
    binary = tmp_path / "b.json"
    binary.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(MessagesError, match="UTF-8"):
        Messages(binary)
    with pytest.raises(MessagesError, match="maximum"):
        Messages(_write(tmp_path / "big.json", "x" * 70000))


def test_cli_rejects_bad_messages(tmp_path: Path) -> None:
    bad = tmp_path / "m.json"
    bad.write_text('[{"level": "loud", "title": "t", "message": "m"}]', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "sylphd", "serve", "--db", str(ECOLI_TWO_STAGE),
         "--tcp", "127.0.0.1:8798", "--messages", str(bad)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0
    assert "level must be one of" in result.stderr


# -- /info licence --------------------------------------------------------


def test_licence_by_catalogue_file_name(tmp_path: Path) -> None:
    with TestClient(_app()) as client:
        (entry,) = client.get("/info").json()["databases"]
    assert entry["licence"] is None  # ecoli.syl2db is not a catalogue file
    ccbysa = {"name": "CC BY-SA 4.0", "url": "https://creativecommons.org/licenses/by-sa/4.0/"}
    # a copy named like a catalogue file, and an alias, must not matter
    named = tmp_path / "globdb_r232_sylph_v2_c200.syl2db"
    named.symlink_to(ECOLI_TWO_STAGE)  # the catalogue name is the link's, not the target's
    from sylphd.profiling import DatabaseSpec

    config = ServerConfig(db_paths=(DatabaseSpec(named, "mine"),))
    with TestClient(create_app(config)) as client:
        (entry,) = client.get("/info").json()["databases"]
    assert entry["name"] == "mine" and entry["licence"] == ccbysa


def test_catalogue_licences() -> None:
    licensed = {name for name, db in DATABASES.items() if db.licence}
    assert all("gtdb" in name for name in licensed - {"globdb_r232_sylph_v2_c200.syl2db"})
    assert "globdb_r232_sylph_v2_c200.syl2db" in licensed
    assert not DATABASES["uhgv_c100_dbv1.syldb"].licence  # not verified: unknown


# -- the command line -------------------------------------------------------


class _Client(SylphClient):
    def __init__(self, handler) -> None:
        super().__init__(base_url="http://x.test")
        import httpx

        self._client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x.test/")


def _notice(handler, capsys, **kwargs) -> str:
    cli._note_messages(_Client(handler), **kwargs)
    return capsys.readouterr().err


def test_cli_prints_warning_and_danger_not_info(capsys) -> None:
    import httpx

    handler = lambda request: httpx.Response(200, json={"messages": [INFO, WARNING, DANGER]})
    err = _notice(handler, capsys)
    assert "warning: Slow: Expect delays." in err
    assert "DANGER: Down: Maintenance tonight." in err
    assert "Licence" not in err
    assert _notice(handler, capsys, quiet=True) == ""


def test_cli_markdown_links_are_plain(capsys) -> None:
    import httpx

    item = {**WARNING, "message": "See [the page](https://example.org/x) or `this`."}
    err = _notice(lambda r: httpx.Response(200, json={"messages": [item]}), capsys)
    assert "See the page (https://example.org/x) or this." in err


@pytest.mark.parametrize("kind", ["404", "500", "garbage", "shape", "timeout", "refused"])
def test_cli_is_silent_when_messages_are_unavailable(capsys, kind: str) -> None:
    import httpx

    def handler(request):
        if kind == "404":
            return httpx.Response(404, json={"error": "not found"})
        if kind == "500":
            return httpx.Response(500)
        if kind == "garbage":
            return httpx.Response(200, content=b"<html>")
        if kind == "shape":
            return httpx.Response(200, json={"messages": [{"level": "danger"}, 5, "x"]})
        if kind == "timeout":
            raise httpx.ReadTimeout("slow")
        raise httpx.ConnectError("no")

    assert _notice(handler, capsys) == ""
