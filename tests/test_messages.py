"""``--messages``: operator messages, ``GET /messages``, the CLI notice and the ``/info`` licence."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import CLODF13_FASTA, ECOLI_TWO_STAGE
from starlette.testclient import TestClient

from sylphd.catalog import DATABASES
from sylphd.messages import (
    MAX_MESSAGE_CHARS,
    MAX_MESSAGES,
    MAX_TITLE_CHARS,
    Messages,
    MessagesError,
    parse_messages,
)
from sylphd.server import ServerConfig, create_app

INFO = {"level": "info", "title": "Licence", "message": "CC BY-SA, [terms](https://example.org)"}
WARNING = {"level": "warning", "title": "Slow", "message": "Expect **delays**."}
DANGER = {"level": "danger", "title": "Down", "message": "Maintenance tonight."}

INFO_WIRE = {**INFO, "plain": "CC BY-SA, terms (https://example.org)"}
WARNING_WIRE = {**WARNING, "plain": "Expect delays."}
DANGER_WIRE = {**DANGER, "plain": "Maintenance tonight."}


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
        assert client.get("/messages").json() == {
            "messages": [INFO_WIRE, WARNING_WIRE, DANGER_WIRE]
        }


def test_plain_strips_markdown_and_collapses_whitespace(tmp_path: Path) -> None:
    item = {
        "level": "info",
        "title": "Notes",
        "message": "See [docs](https://example.org/d), [https://a.test](https://a.test),\n**bold**, __under__ and `code`.",
    }
    path = _write(tmp_path / "m.json", [item])
    with TestClient(_app(path)) as client:
        (got,) = client.get("/messages").json()["messages"]
    assert got["plain"] == "See docs (https://example.org/d), https://a.test, bold, under and code."


def test_reload_on_change_and_keep_last_good(tmp_path: Path, caplog) -> None:
    path = _write(tmp_path / "m.json", [INFO])
    with TestClient(_app(path)) as client:
        assert client.get("/messages").json()["messages"] == [INFO_WIRE]
        _write(path, [INFO, DANGER])
        assert client.get("/messages").json()["messages"] == [INFO_WIRE, DANGER_WIRE]
        # invalid at runtime: the last good list stays, one warning however often asked
        path.write_text("{not json", encoding="utf-8")
        with caplog.at_level("WARNING"):
            for _ in range(3):
                assert client.get("/messages").json()["messages"] == [INFO_WIRE, DANGER_WIRE]
        assert len([r for r in caplog.records if "keeping the previous" in r.message]) == 1
        path.unlink()
        assert client.get("/messages").json()["messages"] == [INFO_WIRE, DANGER_WIRE]
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
        capture_output=True, text=True, timeout=60, check=False,
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


# -- the command line notice (stderr, for the query commands) --------------


def _client_stderr(
    tmp_path: Path, command: str = "profile", *flags: str, quiet: bool = False
) -> str:
    """``sylphd-client <command>`` against a live server: its stderr notes only."""
    path = _write(tmp_path / "m.json", [INFO, WARNING, DANGER])
    socket_path, process = _serve_socket(tmp_path, "--messages", str(path))
    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "sylphd", command, str(CLODF13_FASTA),
                "--socket", str(socket_path), *flags,
                *(["--quiet"] if quiet else []),
            ],
            check=False, capture_output=True, text=True, timeout=600,
        )
    finally:
        process.terminate()
        process.wait(timeout=20)
    assert result.returncode == 0, result.stderr
    return result.stderr


def test_cli_prints_warning_and_danger_not_info(tmp_path: Path) -> None:
    err = _client_stderr(tmp_path, "profile", "--fields", "none")
    assert "sylphd: warning: Slow: Expect delays." in err
    assert "sylphd: DANGER: Down: Maintenance tonight." in err
    assert "Licence" not in err
    assert "CC BY-SA" not in err  # the info message is for `sylphd info` only


def test_cli_is_quiet_when_asked(tmp_path: Path) -> None:
    # --quiet is assembly's ("do not print the summary on stderr") and it also
    # silences the operator messages.
    assert _client_stderr(tmp_path, "assembly", quiet=True) == ""


def test_cli_markdown_links_are_plain(tmp_path: Path) -> None:
    """The client prints the server's ``plain`` field, never the markdown."""
    assert "Expect delays." in _client_stderr(tmp_path, "profile", "--fields", "none")


# -- `sylphd info` shows the messages ---------------------------------------


def _serve_socket(tmp_path: Path, *extra: str):
    from test_cli import _wait_for_socket

    socket_path = tmp_path / "sylphd.sock"
    process = subprocess.Popen(
        [sys.executable, "-m", "sylphd", "serve", "--db", str(ECOLI_TWO_STAGE),
         "--socket", str(socket_path), "--log-level", "warning", *extra],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        _wait_for_socket(socket_path)
    except BaseException:
        process.kill()
        raise
    return socket_path, process


def _info(socket_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "sylphd", "info", "--socket", str(socket_path), *args],
        capture_output=True, text=True, timeout=120, check=True,
    )


def test_info_shows_all_messages_and_json_carries_them(tmp_path: Path) -> None:
    path = _write(tmp_path / "m.json", [INFO, WARNING, DANGER])
    socket_path, process = _serve_socket(tmp_path, "--messages", str(path))
    try:
        text = _info(socket_path).stdout
        assert "messages:" in text
        assert "[info] Licence: CC BY-SA, terms" in text
        assert "[warning] Slow: Expect delays." in text
        assert "[danger] Down: Maintenance tonight." in text
        document = json.loads(_info(socket_path, "--json").stdout)
        assert document["messages"] == [INFO_WIRE, WARNING_WIRE, DANGER_WIRE]
        assert "usage" in document
    finally:
        process.terminate()
        process.wait(timeout=20)


def test_info_omits_the_section_without_messages(tmp_path: Path) -> None:
    socket_path, process = _serve_socket(tmp_path)
    try:
        assert "messages:" not in _info(socket_path).stdout
        assert json.loads(_info(socket_path, "--json").stdout)["messages"] == []
    finally:
        process.terminate()
        process.wait(timeout=20)
