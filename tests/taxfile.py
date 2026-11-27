"""A small omnitax file for the metadata build: an NCBI tree with genome links, and GTDB."""

from __future__ import annotations

from pathlib import Path

import omnitax
from omnitax.store import Link, Rec, Rows

#: What the NCBI nodes of the fixtures carry: ``specified_species`` 0 marks an informal species.
INFORMAL = {"specified_species": 0}


def write_taxonomy_file(
    path: Path,
    nodes: list[tuple[int, int, str, str, int]],
    *,
    assemblies: list[tuple[str, int, str]] = (),
    gtdb: list[tuple[str, str]] = (),
    links: list[tuple[str, int]] = (),
    merged: dict[int, int] | None = None,
    release: str = "232",
) -> Path:
    """Write NCBI (``nodes``: taxid, parent, rank, name, specified_species; the root is its own parent)
    with its genomes ``assemblies`` (accession, taxid, organism; source ``ncbi-assemblies``) and ``links``
    (GTDB's accession, taxid; source ``gtdb:RELEASE``), and GTDB ``gtdb`` (accession, lineage), to ``path``."""
    recs = [
        Rec(str(taxid), None if taxid == parent else str(parent), "domain" if rank == "superkingdom" else rank, name, {"specified_species": specified})
        for taxid, parent, rank, name, specified in nodes
    ]
    linked = [Link(accession, str(taxid), organism, "ncbi-assemblies") for accession, taxid, organism in assemblies]
    linked += [Link(accession, str(taxid), None, f"gtdb:{release}") for accession, taxid in links]
    aliases = [(str(old), str(new)) for old, new in (merged or {}).items()]
    omnitax.Taxonomy.build(Rows(recs, aliases=aliases, links=linked), "ncbi", "2026-01-01", path=path, fuzzy=False).close()
    if gtdb:
        lines = path.with_suffix(".gtdb.tsv")
        lines.write_text("".join(f"{accession}\t{lineage}\n" for accession, lineage in gtdb))
        omnitax.load("gtdb", release, files=[lines], path=path, fuzzy=False).close()
    else:  # omnitax.load refuses a GTDB file without a lineage; a GTDB with one node and no genomes it is
        omnitax.Taxonomy.build(Rows([Rec("d__Bacteria", None, "domain", "Bacteria")]), "gtdb", release, path=path, fuzzy=False).close()
    return path


#: A fungal corner of NCBI, with the assemblies of ``tests/data/ncbi``'s summaries.
FUNGI_NODES = [
    (1, 1, "no rank", "root", 0),
    (2759, 1, "superkingdom", "Eukaryota", 0),
    (4751, 2759, "kingdom", "Fungi", 0),
    (4890, 4751, "phylum", "Ascomycota", 0),
    (4891, 4890, "class", "Saccharomycetes", 0),
    (4892, 4891, "order", "Saccharomycetales", 0),
    (4893, 4892, "family", "Saccharomycetaceae", 0),
    (4930, 4893, "genus", "Saccharomyces", 0),
    (4932, 4930, "species", "Saccharomyces cerevisiae", 1),
    (559292, 4932, "strain", "Saccharomyces cerevisiae S288C", 1),
    (5141, 4890, "species", "Neurospora crassa", 1),
]
FUNGI_ASSEMBLIES = [
    ("GCF_000002515.1", 4932, "Saccharomyces cerevisiae"),
    ("GCF_000002655.2", 559292, "Saccharomyces cerevisiae S288C"),
    ("GCA_000002495.2", 5141, "Neurospora crassa OR74A"),
    ("GCF_000000003.1", 77777, "Merged yeast"),  # 77777 was merged into 4932
]
FUNGI_MERGED = {77777: 4932}
SACCHAROMYCES = (
    "d__Eukaryota;p__Ascomycota;c__Saccharomycetes;o__Saccharomycetales;"
    "f__Saccharomycetaceae;g__Saccharomyces;s__Saccharomyces cerevisiae"
)
