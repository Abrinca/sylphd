"""Tests for the pre-decode sketch validation.

``pysylph``/sylph allocates from the declared k-mer count, so these tests also
pin the layout: an accepted sketch must decode to the same ``c``/``k`` (and
whatever else ``pysylph`` exposes), and a prefix of any valid sketch must be
rejected.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path
from typing import Sequence

import dnaio
import pysylph
import pytest

from sylphd.sketchcheck import (
    SketchFormatError,
    parse_sketch_header,
    validate_sketch_bytes,
)

from conftest import (
    DATA,
    K12_PAIRED_SKETCH,
    K12_R1,
    K12_R1_SKETCH,
    K12_R2,
)

SYLSP_FILES = sorted(DATA.glob("*.sylsp"))
SYLSP_IDS = [path.name for path in SYLSP_FILES]


def _reads(path: Path) -> list[str]:
    with dnaio.open(path) as reader:
        return [record.sequence for record in reader]


@pytest.fixture(scope="module")
def single_sketch() -> pysylph.SampleSketch:
    return pysylph.Sketcher(c=200, k=31).sketch_single("single", _reads(K12_R1))


@pytest.fixture(scope="module")
def paired_sketch() -> pysylph.SampleSketch:
    return pysylph.Sketcher(c=200, k=31).sketch_paired(
        "paired", _reads(K12_R1), _reads(K12_R2)
    )


def _build(
    *,
    count: int | None = None,
    entries: Sequence[tuple[int, int]] = ((7, 1), (9, 3)),
    c: int = 200,
    k: int = 31,
    file_name: bytes = b"sample.fq",
    file_name_length: int | None = None,
    sample_name: bytes | None = None,
    sample_name_length: int | None = None,
    sample_tag: int | None = None,
    paired: int = 0,
    mean_read_length: float = 100.0,
) -> bytes:
    """Serialize a sketch-shaped byte string, with knobs for malformations."""
    data = struct.pack("<Q", len(entries) if count is None else count)
    for kmer, kmer_count in entries:
        data += struct.pack("<QI", kmer, kmer_count)
    data += struct.pack("<QQ", c, k)
    length = len(file_name) if file_name_length is None else file_name_length
    data += struct.pack("<Q", length) + file_name
    if sample_tag is None:
        sample_tag = 0 if sample_name is None else 1
    data += struct.pack("<B", sample_tag)
    if sample_tag == 1:
        name = b"" if sample_name is None else sample_name
        length = len(name) if sample_name_length is None else sample_name_length
        data += struct.pack("<Q", length) + name
    data += struct.pack("<B", paired)
    data += struct.pack("<d", mean_read_length)
    return data


class TestAcceptsValidSketches:
    @pytest.mark.parametrize("path", SYLSP_FILES, ids=SYLSP_IDS)
    def test_stored_sketch(self, path: Path) -> None:
        data = path.read_bytes()
        c, k = validate_sketch_bytes(data)
        sketch = pysylph.SampleSketch.from_bytes(data)
        assert (c, k) == (sketch.c, sketch.k)

    def test_fresh_single_sketch(self, single_sketch: pysylph.SampleSketch) -> None:
        data = single_sketch.to_bytes()
        assert validate_sketch_bytes(data) == (single_sketch.c, single_sketch.k)
        header = parse_sketch_header(data)
        assert header.paired is False
        assert header.file_name == "single"
        assert header.kmer_count == len(single_sketch.kmer_counts)

    def test_fresh_paired_sketch(self, paired_sketch: pysylph.SampleSketch) -> None:
        data = paired_sketch.to_bytes()
        assert validate_sketch_bytes(data) == (paired_sketch.c, paired_sketch.k)
        header = parse_sketch_header(data)
        assert header.paired is True
        assert header.file_name == "paired"
        assert header.kmer_count == len(paired_sketch.kmer_counts)

    def test_trailing_bytes_are_allowed(self) -> None:
        data = _build()
        assert validate_sketch_bytes(data + b"\x00trailing") == (200, 31)

    def test_optional_sample_name(self) -> None:
        header = parse_sketch_header(_build(sample_name=b"sample-a"))
        assert header.sample_name == "sample-a"
        assert parse_sketch_header(_build()).sample_name is None

    def test_empty_kmer_counts(self) -> None:
        header = parse_sketch_header(_build(entries=()))
        assert header.kmer_count == 0
        assert validate_sketch_bytes(_build(entries=())) == (200, 31)


class TestRejectsMalformed:
    def test_empty(self) -> None:
        with pytest.raises(SketchFormatError, match="too short"):
            validate_sketch_bytes(b"")

    def test_shorter_than_length_prefix(self) -> None:
        with pytest.raises(SketchFormatError, match="too short"):
            validate_sketch_bytes(b"\x01\x02\x03")

    @pytest.mark.parametrize("count", [100_000_000, 2**63, 2**64 - 1])
    def test_huge_declared_kmer_count(self, count: int) -> None:
        # The 8-byte allocation bomb: rejected from the length prefix alone.
        with pytest.raises(SketchFormatError, match="declares"):
            validate_sketch_bytes(struct.pack("<Q", count))

    def test_count_one_too_large_for_the_data(self) -> None:
        data = _build(entries=((1, 1), (2, 2)))
        declared = struct.unpack_from("<Q", data)[0]
        patched = struct.pack("<Q", declared + 1) + data[8:]
        with pytest.raises(SketchFormatError, match="declares"):
            validate_sketch_bytes(patched)

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param(_build(), id="no-sample-name"),
            pytest.param(_build(sample_name=b"sample-a", paired=1), id="all-fields"),
        ],
    )
    def test_every_truncation_is_rejected(self, data: bytes) -> None:
        for cut in range(len(data)):
            with pytest.raises(SketchFormatError):
                validate_sketch_bytes(data[:cut])

    def test_truncated_mean_read_length_names_the_field(self) -> None:
        data = _build()
        with pytest.raises(SketchFormatError, match="mean_read_length"):
            validate_sketch_bytes(data[:-1])

    @pytest.mark.parametrize("tag", [2, 3, 255])
    def test_bad_sample_name_tag(self, tag: int) -> None:
        with pytest.raises(SketchFormatError, match="tag"):
            parse_sketch_header(_build(sample_tag=tag))

    def test_some_tag_without_length(self) -> None:
        # Tag 1 must be followed by a length and a bool and the mean length;
        # cut everything after the tag.
        data = _build(sample_tag=1, sample_name=b"")
        with pytest.raises(SketchFormatError, match="sample_name"):
            parse_sketch_header(data[: len(data) - 17])

    @pytest.mark.parametrize("value", [2, 3, 255])
    def test_bad_paired_flag(self, value: int) -> None:
        with pytest.raises(SketchFormatError, match="paired"):
            parse_sketch_header(_build(paired=value))

    def test_invalid_utf8_file_name(self) -> None:
        with pytest.raises(SketchFormatError, match="UTF-8"):
            parse_sketch_header(_build(file_name=b"sample\xff.fq"))

    def test_invalid_utf8_sample_name(self) -> None:
        with pytest.raises(SketchFormatError, match="UTF-8"):
            parse_sketch_header(_build(sample_name=b"\xc3"))

    def test_huge_file_name_length(self) -> None:
        with pytest.raises(SketchFormatError, match="file_name"):
            parse_sketch_header(_build(file_name_length=2**62))

    def test_huge_sample_name_length(self) -> None:
        with pytest.raises(SketchFormatError, match="sample_name"):
            parse_sketch_header(
                _build(sample_name=b"x", sample_name_length=2**62)
            )

    @pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
    def test_non_finite_mean_read_length(self, value: float) -> None:
        with pytest.raises(SketchFormatError, match="mean_read_length"):
            parse_sketch_header(_build(mean_read_length=value))


class TestLayoutMatchesPinnedSylph:
    """Guard against sylph changing ``SequencesSketch`` under us."""

    @pytest.mark.parametrize("path", SYLSP_FILES, ids=SYLSP_IDS)
    def test_header_matches_decoded_sketch(self, path: Path) -> None:
        data = path.read_bytes()
        header = parse_sketch_header(data)
        sketch = pysylph.SampleSketch.from_bytes(data)
        # pysylph exposes c, k and kmer_counts only; compare those.
        assert (header.c, header.k) == (sketch.c, sketch.k)
        assert header.kmer_count == len(sketch.kmer_counts)
        for attribute in ("file_name", "paired", "sample_name", "mean_read_length"):
            if hasattr(sketch, attribute):
                assert getattr(sketch, attribute) == getattr(header, attribute)

    def test_paired_flag_of_known_files(self) -> None:
        # Neither pysylph attribute set includes ``paired``, so pin it against
        # the fixture files whose end type we know.
        assert parse_sketch_header(K12_R1_SKETCH.read_bytes()).paired is False
        assert parse_sketch_header(K12_PAIRED_SKETCH.read_bytes()).paired is True

    def test_header_values_of_known_files(self) -> None:
        header = parse_sketch_header(K12_PAIRED_SKETCH.read_bytes())
        assert (header.c, header.k) == (200, 31)
        assert header.file_name.endswith("k12_R1.fq")
        assert header.sample_name is None
        assert header.mean_read_length > 0
