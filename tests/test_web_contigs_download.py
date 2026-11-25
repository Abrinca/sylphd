"""The web app's downloads and summaries come from ``sylphd-core`` through wasm.

Since step 3 of the thin-client plan the browser has no TSV/JSON/summary
writers of its own: ``web/reads.js``, ``web/app.js`` and ``web/contigs.js``
call the renderer and the per-contig merge exposed by ``sylphd-wasm``
(``rust/sylphd-core/src/document.rs`` and ``render.rs``). This test runs the
built wasm module under node and compares the browser's outputs with the
Python implementation, which is the specification:

* the per-contig merge (``merge_contigs_run``) against
  ``per_contig_client.PerContigRun``, byte for byte for the TSV and value for
  value for the counters, consensus breakdown and contig order;
* the renderer exports against the golden documents in
  ``rust/sylphd-core/tests/data/``, whose expected strings were produced by
  the Python server and renderers;
* the ``api_version`` check (the exact "'update sylphd' error").

``web/contigs.js`` still owns the server-limit helpers, which are checked
through its ``module.exports``. The wasm is gitignored, so the tests skip when
it has not been built (``rust/sylphd-wasm/build.sh``).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from sylphd import per_contig, per_contig_client

ROOT = Path(__file__).resolve().parents[1]
CONTIGS_JS = ROOT / "web" / "contigs.js"
WASM = ROOT / "web" / "wasm" / "sylph_sketch.js"
RENDER_CHECK = ROOT / "rust" / "sylphd-wasm" / "tests" / "render_check.mjs"
GOLDEN = ROOT / "rust" / "sylphd-core" / "tests" / "data"


@pytest.fixture(autouse=True)
def _require_node_and_wasm() -> None:
    if shutil.which("node") is None:
        (pytest.fail if os.environ.get("CI") else pytest.skip)("node is not installed")
    if not WASM.exists():
        pytest.skip("web/wasm is not built (run rust/sylphd-wasm/build.sh)")


FIELDS = ("gtdb_taxonomy", "ncbi_organism_name", "checkm2_completeness", "tax_host")
DATABASE = {"name": "fixture.syldb", "genomes": 10, "c": 200, "k": 31}
THRESHOLDS = {"species": 95.0, "imprecise_below": 85.0}


def _hit(rank: int, ani: float, *, accession: str | None, taxon: str | None, mixed: bool = False) -> dict:
    return {
        "rank": rank,
        "ani": ani,
        "shared_kmers": 78,
        "contig_kmers": 84,
        "explained_fraction": 0.928571,
        "reference_ani": 96.155,
        "reference_containment": [78, 8413],
        "relation": "same species" if rank == 1 else "same genus",
        "shared_rank": "species" if rank == 1 else "genus",
        "ani_species_level": rank == 1,
        "genome_file": "GCF_000005845.2_genomic.fna.gz",
        "genome_id": accession or "GCA_000000001.1",
        "accession": accession,
        "gtdb_species": "Escherichia coli" if not mixed else None,
        "taxon": taxon,
        "gtdb_taxonomy": "d__Bacteria;p__Pseudomonadota;c__Gammaproteobacteria;o__Enterobacterales;f__Enterobacteriaceae;g__Escherichia;s__Escherichia coli",
        "ncbi_organism_name": "Escherichia coli K-12",
        "checkm2_completeness": 99.5,
        "tax_host": None,
    }


RECORDS = [
    {
        "name": "contig1",
        "id": "contig1",
        "index": 1,
        "length": 1954431,
        "gc": 50.76,
        "status": "ok",
        "sketch_kmers": 8413,
        "contig_kmers": 8413,
        "hits": [
            _hit(1, 99.123, accession="GCA_000005845.2", taxon="Escherichia coli"),
            _hit(2, 90.5, accession=None, taxon="Escherichia sp002"),
        ],
        "assignment": {"rank": "species", "taxon": "Escherichia coli", "accession": "GCA_000005845.2", "ani": 99.123},
        "closest": {"rank": "species", "taxon": "Escherichia coli", "accession": "GCA_000005845.2", "ani": 99.123},
        "consensus": {"rank": "family", "taxon": "Enterobacteriaceae", "n_close": 2, "species": ["Escherichia coli", "Escherichia sp002"]},
    },
    {
        "name": "contig2",
        "id": "contig2",
        "index": 2,
        "length": 40000,
        "gc": 44.1,
        "status": "ok",
        "sketch_kmers": 190,
        "contig_kmers": 185,
        "hits": [_hit(1, 97.0, accession=None, taxon="Lambda", mixed=True)],
        "assignment": None,
        "closest": {"rank": None, "taxon": None, "accession": None, "ani": 97.0, "note": "95 % ANI matches the vOTU convention."},
        "consensus": {"rank": None, "taxon": None, "n_close": 2, "species": ["Lambda", "other"], "mixed_sources": True},
    },
    {
        "name": "contig7",
        "id": "contig7",
        "index": 3,
        "length": 2500,
        "gc": 44.1,
        "status": "too_short",
        "sketch_kmers": None,
        "contig_kmers": None,
        "hits": [],
        "assignment": None,
        "closest": None,
        "consensus": None,
    },
    {
        "name": "contig8",
        "id": "contig8",
        "index": 4,
        "length": 18000,
        "gc": 61.2,
        "status": "no_hit",
        "sketch_kmers": 4,
        "contig_kmers": 0,
        "reason": "too few k-mers (4 < 10)",
        "hits": [],
        "assignment": None,
        "closest": None,
        "consensus": None,
    },
    {
        "name": "contig9",
        "id": "contig9",
        "index": 5,
        "length": 30000,
        "gc": 55.0,
        "status": "no_hit",
        "sketch_kmers": 150,
        "contig_kmers": 146,
        "hits": [],
        "assignment": None,
        "closest": {"rank": None, "taxon": None, "accession": None, "ani": None},
        "consensus": None,
    },
]


def batch_document(records: list[dict], elapsed_ms: float) -> dict:
    """One ``POST /contigs`` response, built by the server's own helpers."""
    return {
        "api_version": 1,
        "params": {"top": 2, "min_ani": 80.0, "consensus_ratio": 1.5, "consensus_z": 2.0, "fields": list(FIELDS)},
        "table": per_contig.contigs_table(records, FIELDS),
        "contigs": per_contig.contigs_map(records),
        "summary": per_contig.summarize(records),
        "caveats": per_contig.caveats(records),
        "database": DATABASE,
        "thresholds": THRESHOLDS,
        "elapsed_ms": elapsed_ms,
    }


