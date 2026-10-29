"""Parse one uploaded assembly (FASTA/FASTQ, optionally gzip) for ``POST /assembly``.

Clients normally send a sketch.  A raw sequence is only accepted with
``--allow-sequences``; then the server has to parse and sketch it itself.
Everything here is defensive: raw uploads are bounded before decompression (gzip bombs), after decompression (another bomb),
and every record is checked to be nucleotide sequence.  Error messages never
repeat sequence data or record headers.
"""

from __future__ import annotations

import io
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import dnaio

#: ANI (percent) at or above which two genomes are called the same species.
#: This is GTDB's species ANI circumscription and the only hard threshold.  The
#: same 95 % is applied to every taxonomy source (design note B6); outside GTDB
#: it is a heuristic, which :func:`classify_hits` reports as a note.
SPECIES_ANI = 95.0

#: k-mer ANI below this (percent) is imprecise.  A soft hint, never a cut-off.
IMPRECISE_BELOW_ANI = 85.0

#: Thresholds returned with every ``/assembly`` response.
INTERPRETATION_THRESHOLDS: dict[str, float] = {
    "species": SPECIES_ANI,
    "imprecise_below": IMPRECISE_BELOW_ANI,
}

#: Every rank prefix a lineage may use, shallowest first, with its name.
#: GTDB writes ``d__..;s__``; ICTV and IMG/VR lineages add realm (``r__``),
#: kingdom (``k__``) and strain (``t__``), fungal RefSeq starts at ``k__``.
LINEAGE_RANKS: tuple[tuple[str, str], ...] = (
    ("d", "domain"),
    ("r", "realm"),
    ("k", "kingdom"),
    ("p", "phylum"),
    ("c", "class"),
    ("o", "order"),
    ("f", "family"),
    ("g", "genus"),
    ("s", "species"),
    ("t", "strain"),
)

#: Rank prefix -> display name (``d`` -> ``domain``).
RANK_NAMES: dict[str, str] = dict(LINEAGE_RANKS)

#: GTDB rank prefixes, shallowest first, and their names.
GTDB_RANKS: tuple[tuple[str, str], ...] = tuple(
    (prefix, name) for prefix, name in LINEAGE_RANKS if prefix in "dpcofgs"
)

#: The comparison family of every GTDB lineage: GTDB hits compare with each
#: other whatever taxonomy source resolved them; other lineages only with their
#: own source (design note B6).
GTDB_FAMILY = "GTDB"

#: ``tax_source`` of a GTDB lineage that no taxonomy source named.
GTDB_SOURCE = "GTDB"

#: First bytes of a gzip stream (RFC 1952).
GZIP_MAGIC = b"\x1f\x8b"

#: How much larger the decompressed body may be than ``--max-sequence-mb``.
DECOMPRESSION_FACTOR = 5

#: Nucleotides accepted in an uploaded assembly.
ALLOWED_BASES = frozenset("ACGTN")


class SequenceError(ValueError):
    """An upload is not a usable assembly (client error, HTTP 400)."""


class SequenceTooLarge(SequenceError):
    """An upload (or its decompressed form) is too large (HTTP 413)."""


def is_gzip(data: bytes) -> bool:
    """Whether ``data`` starts with the gzip magic bytes."""
    return data[:2] == GZIP_MAGIC


def _describe_limit(limit: int) -> str:
    if limit >= 1024 * 1024 and limit % (1024 * 1024) == 0:
        return f"{limit // (1024 * 1024)} MB"
    return f"{limit} bytes"


def gunzip_limited(data: bytes, limit: int) -> bytes:
    """Decompress gzip ``data``, refusing output larger than ``limit`` bytes.

    Concatenated gzip members (``cat a.gz b.gz``) are decompressed in turn,
    under one total ``limit``; every member must be complete.
    """
    chunks: list[bytes] = []
    total = 0
    while data:
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            chunk = decompressor.decompress(data, limit + 1 - total)
        except zlib.error as error:
            raise SequenceError("body is not valid gzip data") from error
        total += len(chunk)
        if total > limit:
            raise SequenceTooLarge(
                f"decompressed assembly exceeds the {_describe_limit(limit)} limit"
            )
        if not decompressor.eof:
            raise SequenceError("gzip data is truncated (incomplete stream)")
        chunks.append(chunk)
        data = decompressor.unused_data
    return b"".join(chunks)


