"""The Reads tab renders the server's taxonomic profile (no client aggregation).

Runs the real page against a real local server in headless Chromium: a
ready-made ``.sylsp`` is uploaded, ``/profile`` returns ``taxprof``, and the
abundance chart, the rank selector and the ``.sylphmpa`` download come from
those server clades.  Skipped when node, playwright-core or Chromium is
missing (the same conditions as ``test_web_about_browser.py``); a failure in
CI makes that visible instead of silently skipping.
"""

from __future__ import annotations

import functools
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import dnaio
import pysylph
import pytest
from conftest import CLODF13_FASTA

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


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
    if MISSING:
        (pytest.fail if os.environ.get("CI") else pytest.skip)(MISSING)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for(url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"{url} did not answer in {timeout} s")


@pytest.fixture(scope="module")
def page_server(tiny_database: Path, tiny_metadata: Path, tmp_path_factory):
    """A static server for ``web/`` plus a sylphd server with metadata."""
    api_port = _free_port()
    web_port = _free_port()
    origin = f"http://127.0.0.1:{web_port}"
    process = subprocess.Popen(
        [
            sys.executable, "-m", "sylphd", "serve",
            "--db", str(tiny_database),
            "--meta", str(tiny_metadata),
            "--tcp", f"127.0.0.1:{api_port}",
            "--cors-origin", origin,
            "--log-level", "warning",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(WEB))
    httpd = ThreadingHTTPServer(("127.0.0.1", web_port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        _wait_for(f"http://127.0.0.1:{api_port}/info")
        yield origin, f"http://127.0.0.1:{api_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture(scope="module")
def sketch_file(tmp_path_factory) -> Path:
    directory = tmp_path_factory.mktemp("reads-taxprof")
    with dnaio.open(CLODF13_FASTA) as reader:
        sequences = [record.sequence for record in reader]
    sketch = pysylph.Sketcher(c=200, k=31).sketch_single("clodf13", sequences)
    path = directory / "clodf13.sylsp"
    path.write_bytes(sketch.to_bytes())
    return path


NODE = r"""
const pw = require('node:module').createRequire(process.env.PLAYWRIGHT_DIR + '/')('playwright-core');
(async () => {
  const [url, sketchPath] = JSON.parse(process.argv[1]);
  const browser = await pw.chromium.launch({ headless: true, executablePath: process.env.BROWSER_EXE });
  const page = await browser.newPage({ acceptDownloads: true });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.goto(url);
  await page.waitForFunction(() => window.Sylphd && window.Sylphd.info(), null, { timeout: 20000 });
  await page.waitForSelector('#r-file', { state: 'attached' });
  await page.setInputFiles('#r-file', sketchPath);
  await page.waitForFunction(() => !document.getElementById('r-start').disabled, null, { timeout: 20000 });
  await page.click('#r-start');
  await page.waitForSelector('.abund-controls', { timeout: 60000 });
  // the server's clades: the fixture taxonomy's deepest rank is class
  await page.click('input[name="r-rank"][value="c"]');
  await page.waitForFunction(() => [...document.querySelectorAll('.alist .aname')].some((n) => n.textContent.includes('Test')));
  const rendered = await page.evaluate(() => ({
    names: [...document.querySelectorAll('.alist .aname')].map((n) => n.textContent),
    head: document.querySelector('.abund-head').textContent,
    taxonomies: [...document.querySelectorAll('input[name="r-tax"]')].map((i) => i.value),
  }));
  const { names, head, taxonomies } = rendered;
  // the metric switch still works on server clades
  await page.click('input[name="r-metric"][value="sequence"]');
  await page.waitForFunction(() => document.querySelector('.abund-head').textContent.includes('sequence abundance'));
  const sequenceHead = await page.textContent('.abund-head');
  await page.click('input[name="r-metric"][value="relative"]');
  const download = await Promise.all([
    page.waitForEvent('download'),
    page.click('section[aria-label="Abundance"] .export button'),
  ]).then(([d]) => d);
  const path = await download.path();
  const text = require('node:fs').readFileSync(path, 'utf8');
  const filename = download.suggestedFilename();
  // the "tax" profile (all sources) charts too
  await page.click('input[name="r-tax"][value="tax"]');
  await page.waitForFunction(() => document.querySelector('.abund-head').textContent.includes('All sources'));
  const taxHead = await page.textContent('.abund-head');
  const taxDownload = await Promise.all([
    page.waitForEvent('download'),
    page.click('section[aria-label="Abundance"] .export button'),
  ]).then(([d]) => d);
  const taxText = require('node:fs').readFileSync(await taxDownload.path(), 'utf8');
  console.log(JSON.stringify({
    names, head, taxonomies, text, filename, errors, sequenceHead,
    taxHead, taxText, taxFilename: taxDownload.suggestedFilename(),
  }));
  await browser.close();
})();
"""


def test_reads_tab_renders_server_clades(page_server, sketch_file: Path) -> None:
    origin, api = page_server
    env = {**os.environ, "PLAYWRIGHT_DIR": PLAYWRIGHT or "", "BROWSER_EXE": CHROMIUM or ""}
    done = subprocess.run(
        ["node", "-e", NODE, json.dumps([f"{origin}/index.html?api={api}#reads", str(sketch_file)])],
        check=False, capture_output=True, text=True, env=env, timeout=180,
    )
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result["errors"] == []
    # gtdb, plus the "tax" profile of the metadata file's sources (here the
    # virtual GTDB source, both of which the server can produce)
    assert result["taxonomies"] == ["gtdb", "tax"]
    assert any("Test" in name for name in result["names"]), result["names"]
    assert "taxonomic abundance" in result["head"]
    assert "sequence abundance" in result["sequenceHead"]
    assert result["text"].startswith("#SampleID\tclodf13\tTaxonomies_used:['gtdb']\n")
    assert "d__Bacteria" in result["text"]
    assert result["filename"] == "clodf13.gtdb.sylphmpa"
    # "All sources": the same clades under the sylph-tax rule, still charted
    assert "All sources" in result["taxHead"]
    assert result["taxText"].startswith("#SampleID\tclodf13\tTaxonomies_used:['GTDB_r232']\n")
    assert result["taxFilename"] == "clodf13.tax.sylphmpa"