def run_wasm(payload: dict) -> dict:
    done = subprocess.run(
        ["node", str(RENDER_CHECK)],
        input=json.dumps(payload), capture_output=True, text=True, check=True, cwd=ROOT,
    )
    return json.loads(done.stdout)


def test_wasm_merge_and_tsv_match_the_cli() -> None:
    # Only contigs that were sent have records: records skipped for min_length
    # or too few k-mers never become documents, they are counts in the options.
    classified = [record for record in RECORDS if record["status"] in {"ok", "no_hit"} and "reason" not in record]
    batch = batch_document(classified, 12.5)
    options = per_contig_client.PerContigOptions(
        top=2,
        min_ani=80.0,
        min_length=3000,
        fields=FIELDS,
        consensus_ratio=1.5,
        consensus_z=2.0,
    )
    skipped_short = per_contig_client.Skipped(contigs=1, bp=2500)
    skipped_few_kmers = per_contig_client.Skipped(contigs=1, bp=18000)
    reference = per_contig_client.PerContigRun(
        classified,
        summaries=[batch["summary"]],
        options=options,
        params=per_contig.QUERY_PARAMS,
        database=DATABASE,
        skipped_short=skipped_short,
        skipped_few_kmers=skipped_few_kmers,
    )
    expected_tsv = io.StringIO()
    reference.write_tsv(expected_tsv)
    expected_summary = io.StringIO()
    reference.print_summary(expected_summary)

    got = run_wasm({
        "batches": [batch],
        "options": {
            "skipped_short": {"contigs": 1, "bp": 2500},
            "skipped_few_kmers": {"contigs": 1, "bp": 18000},
            "min_contig_length": 3000,
        },
    })["merged"]
    doc = got["doc"]

    # The per-contig TSV and the two stderr summary lines are byte-identical.
    assert got["tsv"] == expected_tsv.getvalue()
    assert got["summary"] == expected_summary.getvalue()

    # Counters, bucketed skips and the (top-5) consensus breakdown match the CLI.
    summary = doc["summary"]
    for key, value in reference.summary().items():
        assert summary[key] == value, key
    assert "not_classified" not in summary

    # One flat table, the union of the batch tables, with the requested fields.
    assert doc["table"] == per_contig.contigs_table(classified, FIELDS)
    assert doc["contigs"] == per_contig.contigs_map(classified)
    assert list(doc["contigs"]) == ["contig1", "contig2", "contig9"]
    assert doc["caveats"] == reference.caveats()
    assert doc["params"] == batch["params"]
    assert doc["database"] == DATABASE
    assert doc["thresholds"] == THRESHOLDS
    assert doc["api_version"] == 1