@dataclass(frozen=True)
class ParsedAssembly:
    """One assembly: every record of the upload, as one sample."""

    records: int
    length: int
    gc: float
    sequences: tuple[str, ...]


def parse_assembly(data: bytes) -> ParsedAssembly:
    """Parse FASTA or FASTQ bytes into sequences, with basic validation.

    Raises :class:`SequenceError` for empty, malformed, non-nucleotide or
    empty-sequence uploads.  The message says *where* a problem is, never what
    the sequence or header contains.
    """
    stripped = data.lstrip()
    if not stripped:
        raise SequenceError("empty sequence upload")
    if stripped[:1] == b">":
        fileformat = "fasta"
    elif stripped[:1] == b"@":
        fileformat = "fastq"
    else:
        raise SequenceError("body is neither FASTA nor FASTQ")

    sequences: list[str] = []
    length = 0
    gc = 0
    try:
        with dnaio.open(io.BytesIO(stripped), fileformat=fileformat) as reader:
            for index, record in enumerate(reader, start=1):
                sequence = record.sequence.upper()
                if not sequence:
                    raise SequenceError(f"sequence #{index} is empty")
                invalid = set(sequence) - ALLOWED_BASES
                if invalid:
                    raise SequenceError(
                        f"sequence #{index} contains characters other than ACGTN"
                    )
                sequences.append(sequence)
                length += len(sequence)
                gc += sequence.count("G") + sequence.count("C")
    except SequenceError:
        raise
    except (ValueError, OSError, dnaio.FileFormatError, dnaio.UnknownFileFormat) as error:
        # dnaio's messages may quote headers or sequence lines; keep them out.
        raise SequenceError(
            f"body is not a valid {fileformat.upper()} file ({type(error).__name__})"
        ) from error

    if not sequences or length == 0:
        raise SequenceError("the upload contains no sequences")
    return ParsedAssembly(
        records=len(sequences),
        length=length,
        gc=round(100.0 * gc / length, 2),
        sequences=tuple(sequences),
    )


def parse_gtdb_taxonomy(taxonomy: object) -> dict[str, str]:
    """Parse ``d__Bacteria;p__...;s__Escherichia coli`` into ``{"d": "Bacteria", ...}``.

    Unknown or empty ranks (``g__``) are left out.

    >>> parse_gtdb_taxonomy("d__Bacteria;p__Bacillota;g__;s__")
    {'d': 'Bacteria', 'p': 'Bacillota'}
    """
    ranks: dict[str, str] = {}
    if not isinstance(taxonomy, str):
        return ranks
    valid = {prefix for prefix, _ in GTDB_RANKS}
    for part in taxonomy.split(";"):
        prefix, sep, name = part.strip().partition("__")
        name = name.strip()
        if sep and prefix in valid and name:
            ranks[prefix] = name
    return ranks


def gtdb_species(taxonomy: object) -> str:
    """The GTDB species name of a lineage, or ``""``.

    >>> gtdb_species("d__Bacteria;s__Escherichia coli")
    'Escherichia coli'
    >>> gtdb_species("d__Bacteria;g__Escherichia")
    ''
    """
    return parse_gtdb_taxonomy(taxonomy).get("s", "")


def shared_rank(a: dict[str, str], b: dict[str, str]) -> str | None:
    """Deepest rank name two parsed lineages share, or ``None``.

    A rank only counts when it and every shallower rank agree, so two genera
    with the same name in different families do not match.

    >>> shared_rank({"d": "X", "p": "A", "c": "B"}, {"d": "X", "p": "A", "c": "C"})
    'phylum'
    """
    shared: str | None = None
    for prefix, name in GTDB_RANKS:
        if prefix in a and a.get(prefix) == b.get(prefix):
            shared = name
        else:
            break
    return shared


