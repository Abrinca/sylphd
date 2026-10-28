"""Structural validation of uploaded sample-sketch bytes before decoding.

Why this exists
---------------
``pysylph.SampleSketch.from_bytes`` hands the uploaded bytes to ``bincode`` and
then to sylph's ``SequencesSketch`` deserializer.  That deserializer
pre-allocates an ``FxHashMap`` from the *declared* length of ``kmer_counts``
(``FxHashMap::with_capacity`` in sylph's ``types.rs``), before reading a single
k-mer.  An eight-byte upload that declares 100 000 000 k-mers therefore makes
sylph reserve ~2 GB of memory; on failure Rust aborts the process, and an abort
cannot be caught.  One small request would take down the whole server.

So the fixed header is walked here first, with ``struct.unpack_from`` and
without touching the k-mer entries: the length prefix is checked against the
real upload size, and only then is the rest parsed.  ``from_bytes`` is still
the decoder — this module only proves it cannot be made to over-allocate.

The layout is tied to the pinned sylph version (see ``uv.lock`` and
``ref/sylph``): ``ref/sylph/src/types.rs``, ``struct SequencesSketch``,
serialized by bincode 1.x's legacy config (fixint little-endian; trailing bytes
are ignored):

1. ``kmer_counts``: ``u64 n``, then ``n`` × (``u64`` kmer, ``u32`` count)
2. ``c``: u64
3. ``k``: u64
4. ``file_name``: String (u64 length + UTF-8 bytes)
5. ``sample_name``: Option<String> (u8 tag, 0 = None / 1 = Some + String)
6. ``paired``: bool (u8, 0/1)
7. ``mean_read_length``: f64

A sylph upgrade that changes this struct must change this module too; the
tests compare the parsed header against a decoded sketch to catch drift.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Any

#: Size of one ``(kmer, count)`` entry: u64 + u32.
_ENTRY_SIZE = 12

_U64 = struct.Struct("<Q")
_U8 = struct.Struct("<B")
_F64 = struct.Struct("<d")


class SketchFormatError(ValueError):
    """Raised when uploaded bytes cannot be a valid ``SampleSketch``.

    The message names the offending field; the server turns it into a 400.
    """


@dataclass(frozen=True)
class SketchHeader:
    """The fixed-size fields of a ``SequencesSketch``, already validated."""

    c: int
    k: int
    file_name: str
    sample_name: str | None
    paired: bool
    mean_read_length: float
    #: Number of k-mer entries declared by the length prefix.
    kmer_count: int


def _take(
    layout: struct.Struct, view: memoryview, offset: int, field: str
) -> tuple[Any, int]:
    """Unpack one field, or fail if the upload ends inside it."""
    if offset + layout.size > len(view):
        raise SketchFormatError(
            f"sketch is truncated inside '{field}' "
            f"(needs {layout.size} bytes at offset {offset}, "
            f"only {len(view) - offset} left)"
        )
    return layout.unpack_from(view, offset)[0], offset + layout.size


def _take_string(view: memoryview, offset: int, field: str) -> tuple[str, int]:
    """Read a bincode ``String``: u64 length followed by UTF-8 bytes."""
    length, offset = _take(_U64, view, offset, f"{field} length")
    available = len(view) - offset
    if length > available:
        raise SketchFormatError(
            f"sketch '{field}' declares {length} bytes but only {available} remain"
        )
    raw = bytes(view[offset : offset + length])
    try:
        return raw.decode("utf-8"), offset + length
    except UnicodeDecodeError:
        raise SketchFormatError(f"sketch '{field}' is not valid UTF-8") from None


def parse_sketch_header(data: bytes | bytearray | memoryview) -> SketchHeader:
    """Parse and validate the header of ``data``; trailing bytes are allowed."""
    view = memoryview(data)
    size = len(view)

    if size < _U64.size:
        raise SketchFormatError(
            f"sketch is {size} bytes, too short for a k-mer count prefix"
        )
    count = _U64.unpack_from(view, 0)[0]
    # Checked before anything else: this is the value that would make sylph's
    # deserializer reserve gigabytes (and abort) if it were trusted.
    kmer_end = _U64.size + count * _ENTRY_SIZE
    if kmer_end > size:
        raise SketchFormatError(
            f"sketch declares {count} k-mers ({count * _ENTRY_SIZE} bytes) "
            f"but is only {size} bytes long"
        )

    offset = kmer_end
    c, offset = _take(_U64, view, offset, "c")
    k, offset = _take(_U64, view, offset, "k")
    file_name, offset = _take_string(view, offset, "file_name")

    tag, offset = _take(_U8, view, offset, "sample_name tag")
    if tag == 1:
        sample_name: str | None
        sample_name, offset = _take_string(view, offset, "sample_name")
    elif tag == 0:
        sample_name = None
    else:
        raise SketchFormatError(
            f"sketch 'sample_name' tag is {tag}, expected 0 (None) or 1 (Some)"
        )

    paired, offset = _take(_U8, view, offset, "paired")
    if paired not in (0, 1):
        raise SketchFormatError(f"sketch 'paired' flag is {paired}, expected 0 or 1")

    mean_read_length, offset = _take(_F64, view, offset, "mean_read_length")
    if not math.isfinite(mean_read_length):
        raise SketchFormatError(
            f"sketch 'mean_read_length' is {mean_read_length!r}, "
            "expected a finite float"
        )

    return SketchHeader(
        c=c,
        k=k,
        file_name=file_name,
        sample_name=sample_name,
        paired=bool(paired),
        mean_read_length=mean_read_length,
        kmer_count=count,
    )


def validate_sketch_bytes(data: bytes | bytearray | memoryview) -> tuple[int, int]:
    """Return ``(c, k)`` of an upload, or raise :class:`SketchFormatError`.

    Safe to call on untrusted bytes: it never iterates the k-mer entries and
    never asks the decoder to allocate from an unverified length prefix.
    """
    header = parse_sketch_header(data)
    return header.c, header.k
