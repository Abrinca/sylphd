"""The Reads mode sends the reads and bases it counted while sketching.

``params.samples`` carries ``{"name", "reads", "bases"}`` of the sample: the browser's own
counts for FASTQ input, ``null`` for a ready-made ``.sylsp`` (it knows no reads).  Runs the
real page in headless Chromium with ``/info`` and ``/profile`` answered by Playwright; skipped
under the same conditions as ``test_web_reads_taxprof.py``.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import dnaio
import pysylph
import pytest
from conftest import CLODF13_FASTA, K12_R1
from test_web_reads_sources import API, INFO, PROFILE
from test_web_reads_taxprof import CHROMIUM, MISSING, PLAYWRIGHT, WEB, _free_port


@pytest.fixture(autouse=True, scope="module")
def _require_browser() -> None:
    if MISSING:
        (pytest.fail if os.environ.get("CI") else pytest.skip)(MISSING)


@pytest.fixture(scope="module")
def web_origin():
    port = _free_port()
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(WEB))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


NODE = r"""
const pw = require('node:module').createRequire(process.env.PLAYWRIGHT_DIR + '/')('playwright-core');
(async () => {
  const [url, api, inputPath, info, profile] = JSON.parse(process.argv[1]);
  const browser = await pw.chromium.launch({ headless: true, executablePath: process.env.BROWSER_EXE });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  let requested = '';
  await page.route(api + '/**', (route) => {
    const path = new URL(route.request().url()).pathname;
    let body = path === '/info' ? info : {};
    if (path === '/profile') { requested = route.request().postData() || ''; body = profile; }
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
  });
  await page.goto(url);
  await page.waitForSelector('#r-file', { state: 'attached' });
  await page.setInputFiles('#r-file', inputPath);
  await page.waitForFunction(() => !document.getElementById('r-start').disabled, null, { timeout: 20000 });
  await page.click('#r-start');
  for (let i = 0; i < 300 && !requested; i++) await page.waitForTimeout(100);
  console.log(JSON.stringify({ errors, params: (requested.match(/\{"names".*\}/) || [''])[0] }));
  await browser.close();
})();
"""


def _sent_params(web_origin: str, path: Path) -> dict:
    env = {**os.environ, "PLAYWRIGHT_DIR": PLAYWRIGHT or "", "BROWSER_EXE": CHROMIUM or ""}
    args = [f"{web_origin}/index.html?api={API}#reads", API, str(path), INFO, PROFILE]
    done = subprocess.run(["node", "-e", NODE, json.dumps(args)], check=False,
                          capture_output=True, text=True, env=env, timeout=180)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result["errors"] == []
    return json.loads(result["params"])


def test_fastq_reads_and_bases_are_sent(web_origin: str, tmp_path: Path) -> None:
    with dnaio.open(K12_R1) as reader:
        records = [record.sequence for record in reader]
    params = _sent_params(web_origin, K12_R1)
    (sample,) = params["samples"]
    assert sample == {
        "name": params["names"][0],
        "reads": len(records),
        "bases": sum(len(sequence) for sequence in records),
    }


def test_a_sylsp_sends_unknown_counts(web_origin: str, tmp_path: Path) -> None:
    with dnaio.open(CLODF13_FASTA) as reader:
        sequences = [record.sequence for record in reader]
    path = tmp_path / "sample.sylsp"
    path.write_bytes(pysylph.Sketcher(c=200, k=31).sketch_single("sample", sequences).to_bytes())
    params = _sent_params(web_origin, path)
    assert params["samples"] == [{"name": params["names"][0], "reads": None, "bases": None}]