def rank_display(key: str) -> str:
    """Display name of a rank key: ``d`` -> ``domain``, ``level 2`` -> itself."""
    return RANK_NAMES.get(key, key)


@dataclass(frozen=True)
class Lineage:
    """One hit's lineage, parsed into ordered named ranks, whatever its source.

    ``ranks`` holds ``(key, name)`` pairs, shallowest first: a rank prefix
    (``d`` ... ``t``) for prefixed lineages such as GTDB, ICTV, IMG/VR and
    fungal RefSeq, or ``level 1`` ... ``level N`` for the prefix-less ones
    (UHGV).  ``source`` is the ``tax_source`` to report; ``family`` is what the
    lineage may be compared with: every GTDB lineage has the family ``GTDB``
    whatever source resolved it, any other lineage only compares with hits of
    its own source (design note B6).
    """

    ranks: tuple[tuple[str, str], ...] = ()
    source: str | None = None
    family: str | None = None

    def __bool__(self) -> bool:
        return bool(self.ranks)

    def __len__(self) -> int:
        return len(self.ranks)

    @property
    def names(self) -> dict[str, str]:
        """Rank key -> taxon, shallowest first."""
        return dict(self.ranks)

    @property
    def taxon(self) -> str | None:
        """The last named rank: the finest taxon the lineage names."""
        return self.ranks[-1][1] if self.ranks else None

    @property
    def species(self) -> str | None:
        """The name at rank ``species`` (``s__``), or ``None``."""
        return self.names.get("s")

    @property
    def levels(self) -> bool:
        """Whether the ranks are prefix-less levels (``level 1`` ...)."""
        return bool(self.ranks) and all(
            key.startswith("level ") for key, _ in self.ranks
        )

    @property
    def taxonomy(self) -> dict[str, str]:
        """Rank display name -> taxon, shallowest first (the JSON ``taxonomy``)."""
        return {rank_display(key): name for key, name in self.ranks}

    @property
    def species_key(self) -> str | None:
        """The species-equivalent rank key, or ``None``.

        ``s`` for a lineage that has a species rank (GTDB, fungal RefSeq,
        IMG/VR); for a prefix-less lineage the deepest level, which is its
        finest unit (a UHGV vOTU).  A prefixed lineage without ``s__`` (a
        partial GTDB lineage) has none.
        """
        if "s" in self.names:
            return "s"
        if self.levels:
            return self.ranks[-1][0]
        return None


def parse_lineage(
    taxonomy: object, *, source: str | None = None, family: str | None = None
) -> Lineage:
    """Parse any lineage string, GTDB's or not, into a :class:`Lineage`.

    Tokens are separated by ``;``.  Tokens with a known rank prefix
    (``d__``...``t__``) are kept as that rank and unknown or empty ones are
    dropped, as :func:`parse_gtdb_taxonomy` does.  When no token has a known
    prefix the lineage is read as *levels*: a leading ``root`` is dropped (it is
    not a taxon) and the rest become ``level 1`` ... ``level N``, which is how
    UHGV's ``vFAM-...`` and ICTV's ``root;Monodnaviria;...`` lineages are
    labelled.

    >>> parse_lineage("d__Bacteria;p__Bacillota;g__;s__").taxonomy
    {'domain': 'Bacteria', 'phylum': 'Bacillota'}
    >>> parse_lineage("vFAM-00013;vOTU-052719").taxonomy
    {'level 1': 'vFAM-00013', 'level 2': 'vOTU-052719'}
    >>> parse_lineage("root;Monodnaviria;Microviridae").taxonomy
    {'level 1': 'Monodnaviria', 'level 2': 'Microviridae'}
    """
    if not isinstance(taxonomy, str):
        return Lineage((), source, family)
    tokens = [token.strip() for token in taxonomy.split(";")]
    ranks: list[tuple[str, str]] = []
    seen: set[str] = set()
    for token in tokens:
        prefix, separator, name = token.partition("__")
        key = prefix.strip().lower()
        name = name.strip()
        if separator and key in RANK_NAMES and name and key not in seen:
            ranks.append((key, name))
            seen.add(key)
    if not ranks:
        # No rank prefix at all: a prefix-less lineage of levels.
        for token in tokens:
            if not token or token.lower() == "root":
                continue
            ranks.append((f"level {len(ranks) + 1}", token))
    return Lineage(tuple(ranks), source, family)


