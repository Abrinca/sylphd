"""The abundance chart keeps taxonomy sources apart.

A server can profile GTDB together with other catalogs (a gut virome, RefSeq
fungi).  Each sylph-tax source is a taxonomy of its own in the chart
(``tax:<source>``, charted by the ranks its lineages name), and in every view
the hits of the other sources are one segment per source, named after their
``tax_source`` (nothing is hard-coded), so only the real remainder is hatched.
The merged "All sources" profile is no longer charted but stays downloadable.

Runs the real page in headless Chromium with ``/info`` and ``/profile``
answered by Playwright from a small synthetic profile, so no sylph database is
needed.  Skipped when node, playwright-core or Chromium is missing (the same
conditions as ``test_web_reads_taxprof.py``).
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
from conftest import CLODF13_FASTA
from sylphd.taxonomy import build_taxprof
from test_web_reads_taxprof import CHROMIUM, MISSING, PLAYWRIGHT, WEB, _free_port

API = "http://127.0.0.1:9"  # never contacted: every request to it is routed

GTDB_A = "d__Bacteria;p__Bacillota;c__Bacilli;o__Lactobacillales;f__Lactobacillaceae;g__Lactobacillus;s__Lactobacillus alpha"
GTDB_B = "d__Bacteria;p__Bacteroidota;c__Bacteroidia;o__Bacteroidales;f__Bacteroidaceae;g__Bacteroides;s__Bacteroides beta"
NCBI_A = "d__Bacteria;p__Bacillota;c__Bacilli;o__Lactobacillales;f__Lactobacillaceae;g__Lactobacillus;s__Lactobacillus alpha"

# (gtdb lineage, ncbi lineage, tax_source, own lineage, taxonomic %, sequence %)
ROWS = [
    (GTDB_A, NCBI_A, "GTDB_r232", GTDB_A, 20.0, 60.0),
    (GTDB_B, None, "GTDB_r232", GTDB_B, 10.0, 30.0),  # a GTDB genome without NCBI lineage
    (None, None, "UHGV_default", "vFAM-1;vSUBFAM-1;vOTU-1", 65.0, 8.0),
    (None, None, "FungiRefSeq-latest", "k__Fungi;p__Ascomycota;s__Candida gamma", 5.0, 2.0),
]


def _clades(lineages: list[tuple[str | None, float, float]]) -> list[dict]:
    """Every prefix of every lineage, summed, like the server's taxprof clades."""
    sums: dict[str, list[float]] = {}
    for lineage, tax, seq in lineages:
        tokens = lineage.split(";") if lineage else ["unclassified"]
        for depth in range(1, len(tokens) + 1):
            entry = sums.setdefault("|".join(tokens[:depth]), [0.0, 0.0])
            entry[0] += tax
            entry[1] += seq
    return [
        {"clade_name": name, "relative_abundance": tax, "sequence_abundance": seq, "ani": None, "coverage": None}
        for name, (tax, seq) in sums.items()
    ]


def _taxprof(index: int, taxonomy: str, scale: float = 1.0) -> dict:
    lineages = [(row[index], row[4], row[5] * scale) for row in ROWS]
    return {"columns": [], "taxonomies_used": [taxonomy], "clades": _clades(lineages), "sylphmpa": "#SampleID\tsample\n"}


SOURCES = ["GTDB_r232", "UHGV_default", "FungiRefSeq-latest"]

def _profile(scale: float = 1.0) -> list[dict]:
    """The /profile answer; ``scale`` < 1 is sylph's --estimate-unknown, whose sequence
    abundances are shares of all reads (the rest are in no database)."""
    profile = [{
        "name": "sample",
        "kind": "reads",
        "params": {"estimate_unknown": scale != 1.0, "estimate_read_counts": False, "min_number_kmers": 10},
        "rows": [
            {
                "Sample_file": "sample", "Genome_file": f"genome{i}.fna.gz", "Adjusted_ANI": 98.0, "Eff_cov": 1.0,
                "Taxonomic_abundance": tax, "Sequence_abundance": seq * scale, "Contig_name": f"contig{i}",
                "gtdb_taxonomy": gtdb, "ncbi_taxonomy": ncbi, "tax_source": source, "tax_lineage": own,
            }
            for i, (gtdb, ncbi, source, own, tax, seq) in enumerate(ROWS)
        ],
        "taxprof": {name: _taxprof(index, name, scale) for index, name in ((0, "gtdb"), (1, "ncbi"), (3, "tax"))},
    }]
    # The one-source profiles come from the server's own code, as /profile returns them.
    profile[0]["taxprof"].update(
        build_taxprof("sample", [f"tax:{name}" for name in SOURCES], profile[0]["rows"])
    )
    return profile


