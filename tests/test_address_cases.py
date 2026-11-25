"""Address resolution: the shared table also run by the Rust client tests.

``tests/data/address_cases.json`` is the single source of truth for the
precedence in ``docs/commands/address.md``; ``rust/sylphd-client/src/address.rs``
runs the same cases.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from sylphd import cli

CASES_FILE = Path(__file__).parent / "data" / "address_cases.json"
CASES = json.loads(CASES_FILE.read_text())["cases"]
ENV_NAMES = ("SYLPHD_URL", "SYLPHD_SOCKET", "XDG_RUNTIME_DIR")


def _args(case: dict) -> argparse.Namespace:
    return argparse.Namespace(
        url=case["args"]["url"],
        tcp=case["args"]["tcp"],
        socket=case["args"]["socket"],
        token_file=None,
        timeout=None,
        retries=0,
    )


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_address_precedence(case: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in case.get("env", {}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(os, "getuid", lambda: case["uid"])

    expected = case["expect"]
    if expected["kind"] == "error":
        with pytest.raises(ValueError, match=expected["contains"]):
            cli._client_from_args(_args(case))
        return

    client = cli._client_from_args(_args(case))
    try:
        if expected["kind"] == "url":
            assert client.base_url == expected["value"]
            assert client.socket_path is None
        else:
            assert client.socket_path == Path(expected["value"])
            assert client.base_url is None
    finally:
        client.close()
