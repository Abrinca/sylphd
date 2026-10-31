"""GTDB metadata and sylph-tax taxonomy sources: file layout and lookup.

The metadata database keeps every GTDB genome (not only species
representatives), keyed by NCBI accession with the ``RS_``/``GB_`` prefix
stripped.  Repetitive text columns are dictionary-encoded into
``dict_<column>`` tables; the base table (``metadata_data``, ``WITHOUT ROWID``,
keyed by accession) stores integer ids and a view named ``metadata`` exposes
the original columns.  Taxonomy strings are stored as one column each; no
per-rank split happens here.

Format version 3 adds the ``taxonomy_*`` tables: the GTDB metadata is a
*virtual* source (its entries are ``metadata_data.accession`` and
``gtdb_taxonomy``, so no lineage is stored twice) and every imported sylph-tax
file is one ``taxonomy_source`` with its ``key -> lineage [-> host]`` rows in
``taxonomy_entry``.  ``taxonomy_overlap`` records, per pair of sources, how
many keys both have: importing overlapping sources is allowed (the one
imported later wins, as in sylph-tax) but never silent.

The writers live in :mod:`sylphd.metadata_build` and the sylph-tax import in
:mod:`sylphd.taxonomy_sources`.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Self

#: Schema version of the generated SQLite file.  Bump when the column layout
#: or the meaning of the ``info`` table changes.
METADATA_FORMAT_VERSION = "3"


#: Page size of the generated file.  8 KiB trims the b-tree page slack of the
#: dictionary-encoded layout (~2 % smaller, see scratch/sqlite-size/REPORT.md);
#: larger pages buy <1 % more and cost lookup time.
METADATA_PAGE_SIZE = 8192


#: Null markers used by GTDB for "no value".  Compared case-insensitively
#: after stripping whitespace.  ``not type material`` and friends are *values*.
NULL_MARKERS = frozenset({"", "none", "na"})


@dataclass(frozen=True)
class Column:
    """One column of the metadata table."""

    name: str
    kind: str  # "text" | "int" | "float" | "bool"

    @property
    def sql_type(self) -> str:
        return {
            "text": "TEXT",
            "int": "INTEGER",
            "float": "REAL",
            "bool": "INTEGER",
        }[self.kind]


#: Metadata columns kept from the GTDB files, in output order.  The accession
#: column comes first in the table and is handled separately.
COLUMNS: tuple[Column, ...] = (
    Column("gtdb_taxonomy", "text"),
    Column("gtdb_representative", "bool"),
    Column("gtdb_genome_representative", "text"),
    Column("gtdb_type_species_of_genus", "bool"),
    Column("gtdb_type_designation_ncbi_taxa", "text"),
    Column("ncbi_taxid", "int"),
    Column("ncbi_species_taxid", "int"),
    Column("ncbi_taxonomy", "text"),
    Column("ncbi_taxonomy_unfiltered", "text"),
    Column("ncbi_organism_name", "text"),
    Column("ncbi_strain_identifiers", "text"),
    Column("ncbi_isolate", "text"),
    Column("ncbi_genome_category", "text"),
    Column("ncbi_assembly_level", "text"),
    Column("ncbi_refseq_category", "text"),
    Column("ncbi_type_material_designation", "text"),
    Column("ncbi_biosample", "text"),
    Column("ncbi_bioproject", "text"),
    Column("checkm2_completeness", "float"),
    Column("checkm2_contamination", "float"),
    Column("genome_size", "int"),
    Column("gc_percentage", "float"),
    Column("contig_count", "int"),
    Column("n50_contigs", "int"),
    Column("mimag_high_quality", "bool"),
)


#: Names of the metadata columns in output order.
METADATA_COLUMNS: tuple[str, ...] = tuple(column.name for column in COLUMNS)


#: Repetitive text columns stored as integer ids into ``dict_<column>`` tables.
#: These are the long/low-cardinality strings measured in
#: scratch/sqlite-size/REPORT.md; they account for most of the file size.
DICT_ENCODED_COLUMNS: tuple[str, ...] = (
    "gtdb_taxonomy",
    "gtdb_genome_representative",
    "ncbi_taxonomy",
    "ncbi_taxonomy_unfiltered",
    "ncbi_organism_name",
    "ncbi_genome_category",
    "ncbi_assembly_level",
    "ncbi_refseq_category",
    "ncbi_type_material_designation",
    "ncbi_bioproject",
)


#: ``(column position in COLUMNS, column name)`` for dictionary-encoded columns.
_DICT_POSITIONS: tuple[tuple[int, str], ...] = tuple(
    (position, column.name)
    for position, column in enumerate(COLUMNS)
    if column.name in DICT_ENCODED_COLUMNS
)

_ACCESSION_PREFIX_RE = re.compile(r"^(?:RS_|GB_)", re.IGNORECASE)


#: ``GCA_964372435.1`` / ``GCF_000005845.2`` optionally with an ``RS_``/``GB_``
#: prefix.  Used to recover the accession from an arbitrary genome file name.
_ACCESSION_RE = re.compile(r"^(?:RS_|GB_)?(GC[AF]_\d+(?:\.\d+)?)", re.IGNORECASE)
_COMPRESSION_SUFFIXES = (".gz", ".bz2", ".xz", ".zst", ".zstd", ".bgz", ".bgzf")
_SEQUENCE_SUFFIXES = (
    ".fna",
    ".fa",
    ".fasta",
    ".fas",
    ".fsa",
    ".fnn",
    ".faa",
    ".ffn",
)


def normalize_accession(value: str) -> str:
    """Strip the GTDB ``RS_``/``GB_`` prefix and normalise case.

    >>> normalize_accession("RS_GCF_000005845.2")
    'GCF_000005845.2'
    >>> normalize_accession(" gb_gca_964372435.1 ")
    'GCA_964372435.1'
    """
    accession = _ACCESSION_PREFIX_RE.sub("", value.strip())
    return accession.upper()


def public_origin(origin: str | None) -> str | None:
    """The ``origin`` of a taxonomy source as ``/info`` may publish it.

    A catalogue download keeps the URL it came from; anything that is a local
    path is reduced to its base name, so a public ``/info`` never leaks the
    server's directory layout.  The full origin stays in the metadata file,
    and setup logs the paths it read.
    """
    if not origin:
        return None
    if origin.startswith(("http://", "https://")):
        return origin
    return os.path.basename(origin.replace("\\", "/")) or None


def accession_from_genome_name(name: str) -> str | None:
    """Map a sylph genome file name to a GTDB accession.

    Handles paths, compression suffixes, ``_genomic`` suffixes and
    ``_genomic.fna.gz`` style names, as well as NCBI-style names that carry the
    assembly name after the accession::

        /db/GCA_964372435.1_genomic.fna.gz -> GCA_964372435.1
        GCF_000005845.2_ASM584v2_genomic.fna.gz -> GCF_000005845.2

    Returns ``None`` when no accession can be found.
    """
    base = os.path.basename(name.replace("\\", "/")).strip()
    lowered = base.lower()
    for suffix in _COMPRESSION_SUFFIXES:
        if lowered.endswith(suffix):
            base = base[: -len(suffix)]
            lowered = lowered[: -len(suffix)]
            break
    for suffix in _SEQUENCE_SUFFIXES:
        if lowered.endswith(suffix):
            base = base[: -len(suffix)]
            lowered = lowered[: -len(suffix)]
            break
    if lowered.endswith("_genomic"):
        base = base[: -len("_genomic")]
    match = _ACCESSION_RE.match(base)
    if match is not None:
        return normalize_accession(match.group(1))
    return None


# ---------------------------------------------------------------------------
# sylph-tax probing: hit -> key
# ---------------------------------------------------------------------------

#: The order in which sylph-tax tries the probes of one pass, after the four
#: ``gf``/``gf + ".gz"``/``glob``/``contig`` probes: the suffix splits.
_PROBE_SPLITS: tuple[str, ...] = (".fa", ".fasta", ".fna")


def _sylphtax_gf_pass0(file_name: str) -> str:
    """Pass 0's ``gf`` probe: the basename, cut at ``_ASM`` or ``_genomic``.

    ``sylph_to_taxprof.genome_file_to_gcf_acc``: the ``"ASM"`` test looks at
    the **whole path**, the split at the basename.  A path with ``ASM`` in a
    directory therefore cuts the basename at ``_ASM`` even when the basename
    has no such part (the split then returns the basename unchanged).
    """
    if "ASM" in file_name:
        return file_name.split("/")[-1].split("_ASM")[0]
    return file_name.split("/")[-1].split("_genomic")[0]


def _probe_keys(gf: str, glob: str, contig: str) -> tuple[str, ...]:
    """One pass's probes in sylph-tax's order (``gf``, ``.gz``, ``glob``, ...)."""
    keys = [gf, gf + ".gz", glob, contig]
    keys.extend(gf.split(suffix)[0] for suffix in _PROBE_SPLITS)
    return tuple(keys)


@dataclass(frozen=True)
class SylphTaxProbes:
    """The keys sylph-tax probes for one hit, in its order and precedence.

    ``pass0`` is tried first, then ``pass1``; a pass-1 match **overrides** a
    pass-0 match (``sylph_to_taxprof.main`` has no ``break``).  ``genome_file``
    and ``contig_name`` are the pass-1 values, which sylph-tax uses to name an
    unresolved hit (``NO_TAXONOMY;t__<genome_file>:<contig_name>``).
    """

    pass0: tuple[str, ...]
    pass1: tuple[str, ...]
    genome_file: str
    contig_name: str

    def probe_keys(self) -> tuple[str, ...]:
        """Every probe key, both passes, without repeats (for one lookup)."""
        return tuple(dict.fromkeys((*self.pass0, *self.pass1)))

    @property
    def no_taxonomy(self) -> str:
        """sylph-tax's default lineage for a hit without any taxonomy."""
        return f"NO_TAXONOMY;t__{self.genome_file}:{self.contig_name}"