PROFILE = _profile()

INFO = {
    "service": "sylphd", "version": "0.0.0", "name": "Test",
    "database": {"name": "gtdb, uhgv, fungi", "genomes": 4, "c": 200, "k": 31, "two_stage": False},
    "databases": [{"name": "all", "file": "all.syldb", "kind": "plain", "genomes": 4, "c": 200, "k": 31, "gtdb": True}],
    "metadata": {
        "columns": ["gtdb_taxonomy", "ncbi_taxonomy", "tax_source", "tax_lineage"],
        "taxonomy_sources": [
            {"name": "GTDB_r232", "kind": "metadata"},
            {"name": "UHGV_default", "kind": "sylphtax"},
            {"name": "FungiRefSeq-latest", "kind": "sylphtax"},
        ],
        "taxonomy_overlaps": [],
        "taxonomies": ["gtdb", "ncbi", "tax", *(f"tax:{name}" for name in SOURCES)],
    },
    "limits": {"max_jobs": 1, "max_upload_mb": 200, "max_sketches": 10, "max_queue": 4},
    "usage": {"enabled": False},
    "endpoints": ["/health", "/info", "/profile"],
}


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


@pytest.fixture(scope="module")
def sketch_file(tmp_path_factory) -> Path:
    with dnaio.open(CLODF13_FASTA) as reader:
        sequences = [record.sequence for record in reader]
    path = tmp_path_factory.mktemp("reads-sources") / "sample.sylsp"
    path.write_bytes(pysylph.Sketcher(c=200, k=31).sketch_single("sample", sequences).to_bytes())
    return path