def genome_id(hit: Mapping[str, Any], *, contig_name: str | None = None) -> str | None:
    """A stable identifier for the database genome a hit came from.

    A hit's ``Genome_file`` is not always an identifier: UHGV stores every
    genome in ``votus_full.fna.gz``, so the file name is the same for all of
    them.  The rule is

    * ``accession`` for a GTDB hit (and for any hit that carries one),
    * ``tax_key`` for a hit a **non-GTDB** taxonomy source resolved (the UHGV
      contig id, the fungal key, ...),
    * else the first word of the hit's ``Contig_name`` (a database genome's
      description often starts with its own id), and
    * else the ``Genome_file``.

    ``contig_name`` lets a caller pass the genome's ``description`` when the
    hit mapping itself does not carry ``Contig_name`` (``/assembly``).
    """
    source = hit.get("tax_source")
    key = hit.get("tax_key")
    if key and isinstance(source, str) and source and not _is_gtdb_source(source):
        return str(key)
    accession = hit.get("accession")
    if accession:
        return str(accession)
    contig = contig_name if contig_name is not None else hit.get("Contig_name")
    words = str(contig or "").split()
    if words:
        return words[0]
    genome = hit.get("genome_file") or hit.get("Genome_file")
    return str(genome) if genome else None


def _is_gtdb_source(source: object) -> bool:
    """Whether a taxonomy source name is one of GTDB's."""
    return isinstance(source, str) and source.upper().startswith("GTDB")


def hit_lineage(hit: Mapping[str, Any]) -> Lineage:
    """The lineage of one hit mapping: GTDB's for a GTDB hit, else ``tax_lineage``.

    A hit that no taxonomy source resolved (or one a GTDB source resolved) is
    parsed from its ``gtdb_taxonomy``, as before, and compared as a GTDB
    lineage.  A hit that a **non-GTDB** source resolved takes that source's
    ``tax_lineage`` — even when the genome also has GTDB metadata, because the
    resolution says which taxonomy the hit belongs to — and is compared only
    with hits of the same ``tax_source``.  Without a ``tax_lineage`` such a hit
    falls back to its GTDB lineage.
    """
    raw_source = hit.get("tax_source")
    source = raw_source if isinstance(raw_source, str) and raw_source else None
    if source is not None and not _is_gtdb_source(source):
        lineage = parse_lineage(hit.get("tax_lineage"), source=source, family=source)
        if lineage:
            return lineage
        gtdb = parse_lineage(hit.get("gtdb_taxonomy"))
        if gtdb:
            return Lineage(gtdb.ranks, GTDB_SOURCE, GTDB_FAMILY)
        return Lineage(())
    gtdb = parse_lineage(hit.get("gtdb_taxonomy"))
    if gtdb:
        return Lineage(gtdb.ranks, source or GTDB_SOURCE, GTDB_FAMILY)
    family = GTDB_FAMILY if _is_gtdb_source(source) else source
    return parse_lineage(hit.get("tax_lineage"), source=source, family=family)


def shared_lineage_rank(a: Lineage, b: Lineage) -> str | None:
    """Deepest rank two lineages of one family share, or ``None``.

    As :func:`shared_rank`, but over any :class:`Lineage`: prefixes map to
    ``domain`` ... ``strain``, prefix-less levels to ``level 1`` ... .  The
    caller decides whether the two may be compared at all (same family).

    >>> shared_lineage_rank(parse_lineage("d__X;p__A;c__B"), parse_lineage("d__X;p__A;c__C"))
    'phylum'
    """
    shared: str | None = None
    for (key, name), (other_key, other_name) in zip(a.ranks, b.ranks):
        if key != other_key or name != other_name:
            break
        shared = rank_display(key)
    return shared


