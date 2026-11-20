"""The nearest NCBI taxon of genomes that have no NCBI record.

A MAG in a sylph-tax source (GlobDB, ...) has a GTDB-style lineage
(``d__Bacteria;p__...;g__Thiobacillus;s__SPIREOTU_123``) but no assembly NCBI
knows, so ``ncbi_taxid`` stays empty.  ``setup --ncbi-taxonomy`` gives such a
key a *nearest* NCBI taxon instead, kept apart from ``ncbi_taxid`` (which is
only ever an assembly's real taxid):

1. the key's GTDB lineage is walked from the deepest named rank upwards;
2. for each GTDB clade (``g__Thiobacillus``) the NCBI taxids of the member
   genomes in GTDB's metadata (``ncbi_taxid``) are lifted to the nearest
   ancestor-or-self with a major rank (species ... domain), so strains and
   no-rank nodes vote as their species; members under NCBI's *unclassified
   sequences*, *other sequences* and *environmental samples* are ignored;
3. the clade's **consensus** is the deepest NCBI node that **more than half**
   of the members (counted per genome) have as ancestor-or-self.  A strict LCA
   would be pulled up by one misclassified genome; this ignores any minority
   up to just under half, and when no node has a majority the members'
   common ancestors still have all of them, so a split clade still yields the
   nearest node they share.  The consensus is then lifted to a major rank and
   the walk stops at the first (deepest) clade that has one;
4. the result is stored as the taxid, its rank (``species``, ``genus``,
   ``family``, ``order``, ``class``, ``phylum``, ``kingdom``, ``domain``), its
   name and ranked lineage, the GTDB clade it came from and how many members
   supported it (``members``, ``support`` = the share);
5. NCBI names many species only informally (``nodes.dmp``'s
   ``specified_species`` is 0: *Clostridia bacterium*, *uncultured Collinsella
   sp.*, *… sp.*).  The raw consensus is reported as it is; alongside it, the
   *formal* taxon is the nearest ancestor-or-self of that raw taxon that is not
   such an informal species (:meth:`NcbiTree.lift_formal`) -- the class
   *Clostridia* for *Clostridia bacterium*, the genus *Collinsella* for
   *uncultured Collinsella sp.*.  For a formally named raw taxon the formal
   taxon is the same node.  The formal taxon is extra information, never a
   replacement: it is a structural flag from the taxdump, not a pattern on
   names.

Clades are matched by ``rank__name`` (``g__Thiobacillus``), not by the whole
path, so a phylum renamed between GTDB releases does not hide a genus whose
name did not change.  The code only reads GTDB rows (``gtdb_taxonomy`` set) and
``nodes.dmp``'s structural flags; nothing is matched by organism name.
"""

from __future__ import annotations

import sqlite3
from array import array
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

from . import ncbi_taxonomy
from .taxonomy_sources import match_accessions

#: ``nodes.dmp`` rank name -> the rank reported (``superkingdom`` is the domain).
MAJOR_RANKS: dict[str, str] = {
    "species": "species",
    "genus": "genus",
    "family": "family",
    "order": "order",
    "class": "class",
    "phylum": "phylum",
    "kingdom": "kingdom",
    "superkingdom": "domain",
    "domain": "domain",
}

_RANK_NAMES: tuple[str, ...] = (
    "",
    "species",
    "genus",
    "family",
    "order",
    "class",
    "phylum",
    "kingdom",
    "domain",
)
_RANK_CODES = {name: code for code, name in enumerate(_RANK_NAMES) if name}

#: NCBI nodes whose descendants are not a taxonomic placement: *unclassified
#: sequences*, *other sequences* and *environmental samples* (``uncultured
#: bacterium`` and friends).  A member below one of them does not vote.
UNINFORMATIVE_ANCESTORS: frozenset[int] = frozenset({12908, 28384, 48479})

#: The share of the members that must agree (strictly more than this).
CONSENSUS = 0.5

#: Index of ``specified_species`` in a ``nodes.dmp`` row (field 16 of 18).
SPECIFIED_SPECIES = 15

#: Code of the ``species`` rank in the ``ranks`` array.
_SPECIES = _RANK_CODES["species"]