NODE = r"""
const pw = require('node:module').createRequire(process.env.PLAYWRIGHT_DIR + '/')('playwright-core');
(async () => {
  const [url, api, sketchPath, info, profile] = JSON.parse(process.argv[1]);
  const browser = await pw.chromium.launch({ headless: true, executablePath: process.env.BROWSER_EXE });
  const page = await browser.newPage({ acceptDownloads: true });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  let requested = '';
  await page.route(api + '/**', (route) => {
    const path = new URL(route.request().url()).pathname;
    let body = path === '/info' ? info : {};
    if (path === '/profile') {
      // like the server: only the requested taxonomies come back
      requested = route.request().postData() || '';
      const asked = JSON.parse((requested.match(/\{"names".*\}/) || ['{}'])[0]).taxprof || [];
      body = profile.map((sample) => ({ ...sample, taxprof: Object.fromEntries(asked.map((t) => [t, sample.taxprof[t]])) }));
    }
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
  });
  await page.goto(url);
  await page.waitForSelector('#r-file', { state: 'attached' });
  await page.setInputFiles('#r-file', sketchPath);
  await page.waitForFunction(() => !document.getElementById('r-start').disabled, null, { timeout: 20000 });
  await page.click('#r-start');
  await page.waitForSelector('section[aria-label="Abundance"] .alist', { timeout: 30000 });
  const card = 'section[aria-label="Abundance"]';
  const snap = () => page.evaluate((card) => ({
    rows: [...document.querySelectorAll(`${card} .alist .arow`)].map((r) => [r.querySelector('.aname').textContent, r.querySelector('.sw').className]),
    values: Object.fromEntries([...document.querySelectorAll(`${card} .alist .arow`)].map((r) => [r.querySelector('.aname').textContent, r.textContent])),
    segs: [...document.querySelectorAll(`${card} .stack > span`)].map((s) => s.className),
    ranks: [...document.querySelectorAll(`${card} input[name="r-rank"]`)].map((i) => i.value),
    taxonomies: [...document.querySelectorAll(`${card} input[name="r-tax"]`)].map((i) => i.value),
    downloads: [...document.querySelectorAll(`${card} .export button`)].filter((b) => !b.hidden).map((b) => b.textContent),
  }), card);
  const gtdbSeq = await snap();
  await page.click('input[name="r-metric"][value="relative"]');
  const gtdbTax = await snap();
  await page.click('input[name="r-tax"][value="ncbi"]');
  const ncbiTax = await snap();
  await page.click('input[name="r-tax"][value="tax:UHGV_default"]');
  const uhgv = await snap();
  await page.click('input[name="r-rank"][value="vSUBFAM"]');
  const uhgvSubfam = await snap();
  const params = (requested.match(/\{"names".*\}/) || [''])[0];
  // the sunburst and treemap: every source in one chart (ECharts, loaded on first use)
  await page.click('input[name="r-metric"][value="sequence"]');
  const charts = {};
  for (const kind of ['sunburst', 'treemap']) {
    await page.click(`input[name="r-chart"][value="${kind}"]`);
    await page.waitForSelector(`${card} .taxtree-${kind} svg`, { timeout: 30000 });
    charts[kind] = await page.evaluate((card) => {
      const chart = window.echarts.getInstanceByDom(document.querySelector(`${card} .taxtree`));
      const series = chart.getOption().series[0];
      const names = (nodes) => nodes.map((n) => [n.name, Math.round(n.value * 100) / 100, n.children ? names(n.children) : null]);
      return {
        type: series.type,
        data: names(series.data),
        hidden: ['r-tax', 'r-rank'].map((name) => document.querySelector(`${card} input[name="${name}"]`).closest('.ctl').hidden),
        legend: [...document.querySelectorAll(`${card} .alist .arow`)].map((r) => r.querySelector('.aname').textContent),
        downloads: [...document.querySelectorAll(`${card} .export button`)].filter((b) => !b.hidden).map((b) => b.textContent),
        reset: !!document.querySelector(`${card} .taxtree-reset`),
      };
    }, card);
  }
  await page.click('input[name="r-chart"][value="bars"]');
  const backToBars = await snap();
  // "Unknown reads: Hide" leaves the unknown out and rescales the rest (only offered when there is one)
  const unknownCtl = await page.evaluate((card) => !document.querySelector(`${card} input[name="r-unknown-view"]`).closest('.ctl').hidden, card);
  let hidden = null;
  if (unknownCtl) {
    await page.click('input[name="r-unknown-view"][value="hide"]');
    const bars = await snap();
    const head = await page.textContent(`${card} .abund-head`);
    await page.click('input[name="r-chart"][value="treemap"]');
    await page.waitForSelector(`${card} .taxtree-treemap svg`, { timeout: 30000 });
    const tree = await page.evaluate((card) => window.echarts.getInstanceByDom(document.querySelector(`${card} .taxtree`))
      .getOption().series[0].data.map((n) => [n.name, Math.round(n.value * 100) / 100]), card);
    const roam = await page.evaluate((card) => window.echarts.getInstanceByDom(document.querySelector(`${card} .taxtree`)).getOption().series[0].roam, card);
    // right click on the largest box (GTDB, the left part): a panel with details and a GTDB link
    await page.waitForTimeout(800);
    await page.locator(`${card} .taxtree`).scrollIntoViewIfNeeded();
    const box = await page.locator(`${card} .taxtree`).boundingBox();
    await page.mouse.click(box.x + box.width * 0.3, box.y + box.height * 0.5, { button: 'right' });
    const menu = await page.evaluate((card) => {
      const m = document.querySelector(`${card} .taxtree-menu`);
      return { hidden: m.hidden, text: m.innerText, links: [...m.querySelectorAll('a')].map((a) => a.href) };
    }, card);
    await page.keyboard.press('Escape');
    const menuAfterEsc = await page.evaluate((card) => document.querySelector(`${card} .taxtree-menu`).hidden, card);
    // the chart as SVG
    const [svgDownload] = await Promise.all([
      page.waitForEvent('download'),
      page.locator(`${card} .export button`, { hasText: 'SVG' }).click(),
    ]);
    const svg = require('node:fs').readFileSync(await svgDownload.path(), 'utf8');
    hidden = { bars, head, tree, roam, menu, menuAfterEsc, svg: { name: svgDownload.suggestedFilename(), start: svg.slice(0, 4), lactobacillus: svg.includes('Lactobacillus') } };
  }
  console.log(JSON.stringify({ errors, gtdbSeq, gtdbTax, ncbiTax, uhgv, uhgvSubfam, params, charts, backToBars, unknownCtl, hidden }));
  await browser.close();
})();
"""


