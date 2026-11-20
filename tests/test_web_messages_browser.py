"""web/messages.js: operator messages under the status bar, dismissal kept per content hash.

Runs in headless Chromium through playwright-core (see test_web_about_browser.py for the
environment and for the skip rules).
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from test_web_about_browser import (  # noqa: F401  (the fixture applies to this module too)
    CHROMIUM, LIBS, MISSING, PLAYWRIGHT, WEB, _require_browser,
)

FILES = [*LIBS, str(WEB / "messages.js")]

NODE = r"""
const pw = require('node:module').createRequire(process.env.PLAYWRIGHT_DIR + '/')('playwright-core');
(async () => {
  const browser = await pw.chromium.launch({ headless: true, executablePath: process.env.BROWSER_EXE });
  const page = await browser.newPage();
  await page.setContent('<!doctype html><div id="box" hidden></div>');
  for (const file of JSON.parse(process.argv[1])) await page.addScriptTag({ path: file });
  const out = await page.evaluate(() => {
    window.__pwned = 0;
    window.alert = () => { window.__pwned++; };
    const M = window.SylphdMessages;
    const info = { level: 'info', title: 'Licence', message: 'See [terms](https://example.org/t)' };
    const warning = { level: 'warning', title: 'Slow', message: 'Expect **delays**' };
    const danger = { level: 'danger', title: 'Down', message: 'Tonight' };
    const evil = { level: 'warning', title: '<img src=x onerror=alert(1)>', message: '<img src=x onerror=alert(1)> [x](javascript:alert(1))' };
    const box = document.getElementById('box');
    const snap = () => ({
      hidden: box.hidden,
      items: [...box.children].map((n) => ({
        cls: n.className, role: n.getAttribute('role'), title: n.querySelector('.msg-title').textContent,
        text: n.textContent, close: !!n.querySelector('.msg-close'),
        label: n.querySelector('.msg-close')?.getAttribute('aria-label') ?? null,
        links: [...n.querySelectorAll('a')].map((a) => a.getAttribute('href')),
      })),
    });
    const r = {};
    const map = new Map();
    const store = { getItem: (k) => (map.has(k) ? map.get(k) : null), setItem: (k, v) => map.set(k, String(v)) };
    const opts = { render: window.SylphdAbout.render, storage: store };
    let view = M.create(box, opts);
    r.empty = (view.show([]), snap());
    view.show([info, warning, danger]);
    r.all = snap();
    box.querySelector('.msg-info .msg-close').click();
    r.afterDismissInfo = snap().items.map((i) => i.title);
    // a "reload": a new view over the same storage keeps the dismissal
    view = M.create(box, opts);
    view.show([info, warning, danger]);
    r.afterReload = snap().items.map((i) => i.title);
    // changed text is a different message
    view.show([{ ...info, message: info.message + ' (new)' }, warning, danger]);
    r.changed = snap().items.map((i) => i.title);
    box.querySelector('.msg-warning .msg-close').click();
    view.show([info, warning, danger]);
    r.dismissAll = snap().items.map((i) => i.title);
    r.stored = JSON.parse(store.getItem('sylphd.messages.dismissed')).length;
    // danger is never hidden, even if its hash were stored
    store.setItem('sylphd.messages.dismissed', JSON.stringify([M.hash(danger)]));
    view.show([danger]);
    r.dangerStays = snap();
    view.clear();
    r.cleared = snap().hidden;
    view.show([evil]);
    r.evil = snap();
    r.hash = [M.hash(info) === M.hash({ ...info }), M.hash(info) === M.hash({ ...info, level: 'warning' }),
              M.hash({ level: 'a', title: 'bc', message: 'd' }) === M.hash({ level: 'a', title: 'b', message: 'cd' })];
    r.clean = M.clean({ messages: [info, { level: 'loud', title: 't', message: 'm' }, { level: 'info', title: 1, message: 'm' }, 5, null] });
    r.cleanBad = [M.clean(null), M.clean({}), M.clean({ messages: 'x' })];
    // storage that throws (private mode): still dismisses for this page
    const broken = { getItem() { throw new Error('x'); }, setItem() { throw new Error('x'); } };
    const view2 = M.create(box, { render: window.SylphdAbout.render, storage: broken });
    view2.show([info]);
    box.querySelector('.msg-close').click();
    r.brokenStorage = snap().items.length;
    r.pwned = window.__pwned;
    return r;
  });
  await new Promise((r) => setTimeout(r, 150));
  out.pwned = await page.evaluate(() => window.__pwned);
  console.log(JSON.stringify(out));
  await browser.close();
})();
"""


@pytest.fixture(scope="module")
def run() -> dict:
    env = {**os.environ, "PLAYWRIGHT_DIR": PLAYWRIGHT or "", "BROWSER_EXE": CHROMIUM or ""}
    done = subprocess.run(["node", "-e", NODE, json.dumps(FILES)], capture_output=True, text=True,
                          env=env, timeout=120)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_levels_roles_and_dismiss_buttons(run) -> None:
    assert run["empty"] == {"hidden": True, "items": []}
    info, warning, danger = run["all"]["items"]
    assert (info["cls"], warning["cls"], danger["cls"]) == ("msg msg-info", "msg msg-warning", "msg msg-danger")
    assert (info["role"], warning["role"], danger["role"]) == ("status", "status", "alert")
    assert (info["close"], warning["close"], danger["close"]) == (True, True, False)
    assert info["label"] == "Dismiss: Licence"
    assert info["links"] == ["https://example.org/t"] and "Expect delays" in warning["text"]


def test_dismissal_is_remembered_by_content(run) -> None:
    assert run["afterDismissInfo"] == ["Slow", "Down"]
    assert run["afterReload"] == ["Slow", "Down"]
    assert run["changed"] == ["Licence", "Slow", "Down"]  # changed text appears again
    assert run["dismissAll"] == ["Down"]
    assert run["stored"] == 2
    assert [i["title"] for i in run["dangerStays"]["items"]] == ["Down"]
    assert run["cleared"] is True
    assert run["brokenStorage"] == 0


def test_hash_clean_and_inertness(run) -> None:
    assert run["hash"] == [True, False, False]  # level and field boundaries matter
    assert len(run["clean"]) == 1 and run["cleanBad"] == [[], [], []]
    (evil,) = run["evil"]["items"]
    assert all(h is None or h.startswith(("http:", "https:", "mailto:")) for h in evil["links"])
    assert run["pwned"] == 0