def test_skipped_records_have_no_rows_and_no_contig_entries() -> None:
    classified = [record for record in RECORDS if record["status"] == "ok"]
    got = run_wasm({
        "batches": [batch_document(classified, 1.0)],
        "options": {"fields": list(FIELDS), "min_contig_length": 3000},
    })["merged"]
    lines = got["tsv"].splitlines()
    columns = list(per_contig_client.COLUMNS) + list(FIELDS)
    assert lines[0].split("\t") == columns
    assert [line.split("\t")[0] for line in lines[1:]] == ["contig1", "contig1", "contig2"]
    assert "contig7" not in got["tsv"]
    assert "contig8" not in got["tsv"]
    assert set(got["doc"]["contigs"]) == {"contig1", "contig2"}


def test_browser_limit_is_an_extra_summary_field() -> None:
    """The browser's 100-contig cap is reported separately, not as a CLI bucket."""
    got = run_wasm({
        "batches": [],
        "options": {
            "fields": ["gtdb_taxonomy"],
            "min_contig_length": 20000,
            "skipped_short": {"contigs": 3, "bp": 30000},
            "skipped_few_kmers": {"contigs": 7, "bp": 20000},
            "not_classified": {"contigs": 140, "bp": 3500000, "limit": 100},
        },
    })["merged"]
    doc = got["doc"]
    assert doc["summary"]["contigs"] == 150
    assert doc["summary"]["bp"] == 3_550_000
    assert doc["summary"]["queried_contigs"] == 0
    assert doc["summary"]["not_classified"] == {"contigs": 140, "bp": 3_500_000, "limit": 100}
    assert doc["summary"]["skipped_short"] == {"contigs": 3, "bp": 30_000}
    assert doc["summary"]["skipped_few_kmers"] == {"contigs": 7, "bp": 20_000}
    assert doc["table"]["columns"] == [*per_contig_client.COLUMNS, "gtdb_taxonomy"]
    assert doc["table"]["data"] == []
    # With no batch, the run document still has the merge options' params/database.
    assert doc["params"] == {}
    assert doc["database"] == {}
    assert got["summary"].splitlines()[0].startswith("per-contig: 0 of 150 contig(s), 0 bp queried;")


def test_wasm_renderer_exports_match_the_python_goldens() -> None:
    """Profile/assembly/info exports through the JS glue, against the goldens."""
    profile_case = json.loads((GOLDEN / "profile_golden.json").read_text())["cases"][0]
    assembly_case = json.loads((GOLDEN / "assembly_golden.json").read_text())["cases"][0]
    info_case = json.loads((GOLDEN / "info_golden.json").read_text())["cases"][0]

    got = run_wasm({
        "profile": profile_case["document"],
        "assembly": assembly_case["document"],
        "info": info_case["info"],
        "messages": info_case["messages"],
    })

    assert got["supported_api_version"] == 1
    assert got["api_version_1"] == "ok"
    assert "server API version 2, this client supports 1: update sylphd" in got["api_version_2"]["error"]

    assert got["profile"]["tsv"] == profile_case["expected_tsv"]
    assert got["assembly"]["tsv"] == assembly_case["expected_tsv"]
    assert got["assembly"]["summary"] == assembly_case["expected_summary"]
    assert got["assembly"]["stderr"] == assembly_case["expected_stderr"]
    assert got["info"] == info_case["expected_text"]


NODE_CHUNK = r"""
const F = require(process.argv[1]);
console.log(JSON.stringify({
  configured: F.chunkSizeFor({ limits: { max_contigs: 2, max_sketches: 4096 } }),
  default: F.chunkSizeFor({}),
  cap: F.MAX_CLASSIFIED,
}));
"""


def test_the_web_batches_by_max_contigs_and_keeps_its_own_cap() -> None:
    """The browser follows ``limits.max_contigs`` but never sends over 100 contigs."""
    done = subprocess.run(
        ["node", "-e", NODE_CHUNK, str(CONTIGS_JS)],
        capture_output=True, text=True, check=True,
    )
    result = json.loads(done.stdout)
    assert result == {"configured": 2, "default": 4096, "cap": 100}
