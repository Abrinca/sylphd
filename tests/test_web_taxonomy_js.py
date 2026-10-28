"""web/taxonomy.js (the web page's aggregation) must give the same output as sylphd.taxonomy."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from sylphd.taxonomy import aggregate_rows, parse_lineage, taxprof_lines

WEB_JS = Path(__file__).resolve().parents[1] / "web" / "taxonomy.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

GTDB = (
    "d__Bacteria;p__Bacillota;c__Clostridia;o__Clostridiales;"
    "f__Clostridiaceae;g__Clostridium;s__Clostridium difficile"
)
LINEAGES = [
    GTDB,
    "d__Bacteria;p__Bacillota;c__Clostridia;o__;f__;g__Candidatus Avispirillum;s__",  # internal gaps, trailing dropped
    "d__Bacteria;p__Pseudomonadota;c__Gammaproteobacteria;o__Enterobacterales;f__Enterobacteriaceae;g__Escherichia;s__Escherichia coli",
    "d__Bacteria;x__Pseudomonadati;p__Pseudomonadota;c__;o__;f__;g__;s__Odd name",  # unknown rank prefix, gap before s__
    "d__;p__;c__;o__;f__;g__;s__Lonely",  # no ancestor named: "root"
    " d__Archaea ; p__Halobacteriota ;t__ignored;; ",
    "none",
    "",
    None,
    "d__Bacteria;d__Other;p__Bacillota;p__X",  # first mention wins
]


def _rows() -> list[dict]:
    rows = []
    for i, lin in enumerate(LINEAGES):
        rows.append(
            {
                "Genome_file": f"db/GCA_00000000{i}.1_genomic.fna.gz",
                "accession": None if i % 4 == 3 else f"GCA_00000000{i}.1",
                "Taxonomic_abundance": [60.0, 25.5, 9.25, 3.0, 1.0, 1e-5, 0.0123456789, 0.5, 0.2500000001, 2.5e-7][i],
                "Sequence_abundance": [50.0, 20.0, 9.0, 3.0, 1.0, 0.00002, 0.01, 0.4, 0.3, 1e-9][i],
                "Adjusted_ANI": 97.5 + i / 100,
                "Eff_cov": None if i == 5 else 0.1 * (i + 1),
                "gtdb_taxonomy": lin,
                "ncbi_taxonomy": LINEAGES[(i + 2) % len(LINEAGES)],
            }
        )
    return rows


NODE = r"""
const tax = require(process.argv[1]);
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = { lineages: input.lineages.map((l) => tax.parseLineage(l)), taxprof: {} };
for (const t of ['gtdb', 'ncbi']) out.taxprof[t] = tax.taxprofLines('s1', t, tax.aggregateRows(input.rows, t));
console.log(JSON.stringify(out));
"""


def test_matches_python() -> None:
    rows = _rows()
    payload = json.dumps({"rows": rows, "lineages": LINEAGES})
    done = subprocess.run(
        ["node", "-e", NODE, str(WEB_JS)], input=payload, capture_output=True, text=True, check=True
    )
    js = json.loads(done.stdout)
    assert js["lineages"] == [list(parse_lineage(lin)) for lin in LINEAGES]
    for taxonomy in ("gtdb", "ncbi"):
        expected = list(taxprof_lines("s1", taxonomy, aggregate_rows(rows, taxonomy)))
        assert js["taxprof"][taxonomy] == expected