def sylphtax_probes(genome_file: str, contig_name: str) -> SylphTaxProbes:
    """The probe keys of one hit, exactly as ``sylph-to-taxprof`` builds them.

    Replicates ``sylph_to_taxprof.py`` (v1.9.2) rule for rule::

        pass 0  gf     basename, split at "_ASM" if "ASM" is in the full path,
                       else at "_genomic"
                glob   basename.split(".")[0]
                contig Contig_name.split(" ")[0].split("|")[0]   (space only)
        pass 1  gf     basename
                glob   gf
                contig Contig_name.split()[0]                   (any whitespace)

    and per pass the probes ``gf``, ``gf + ".gz"``, ``glob``, ``contig``, then
    ``gf.split(".fa")[0]``, ``gf.split(".fasta")[0]`` and
    ``gf.split(".fna")[0]``.  Keys are **not** normalised: sylph-tax stores
    them verbatim, so ``GCF_010093815.1_Psehy1`` and ``GCF_010093815.1`` are
    different keys.  The rules are verified against the real sylph-tax in
    ``tests/test_sylphtax_parity.py``.

    >>> probes = sylphtax_probes(
    ...     "gtdb_reps/database/GCA/047/199/115/GCA_047199115.1_genomic.fna.gz",
    ...     "GCA_047199115.1 contig 1",
    ... )
    >>> probes.pass0[0], probes.pass0[2]
    ('GCA_047199115.1', 'GCA_047199115')
    >>> sylphtax_probes("votus_full.fna.gz", "UHGV-0320751 species=vOTU-125334").pass1
    ('votus_full.fna.gz', 'votus_full.fna.gz.gz', 'votus_full.fna.gz',
     'UHGV-0320751', 'votus_full.fna.gz', 'votus_full.fna.gz',
     'votus_full')
    """
    basename = genome_file.split("/")[-1]
    gf0 = _sylphtax_gf_pass0(genome_file)
    contig0 = contig_name.split(" ")[0].split("|")[0]
    glob0 = basename.split(".")[0]

    # ``str.split()`` drops leading whitespace and returns [] for an empty or
    # blank name, where sylph-tax would raise IndexError; "no contig name" is a
    # miss, not a crash.
    words = contig_name.split()
    contig1 = words[0] if words else ""

    return SylphTaxProbes(
        pass0=_probe_keys(gf0, glob0, contig0),
        pass1=_probe_keys(basename, basename, contig1),
        genome_file=basename,
        contig_name=contig1,
    )


