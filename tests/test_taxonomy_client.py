"""``sylphd.taxonomy()``: address and token resolution (the cases shared with the Rust client) and a live server."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from conftest import ECOLI_TWO_STAGE
from omnitax.client import ClientError
from taxfile import (
    FUNGI_ASSEMBLIES,
    FUNGI_MERGED,
    FUNGI_NODES,
    SACCHAROMYCES,
    write_taxonomy_file,
)
from test_taxonomy_mount import TOKEN, _free_port

import sylphd
from sylphd.taxonomy_client import check_token_transport, resolve_address

CASES = json.loads((Path(__file__).parent / "data" / "address_cases.json").read_text())
ADDRESS = [c for c in CASES["cases"] if not c["args"]["tcp"]]  # --tcp is a flag of the command line


@pytest.mark.parametrize("case", ADDRESS, ids=lambda c: c["name"])
def test_address_cases(case: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SYLPHD_URL", "SYLPHD_SOCKET", "XDG_RUNTIME_DIR"):
        monkeypatch.delenv(name, raising=False)
    for name, value in case["env"].items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(os, "getuid", lambda: case["uid"])
    expect = case["expect"]
    if expect["kind"] == "error":
        with pytest.raises(ValueError, match=expect["contains"]):
            resolve_address(case["args"]["url"], case["args"]["socket"])
    else:
        assert resolve_address(case["args"]["url"], case["args"]["socket"]) == (expect["kind"], expect["value"])


def test_url_and_socket_together_are_refused() -> None:
    with pytest.raises(ValueError, match="at most one"):
        resolve_address("http://x", "/run/x.sock")


@pytest.mark.parametrize("case", CASES["token_cases"], ids=lambda c: c["name"])
def test_token_cases(case: dict) -> None:
    if case["allowed"]:
        check_token_transport(case["url"])
    else:
        with pytest.raises(ValueError, match="refusing to send a token"):
            check_token_transport(case["url"])


def test_token_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sylphd.taxonomy_client import resolve_token

    file = tmp_path / "token"
    file.write_text(" from-file \n")
    monkeypatch.setenv("SYLPHD_TOKEN", "from-env")
    assert resolve_token("explicit", file) == "explicit"
    assert resolve_token(None, file) == "from-file"
    assert resolve_token() == "from-env"
    monkeypatch.delenv("SYLPHD_TOKEN")
    assert resolve_token() is None


def test_a_token_is_not_sent_over_plain_http_to_a_remote_host() -> None:
    with pytest.raises(ValueError, match="refusing to send a token"):
        sylphd.taxonomy("ncbi", url="http://example.org", token="secret")


# -- a live server -----------------------------------------------------------


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("live")
    db = write_taxonomy_file(
        root / "taxonomies.sqlite", FUNGI_NODES, assemblies=FUNGI_ASSEMBLIES, merged=FUNGI_MERGED,
        gtdb=[("RS_GCF_000002515.1", "d__Eukaryota;p__Ascomycota;s__Saccharomyces cerevisiae")], links=[("RS_GCF_000002515.1", 4932)],
    )
    token_file = root / "token"
    token_file.write_text(TOKEN + "\n")
    port, sock = _free_port(), root / "s.sock"
    common = [
        sys.executable, "-m", "sylphd", "serve", "--db", str(ECOLI_TWO_STAGE), "--taxonomy-db", str(db),
        "--taxonomy-rate-limit", "8/min", "--privileged-token-file", str(token_file), "--log-level", "warning",
    ]
    base = f"http://127.0.0.1:{port}"
    processes = [subprocess.Popen([*common, *address], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for address in (["--tcp", f"127.0.0.1:{port}"], ["--socket", str(sock)])]
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"{base}/health", timeout=1).close()
                if sock.exists():
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("the servers did not start")
        yield {"url": base, "socket": str(sock), "token_file": token_file}
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.parametrize("how", ["url", "socket"])
def test_lookup_and_lineage(live: dict, how: str) -> None:
    with sylphd.taxonomy("ncbi", **{how: live[how]}, token_file=live["token_file"]) as ncbi:
        assert ncbi["4932"].name == "Saccharomyces cerevisiae"
        assert ncbi["4932"].lineage_string() == SACCHAROMYCES
    with pytest.raises(KeyError, match="several taxonomies"):  # no name: ambiguous here
        sylphd.taxonomy(**{how: live[how]}, token_file=live["token_file"])


def test_environment_picks_the_server(live: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYLPHD_TOKEN", TOKEN)  # the anonymous limit is for the last test
    monkeypatch.setenv("SYLPHD_URL", live["url"] + "/")
    with sylphd.taxonomy("gtdb:232") as gtdb:
        assert gtdb.kind == "gtdb"
    monkeypatch.delenv("SYLPHD_URL")
    monkeypatch.setenv("SYLPHD_SOCKET", live["socket"])
    with sylphd.taxonomy("ncbi") as ncbi:
        assert ncbi["4932"] is not None


@pytest.mark.parametrize("how", ["url", "socket"])
def test_the_token_file_lifts_the_rate_limit(live: dict, how: str) -> None:
    with sylphd.taxonomy("ncbi", **{how: live[how]}, token_file=live["token_file"]) as ncbi:
        for _ in range(20):  # over the 8 a minute of an anonymous client
            assert ncbi.get("4932") is not None
    with sylphd.taxonomy("ncbi", **{how: live[how]}) as ncbi, pytest.raises(ClientError) as limited:
        for _ in range(20):
            ncbi.get("4932")
    assert limited.value.status_code == 429


# -- a taxonomy file that omnitax cannot read ----------------------------------


def _sqlite_file(path: Path, layout: int | None) -> Path:
    import sqlite3

    with sqlite3.connect(path) as db:
        if layout is not None:
            db.execute("CREATE TABLE taxonomy (id INTEGER)")
            db.execute(f"PRAGMA user_version = {layout}")
        else:
            db.execute("CREATE TABLE taxa (taxid INTEGER)")  # an old taxdb file
    return path


@pytest.mark.parametrize(
    ("layout", "words"),
    [(None, "is not an omnitax file"), (3, "rebuild it with this omnitax version"), ("missing", "not found")],
)
def test_a_bad_taxonomy_file_fails_at_once_with_a_hint(tmp_path: Path, layout, words: str) -> None:
    file = tmp_path / "taxdump.sqlite" if layout == "missing" else _sqlite_file(tmp_path / "taxdump.sqlite", layout)
    started = time.monotonic()
    serve = subprocess.run(
        [sys.executable, "-m", "sylphd", "serve", "--db", str(ECOLI_TWO_STAGE), "--tcp", f"127.0.0.1:{_free_port()}", "--taxonomy-db", str(file)],
        check=False, capture_output=True, text=True, timeout=60,
    )
    assert serve.returncode == 2 and words in serve.stderr and "omnitax build -o FILE" in serve.stderr, serve.stderr
    assert time.monotonic() - started < 10  # the databases were not loaded
    setup = subprocess.run(
        [sys.executable, "-m", "sylphd", "setup", "-m", str(tmp_path / "none.tsv"), "-o", str(tmp_path / "meta.sqlite"), "--taxonomy-db", str(file)],
        check=False, capture_output=True, text=True, timeout=60,
    )
    assert setup.returncode == 1 and words in setup.stderr and not (tmp_path / "meta.sqlite").exists(), setup.stderr
