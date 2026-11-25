"""Compare sketches written by the Rust test with pysylph's own.

usage: compare_pysylph.py OUT_DIR CASE [CASE ...]

Run from the repository root with the sylphd venv (``pysylph``, ``dnaio`` and
``sylphd`` importable). Case definitions come from ``tests/cases.json``; the
sketch under test is ``OUT_DIR/<case>.sylsp``.

For every case:

1. pysylph sketches the same reads (via ``dnaio``, exactly as sylphd does);
2. both byte strings are checked with ``sylphd.sketchcheck`` and
   ``SampleSketch.from_bytes``;
3. the pysylph bytes are re-emitted with the k-mer entries sorted (our writer
   sorts them, sylph writes hash-map order) and compared byte for byte;
4. if the k-mers or the header differ, the difference is itemised.

On the small cases a database query against ``tests/data/ecoli.syl2db`` is
compared as well.
"""

from __future__ import annotations

import json
import struct
import sys
import time
from pathlib import Path

import dnaio
import pysylph

from sylphd.sketchcheck import parse_sketch_header

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def sorted_bytes(data: bytes) -> bytes:
    """Re-emit a SequencesSketch with its k-mer entries sorted."""
    (n,) = struct.unpack_from("<Q", data, 0)
    end = 8 + 12 * n
    entries = sorted(struct.iter_unpack("<QI", data[8:end]))
    body = b"".join(struct.pack("<QI", *e) for e in entries)
    return data[:8] + body + data[end:]


def entries(data: bytes) -> dict[int, int]:
    (n,) = struct.unpack_from("<Q", data, 0)
    return dict(struct.iter_unpack("<QI", data[8 : 8 + 12 * n]))


def resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def reference(case: dict) -> tuple[bytes, float]:
    sketcher = pysylph.Sketcher(
        c=case.get("c", 200),
        k=case.get("k", 31),
        deduplicate=case.get("deduplicate", True),
    )
    files = [resolve(f) for f in case["files"]]
    start = time.perf_counter()
    if case["kind"] == "paired":
        with dnaio.open(files[0], file2=files[1]) as reader:
            pairs = ((a.sequence, b.sequence) for a, b in reader)
            # same lockstep trick as sylphd.sketching._split_pairs
            import collections

            pending: collections.deque[str] = collections.deque()

            def first():
                for s1, s2 in pairs:
                    pending.append(s2)
                    yield s1

            def second():
                while pending:
                    yield pending.popleft()

            sketch = sketcher.sketch_paired(case["name"], first(), second())
    else:

        def reads():
            for f in files:
                with dnaio.open(f) as reader:
                    for record in reader:
                        yield record.sequence

        sketch = sketcher.sketch_single(case["name"], reads())
    return sketch.to_bytes(), time.perf_counter() - start


def describe(data: bytes) -> str:
    h = parse_sketch_header(data)
    return (
        f"kmers={h.kmer_count} c={h.c} k={h.k} paired={h.paired} "
        f"mean_read_length={h.mean_read_length!r} file_name={h.file_name!r}"
    )


def compare_profile(ours: bytes, theirs: bytes) -> str:
    db = pysylph.TwoStageDatabase.load(str(ROOT / "tests/data/ecoli.syl2db"))

    def rows(blob: bytes) -> list[tuple]:
        sketch = pysylph.SampleSketch.from_bytes(blob)
        profiler = pysylph.Profiler(db)
        return sorted(
            (r.genome_sketch.name, float(r.ani), tuple(r.containment), float(r.taxonomic_abundance))
            for r in profiler.profile(sketch)
        )

    a, b = rows(ours), rows(theirs)
    assert a == b, f"profile differs: {a!r} != {b!r}"
    return f"profile identical ({len(a)} hit(s))"


def main() -> int:
    out = Path(sys.argv[1])
    wanted = set(sys.argv[2:])
    cases = [c for c in json.loads((HERE / "cases.json").read_text()) if c["name"] in wanted]
    failures = 0
    for case in cases:
        ours = (out / f"{case['name']}.sylsp").read_bytes()
        theirs, seconds = reference(case)
        print(f"[{case['name']}] pysylph {seconds:.1f}s")
        print(f"  ours:    {describe(ours)}")
        print(f"  pysylph: {describe(theirs)}")
        pysylph.SampleSketch.from_bytes(ours)  # must load

        expected = sorted_bytes(theirs)
        if ours == expected:
            print(f"  IDENTICAL after sorting k-mer entries ({len(ours)} bytes)")
            if case["name"].startswith("k12") and case.get("k", 31) == 31:
                try:
                    print("  " + compare_profile(ours, theirs))
                except AssertionError as error:
                    failures += 1
                    print(f"  FAIL {error}")
            continue

        failures += 1
        a, b = entries(ours), entries(theirs)
        print(
            f"  DIFFERENT: only ours {len(a.keys() - b.keys())}, "
            f"only pysylph {len(b.keys() - a.keys())}, "
            f"count differs {sum(1 for k in a.keys() & b.keys() if a[k] != b[k])}"
        )
        ho, ht = parse_sketch_header(ours), parse_sketch_header(theirs)
        print(f"  header equal: {ho == ht}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
