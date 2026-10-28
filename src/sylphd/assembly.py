"""Parse one uploaded assembly (FASTA/FASTQ, optionally gzip) for ``POST /assembly``.

The web page sends raw sequences instead of a sketch, so the server has to
parse and sketch them itself.  Everything here is defensive: uploads are
bounded before decompression (gzip bombs), after decompression (another bomb),
and every record is checked to be nucleotide sequence.  Error messages never
repeat sequence data or record headers.
"""

from __future__ import annotations

import io
import zlib
from dataclasses import dataclass
from typing import Any

import dnaio

#: ANI (percent) at or above which two genomes are called the same species.
#: This is GTDB's species ANI circumscription and the only hard threshold.
SPECIES_ANI = 95.0

#: k-mer ANI below this (percent) is imprecise.  A soft hint, never a cut-off.
IMPRECISE_BELOW_ANI = 85.0

#: Thresholds returned with every ``/assembly`` response.
INTERPRETATION_THRESHOLDS: dict[str, float] = {
    "species": SPECIES_ANI,
    "imprecise_below": IMPRECISE_BELOW_ANI,
}

#: GTDB rank prefixes, shallowest first, and their names.
GTDB_RANKS: tuple[tuple[str, str], ...] = (
    ("d", "domain"),
    ("p", "phylum"),
    ("c", "class"),
    ("o", "order"),
    ("f", "family"),
    ("g", "genus"),
    ("s", "species"),
)

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
    """Decompress gzip ``data``, refusing output larger than ``limit`` bytes."""
    too_large = SequenceTooLarge(
        f"decompressed assembly exceeds the {_describe_limit(limit)} limit"
    )
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = decompressor.decompress(data, limit + 1)
        if len(out) > limit or decompressor.unconsumed_tail:
            raise too_large
        out += decompressor.flush()
    except zlib.error as error:
        raise SequenceError("body is not valid gzip data") from error
    if len(out) > limit:
        raise too_large
    return out


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


def classify_hits(
    hits: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Add ``relation``, ``shared_rank`` and ``ani_species_level`` to every hit.

    ``hits`` must be sorted by ANI, best first, and carry ``ani`` (percent),
    ``accession`` and ``gtdb_taxonomy`` (``None`` without metadata); they are
    modified in place.  Returns ``(assignment, closest)``.

    * Best hit ANI >= 95 and a known lineage: the query is assigned to that
      hit's GTDB species (``assignment``).  Every hit's ``relation`` is the
      deepest GTDB rank it shares with the assigned lineage, e.g. ``same
      genus``; no shared domain is ``different domain``.  The same GTDB
      species is ``same species`` whatever the hit's own ANI.
    * Best hit ANI < 95: no assignment.  The best hit is the ``closest
      relative`` and the others are described relative to it (``same genus as
      closest relative``).
    * No lineage for the best hit (no metadata): ANI only, ``same species``
      from 95, otherwise ``related``.  A species hit still yields an
      ``assignment``, with ``taxon`` and ``gtdb_taxonomy`` set to ``None``.

    ``shared_rank`` is the machine-readable form of the rank (``species`` ...
    ``domain`` or ``None``).  ``ani_species_level`` says the hit's ANI alone
    reaches the species threshold, which can happen for a hit whose GTDB
    species differs from the assigned one.  ``closest`` is the best hit's
    summary, or ``None`` without hits.
    """
    if not hits:
        return None, None
    best = hits[0]
    best_lineage = parse_gtdb_taxonomy(best.get("gtdb_taxonomy"))
    best_ani = float(best["ani"])
    species_level = best_ani >= SPECIES_ANI

    def summary(hit: dict[str, Any]) -> dict[str, Any]:
        lineage = parse_gtdb_taxonomy(hit.get("gtdb_taxonomy"))
        return {
            "rank": "species",
            "taxon": lineage.get("s"),
            "gtdb_taxonomy": hit.get("gtdb_taxonomy") if lineage else None,
            "accession": hit.get("accession"),
            "ani": hit["ani"],
        }

    closest = summary(best)
    assignment = summary(best) if species_level else None

    for index, hit in enumerate(hits):
        ani = float(hit["ani"])
        hit["ani_species_level"] = ani >= SPECIES_ANI
        if not best_lineage:
            hit["shared_rank"] = "species" if ani >= SPECIES_ANI else None
            hit["relation"] = "same species" if ani >= SPECIES_ANI else "related"
            continue
        if index == 0:
            hit["shared_rank"] = "species" if species_level else None
            hit["relation"] = "same species" if species_level else "closest relative"
            continue
        lineage = parse_gtdb_taxonomy(hit.get("gtdb_taxonomy"))
        if not lineage:
            hit["shared_rank"] = None
            hit["relation"] = "unknown lineage"
            continue
        rank = shared_rank(best_lineage, lineage)
        hit["shared_rank"] = rank
        if species_level:
            hit["relation"] = f"same {rank}" if rank else "different domain"
        else:
            hit["relation"] = (
                f"same {rank} as closest relative"
                if rank
                else "different domain from closest relative"
            )
    return assignment, closest