#: GTDB rank prefixes in lineage order.
GTDB_RANKS: tuple[str, ...] = ("d", "p", "c", "o", "f", "g", "s")


class NcbiTree:
    """``nodes.dmp`` parents, major ranks and formal species, and ``merged.dmp``.

    Three flat arrays indexed by taxid (the largest is a few million), which
    keeps the 3 million nodes of the taxdump to a few tens of megabytes, plus
    the merged-taxid mapping.
    """

    def __init__(
        self,
        parents: array[int],
        ranks: array[int],
        specified: array[int],
        merged: Mapping[int, int],
    ):
        self._parents = parents
        self._ranks = ranks
        #: ``nodes.dmp``'s ``specified_species``: 1 when a species in the node's
        #: lineage has a formal name (see :meth:`lift`).
        self._specified = specified
        self.merged = merged

    @classmethod
    def from_files(cls, nodes_path: Path, merged_path: Path) -> NcbiTree:
        parents: array[int] = array("i")
        ranks: array[int] = array("B")
        specified: array[int] = array("B")
        with open(nodes_path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.split("\t|\t")
                if len(parts) < 3:
                    continue
                try:
                    taxid = int(parts[0])
                    parent = int(parts[1])
                except ValueError:
                    continue
                if taxid >= len(parents):
                    grow = taxid + 1 - len(parents)
                    parents.extend([0] * grow)
                    ranks.extend([0] * grow)
                    specified.extend([0] * grow)
                parents[taxid] = parent
                name = MAJOR_RANKS.get(parts[2].strip().lower())
                ranks[taxid] = _RANK_CODES[name] if name else 0
                # A row without the documented column (an older or a test
                # taxdump) means "unknown": keep a species rather than lift it.
                flag = parts[SPECIFIED_SPECIES].strip() if len(parts) > SPECIFIED_SPECIES else "1"
                specified[taxid] = 0 if flag == "0" else 1
        with open(merged_path, encoding="utf-8", errors="replace") as handle:
            merged = dict(ncbi_taxonomy.iter_merged(handle))
        return cls(parents, ranks, specified, merged)

    def current(self, taxid: int) -> int:
        """``taxid`` with merged taxids followed (bounded, a loop stops)."""
        for _ in range(8):
            newer = self.merged.get(taxid)
            if newer is None or newer == taxid:
                break
            taxid = newer
        return taxid

    def __contains__(self, taxid: int) -> bool:
        return 0 < taxid < len(self._parents) and self._parents[taxid] != 0

    def rank(self, taxid: int) -> str | None:
        """The major rank of ``taxid`` itself, or ``None``."""
        if taxid not in self:
            return None
        return _RANK_NAMES[self._ranks[taxid]] or None

    def ancestors(self, taxid: int) -> list[int]:
        """``taxid`` and its ancestors up to the root, in that order."""
        chain: list[int] = []
        while taxid in self and taxid not in chain:
            chain.append(taxid)
            parent = self._parents[taxid]
            if parent == taxid:
                break
            taxid = parent
        return chain

    def lift(self, taxid: int) -> int | None:
        """The nearest ancestor-or-self with a major rank, or ``None``.

        ``None`` for a taxid the taxdump does not have, one below an
        uninformative node (``UNINFORMATIVE_ANCESTORS``), or one whose
        ancestors have no major rank at all.  An informal species
        (``specified_species`` 0) is returned like any other major rank: this is
        the raw placement the nearest taxon reports.
        """
        chain = self.ancestors(self.current(taxid))
        if not chain or UNINFORMATIVE_ANCESTORS.intersection(chain):
            return None
        return next((node for node in chain if self._ranks[node]), None)

    def lift_formal(self, taxid: int) -> int | None:
        """The nearest ancestor-or-self that is a *formally named* placement.

        Like :meth:`lift`, but skips a species-rank node with
        ``specified_species`` 0 (*Clostridia bacterium*, *uncultured Collinsella
        sp.*): NCBI gives such a node no formal name, so the answer is the
        genus, family, class, ... above it.  A binomial species is returned as
        it is, so for a formal taxid this equals :meth:`lift`.

        ``None`` on the same conditions as :meth:`lift`.
        """
        chain = self.ancestors(self.current(taxid))
        if not chain or UNINFORMATIVE_ANCESTORS.intersection(chain):
            return None
        for node in chain:
            rank = self._ranks[node]
            if not rank:
                continue
            if rank == _SPECIES and not self._specified[node]:
                continue
            return node
        return None


def consensus(
    tree: NcbiTree, weights: Mapping[int, int], *, threshold: float = CONSENSUS
) -> tuple[int, int, int] | None:
    """``(taxid, supporting, total)`` of the members' consensus node, or ``None``.

    ``weights`` maps an already lifted taxid to the number of genomes that have
    it.  The consensus is the deepest node that strictly more than
    ``threshold`` of the genomes have as ancestor-or-self, lifted to a major
    rank; ``None`` when no informative node qualifies.
    """
    total = sum(weights.values())
    if total <= 0:
        return None
    counts: Counter[int] = Counter()
    depth: dict[int, int] = {}
    for taxid, weight in weights.items():
        chain = tree.ancestors(taxid)
        for index, node in enumerate(chain):
            counts[node] += weight
            depth[node] = len(chain) - index
    best: int | None = None
    for node, count in counts.items():
        if count > threshold * total and (best is None or depth[node] > depth[best]):
            best = node
    if best is None:
        return None
    lifted = tree.lift(best)
    if lifted is None:
        return None
    return lifted, counts[lifted], total


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


#: Every rank :class:`NcbiTree` reports, shallowest first (``domain`` before ``species``).
REPORTED_RANKS: tuple[str, ...] = tuple(reversed(_RANK_NAMES[1:]))
_RANK_ORDER = {name: index for index, name in enumerate(REPORTED_RANKS)}


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


def _targets(connection: sqlite3.Connection) -> Iterator[tuple[int, str, str]]:
    """``(source_id, key, lineage)`` of every key with a GTDB-style lineage but
    no real NCBI taxid, in the stored sources and in the virtual GTDB source.

    A key has a taxid when the accession in it (or the key itself) resolves,
    exactly or as the single version of an unversioned base, to a row of
    ``metadata_data`` with ``ncbi_taxid`` -- a GTDB row or an enrichment row.
    """
    connection.create_function(
        "assembly_accession", 1, ncbi_taxonomy.assembly_accession, deterministic=True
    )
    connection.execute("DROP TABLE IF EXISTS temp._near_keys")
    connection.execute(
        "CREATE TEMP TABLE _near_keys (key TEXT PRIMARY KEY) WITHOUT ROWID"
    )
    connection.execute(
        "INSERT OR IGNORE INTO _near_keys (key) "
        "SELECT COALESCE(assembly_accession(key), key) FROM taxonomy_entry"
    )
    match_accessions(
        connection, "_near_keys", "_near_have", "m.ncbi_taxid IS NOT NULL"
    )
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


def _member_weights(
    connection: sqlite3.Connection, tree: NcbiTree, needed: set[str]
) -> dict[str, Counter[int]]:
    """``clade -> lifted taxid -> genomes`` over the GTDB genomes with a taxid."""
    weights: dict[str, Counter[int]] = {clade: Counter() for clade in needed}
    lifted: dict[int, int | None] = {}
    cursor = connection.execute(
        "SELECT d.value, m.ncbi_taxid, COUNT(*) FROM metadata_data AS m "
        "JOIN dict_gtdb_taxonomy AS d ON d.id = m.gtdb_taxonomy "
        "WHERE m.ncbi_taxid IS NOT NULL "
        "GROUP BY m.gtdb_taxonomy, m.ncbi_taxid"
    )
    last_lineage: str | None = None
    last_clades: tuple[str, ...] = ()
    for lineage, taxid, count in cursor:
        if lineage != last_lineage:
            last_lineage = lineage
            last_clades = tuple(c for c in lineage_clades(lineage) if c in needed)
        if not last_clades:
            continue
        taxid = int(taxid)
        if taxid not in lifted:
            lifted[taxid] = tree.lift(taxid)
        node = lifted[taxid]
        if node is None:
            continue
        for clade in last_clades:
            weights[clade][node] += int(count)
    return weights


def compute_nearest(
    connection: sqlite3.Connection,
    load: Callable[[], tuple[NcbiTree, Path, Path]],
    *,
    source_names: Mapping[int, str] | None = None,
) -> dict[str, dict[str, object]]:
    """Fill ``taxonomy_nearest``/``ncbi_nearest_taxon``; return per-source counts.

    ``load()`` returns ``(tree, rankedlineage.dmp, nodes.dmp)``; it is only
    called when some key needs a nearest taxon, so a file without such keys
    never downloads the taxdump.

    The tables are dropped and rebuilt on every call, so a rerun is idempotent.
    The counts are
    ``{source: {"keys": n, "assigned": n, "informal": n, "ranks": {rank: n}}}``
    where ``keys`` are the keys with a GTDB-style lineage and no real taxid,
    ``ranks`` counts the *raw* nearest taxa and ``informal`` how many of them
    are a species NCBI only names informally (their formal taxon is above them).
    """
    targets = list(_targets(connection))
    if not targets:
        for statement in _SCHEMA:
            connection.execute(statement)
        return {}
    tree, ranked_path, nodes_path = load()
    names = dict(
        source_names
        or {
            int(row[0]): str(row[1])
            for row in connection.execute("SELECT id, name FROM taxonomy_source")
        }
    )
    plans: list[tuple[int, str, tuple[str, ...]]] = [
        (int(source_id), str(key), tuple(reversed(lineage_clades(lineage))))
        for source_id, key, lineage in targets
    ]
    needed = {clade for _, _, clades in plans for clade in clades}

    weights = _member_weights(connection, tree, needed)
    results: dict[str, tuple[int, int, int] | None] = {}

    def of(clade: str) -> tuple[int, int, int] | None:
        if clade not in results:
            results[clade] = consensus(tree, weights.pop(clade, Counter()))
        return results[clade]

    assigned: list[tuple[int, str, int, str, int, float, int]] = []
    counts: dict[str, dict[str, object]] = {}
    chosen: list[tuple[str, int, int]] = []
    for source_id, key, clades in plans:
        name = names.get(source_id, str(source_id))
        count = counts.setdefault(
            name, {"keys": 0, "assigned": 0, "informal": 0, "ranks": {}}
        )
        count["keys"] = int(count["keys"]) + 1  # type: ignore[call-overload]
        for clade in clades:
            result = of(clade)
            if result is None:
                continue
            taxid, supporting, total = result
            # The formal taxon is the raw one lifted past an informal species,
            # never a new vote (so it is always the raw taxon or an ancestor).
            formal = tree.lift_formal(taxid) or taxid
            assigned.append(
                (source_id, key, taxid, clade, total, supporting / total, formal)
            )
            chosen.append((name, taxid, formal))
            break
    taxids = {taxid for _, taxid, _ in chosen} | {formal for _, _, formal in chosen}

    rank_of = {taxid: tree.rank(taxid) or "" for taxid in taxids}
    for name, taxid, formal in chosen:
        count = counts[name]
        count["assigned"] = int(count["assigned"]) + 1  # type: ignore[call-overload]
        if formal != taxid:
            count["informal"] = int(count["informal"]) + 1  # type: ignore[call-overload]
        ranks = count["ranks"]
        assert isinstance(ranks, dict)
        ranks[rank_of[taxid]] = ranks.get(rank_of[taxid], 0) + 1

    node_names = ncbi_taxonomy.ranked_names(ranked_path, taxids)
    lineages = ncbi_taxonomy.ranked_lineages(ranked_path, nodes_path, taxids)

    for statement in _SCHEMA:
        connection.execute(statement)
    connection.executemany(
        "INSERT INTO ncbi_nearest_taxon (taxid, rank, name, lineage) VALUES (?, ?, ?, ?)",
        [
            (taxid, rank_of[taxid], node_names.get(taxid, ""), lineages.get(taxid))
            for taxid in sorted(taxids)
        ],
    )
    connection.executemany(
        "INSERT INTO taxonomy_nearest "
        "(source_id, key, taxid, clade, members, support, formal_taxid) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        assigned,
    )
    for count in counts.values():
        ranks = count["ranks"]
        assert isinstance(ranks, dict)
        count["ranks"] = {
            rank: ranks[rank] for rank in sorted(ranks, key=lambda r: _RANK_ORDER.get(r, 99))
        }
    return counts