def comparable(a: Lineage, b: Lineage) -> bool:
    """Whether two lineages may be compared (same family, design note B6)."""
    return a.family is not None and a.family == b.family


#: Species-threshold caveats for the sources that are not GTDB (B6).  The 95 %
#: threshold is a prokaryote convention; outside prokaryotes it is a hint.  The
#: source's actual name is filled in, so a server with UHGV_ictv or
#: FungiRefSeq-latest does not talk about UHGV or "fungi" in general.
VIRUS_CAVEAT = (
    "{source} (viruses): 95 % ANI matches the vOTU convention (MIUViG: 95 % ANI "
    "over 85 % of the shorter genome), but sylph's ANI ignores the aligned "
    "fraction, so a species call may be wrong"
)
FUNGI_CAVEAT = (
    "{source} (fungi): there is no established ANI species boundary, so 95 % is "
    "a heuristic and species calls may be wrong"
)
GENERIC_CAVEAT = (
    "{source}: the 95 % species threshold is a prokaryote convention, so it is a "
    "heuristic here and a species call may be wrong"
)


def _is_virus(lineage: Lineage) -> bool:
    """Whether a lineage is a viral one (UHGV, IMG/VR, or a Viruses lineage)."""
    source = (lineage.source or "").upper()
    if source.startswith(("UHGV", "IMGVR")):
        return True
    for _, name in lineage.ranks:
        lowered = name.lower()
        if lowered.startswith(("votu", "vfam")) or lowered == "viruses":
            return True
    return False


def _is_fungi(lineage: Lineage) -> bool:
    """Whether a lineage is a fungal one (a Fungi* source, or ``k__Fungi``)."""
    source = (lineage.source or "").lower()
    if source.startswith("fungi"):
        return True
    return any(name.lower().startswith("fungi") for _, name in lineage.ranks)


def species_caveat(lineage: Lineage) -> str | None:
    """Why the 95 % threshold is only a heuristic for this lineage, or ``None``.

    ``None`` for GTDB (the threshold is GTDB's species circumscription) and for
    a hit without a lineage; virus and fungus lineages get a specific text, any
    other source the generic one.
    """
    if not lineage or lineage.family == GTDB_FAMILY:
        return None
    source = lineage.source or "this source"
    if _is_virus(lineage):
        return VIRUS_CAVEAT.format(source=source)
    if _is_fungi(lineage):
        return FUNGI_CAVEAT.format(source=source)
    return GENERIC_CAVEAT.format(source=source)


