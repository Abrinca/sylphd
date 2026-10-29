"""The web app's per-contig TSV and JSON must match ``sylphd assembly --per-contig``.

The browser writes the downloads itself (``web/contigs.js``), because the CLI's
``per_contig_client.PerContigRun`` writers cannot run there.  This test feeds the
same merged records, the same per-request summary and the same options to both
writers and compares them byte for byte (TSV) and value for value (the JSON
document): column order and the Python number/None/bool formatting are a
contract with the command line.

The pure writers in ``web/contigs.js`` are exported through ``module.exports``
when the file is loaded by node, so no browser or DOM is needed.
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


@pytest.fixture(autouse=True)
def _require_node() -> None:
    if shutil.which("node") is None:
        (pytest.fail if os.environ.get("CI") else pytest.skip)("node is not installed")


FIELDS = ("gtdb_taxonomy", "ncbi_organism_name", "checkm2_completeness", "tax_host")


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


NODE = r"""
const F = require(process.argv[1]);
const payload = JSON.parse(require('fs').readFileSync(0, 'utf8'));
console.log(JSON.stringify({
  tsv: F.tsv(payload.records, payload.fields),
  doc: F.document({
    records: payload.records,
    summaries: payload.summaries,
    options: payload.options,
    database: payload.database,
    file: payload.file,
    totals: payload.totals,
    skippedShort: payload.skippedShort,
    skippedFewKmers: payload.skippedFewKmers,
    notClassified: payload.notClassified,
  }),
}));
"""


def test_tsv_and_json_match_the_cli() -> None:
    # Only contigs that were sent have records (the browser does not keep the
    # skipped ones); the CLI's table is compared on exactly those rows.
    classified = [record for record in RECORDS if record["status"] in {"ok", "no_hit"} and "reason" not in record]
    summaries = [per_contig.summarize(classified)]
    options = per_contig_client.PerContigOptions(
        top=2,
        min_ani=80.0,
        min_length=3000,
        fields=FIELDS,
        consensus_ratio=1.5,
        consensus_z=2.0,
    )
    database = {"name": "fixture.syldb", "genomes": 10, "c": 200, "k": 31}
    skipped_short = per_contig_client.Skipped(contigs=1, bp=2500)
    skipped_few_kmers = per_contig_client.Skipped(contigs=1, bp=18000)
    run = per_contig_client.PerContigRun(
        classified,
        summaries=summaries,
        options=options,
        params=per_contig.QUERY_PARAMS,
        database=database,
        skipped_short=skipped_short,
        skipped_few_kmers=skipped_few_kmers,
    )
    expected_tsv = io.StringIO()
    run.write_tsv(expected_tsv)

    totals = {"contigs": len(RECORDS), "bp": sum(record["length"] for record in RECORDS)}
    payload = {
        "records": classified,
        "summaries": summaries,
        "fields": list(FIELDS),
        "options": {
            "top": options.top,
            "min_ani": options.min_ani,
            "minLength": options.min_length,
            "consensus_ratio": options.consensus_ratio,
            "consensus_z": options.consensus_z,
        },
        "database": database,
        "file": "mock.fa",
        "totals": totals,
        "skippedShort": {"contigs": skipped_short.contigs, "bp": skipped_short.bp},
        "skippedFewKmers": {"contigs": skipped_few_kmers.contigs, "bp": skipped_few_kmers.bp},
    }
    done = subprocess.run(
        ["node", "-e", NODE, str(CONTIGS_JS)],
        input=json.dumps(payload), capture_output=True, text=True, check=True, cwd=ROOT,
    )
    got = json.loads(done.stdout)

    assert got["tsv"] == expected_tsv.getvalue()
    # The whole JSON document, including the two skipped-count objects.
    assert got["doc"] == run.document("mock.fa")
    assert got["doc"]["summary"]["contigs"] == totals["contigs"]
    assert got["doc"]["summary"]["bp"] == totals["bp"]


NODE_CHUNK = r"""
const F = require(process.argv[1]);
console.log(JSON.stringify({
  configured: F.chunkSizeFor({ limits: { max_contigs: 2, max_sketches: 4096 } }),
  fallback: F.chunkSizeFor({ limits: { max_sketches: 4 } }),
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
    assert result == {"configured": 2, "fallback": 4, "default": 4096, "cap": 100}


def test_skipped_records_have_no_tsv_rows() -> None:
    """A skipped record is a count, not a row (the CLI is changing the same way)."""
    classified = [record for record in RECORDS if record["status"] == "ok"]
    node = r"""
    const F = require(process.argv[1]);
    const records = JSON.parse(require('fs').readFileSync(0, 'utf8'));
    process.stdout.write(F.tsv(records, []));
    """
    done = subprocess.run(
        ["node", "-e", node, str(CONTIGS_JS)],
        input=json.dumps(classified), capture_output=True, text=True, check=True, cwd=ROOT,
    )
    lines = done.stdout.splitlines()
    assert lines[0].split("\t") == list(__import__("sylphd.per_contig_client", fromlist=["COLUMNS"]).COLUMNS)
    assert [line.split("\t")[0] for line in lines[1:]] == ["contig1", "contig1", "contig2"]
    assert "contig7" not in done.stdout
    assert "contig8" not in done.stdout


def test_browser_limit_is_an_extra_summary_field() -> None:
    """The browser's 100-contig cap is reported separately, not as a CLI bucket."""
    node = r"""
    const F = require(process.argv[1]);
    const options = { top: 1, min_ani: 80, minLength: 20000, consensus_ratio: 1.5, consensus_z: 2 };
    const summary = F.document({
      records: [], summaries: [], options, database: {},
      file: 'x.fa', totals: { contigs: 250, bp: 5_000_000 },
      skippedShort: { contigs: 3, bp: 30_000 },
      skippedFewKmers: { contigs: 7, bp: 20_000 },
      notClassified: { contigs: 140, bp: 3_500_000, limit: 100 },
    }).summary;
    console.log(JSON.stringify(summary));
    """
    done = subprocess.run(
        ["node", "-e", node, str(CONTIGS_JS)],
        capture_output=True, text=True, check=True, cwd=ROOT,
    )
    summary = json.loads(done.stdout)
    assert summary["not_classified"] == {"contigs": 140, "bp": 3_500_000, "limit": 100}
    assert summary["skipped_short"] == {"contigs": 3, "bp": 30_000}
    assert summary["skipped_few_kmers"] == {"contigs": 7, "bp": 20_000}
