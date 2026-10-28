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

import dnaio

#: ANI (percent) at or above which two genomes are called the same species.
SPECIES_ANI = 95.0

#: ANI (percent) at or above which two genomes are called the same genus.
GENUS_ANI = 85.0

#: Thresholds returned with every ``/assembly`` response.
INTERPRETATION_THRESHOLDS: dict[str, float] = {
    "species": SPECIES_ANI,
    "genus": GENUS_ANI,
}

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


def interpret_ani(ani: float) -> str:
    """Label an ANI (percent) as ``same species``, ``same genus`` or ``distant``.

    >>> interpret_ani(99.0), interpret_ani(90.0), interpret_ani(70.0)
    ('same species', 'same genus', 'distant')
    """
    if ani >= SPECIES_ANI:
        return "same species"
    if ani >= GENUS_ANI:
        return "same genus"
    return "distant"