#: The columns a resolved hit carries (format 3).  They are *virtual*: they are
#: not in the ``metadata`` view, ``resolve`` fills them.
HIT_TAXONOMY_COLUMNS: tuple[str, ...] = (
    "tax_source",
    "tax_key",
    "tax_lineage",
    "tax_host",
)


@dataclass(frozen=True)
class HitTaxonomy:
    """The taxonomy ``resolve`` found for one hit (all ``None`` for a miss)."""

    source: str | None = None
    key: str | None = None
    lineage: str | None = None
    host: str | None = None

    def columns(self) -> dict[str, str | None]:
        """The four ``tax_*`` values, keyed like the metadata columns."""
        return {
            "tax_source": self.source,
            "tax_key": self.key,
            "tax_lineage": self.lineage,
            "tax_host": self.host,
        }


#: What ``resolve`` returns for a hit without any taxonomy.
NO_HIT_TAXONOMY = HitTaxonomy()


def open_text(path: str | os.PathLike[str]) -> IO[str]:
    """Open a possibly gzip-compressed TSV file for text reading."""
    with open(path, "rb") as handle:
        magic = handle.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rt", newline="", encoding="utf-8")
    return open(path, "rt", newline="", encoding="utf-8")


@dataclass(frozen=True)
class TaxonomySourceRow:
    """One row of ``taxonomy_source`` as read by :class:`MetadataDB`."""

    id: int
    ord: int
    name: str
    kind: str
    origin: str | None
    md5: str | None
    rows: int | None
    has_host: bool