def test_other_sources_get_their_own_segments(web_origin: str, sketch_file: Path) -> None:
    env = {**os.environ, "PLAYWRIGHT_DIR": PLAYWRIGHT or "", "BROWSER_EXE": CHROMIUM or ""}
    args = [f"{web_origin}/index.html?api={API}#reads", API, str(sketch_file), INFO, PROFILE]
    done = subprocess.run(["node", "-e", NODE, json.dumps(args)], check=False, capture_output=True,
                          text=True, env=env, timeout=180)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result["errors"] == []

    # GTDB: both catalogs are named segments (largest first), nothing is hatched
    for view in ("gtdbSeq", "gtdbTax"):
        rows = dict(result[view]["rows"])
        assert rows["UHGV_default (no GTDB lineage)"] == "sw seg-src-1", (view, rows)
        assert "FungiRefSeq-latest (no GTDB lineage)" in rows
        assert not any(name.startswith("Not classified") for name in rows), rows
        assert "seg-cat-none" not in result[view]["segs"]
        assert sum(cls.startswith("seg-src-") for cls in result[view]["segs"]) == 2
    # sequence abundance is the default, so the bacteria lead there; by genome copies the phage does
    assert result["gtdbSeq"]["rows"][0][0] == "Lactobacillus alpha"
    assert [name for name, _ in result["gtdbTax"]["rows"]][-2:] == ["UHGV_default (no GTDB lineage)", "FungiRefSeq-latest (no GTDB lineage)"]

    # NCBI: the GTDB genome without an NCBI lineage is its source's segment too
    ncbi = dict(result["ncbiTax"]["rows"])
    assert {"UHGV_default (no NCBI lineage)", "GTDB_r232 (no NCBI lineage)", "FungiRefSeq-latest (no NCBI lineage)"} <= set(ncbi)

    # the page asks for one profile per source, but not for the metadata file's
    # own source (that is the GTDB/NCBI view); "All sources" is downloadable, not charted
    # the unknown estimate is asked for by default (the dark matter is shown when there is any)
    assert json.loads(result["params"])["estimate_unknown"] is True
    taxprof = json.loads(result["params"])["taxprof"]
    assert taxprof == ["gtdb", "ncbi", "tax", "tax:UHGV_default", "tax:FungiRefSeq-latest"]
    assert result["gtdbSeq"]["taxonomies"] == ["gtdb", "ncbi", "tax:UHGV_default", "tax:FungiRefSeq-latest"]
    assert result["gtdbSeq"]["downloads"] == ["Download sylphmpa (GTDB)", "Download sylphmpa (All sources)"]
    assert result["gtdbSeq"]["ranks"] == ["s", "g", "f", "o", "c", "p", "d"]

    # UHGV: its own ranks (the rank control follows the taxonomy), its vOTU,
    # and the other sources as blocks
    uhgv = dict(result["uhgv"]["rows"])
    assert result["uhgv"]["ranks"] == ["vOTU", "vSUBFAM", "vFAM"]
    assert "vOTU-1" in uhgv
    assert {"GTDB_r232 (no UHGV_default lineage)", "FungiRefSeq-latest (no UHGV_default lineage)"} <= set(uhgv)
    assert not any(name.startswith("Not classified") for name in uhgv)
    assert result["uhgv"]["downloads"][0] == "Download sylphmpa (UHGV_default)"
    assert "vSUBFAM-1" in dict(result["uhgvSubfam"]["rows"])


