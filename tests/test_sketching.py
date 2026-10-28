"""Tests for input parsing and local sketching."""

from __future__ import annotations

from pathlib import Path

import dnaio
import pytest

from sylphd.sketching import (
    InputError,
    SampleInput,
    build_sample_inputs,
    check_sketch,
    parse_multi_file_spec,
    sample_name_from_path,
    sketch_sample,
)

from conftest import CLODF13_FASTA, K12_PAIRED_SKETCH, K12_R1, K12_R1_SKETCH, K12_R2


class TestSampleNames:
    @pytest.mark.parametrize(
        "path, paired, expected",
        [
            ("reads.fastq.gz", False, "reads"),
            ("reads.fq", False, "reads"),
            ("reads.fasta.gz", False, "reads"),
            ("reads.sylsp", False, "reads"),
            ("/data/sample-a_R1.fastq.gz", True, "sample-a"),
            ("/data/sample-a_R2.fq", True, "sample-a"),
            ("/data/sample-a_1.fq.gz", True, "sample-a"),
            ("/data/sample-a_2.fq.gz", True, "sample-a"),
            ("/data/sample-a_R1.fastq.gz", False, "sample-a_R1"),
        ],
    )
    def test_from_path(self, path: str, paired: bool, expected: str) -> None:
        assert sample_name_from_path(path, paired=paired) == expected


class TestMultiFileSpecs:
    def test_single_files(self) -> None:
        sample = parse_multi_file_spec("s1:a.fq,b.fq")
        assert sample.name == "s1"
        assert sample.inputs == (("a.fq",), ("b.fq",))
        assert not sample.paired

    def test_pairs(self) -> None:
        sample = parse_multi_file_spec("s1:a1.fq+a2.fq,b1.fq+b2.fq")
        assert sample.name == "s1"
        assert sample.inputs == (("a1.fq", "a2.fq"), ("b1.fq", "b2.fq"))
        assert sample.paired

    def test_name_defaults_to_first_file(self) -> None:
        sample = parse_multi_file_spec("reads.fq.gz")
        assert sample.name == "reads"
        assert sample.inputs == (("reads.fq.gz",),)

    def test_errors(self) -> None:
        with pytest.raises(InputError, match="empty sample name"):
            parse_multi_file_spec(":a.fq")
        with pytest.raises(InputError, match="at most two"):
            parse_multi_file_spec("s1:a+b+c")
        with pytest.raises(InputError, match="no input files"):
            parse_multi_file_spec("s1:")
        with pytest.raises(InputError, match="mixing"):
            parse_multi_file_spec("s1:a.fq,b1.fq+b2.fq")


class TestBuildSamples:
    def test_ordering(self) -> None:
        samples = build_sample_inputs(
            files=["a.fq"],
            reads=["b.fq"],
            first=["c_R1.fq"],
            second=["c_R2.fq"],
            interleaved=["d.fq"],
            specs=["e:x.fq"],
        )
        assert [sample.name for sample in samples] == ["a", "b", "c", "d", "e"]
        assert samples[2].paired
        assert samples[3].interleaved

    def test_pair_count_mismatch(self) -> None:
        with pytest.raises(InputError, match="differ in length"):
            build_sample_inputs(first=["a.fq"], second=["b.fq", "c.fq"])

    def test_duplicate_names(self) -> None:
        with pytest.raises(InputError, match="duplicate sample name"):
            build_sample_inputs(files=["a/x.fq", "b/x.fq"])

    def test_no_inputs(self) -> None:
        with pytest.raises(InputError, match="no input samples"):
            build_sample_inputs()


