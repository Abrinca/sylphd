"""The web app's downloads and summaries come from ``sylphd-core`` through wasm.

The browser has no TSV/JSON/summary
writers of its own: ``web/reads.js``, ``web/app.js`` and ``web/contigs.js``
call the renderer and the per-contig merge exposed by ``sylphd-wasm``
(``rust/sylphd-core/src/document.rs`` and ``render.rs``). This test runs the
built wasm module under node and compares the browser's outputs with:

* the per-contig merge (``merge_contigs_run``) against the server's own
  table and map (``sylphd.per_contig``) and fixed expected counters and
  consensus breakdown;
* the renderer exports against the golden documents in
  ``rust/sylphd-core/tests/data/``;
* the ``api_version`` check (the exact "'update sylphd' error").

``web/contigs.js`` still owns the server-limit helpers, which are checked
through its ``module.exports``. The wasm is gitignored, so the tests skip when
it has not been built (``rust/sylphd-wasm/build.sh``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from sylphd import per_contig

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

# The consensus breakdown of the one batch below, merged and cut to the top 5
# (fractions over the queried bases).
EXPECTED_CONSENSUS_BP = [
    {"rank": "family", "taxon": "Enterobacteriaceae", "bp": 1_954_431, "fraction": 0.9654},
    {"rank": None, "taxon": "unassigned", "bp": 70_000, "fraction": 0.0346},
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


def test_wasm_merge_and_tsv_match_the_server() -> None:
    # Only contigs that were sent have records: records skipped for min_length
    # or too few k-mers never become documents, they are counts in the options.
    classified = [record for record in RECORDS if record["status"] in {"ok", "no_hit"} and "reason" not in record]
    batch = batch_document(classified, 12.5)
    # One batch: the merge must keep the server's summary as it is, and add the
    # client-side buckets (a single batch is the unit test of the merge itself;
    # several batches are covered in rust/sylphd-core/tests/render.rs).

    got = run_wasm({
        "batches": [batch],
        "options": {
            "skipped_short": {"contigs": 1, "bp": 2500},
            "skipped_few_kmers": {"contigs": 1, "bp": 18000},
            "min_contig_length": 3000,
        },
    })["merged"]
    doc = got["doc"]

    # Counters, bucketed skips and the (top-5) consensus breakdown.
    summary = doc["summary"]
    # The batch's own numbers survive; the run adds the two skip buckets and
    # counts the skipped records in `contigs`/`bp`.
    assert summary["consensus_bp"] == EXPECTED_CONSENSUS_BP
    assert summary["skipped_short"] == {"contigs": 1, "bp": 2500}
    assert summary["skipped_few_kmers"] == {"contigs": 1, "bp": 18000}
    assert summary["min_contig_length"] == 3000
    assert summary["contigs"] == 5
    assert summary["queried_contigs"] == 3
    assert summary["queried_bp"] == 1_954_431 + 40_000 + 30_000
    assert summary["no_hit_contigs"] == 1
    assert "not_classified" not in summary

    # One flat table, the union of the batch tables, with the requested fields.
    assert doc["table"] == per_contig.contigs_table(classified, FIELDS)
    assert doc["contigs"] == per_contig.contigs_map(classified)
    assert list(doc["contigs"]) == ["contig1", "contig2", "contig9"]
    assert doc["caveats"] == per_contig.caveats(classified)
    assert doc["params"] == batch["params"]
    assert doc["database"] == DATABASE
    assert doc["thresholds"] == THRESHOLDS
    assert doc["api_version"] == 1

    # The TSV: the header, then one row per hit, the skipped contigs nowhere.
    lines = got["tsv"].splitlines()
    assert lines[0].split("\t") == [*per_contig.TABLE_COLUMNS, *FIELDS]
    # A contig without a hit is one row with empty hit columns, not no row.
    assert [line.split("\t")[0] for line in lines[1:]] == [
        "contig1",
        "contig1",
        "contig2",
        "contig9",
    ]


def test_skipped_records_have_no_rows_and_no_contig_entries() -> None:
    classified = [record for record in RECORDS if record["status"] == "ok"]
    got = run_wasm({
        "batches": [batch_document(classified, 1.0)],
        "options": {"request": {"fields": list(FIELDS)}, "min_contig_length": 3000},
    })["merged"]
    lines = got["tsv"].splitlines()
    columns = list(per_contig.TABLE_COLUMNS) + list(FIELDS)
    assert lines[0].split("\t") == columns
    # Records this run never sent have no row at all.
    assert [line.split("\t")[0] for line in lines[1:]] == ["contig1", "contig1", "contig2"]
    assert "contig7" not in got["tsv"]
    assert "contig8" not in got["tsv"]
    assert set(got["doc"]["contigs"]) == {"contig1", "contig2"}


def test_browser_limit_is_an_extra_summary_field() -> None:
    """The browser's 100-contig cap is reported separately, not as a CLI bucket."""
    got = run_wasm({
        "batches": [],
        "options": {
            "request": {"fields": ["gtdb_taxonomy"]},
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
    assert doc["table"]["columns"] == [*per_contig.TABLE_COLUMNS, "gtdb_taxonomy"]
    assert doc["table"]["data"] == []
    # With no batch, the run document's params are the request's, shaped like a batch's.
    assert doc["params"] == {
        "top": 1,
        "min_ani": 80.0,
        "consensus_ratio": 1.5,
        "consensus_z": 2.0,
        "fields": ["gtdb_taxonomy"],
    }
    assert doc["database"] == {}


def test_wasm_renderer_exports_match_the_goldens() -> None:
    """Profile/assembly exports through the JS glue, against the goldens."""
    profile_case = json.loads((GOLDEN / "profile_golden.json").read_text())["cases"][0]
    assembly_case = json.loads((GOLDEN / "assembly_golden.json").read_text())["cases"][0]

    got = run_wasm({
        "profile": profile_case["document"],
        "assembly": assembly_case["document"],
    })

    assert got["api_version_1"] == "ok"
    assert "server API version 2 is newer than this client's (1): update sylphd" in got["api_version_2"]["error"]

    assert got["profile"]["tsv"] == profile_case["expected_tsv"]
    assert got["assembly"]["tsv"] == assembly_case["expected_tsv"]


CORE_CHECK = ROOT / "rust" / "sylphd-wasm" / "tests" / "core_check.mjs"


def plan(contigs: list[dict], limits: dict) -> list[dict] | dict:
    done = subprocess.run(
        ["node", str(CORE_CHECK)],
        input=json.dumps({
            "plan": {
                "contigs": contigs,
                "limits": limits,
                "request": {"top": 1, "min_ani": 80.0, "consensus_ratio": 1.5, "consensus_z": 2.0, "fields": []},
            },
        }),
        capture_output=True, text=True, check=True, cwd=ROOT,
    )
    return json.loads(done.stdout)["plan"]


def contig(name: str, payload_bytes: int = 1000) -> dict:
    return {"name": name, "contig_kmers": 100, "length": 30_000, "gc": 50.0, "payload_bytes": payload_bytes}


def test_the_web_batches_by_the_servers_limits() -> None:
    """The browser plans its requests with the core: ``limits.max_contigs``, params and upload size."""
    contigs = [contig(f"c{i}") for i in range(5)]

    by_count = plan(contigs, {"max_contigs": 2})
    assert [batch["indices"] for batch in by_count] == [[0, 1], [2, 3], [4]]
    # The params text sent is the planned one, with exactly that batch's contigs.
    sent = json.loads(by_count[1]["params"])
    assert [entry["name"] for entry in sent["contigs"]] == ["c2", "c3"]
    assert sent["top"] == 1 and sent["fields"] == []

    # No limits: one request for everything (the default is 4096 contigs).
    assert [batch["indices"] for batch in plan(contigs, {})] == [[0, 1, 2, 3, 4]]

    # The upload limit (1 MB) splits by sketch size; a sketch that cannot fit fails.
    big = [contig(f"b{i}", 400_000) for i in range(3)]
    assert [batch["indices"] for batch in plan(big, {"max_upload_mb": 1})] == [[0, 1], [2]]
    too_big = plan([contig("huge", 2_000_000)], {"max_upload_mb": 1})
    assert "huge" in too_big["error"] and "upload limit" in too_big["error"]


def test_the_web_checks_the_api_version_from_info_with_the_core() -> None:
    """``loadInfo`` passes ``/info``'s api_version to this export before anything is sketched."""
    got = run_wasm({})
    assert got["api_version_1"] == "ok"
    assert "newer than this client's (1): update sylphd" in got["api_version_2"]["error"]
    missing = subprocess.run(
        ["node", "--input-type=module", "-e",
         ("import {readFileSync} from 'node:fs';"
         "import {initSync, check_api_version} from './web/wasm/sylph_sketch.js';"
         "initSync({module: readFileSync('./web/wasm/sylph_sketch_bg.wasm')});"
         "try { check_api_version(undefined); } catch (e) { console.log(String(e)); }")],
        capture_output=True, text=True, check=True, cwd=ROOT,
    )
    assert "missing or invalid api_version" in missing.stdout
