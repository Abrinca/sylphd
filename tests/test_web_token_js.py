"""web/token.js: the access token is saved per server, sent only to it, and judged by the server.

Runs the real script under node with a stand-in for ``localStorage``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

TOKEN_JS = Path(__file__).resolve().parents[1] / "web" / "token.js"


@pytest.fixture(autouse=True)
def _require_node() -> None:
    if shutil.which("node") is None:
        (pytest.fail if os.environ.get("CI") else pytest.skip)("node is not installed")


NODE = r"""
globalThis.window = globalThis;
const store = new Map();
let broken = false;
globalThis.localStorage = {
  get length() { if (broken) throw new Error('no storage'); return store.size; },
  key: (i) => [...store.keys()][i] ?? null,
  getItem: (k) => { if (broken) throw new Error('no storage'); return store.has(k) ? store.get(k) : null; },
  setItem: (k, v) => { if (broken) throw new Error('no storage'); store.set(k, String(v)); },
  removeItem: (k) => { if (broken) throw new Error('no storage'); store.delete(k); },
};
require(process.argv[1]);
const T = window.SylphdToken;
const out = {};
const a = T.forServer('https://a.example');
const b = T.forServer('https://b.example');
out.start = [a.status, a.has(), a.headers()];
const events = [];
a.onChange((s) => events.push(s));
out.badChars = [a.set('has space'), a.set('né'), a.status];
out.saved = [a.set('  tok-aaaaaaaaaaaa  '), a.status, a.headers()];
out.perServer = [b.has(), b.headers(), JSON.stringify([...store.keys()])];
out.reload = (() => { const again = T.forServer('https://a.example'); return [again.status, again.headers()]; })();
a.note(null); a.note('true'); a.note('false'); a.note('true'); a.note('TRUE');
out.events = events.slice();
a.set('tok-bbbbbbbbbbbb');
out.newTokenResets = a.status;
a.note('false');
out.rejected = a.status;
out.forget = [a.forget(), a.status, a.has(), a.headers(), store.size];
out.plainHttp = (() => {
  const remote = T.forServer('http://api.example'); remote.set('tok-cccccccccccc');
  const local = T.forServer('http://127.0.0.1:8765'); local.set('tok-dddddddddddd');
  const localhost = T.forServer('http://localhost:8765'); localhost.set('tok-eeeeeeeeeeee');
  return [remote.canSend(), remote.headers(), local.canSend(), Object.keys(local.headers()), localhost.canSend()];
})();
out.saved2 = T.savedServers().sort();
T.forgetAll();
out.afterForgetAll = [T.savedServers(), store.size];
broken = true;
const mem = T.forServer('https://m.example');
out.noStorage = [mem.set('tok-ffffffffffff'), mem.has(), mem.headers()];
console.log(JSON.stringify(out));
"""


def _run() -> dict:
    result = subprocess.run(
        ["node", "-e", NODE, str(TOKEN_JS)], check=True, capture_output=True, text=True, timeout=30
    )
    return json.loads(result.stdout)


def test_token_is_saved_per_server_and_only_sent_there() -> None:
    out = _run()
    assert out["start"] == ["none", False, {}]
    assert out["badChars"] == [False, False, "none"]
    assert out["saved"] == [True, "unknown", {"Authorization": "Bearer tok-aaaaaaaaaaaa"}]
    assert out["perServer"][0] is False and out["perServer"][1] == {}
    assert json.loads(out["perServer"][2]) == ["sylphd.token:https://a.example"]
    assert out["reload"] == ["unknown", {"Authorization": "Bearer tok-aaaaaaaaaaaa"}]


def test_the_servers_verdict_drives_the_status() -> None:
    out = _run()
    # note(null) changes nothing; verdicts are case-insensitive; repeats do not re-notify
    assert out["events"] == ["unknown", "valid", "rejected", "valid"]
    assert out["newTokenResets"] == "unknown"
    assert out["rejected"] == "rejected"


def test_forgetting_and_plain_http() -> None:
    out = _run()
    assert out["forget"] == [True, "none", False, {}, 0]  # a's key is gone, nothing else was saved
    remote_ok, remote_headers, local_ok, local_keys, localhost_ok = out["plainHttp"]
    assert (remote_ok, remote_headers) == (False, {})  # never in the clear to a remote host
    assert local_ok and local_keys == ["Authorization"] and localhost_ok
    assert out["saved2"] == [
        "http://127.0.0.1:8765",
        "http://api.example",
        "http://localhost:8765",
    ]  # one key per server, also for a token that is never sent
    assert out["afterForgetAll"] == [[], 0]


def test_without_localstorage_the_token_lives_in_memory() -> None:
    out = _run()
    assert out["noStorage"] == [True, True, {"Authorization": "Bearer tok-ffffffffffff"}]
