"""web/usage.js: the agreement asked before the first upload to a server that keeps usage statistics.

Runs the real script under node with a stand-in for the page helpers and for
``localStorage``; the modal itself is a stub whose answer the test chooses.
The rendering of the dialog and of the usage modal is covered by the
browser walkthrough, not here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
USAGE_JS = ROOT / "web" / "usage.js"


@pytest.fixture(autouse=True)
def _require_node() -> None:
    if shutil.which("node") is None:
        (pytest.fail if os.environ.get("CI") else pytest.skip)("node is not installed")


NODE = r"""
globalThis.window = globalThis;
globalThis.location = { href: 'https://sylphd.example/?api=https%3A%2F%2Fapi.example' };
const store = new Map();
globalThis.localStorage = {
  get length() { return store.size; },
  key: (i) => [...store.keys()][i] ?? null,
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => { store.set(k, String(v)); },
  removeItem: (k) => { store.delete(k); },
};
let api = 'https://a.example';
let usage = { enabled: true, policy: 1, window_minutes: 10 };
let answer = 'agree';
let asked = 0;
const node = (...args) => ({ args });
window.Sylphd = {
  el: node, svg: node,
  readChoice: (key, allowed) => { const v = localStorage.getItem(key); return allowed.includes(v) ? v : null; },
  writeChoice: (key, value) => { if (value) localStorage.setItem(key, value); else localStorage.removeItem(key); },
  choiceModal: async () => { asked += 1; return answer; },
  get api() { return api; },
  usage: () => usage,
  onMode() {}, onInfo() {}, mode: () => 'assembly',
};
require(process.argv[1]);
const U = window.SylphdUsage;
(async () => {
  const log = [];
  const step = async (label, change) => {
    if (change) change();
    const before = asked;
    const ok = await U.ensureAgreed();
    log.push({ label, ok, asked: asked - before });
  };
  await step('first upload asks and agrees');
  await step('then it is remembered');
  await step('another server asks again', () => { api = 'https://b.example'; });
  await step('a higher policy asks again', () => { usage = { ...usage, policy: 2 }; });
  await step('and is remembered', null);
  await step('back to the first server: its policy 1 no longer matches', () => { api = 'https://a.example'; });
  await step('cancel sends nothing', () => { api = 'https://c.example'; answer = 'cancel'; });
  await step('and asks again next time');
  await step('tracking off needs no agreement', () => { usage = null; });
  const stored = [...store.entries()];
  U.forgetAgreements();
  console.log(JSON.stringify({ log, stored, hasAfterForget: U.hasAgreements(), left: store.size }));
})();
"""


def _run() -> dict:
    result = subprocess.run(
        ["node", "-e", NODE, str(USAGE_JS)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def test_agreement_is_per_server_and_policy() -> None:
    out = _run()
    assert out["log"] == [
        {"label": "first upload asks and agrees", "ok": True, "asked": 1},
        {"label": "then it is remembered", "ok": True, "asked": 0},
        {"label": "another server asks again", "ok": True, "asked": 1},
        {"label": "a higher policy asks again", "ok": True, "asked": 1},
        {"label": "and is remembered", "ok": True, "asked": 0},
        {"label": "back to the first server: its policy 1 no longer matches", "ok": True, "asked": 1},
        {"label": "cancel sends nothing", "ok": False, "asked": 1},
        {"label": "and asks again next time", "ok": False, "asked": 1},
        {"label": "tracking off needs no agreement", "ok": True, "asked": 0},
    ]


def test_stored_agreements_and_reset() -> None:
    out = _run()
    assert dict(out["stored"]) == {
        "sylphd.usage.agreed:https://a.example": "2",
        "sylphd.usage.agreed:https://b.example": "2",
    }
    assert out["hasAfterForget"] is False
    assert out["left"] == 0