@dataclass(frozen=True)
class TaxonomyOverlapPair:
    """One pair of sources that share keys, as ``/info`` reports it.

    ``sources`` is in ``ord`` order (the later one wins for these keys) and the
    pair appears once, even though ``taxonomy_overlap`` stores both directions.
    """

    sources: tuple[str, str]
    keys: int
    examples: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "sources": list(self.sources),
            "keys": self.keys,
            "examples": list(self.examples),
        }


class MetadataDB:
    """Read-only handle on a metadata SQLite file built by ``sylphd setup``."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"metadata database not found: {self.path}")
        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        self._connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        tables = {
            row[0] for row in self._connection.execute("SELECT name FROM sqlite_master")
        }
        if "metadata" not in tables:
            self._connection.close()
            raise ValueError(f"{self.path} is not a sylphd metadata database")
        self.columns: tuple[str, ...] = tuple(
            row[1]
            for row in self._connection.execute("PRAGMA table_info(metadata)")
            if row[1] != "accession"
        )
        self.info: dict[str, str] = {}
        if "info" in tables:
            self.info = {
                row[0]: row[1]
                for row in self._connection.execute("SELECT key, value FROM info")
            }
        version = self.info.get("format_version")
        if version != METADATA_FORMAT_VERSION:
            self._connection.close()
            raise ValueError(
                f"{self.path} has metadata format version {version or 'unknown'}; "
                f"this sylphd reads only version {METADATA_FORMAT_VERSION}: "
                "rebuild the file with `sylphd setup`"
            )
        self.format_version: str = version
        #: The sources, in ``ord`` order.
        self._sources: tuple[TaxonomySourceRow, ...] = self._read_sources()
        #: The de-duplicated overlap pairs, in ``ord`` order.
        self._overlaps: tuple[TaxonomyOverlapPair, ...] = self._read_overlaps()
        #: The virtual GTDB source, if the file has one.
        self._virtual_source: TaxonomySourceRow | None = next(
            (source for source in self._sources if source.kind == "metadata"), None
        )

    def _read_sources(self) -> tuple[TaxonomySourceRow, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, ord, name, kind, origin, md5, rows, has_host "
                "FROM taxonomy_source ORDER BY ord, id"
            ).fetchall()
        return tuple(
            TaxonomySourceRow(
                id=int(row["id"]),
                ord=int(row["ord"]),
                name=str(row["name"]),
                kind=str(row["kind"]),
                origin=row["origin"],
                md5=row["md5"],
                rows=row["rows"],
                has_host=bool(row["has_host"]),
            )
            for row in rows
        )

    def _read_overlaps(self) -> tuple[TaxonomyOverlapPair, ...]:
        """The de-duplicated overlap pairs, each source pair listed once.

        ``taxonomy_overlap`` stores both directions of a pair; the pair is
        reported once, with the names in ``ord`` order (the later one wins).
        The first shared keys are read here, once at startup, because the
        metadata file only stores the counts.
        """
        sources = {source.id: source for source in self._sources}
        sources_by_name = {source.name: source for source in self._sources}
        with self._lock:
            rows = self._connection.execute(
                "SELECT source_id, other_id, keys FROM taxonomy_overlap"
            ).fetchall()
        pairs: list[TaxonomyOverlapPair] = []
        seen: set[tuple[int, int]] = set()
        for row in rows:
            first_id = int(row["source_id"])
            second_id = int(row["other_id"])
            if first_id == second_id:
                continue
            key = (min(first_id, second_id), max(first_id, second_id))
            if key in seen:
                continue
            seen.add(key)
            first = sources.get(first_id)
            second = sources.get(second_id)
            if first is None or second is None:
                continue
            if (second.ord, second.id) < (first.ord, first.id):
                first, second = second, first
            pairs.append(
                TaxonomyOverlapPair(
                    sources=(first.name, second.name),
                    keys=int(row["keys"]),
                    examples=self._overlap_examples(first, second),
                )
            )
        pairs.sort(
            key=lambda pair: (
                sources_by_name[pair.sources[0]].ord,
                sources_by_name[pair.sources[1]].ord,
            )
        )
        return tuple(pairs)

    def _overlap_examples(
        self, first: TaxonomySourceRow, second: TaxonomySourceRow, limit: int = 5
    ) -> tuple[str, ...]:
        """The first ``limit`` keys two sources share, sorted.

        One of the two may be the virtual GTDB source, whose keys are
        ``metadata_data.accession`` rows that have a lineage (exactly what
        ``resolve`` looks up); the other keys are ``taxonomy_entry.key``.
        """
        with self._lock:
            if first.kind == "metadata" or second.kind == "metadata":
                other = second if first.kind == "metadata" else first
                rows = self._connection.execute(
                    "SELECT DISTINCT e.key AS key FROM taxonomy_entry AS e "
                    "JOIN metadata_data AS m ON m.accession = e.key "
                    "WHERE e.source_id = ? AND m.gtdb_taxonomy IS NOT NULL "
                    "ORDER BY e.key LIMIT ?",
                    (other.id, limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT e.key AS key FROM taxonomy_entry AS e "
                    "JOIN taxonomy_entry AS o ON o.key = e.key AND o.source_id = ? "
                    "WHERE e.source_id = ? ORDER BY e.key LIMIT ?",
                    (second.id, first.id, limit),
                ).fetchall()
        return tuple(str(row["key"]) for row in rows)

    @property
    def taxonomy_columns(self) -> tuple[str, ...]:
        """The ``tax_*`` columns a row can carry (empty without sources)."""
        return HIT_TAXONOMY_COLUMNS if self._sources else ()

    @property
    def field_columns(self) -> tuple[str, ...]:
        """Every column a client may request: metadata plus ``tax_*``."""
        return self.columns + self.taxonomy_columns

    def taxonomy_sources(self) -> list[dict[str, object]]:
        """The taxonomy sources of the file, in ``ord`` order .

        ``keys`` is the number of stored keys of the source.  Keys shared with
        another source are reported once per pair at the metadata level
        (:meth:`taxonomy_overlaps`), not per source.
        """
        return [
            {
                "name": source.name,
                "kind": source.kind,
                "origin": public_origin(source.origin),
                "md5": source.md5,
                "keys": source.rows,
                "has_host": source.has_host,
            }
            for source in self._sources
        ]

    def taxonomy_overlaps(self) -> list[dict[str, object]]:
        """The pairs of sources that share keys, each pair once, in ord order."""
        return [pair.as_dict() for pair in self._overlaps]

    def source_names(self) -> list[str]:
        """Names of the taxonomy sources, in ``ord`` order."""
        return [source.name for source in self._sources]

    def _chunked_lookup(self, accessions: Sequence[str]) -> dict[str, dict[str, object]]:
        unique = list(dict.fromkeys(accession for accession in accessions if accession))
        found: dict[str, dict[str, object]] = {}
        # 500 parameters per statement stays well below SQLite's variable limit.
        for start in range(0, len(unique), 500):
            chunk = unique[start : start + 500]
            placeholders = ", ".join("?" * len(chunk))
            query = f"SELECT * FROM metadata WHERE accession IN ({placeholders})"
            with self._lock:
                for row in self._connection.execute(query, chunk):
                    record = dict(row)
                    found[record.pop("accession")] = record
        return found

    def lookup(self, accessions: Iterable[str]) -> dict[str, dict[str, object]]:
        """Return metadata rows keyed by accession, for the accessions found."""
        return self._chunked_lookup(list(accessions))

    def contains_accessions(self, accessions: Iterable[str]) -> bool:
        """Whether any of ``accessions`` is a genome of this metadata file.

        Used to tell a GTDB database from one whose genome names merely look
        like accessions (fungal RefSeq genomes are ``GCF_`` too, but they are
        not in GTDB's metadata).  Stopping at the first match keeps the query
        small even for a batch of sampled names.
        """
        unique = list(dict.fromkeys(value for value in accessions if value))
        for start in range(0, len(unique), 500):
            chunk = unique[start : start + 500]
            placeholders = ", ".join("?" * len(chunk))
            with self._lock:
                row = self._connection.execute(
                    f"SELECT 1 FROM metadata_data WHERE accession IN ({placeholders}) "
                    "LIMIT 1",
                    chunk,
                ).fetchone()
            if row is not None:
                return True
        return False

    # -- hit -> taxonomy -------------------------------------------------

    def resolve(
        self,
        hits: Iterable[tuple[str, str]],
        *,
        sources: Sequence[str] | None = None,
    ) -> list[HitTaxonomy]:
        """Map each ``(Genome_file, Contig_name)`` hit to its taxonomy.

        Replicates ``sylph-tax taxprof`` (see :func:`sylphtax_probes`): the
        probe keys of both passes are fetched in one batch, and per hit the
        first key of pass 1 wins over the first key of pass 0.  ``sources``
        restricts the search to those source names (``--tax-source``), as if
        sylph-tax had been given only those files: a key that exists only in an
        excluded source is a miss.

        For a key found in several sources the **lineage** comes from the
        largest ``ord`` and the **host** from the largest ``ord`` whose row has
        one — two different sources, possibly, exactly like sylph-tax's two
        dicts (docs/project/multi-db-design.md B3).  The returned
        :class:`HitTaxonomy` is all ``None`` for a hit without any taxonomy.
        """
        pairs = [
            (str(genome or ""), str(contig or "")) for genome, contig in hits
        ]
        if not pairs:
            return []
        if not self._sources:
            return [NO_HIT_TAXONOMY] * len(pairs)
        allowed = None if sources is None else set(sources)
        probes = [sylphtax_probes(genome, contig) for genome, contig in pairs]
        keys = {key for probe in probes for key in probe.probe_keys()}
        entries = self._taxonomy_entries(keys, allowed)

        resolved: list[HitTaxonomy] = []
        for probe in probes:
            taxonomy = NO_HIT_TAXONOMY
            for pass_keys in (probe.pass0, probe.pass1):
                for key in pass_keys:
                    entry = entries.get(key)
                    if entry is not None:
                        taxonomy = HitTaxonomy(
                            source=entry[0], key=key, lineage=entry[1], host=entry[2]
                        )
                        break
            resolved.append(taxonomy)
        return resolved

    def _taxonomy_entries(
        self, keys: Iterable[str], allowed: set[str] | None
    ) -> dict[str, tuple[str, str, str | None]]:
        """``key -> (source name, lineage, host)`` merged across sources by ``ord``.

        Two dictionaries, as sylph-tax has: the lineage from the largest
        ``ord`` that has the key, the host from the largest ``ord`` that has the
        key *and* a host.
        """
        # The empty key is a valid key: setup stores it (sylph-tax accepts it)
        # and a pass-0 contig probe is "" for a contig such as "|extra".
        unique = list(dict.fromkeys(keys))
        lineages: dict[str, tuple[int, str, str]] = {}
        hosts: dict[str, tuple[int, str]] = {}

        def add(key: str, ord_value: int, name: str, lineage: str, host: str | None,
                ) -> None:
            best = lineages.get(key)
            if best is None or ord_value >= best[0]:
                lineages[key] = (ord_value, name, lineage)
            if host is not None:
                previous = hosts.get(key)
                if previous is None or ord_value >= previous[0]:
                    hosts[key] = (ord_value, host)

        # 500 parameters per statement stays well below SQLite's variable limit.
        for start in range(0, len(unique), 500):
            chunk = unique[start : start + 500]
            placeholders = ", ".join("?" * len(chunk))
            query = (
                "SELECT e.key AS key, s.ord AS ord, s.name AS name, "
                "l.value AS lineage, h.value AS host "
                "FROM taxonomy_entry AS e "
                "JOIN taxonomy_source AS s ON s.id = e.source_id "
                "LEFT JOIN dict_tax_lineage AS l ON l.id = e.lineage "
                "LEFT JOIN dict_tax_host AS h ON h.id = e.host "
                f"WHERE e.key IN ({placeholders})"
            )
            with self._lock:
                rows = self._connection.execute(query, chunk).fetchall()
            for row in rows:
                name = str(row["name"])
                if allowed is not None and name not in allowed:
                    continue
                lineage = row["lineage"]
                if lineage is None:  # pragma: no cover - the builder always sets it
                    continue
                add(str(row["key"]), int(row["ord"]), name, str(lineage), row["host"])

        virtual = self._virtual_source
        if virtual is not None and (allowed is None or virtual.name in allowed):
            for start in range(0, len(unique), 500):
                chunk = unique[start : start + 500]
                placeholders = ", ".join("?" * len(chunk))
                query = (
                    "SELECT m.accession AS key, d.value AS lineage "
                    "FROM metadata_data AS m "
                    "JOIN dict_gtdb_taxonomy AS d ON d.id = m.gtdb_taxonomy "
                    f"WHERE m.accession IN ({placeholders})"
                )
                with self._lock:
                    rows = self._connection.execute(query, chunk).fetchall()
                for row in rows:
                    add(
                        str(row["key"]),
                        virtual.ord,
                        virtual.name,
                        str(row["lineage"]),
                        None,
                    )

        return {
            key: (name, lineage, hosts[key][1] if key in hosts else None)
            for key, (_, name, lineage) in lineages.items()
        }

    def summary(self) -> dict[str, object]:
        """Return a JSON-friendly description of this metadata database.

        This is what ``GET /info`` publishes, so it names the file without its
        directory: the absolute path is not exposed to clients.  Likewise a
        taxonomy source's ``origin`` is reduced to its base name (or kept as
        the URL it was downloaded from), never a local path.
        """
        summary: dict[str, object] = {
            "file": self.path.name,
            "size": self.path.stat().st_size,
            "columns": list(self.field_columns),
            "rows": None,
            "release": self.info.get("release") or None,
            "built": self.info.get("built") or None,
            "format_version": self.info.get("format_version") or None,
            "reps_only": self.info.get("reps_only") == "true",
            "sources": [],
        }
        try:
            summary["rows"] = int(self.info.get("rows", ""))
        except ValueError:
            with self._lock:
                row = self._connection.execute("SELECT COUNT(*) FROM metadata").fetchone()
            summary["rows"] = int(row[0])
        try:
            summary["sources"] = json.loads(self.info.get("sources", "[]"))
        except json.JSONDecodeError:
            summary["sources"] = []
        summary["taxonomy_sources"] = self.taxonomy_sources()
        summary["taxonomy_overlaps"] = self.taxonomy_overlaps()
        summary["sylphtax_rules"] = self.info.get("sylphtax_rules") or None
        return summary

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