def classify_hits(
    hits: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Add ``relation``, ``shared_rank``, ``taxonomy`` and friends to every hit.

    ``hits`` must be sorted by ANI, best first, and carry ``ani`` (percent, full
    precision: the 95 threshold is applied to it unrounded), ``accession``,
    ``gtdb_taxonomy`` and, for a hit from a sylph-tax source, ``tax_lineage``
    and ``tax_source`` (all ``None`` without metadata); they are modified in
    place, and their ``ani`` is rounded to two decimals for display.  Returns
    ``(assignment, closest)``.

    * Best hit ANI >= 95 and a known lineage: the query is assigned to that
      hit's species — GTDB's for a GTDB hit, the last named rank for any other
      source (design note B6: 95 % ANI for every source).  Every hit's
      ``relation`` is the deepest rank it shares with the assigned lineage,
      e.g. ``same genus``; no shared domain is ``different domain``.  The same
      species is ``same species`` whatever the hit's own ANI.
    * Best hit ANI < 95: no assignment.  The best hit is the ``closest
      relative`` and the others are described relative to it (``same genus as
      closest relative``).
    * No lineage for the best hit (no metadata): ANI only, ``same species``
      from 95, otherwise ``related``.  A species hit still yields an
      ``assignment``, with ``taxon`` and ``gtdb_taxonomy`` set to ``None``.

    Lineages are parsed generically (:func:`parse_lineage`), so GTDB's
    ``d__..;s__``, ICTV's ``root;Monodnaviria;..``, fungal ``k__Eukaryota;..``
    and UHGV's prefix-less ``vFAM-..;vOTU-..`` all classify.  Relations are
    only computed **within one source** (:func:`comparable`): a hit from
    another taxonomy source is ``different source`` (or ``different source
    from closest relative``) with no ``shared_rank``, because rank names of two
    sources are not the same yardstick.

    A hit (and ``assignment``/``closest``) with a lineage also carries
    ``taxonomy`` (rank display name -> taxon, shallowest first) and
    ``taxonomy_source``; those keys are absent without a lineage, so a
    metadata-less response keeps its shape.  When the best hit is not from GTDB
    the summary carries a ``note`` explaining what 95 % ANI means for that
    source (:func:`species_caveat`); the CLI prints it on stderr.

    ``shared_rank`` is the machine-readable form of the rank (``species`` ...
    ``domain``, ``realm``, ``kingdom``, ``strain``, ``level N`` or ``None``).
    ``ani_species_level`` says the hit's ANI alone reaches the species
    threshold, which can happen for a hit whose species differs from the
    assigned one.  ``closest`` is the best hit's summary, or ``None`` without
    hits.
    """
    if not hits:
        return None, None
    best = hits[0]
    best_lineage = hit_lineage(best)
    best_ani = float(best["ani"])
    species_level = best_ani >= SPECIES_ANI
    note = species_caveat(best_lineage)

    def summary(hit: dict[str, Any]) -> dict[str, Any]:
        lineage = hit_lineage(hit)
        summary = {
            "rank": "species",
            "taxon": lineage.taxon,
            "gtdb_taxonomy": (
                hit.get("gtdb_taxonomy")
                if lineage and lineage.family == GTDB_FAMILY
                else None
            ),
            "accession": hit.get("accession"),
            "ani": round(float(hit["ani"]), 2),
        }
        if lineage:
            summary["taxonomy"] = lineage.taxonomy
            summary["taxonomy_source"] = lineage.source
        if note:
            summary["note"] = note
        return summary

    closest = summary(best)
    assignment = summary(best) if species_level else None

    for index, hit in enumerate(hits):
        ani = float(hit["ani"])
        hit["ani"] = round(ani, 2)
        hit["ani_species_level"] = ani >= SPECIES_ANI
        lineage = hit_lineage(hit)
        if lineage:
            hit["taxonomy"] = lineage.taxonomy
            hit["taxonomy_source"] = lineage.source
        if not best_lineage:
            hit["shared_rank"] = "species" if ani >= SPECIES_ANI else None
            hit["relation"] = "same species" if ani >= SPECIES_ANI else "related"
            continue
        if index == 0:
            hit["shared_rank"] = "species" if species_level else None
            hit["relation"] = "same species" if species_level else "closest relative"
            continue
        if not lineage:
            hit["shared_rank"] = None
            hit["relation"] = "unknown lineage"
            continue
        if not comparable(best_lineage, lineage):
            hit["shared_rank"] = None
            hit["relation"] = (
                "different source"
                if species_level
                else "different source from closest relative"
            )
            continue
        rank = shared_lineage_rank(best_lineage, lineage)
        hit["shared_rank"] = rank
        # "different domain" is GTDB's wording; another source may start at a
        # realm or a level, so it gets a source-neutral label.
        nothing = "different domain" if lineage.family == GTDB_FAMILY else "different lineage"
        if species_level:
            hit["relation"] = f"same {rank}" if rank else nothing
        else:
            hit["relation"] = (
                f"same {rank} as closest relative"
                if rank
                else f"{nothing} from closest relative"
            )
    return assignment, closest
