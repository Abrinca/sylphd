"""The server-side ``taxprof`` payload: exact text, round-trippable clades.

``POST /profile`` aggregates the rows once, on the server, and returns both the
structured clades and the rendered ``.sylphmpa`` text.  ``profile --taxprof``
writes the text and ``sylphd taxmatrix`` merges clades; these tests guard that
split without re-aggregating anything.
"""

from __future__ import annotations

import pytest

from sylphd.taxprofile import (
    aggregate_rows,
    aggregate_sylphtax_rows,
    build_taxprof,
    clade_matrix_lines,
    clades_from_dict,
    taxprof_lines,
    taxprof_payload,
)

GTDB = (
    "d__Bacteria;p__Bacillota;c__Clostridia;o__Clostridiales;"
    "f__Clostridiaceae;g__Clostridium;s__Clostridium difficile"
)
NCBI = GTDB.replace("s__Clostridium difficile", "s__Clostridioides difficile")


def rows() -> list[dict]:
    return [
        {
            "Genome_file": "db/GCA_000000001.1_genomic.fna.gz",
            "accession": "GCA_000000001.1",
            "Taxonomic_abundance": 60.0,
            "Sequence_abundance": 55.0,
            "Adjusted_ANI": 99.1,
            "Eff_cov": 12.5,
            "gtdb_taxonomy": GTDB,
            "ncbi_taxonomy": NCBI,
            "tax_lineage": "vFAM-1;vOTU-1",
            "tax_key": "GCA_000000001.1",
            "tax_source": "UHGV",
        },
        {
            "Genome_file": "db/GCA_000000002.1_genomic.fna.gz",
            "accession": "GCA_000000002.1",
            "Taxonomic_abundance": 40.0,
            "Sequence_abundance": 45.0,
            "Adjusted_ANI": 97.2,
            "Eff_cov": 4.5,
            "gtdb_taxonomy": GTDB.replace(
                "Clostridium difficile", "Clostridium innocuum"
            ),
            "ncbi_taxonomy": NCBI,
            "tax_lineage": "vFAM-1;vOTU-2",
            "tax_key": "GCA_000000002.1",
            "tax_source": "UHGV",
        },
    ]


@pytest.mark.parametrize("taxonomy", ["gtdb", "ncbi", "tax"])
def test_payload_text_matches_the_renderer(taxonomy: str) -> None:
    clades = (
        aggregate_sylphtax_rows(rows())
        if taxonomy == "tax"
        else aggregate_rows(rows(), taxonomy)
    )
    used = ["UHGV"] if taxonomy == "tax" else [taxonomy]
    payload = taxprof_payload("one", taxonomy, clades, taxonomies_used=used)
    assert payload["columns"] == [
        "clade_name",
        "relative_abundance",
        "sequence_abundance",
        "ANI (if strain-level)",
        "Coverage (if strain-level)",
    ]
    assert payload["sylphmpa"] == "\n".join(
        taxprof_lines("one", taxonomy, clades, taxonomies_used=used)
    ) + "\n"
    assert payload["taxonomies_used"] == used


def test_clades_round_trip_for_the_matrix() -> None:
    samples = [
        {"name": "one", "rows": rows()},
        {"name": "two", "rows": rows()[:1]},
    ]
    payloads = [
        build_taxprof(sample["name"], ("gtdb",), sample["rows"])
        for sample in samples
    ]
    rebuilt = [
        (sample["name"], clades_from_dict(payloads[index]["gtdb"]["clades"]))
        for index, sample in enumerate(samples)
    ]
    expected = [
        (sample["name"], aggregate_rows(sample["rows"], "gtdb"))
        for sample in samples
    ]
    for value in ("relative", "sequence", "ani", "coverage"):
        assert list(clade_matrix_lines(rebuilt, value)) == list(
            clade_matrix_lines(expected, value)
        )
