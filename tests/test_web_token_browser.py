"""The web page's access token, end to end in headless Chromium against a real private server.

A server started with ``--require-token`` and ``--cors-origin '*'``, the real ``web/`` directory
served statically, and a walkthrough: the page asks for a token, a wrong one is flagged, the right
one is accepted and cached across reloads, and forgetting it brings the prompt back. Skipped when
node, playwright-core or Chromium is missing (see test_web_about_browser.py), but a failure when
the environment variable CI is set.
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from conftest import ECOLI_TWO_STAGE
from test_web_about_browser import CHROMIUM, MISSING, PLAYWRIGHT

WEB = Path(__file__).resolve().parents[1] / "web"
TOKEN = "browser-token-" + "q" * 24

pytestmark = pytest.mark.skipif(
    bool(MISSING) and not os.environ.get("CI"), reason=MISSING or ""
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    if MISSING:
        pytest.fail(MISSING)
    directory = tmp_path_factory.mktemp("browser")
    tokens = directory / "tokens"
    tokens.write_text(TOKEN + "\n")
    api_port, web_port = _free_port(), _free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "sylphd", "serve", "--db", str(ECOLI_TWO_STAGE),
         "--tcp", f"127.0.0.1:{api_port}", "--privileged-token-file", str(tokens),
         "--require-token", "--cors-origin", "*", "--log-level", "warning"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(WEB))
    handler.log_message = lambda *args: None  # type: ignore[attr-defined]
    web = http.server.ThreadingHTTPServer(("127.0.0.1", web_port), handler)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{api_port}/health", timeout=1).read()
            break
        except OSError:
            time.sleep(0.2)
    else:
        raise AssertionError("server did not start")
    try:
        yield f"http://127.0.0.1:{web_port}/?api=http://127.0.0.1:{api_port}", api_port
    finally:
        web.shutdown()
        server.terminate()
        server.wait(timeout=20)


NODE = r"""
const pw = require('node:module').createRequire(process.env.PLAYWRIGHT_DIR + '/')('playwright-core');
(async () => {
  const { url, token } = JSON.parse(process.argv[1]);
  const browser = await pw.chromium.launch({ headless: true, executablePath: process.env.BROWSER_EXE });
  const page = await browser.newPage();
  const sent = [];
  page.on('request', (r) => { const h = r.headers()['authorization']; if (h) sent.push(h); });
  const log = {};
  const snap = async (label) => {
    log[label] = await page.evaluate(() => ({
      status: document.querySelector('#status .status-text').textContent,
      btn: document.getElementById('sp-token-btn').dataset.state,
      panelOpen: !document.getElementById('sp-token').hidden,
      panelText: document.querySelector('#sp-token .token-state')?.textContent || '',
      chip: document.querySelector('.sp-token-chip')?.textContent || null,
      stored: Object.keys(localStorage).filter((k) => k.startsWith('sylphd.token:')),
    }));
  };
  const settled = () => page.waitForFunction(() => !/Checking/.test(document.querySelector('#status .status-text').textContent), null, { timeout: 15000 });
  const enter = async (value) => { await page.fill('#api-token', value); await page.click('#sp-token button[type=submit]'); await page.waitForTimeout(100); await settled(); };

  await page.goto(url);
  await settled();
  await snap('first visit');
  await enter('wrong-token-' + 'w'.repeat(20));
  await snap('wrong token');
  await enter(token);
  await snap('right token');
  await page.reload();
  await settled();
  await snap('after reload');
  await page.click('#sp-token-btn');
  await page.click('#sp-token button:has-text("Forget")');
  await settled();
  await snap('after forget');
  log.sent = sent;
  console.log(JSON.stringify(log));
  await browser.close();
})();
"""


@pytest.fixture(scope="module")
def walkthrough(stack):
    url, _ = stack
    env = {**os.environ, "PLAYWRIGHT_DIR": PLAYWRIGHT or "", "BROWSER_EXE": CHROMIUM or ""}
    done = subprocess.run(
        ["node", "-e", NODE, json.dumps({"url": url, "token": TOKEN})],
        capture_output=True, text=True, env=env, timeout=180,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_a_server_that_requires_a_token_asks_for_it(walkthrough) -> None:
    first = walkthrough["first visit"]
    assert "access token required" in first["status"]
    assert first["btn"] == "required" and first["panelOpen"] is True
    assert "requires an access token" in first["panelText"]
    assert first["chip"] == "token required" and first["stored"] == []


def test_a_wrong_token_is_flagged_not_silently_ignored(walkthrough) -> None:
    wrong = walkthrough["wrong token"]
    assert wrong["btn"] == "rejected" and wrong["panelOpen"] is True
    assert "does not accept" in wrong["panelText"]
    assert wrong["chip"] == "token not accepted"
    assert "token not accepted" in wrong["status"]
    assert "requires an access token" in wrong["panelText"]


def test_the_right_token_is_accepted_and_cached(walkthrough) -> None:
    ok = walkthrough["right token"]
    assert ok["status"] == "Server online" and ok["btn"] == "valid"
    assert ok["chip"] == "token accepted" and len(ok["stored"]) == 1
    again = walkthrough["after reload"]
    assert again["status"] == "Server online" and again["btn"] == "valid"  # cached in localStorage
    assert again["panelOpen"] is False


def test_forgetting_brings_the_prompt_back(walkthrough) -> None:
    gone = walkthrough["after forget"]
    assert gone["stored"] == [] and gone["btn"] == "required"
    assert "access token required" in gone["status"]


def test_the_token_is_only_sent_after_it_was_entered(walkthrough) -> None:
    sent = walkthrough["sent"]
    assert sent and all(h.startswith("Bearer ") for h in sent)
    assert f"Bearer {TOKEN}" in sent and any("wrong-token" in h for h in sent)
