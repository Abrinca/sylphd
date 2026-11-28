"""The nearest NCBI taxon of genomes that have no NCBI record.

A MAG in a sylph-tax source (GlobDB, ...) has a GTDB-style lineage
(``d__Bacteria;p__...;g__Thiobacillus;s__SPIREOTU_123``) but no assembly NCBI
knows, so ``ncbi_taxid`` stays empty.  ``setup --taxonomy-db`` gives such a key
a *nearest* NCBI taxon instead, kept apart from ``ncbi_taxid`` (which is only
ever an assembly's real taxid):

1. the key's GTDB lineage is walked from the deepest named rank upwards;
2. each GTDB clade (``g__Thiobacillus``) is translated into NCBI through the
   genomes both taxonomies know (:meth:`omnitax.Taxonomy.translate`): every
   GTDB leaf below it votes once, with the NCBI node of its genomes (strains
   and no-rank nodes vote as their species; members under *unclassified
   sequences*, *other sequences* and *environmental samples* do not vote), and
   the answer is the deepest NCBI node that more than 80 % (:data:`THRESHOLD`)
   of the votes have as ancestor-or-self, lifted to one of :data:`NEAREST_RANKS`.  A vote
   that only reaches a higher rank (*Xanthomonadaceae bacterium*) abstains
   below it instead of counting against a genus it does not contradict, but only
   while at least 30 % of the votes reach that rank (:data:`MIN_INFORMATIVE`; below it
   plain voting applies).  The answer is capped (:data:`CAP`): a clade is never
   answered by something finer than its own rank -- a genus clade by a genus at
   most, never by a species, even when all its genomes are one species.  The
   ladder of the cap is :data:`NEAREST_RANKS`, kingdom included, so a domain
   clade is not answered by a kingdom either; an answer that is no finer than
   the clade's rank (including one on a rank outside the ladder, strains
   counted as their species) is kept as it is.  All three numbers are passed
   to omnitax explicitly, so a placement never changes with an omnitax default
   (omnitax 0.2: strict precision 82.24 against 80.01 % leaving a species out);
   the walk stops at the first (deepest) clade that has an answer;
3. the result is stored as the taxid, its rank, name and ranked lineage, the
   GTDB clade it came from, the votes that counted at the answer's rank
   (``members``, abstentions excluded) and the share of them that agreed
   (``support``);
4. NCBI names many species only informally (*Clostridia bacterium*, *uncultured
   Collinsella sp.*).  The raw consensus is reported as it is; alongside it, the
   *formal* taxon is the nearest ancestor-or-self of it that is not such an
   informal species -- the class *Clostridia* for *Clostridia bacterium*.  For a
   formally named raw taxon it is the same node.

Clades are matched by ``rank__name`` (``g__Thiobacillus``), the ids of omnitax's
GTDB taxonomies, not by the whole path, so a phylum renamed between GTDB
releases does not hide a genus whose name did not change.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence

from omnitax import GTDB_RANKS as OMNITAX_RANKS
from omnitax import Taxon, Taxonomy

from .taxonomy_sources import assembly_accession, match_accessions

#: GTDB rank prefixes in lineage order.
GTDB_RANKS: tuple[str, ...] = ("d", "p", "c", "o", "f", "g", "s")

#: The ranks a nearest taxon may have, shallowest first: GTDB's seven plus NCBI's kingdom, the level
#: (Bacillati, Pseudomonadati, Nanobdellati, ...) where clades that NCBI splits across phyla still agree.
NEAREST_RANKS: tuple[str, ...] = (OMNITAX_RANKS[0], "kingdom", *OMNITAX_RANKS[1:])

_RANK_ORDER = {rank: index for index, rank in enumerate(NEAREST_RANKS)}

_SCHEMA = (
    "DROP TABLE IF EXISTS taxonomy_nearest",
    "DROP TABLE IF EXISTS ncbi_nearest_taxon",
    (
        "CREATE TABLE ncbi_nearest_taxon (\n"
        "    taxid INTEGER PRIMARY KEY,\n"
        "    rank TEXT NOT NULL,\n"
        "    name TEXT NOT NULL,\n"
        "    lineage TEXT\n"
        ")"
    ),
    (
        "CREATE TABLE taxonomy_nearest (\n"
        "    source_id INTEGER NOT NULL,\n"
        "    key TEXT NOT NULL,\n"
        "    taxid INTEGER NOT NULL,\n"
        "    clade TEXT NOT NULL,\n"
        "    members INTEGER NOT NULL,\n"
        "    support REAL NOT NULL,\n"
        "    formal_taxid INTEGER NOT NULL,\n"
        "    PRIMARY KEY (source_id, key)\n"
        ") WITHOUT ROWID"
    ),
    "CREATE INDEX taxonomy_nearest_key ON taxonomy_nearest (key)",
)



#: Share of the counted votes the nearest taxon must exceed (strictly more than; omnitax recommends 0.8 for placing novel genomes).
THRESHOLD = 0.8

#: Votes that only reach a higher rank abstain while at least this share of the votes reach the rank asked about (omnitax 0.2's default, kept explicit).
MIN_INFORMATIVE = 0.3

#: The ladder of omnitax's ``cap``: no answer finer than the clade's own rank, on NEAREST_RANKS (GTDB's seven plus kingdom); omnitax's own default ladder lacks the kingdom.
CAP: tuple[str, ...] = NEAREST_RANKS

def lineage_clades(lineage: str | None) -> tuple[str, ...]:
    """The named GTDB clades of a lineage, shallowest first (``g__Thiobacillus``).

    >>> lineage_clades("d__Bacteria;p__Pseudomonadota;c__;g__Thiobacillus;s__X")
    ('d__Bacteria', 'p__Pseudomonadota', 'g__Thiobacillus', 's__X')
    """
    clades: list[str] = []
    for token in (lineage or "").split(";"):
        prefix, separator, name = token.strip().partition("__")
        if separator and prefix in GTDB_RANKS and name.strip():
            clades.append(f"{prefix}__{name.strip()}")
    return tuple(clades)


def _targets(connection: sqlite3.Connection) -> Iterator[tuple[int, str, str]]:
    """``(source_id, key, lineage)`` of every key with a GTDB-style lineage but
    no real NCBI taxid, in the stored sources and in the virtual GTDB source.

    A key has a taxid when the accession in it (or the key itself) resolves,
    exactly or as the single version of an unversioned base, to a row of
    ``metadata_data`` with ``ncbi_taxid`` -- a GTDB row or an enrichment row.
    """
    connection.create_function("assembly_accession", 1, assembly_accession, deterministic=True)
    connection.execute("DROP TABLE IF EXISTS temp._near_keys")
    connection.execute("CREATE TEMP TABLE _near_keys (key TEXT PRIMARY KEY) WITHOUT ROWID")
    connection.execute(
        "INSERT OR IGNORE INTO _near_keys (key) "
        "SELECT COALESCE(assembly_accession(key), key) FROM taxonomy_entry"
    )
    match_accessions(connection, "_near_keys", "_near_have", "m.ncbi_taxid IS NOT NULL")
    try:
        yield from connection.execute(
            "SELECT e.source_id, e.key, l.value FROM taxonomy_entry AS e "
            "JOIN dict_tax_lineage AS l ON l.id = e.lineage "
            "LEFT JOIN _near_have AS h "
            "ON h.key = COALESCE(assembly_accession(e.key), e.key) "
            "WHERE h.key IS NULL AND l.value LIKE 'd\\_\\_%' ESCAPE '\\'"
        ).fetchall()
        yield from connection.execute(
            "SELECT s.id, m.accession, d.value FROM metadata_data AS m "
            "JOIN dict_gtdb_taxonomy AS d ON d.id = m.gtdb_taxonomy "
            "JOIN taxonomy_source AS s ON s.kind = 'metadata' "
            "WHERE m.ncbi_taxid IS NULL"
        ).fetchall()
    finally:
        connection.execute("DROP TABLE IF EXISTS temp._near_keys")
        connection.execute("DROP TABLE IF EXISTS temp._near_have")


def compute_nearest(
    connection: sqlite3.Connection,
    ncbi: Taxonomy,
    gtdb: Taxonomy,
    sources: Sequence[str],
) -> dict[str, dict[str, object]]:
    """Fill ``taxonomy_nearest``/``ncbi_nearest_taxon``; return per-source counts.

    ``gtdb`` is translated into ``ncbi`` through their genomes; ``sources`` are
    the genome sources of ``ncbi`` to use, best first.  The tables are dropped
    and rebuilt on every call, so a rerun is idempotent.  The counts are
    ``{source: {"keys": n, "assigned": n, "informal": n, "ranks": {rank: n}}}``
    where ``keys`` are the keys with a GTDB-style lineage and no real taxid,
    ``ranks`` counts the *raw* nearest taxa and ``informal`` how many of them
    are a species NCBI only names informally (their formal taxon is above them).
    """
    names = {int(i): str(name) for i, name in connection.execute("SELECT id, name FROM taxonomy_source")}
    placed: dict[str, tuple[Taxon, int, float] | None] = {}

    def of(clade: str) -> tuple[Taxon, int, float] | None:
        if clade not in placed:
            placed[clade] = None
            if clade in gtdb:
                placement = gtdb.translate(
                    clade,
                    ncbi,
                    details=True,
                    threshold=THRESHOLD,
                    min_informative=MIN_INFORMATIVE,
                    cap=CAP,
                    exclude=ncbi.uninformative,
                    ranks=NEAREST_RANKS,
                    other_sources=list(sources),
                )
                if placement.taxon is not None:
                    placed[clade] = (placement.taxon, round(placement.total), placement.share)
        return placed[clade]

    assigned: list[tuple[int, str, int, str, int, float, int]] = []
    taxa: dict[str, Taxon] = {}
    counts: dict[str, dict[str, object]] = {}
    for source_id, key, lineage in _targets(connection):
        name = names.get(source_id, str(source_id))
        count = counts.setdefault(name, {"keys": 0, "assigned": 0, "informal": 0, "ranks": {}})
        count["keys"] += 1  # type: ignore[operator]
        for clade in reversed(lineage_clades(lineage)):
            if (result := of(clade)) is None:
                continue
            taxon, members, support = result
            # The formal taxon is the raw one lifted past an informal species, never a new vote.
            formal = taxon.closest(NEAREST_RANKS, formal=True) or taxon
            taxa[taxon.id] = taxon
            taxa[formal.id] = formal
            assigned.append((source_id, key, int(taxon.id), clade, members, support, int(formal.id)))
            count["assigned"] += 1  # type: ignore[operator]
            if formal != taxon:
                count["informal"] += 1  # type: ignore[operator]
            ranks: dict[str, int] = count["ranks"]  # type: ignore[assignment]
            ranks[taxon.rank] = ranks.get(taxon.rank, 0) + 1
            break

    for statement in _SCHEMA:
        connection.execute(statement)
    connection.executemany(
        "INSERT INTO ncbi_nearest_taxon (taxid, rank, name, lineage) VALUES (?, ?, ?, ?)",
        [(int(taxon.id), taxon.rank, taxon.name, taxon.lineage_string()) for taxon in taxa.values()],
    )
    connection.executemany(
        "INSERT INTO taxonomy_nearest (source_id, key, taxid, clade, members, support, formal_taxid) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        assigned,
    )
    for count in counts.values():
        ranks = count["ranks"]  # type: ignore[assignment]
        count["ranks"] = {rank: ranks[rank] for rank in sorted(ranks, key=lambda r: _RANK_ORDER.get(r, 99))}
    return counts