class TestSketching:
    def test_single_end(self) -> None:
        sample = SampleInput("k12", ((str(K12_R1),),))
        sketch = sketch_sample(sample, c=200, k=31)
        assert sketch.c == 200
        assert sketch.k == 31
        assert len(sketch.to_bytes()) > 100

    def test_paired_end(self) -> None:
        sample = SampleInput("k12", ((str(K12_R1), str(K12_R2)),))
        sketch = sketch_sample(sample, c=200, k=31)
        assert sketch.c == 200
        assert sketch.k == 31
        assert len(sketch.to_bytes()) > 100

    def test_interleaved_matches_paired(self, tmp_path: Path) -> None:
        path = tmp_path / "interleaved.fq"
        with dnaio.open(K12_R1) as r1, dnaio.open(K12_R2) as r2, dnaio.open(
            path, mode="w"
        ) as out:
            for a, b in zip(r1, r2):
                out.write(a)
                out.write(b)
        paired = sketch_sample(SampleInput("x", ((str(K12_R1), str(K12_R2)),)), c=200, k=31)
        interleaved = sketch_sample(
            SampleInput("x", ((str(path),),), interleaved=True), c=200, k=31
        )
        assert interleaved.to_bytes() == paired.to_bytes()

    def test_load_existing_sketch(self) -> None:
        sample = SampleInput("k12", ((str(K12_R1_SKETCH),),))
        sketch = sketch_sample(sample, c=200, k=31)
        assert sketch.to_bytes() == K12_R1_SKETCH.read_bytes()

    def test_check_sketch(self) -> None:
        sketch = sketch_sample(SampleInput("k12", ((str(K12_PAIRED_SKETCH),),)), c=200, k=31)
        check_sketch(sketch, c=200, k=31)
        with pytest.raises(InputError, match="k="):
            check_sketch(sketch, c=200, k=21)
        with pytest.raises(InputError, match="sparser"):
            check_sketch(sketch, c=100, k=31)

    def test_assembly_fasta(self) -> None:
        sample = SampleInput("clodf13", ((str(CLODF13_FASTA),),))
        sketch = sketch_sample(sample, c=200, k=31)
        assert sketch.c == 200

    def test_missing_file(self, tmp_path: Path) -> None:
        sample = SampleInput("gone", ((str(tmp_path / "nope.fq"),),))
        with pytest.raises(InputError):
            sketch_sample(sample, c=200, k=31)


class TestPairedValidation:
    @staticmethod
    def _write(path: Path, records: list[tuple[str, str]]) -> None:
        with open(path, "w") as handle:
            for name, seq in records:
                handle.write(f"@{name}\n{seq}\n+\n{'I' * len(seq)}\n")

    def test_paired_count_mismatch(self, tmp_path: Path) -> None:
        r1 = tmp_path / "r1.fq"
        r2 = tmp_path / "r2.fq"
        self._write(r1, [("read1/1", "AAAA"), ("read2/1", "CCCC")])
        self._write(r2, [("read1/2", "TTTT")])
        sample = SampleInput("x", ((str(r1), str(r2)),))
        with pytest.raises(InputError, match="improperly paired|more reads"):
            sketch_sample(sample, c=200, k=31)

    def test_paired_name_mismatch(self, tmp_path: Path) -> None:
        r1 = tmp_path / "r1.fq"
        r2 = tmp_path / "r2.fq"
        self._write(r1, [("read1/1", "AAAA"), ("read2/1", "CCCC")])
        self._write(r2, [("other/2", "TTTT"), ("read2/2", "GGGG")])
        sample = SampleInput("x", ((str(r1), str(r2)),))
        with pytest.raises(InputError, match="does not match|improperly paired"):
            sketch_sample(sample, c=200, k=31)

    def test_odd_interleaved(self, tmp_path: Path) -> None:
        path = tmp_path / "il.fq"
        self._write(
            path,
            [("read1/1", "AAAA"), ("read1/2", "TTTT"), ("read2/1", "CCCC")],
        )
        sample = SampleInput("x", ((str(path),),), interleaved=True)
        with pytest.raises(InputError, match="incomplete|no partner|improperly paired"):
            sketch_sample(sample, c=200, k=31)

    def test_mixed_lane_shapes(self, tmp_path: Path) -> None:
        r1 = tmp_path / "r1.fq"
        r2 = tmp_path / "r2.fq"
        self._write(r1, [("read1/1", "AAAA")])
        self._write(r2, [("read1/2", "TTTT")])
        sample = SampleInput("x", ((str(r1), str(r2)), (str(r1),)))
        with pytest.raises(InputError, match="exactly two files per input"):
            sketch_sample(sample, c=200, k=31)