def test_reads_in_no_database_are_shown(web_origin: str, sketch_file: Path) -> None:
    """With --estimate-unknown, sequence abundance is a share of all reads: the rest
    (here 80 %) is the "Unknown" segment of the sequence view, not of the taxonomic one."""
    env = {**os.environ, "PLAYWRIGHT_DIR": PLAYWRIGHT or "", "BROWSER_EXE": CHROMIUM or ""}
    args = [f"{web_origin}/index.html?api={API}#reads", API, str(sketch_file), INFO, _profile(0.2)]
    done = subprocess.run(["node", "-e", NODE, json.dumps(args)], check=False, capture_output=True,
                          text=True, env=env, timeout=180)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result["errors"] == []
    label = "Unknown: in no database (estimated)"
    rows = dict(result["gtdbSeq"]["rows"])
    assert rows[label] == "sw seg-cat-unknown"
    assert "80" in result["gtdbSeq"]["values"][label]
    assert result["gtdbSeq"]["segs"][-1] == "seg-cat-unknown"
    # the bacteria keep their share of all reads (60 % of the 20 % found)
    assert "12" in result["gtdbSeq"]["values"]["Lactobacillus alpha"]
    assert not any(name.startswith("Not classified") for name in rows), rows
    # taxonomic abundance is a share of the genomes found: no unknown there
    assert label not in dict(result["gtdbTax"]["rows"])
    assert label not in dict(result["uhgv"]["rows"])  # still the taxonomic measure

    # the sunburst and treemap: one bin per taxonomy source, subdivided by its own ranks
    # (single-child chains merged, strain rows left out), then the reads in no database
    for kind in ("sunburst", "treemap"):
        chart = result["charts"][kind]
        assert chart["type"] == kind
        assert chart["hidden"] == [True, True]  # every source at every rank: no taxonomy or rank control
        assert chart["downloads"] == ["Download sylphmpa (All sources)", "Download SVG"]
        assert chart["legend"] == ["GTDB_r232", "UHGV_default", "FungiRefSeq-latest", label]
        gtdb, uhgv_bin, fungi, unknown = chart["data"]
        assert gtdb[0] == "GTDB_r232 › Bacteria" and gtdb[1] == pytest.approx(18.0)
        assert [child[0] for child in gtdb[2]] == ["Bacillota › Bacilli › Lactobacillales › Lactobacillaceae › Lactobacillus › Lactobacillus alpha",
                                                   "Bacteroidota › Bacteroidia › Bacteroidales › Bacteroidaceae › Bacteroides › Bacteroides beta"]
        assert uhgv_bin[:2] == ["UHGV_default › vFAM-1 › vSUBFAM-1 › vOTU-1", pytest.approx(1.6)]
        assert fungi[0] == "FungiRefSeq-latest › Fungi › Ascomycota › Candida gamma"
        assert unknown[:2] == [label, pytest.approx(80.0)]
    # the treemap pans and zooms freely, so it has a way back; the sunburst zooms out via its centre
    assert result["charts"]["treemap"]["reset"] is True
    assert result["charts"]["sunburst"]["reset"] is False
    # back to the bars, with their controls
    assert dict(result["backToBars"]["rows"])[label] == "sw seg-cat-unknown"

    # hiding the unknown: gone from every chart, the rest rescaled to the reads found (20 % -> 100 %)
    assert result["unknownCtl"] is True
    hidden = result["hidden"]
    assert label not in dict(hidden["bars"]["rows"])
    assert "90" in hidden["bars"]["values"]["GTDB_r232 (no UHGV_default lineage)"]  # the bars are on the UHGV view here
    assert "without the 80.0 % unknown" in hidden["head"]
    assert [name for name, _ in hidden["tree"]] == ["GTDB_r232 › Bacteria", "UHGV_default › vFAM-1 › vSUBFAM-1 › vOTU-1", "FungiRefSeq-latest › Fungi › Ascomycota › Candida gamma"]
    assert hidden["tree"][0][1] == pytest.approx(90.0)
    assert hidden["roam"] is True  # drag and wheel zoom

    # right click: the clade's details and a GTDB link; Escape closes the panel
    menu = hidden["menu"]
    assert menu["hidden"] is False
    assert "GTDB_r232" in menu["text"] and "%" in menu["text"]
    assert menu["links"] and menu["links"][0].startswith("https://gtdb.ecogenomic.org/tree?r=")
    assert hidden["menuAfterEsc"] is True
    # the chart downloads as SVG (ECharts' SVG renderer)
    assert hidden["svg"]["name"].endswith(".treemap.svg")
    assert hidden["svg"]["start"] == "<svg" and hidden["svg"]["lactobacillus"]
