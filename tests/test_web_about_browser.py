"""web/about.js (marked + DOMPurify) renders the operator text from /info: correct and inert.

Runs in headless Chromium through playwright-core from node (`npm ci` in tests/ installs the pinned
version). Skipped when node, playwright-core or a Chromium binary is missing (PLAYWRIGHT_DIR,
BROWSER_EXE override the search), but a failure when the environment variable CI is set.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web"
ALLOWED_TAGS = {"p", "br", "strong", "em", "code", "pre", "ul", "ol", "li", "a", "blockquote",
                "h1", "h2", "h3", "h4", "h5", "h6"}
LIBS = [str(WEB / "vendor" / name) for name in ("marked.umd.js", "purify.min.js")] + [str(WEB / "about.js")]

EXAMPLE = """# Local test server

This server contains the [official pre-built](https://sylph-docs.github.io/pre%E2%80%90built-databases/)
gtdb-r232 database, updated **weekly**, with 199,923 prokaryotic species.

- gtdb-r232
- no eukaryotes

Run `sylphd serve --about about.md` for your own text.
"""

PAYLOADS = [
    "<script>alert(1)</script>", "<img src=x onerror=alert(1)>", "<svg onload=alert(1)>",
    "<iframe src=javascript:alert(1)></iframe>", "<style>*{background:url(javascript:alert(1))}</style>",
    "[x](javascript:alert(1))", "[x](JaVaScRiPt:alert(1))",
    "[x](data:text/html,<script>alert(1)</script>)", "[x](//evil.example/x)", "<javascript:alert(1)>",
    '<a href="javascript:alert(1)" onclick="alert(1)">x</a>',
    '<p onmouseover="alert(1)" style="color:red">hover</p>',
]


def _playwright_dir() -> str | None:
    for candidate in (os.environ.get("PLAYWRIGHT_DIR"), str(Path(__file__).parent / "node_modules")):
        if candidate and (Path(candidate) / "playwright-core" / "package.json").exists():
            return candidate
    return None


def _chromium() -> str | None:
    candidates = [os.environ.get("BROWSER_EXE"), "/usr/bin/chromium-browser", "/usr/bin/chromium",
                  *glob.glob(str(Path.home() / ".cache/ms-playwright/chromium-*/chrome-linux*/chrome"))]
    return next((c for c in candidates if c and Path(c).exists()), None)


PLAYWRIGHT, CHROMIUM = _playwright_dir(), _chromium()
MISSING = None if (shutil.which("node") and PLAYWRIGHT and CHROMIUM) else (
    "needs node, playwright-core (`npm ci` in tests/, or PLAYWRIGHT_DIR) and Chromium "
    "(`tests/node_modules/.bin/playwright-core install chromium`, or BROWSER_EXE)")


@pytest.fixture(autouse=True, scope="module")
def _require_browser() -> None:
    # a skipped XSS test must not pass unnoticed in CI
    if MISSING:
        (pytest.fail if os.environ.get("CI") else pytest.skip)(MISSING)


NODE = r"""
const pw = require('node:module').createRequire(process.env.PLAYWRIGHT_DIR + '/')('playwright-core');
(async () => {
  const cases = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
  const browser = await pw.chromium.launch({ headless: true, executablePath: process.env.BROWSER_EXE });
  const page = await browser.newPage();
  await page.setContent('<!doctype html><div id="out"></div>');
  for (const file of JSON.parse(process.argv[1])) await page.addScriptTag({ path: file });
  const results = await page.evaluate(async (cases) => {
    window.__pwned = 0;
    window.alert = window.prompt = window.confirm = () => { window.__pwned++; };
    const host = document.getElementById('out');
    const out = [];
    for (const src of cases) {
      host.replaceChildren(window.SylphdAbout.render(src)); // live document: anything executable runs now
      const tags = new Set(); const attrs = []; const links = [];
      const walk = (n) => { for (const c of n.children) { tags.add(c.tagName.toLowerCase());
          for (const a of c.attributes) attrs.push(c.tagName.toLowerCase() + ':' + a.name);
          if (c.tagName === 'A') links.push({ href: c.getAttribute('href'), target: c.getAttribute('target'), rel: c.getAttribute('rel') });
          walk(c); } };
      walk(host);
      out.push({ html: host.innerHTML, text: host.textContent, tags: [...tags], attrs, links });
    }
    await new Promise((r) => setTimeout(r, 200));
    return { out, pwned: window.__pwned };
  }, cases);
  console.log(JSON.stringify(results));
  await browser.close();
})();
"""


@pytest.fixture(scope="module")
def browser_run():
    env = {**os.environ, "PLAYWRIGHT_DIR": PLAYWRIGHT or "", "BROWSER_EXE": CHROMIUM or ""}
    done = subprocess.run(["node", "-e", NODE, json.dumps(LIBS)], input=json.dumps([EXAMPLE, *PAYLOADS]),
                          capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stderr
    data = json.loads(done.stdout)
    return {"example": data["out"][0], "payloads": data["out"][1:], "pwned": data["pwned"]}


def check_inert(result: dict) -> None:
    assert set(result["tags"]) <= ALLOWED_TAGS, result["tags"]
    for attr in result["attrs"]:
        tag, name = attr.split(":")
        assert not name.startswith("on") and name != "style", attr
        assert name in {"href", "title", "target", "rel"}, attr
        assert tag == "a" or name != "href", attr
    for link in result["links"]:
        if link["href"] is not None:
            assert link["href"].lower().startswith(("http://", "https://", "mailto:")), link["href"]
            assert link["target"] == "_blank" and link["rel"] == "noopener noreferrer nofollow ugc"


def test_operator_example(browser_run) -> None:
    r = browser_run["example"]
    assert {"p", "a", "strong", "ul", "li", "h1", "code"} <= set(r["tags"]), r["tags"]
    assert r["links"] == [{"href": "https://sylph-docs.github.io/pre%E2%80%90built-databases/",
                           "target": "_blank", "rel": "noopener noreferrer nofollow ugc"}]
    assert "199,923 prokaryotic species" in r["text"] and r["text"].startswith("Local test server")
    assert "<h1>Local test server</h1>" in r["html"] and "<li>gtdb-r232</li>" in r["html"]
    check_inert(r)


@pytest.mark.parametrize("index", range(len(PAYLOADS)))
def test_xss_payloads_are_inert(browser_run, index: int) -> None:
    assert browser_run["pwned"] == 0
    check_inert(browser_run["payloads"][index])
