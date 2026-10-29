"""Per-contig sketching in the browser wasm must match pysylph / sylphd exactly.

`sketch-wasm/src/contig.rs` (WASM API `WasmContigSketcher`) sketches every record
of a FASTA/FASTQ file as its own sample and computes the record's genome-style
k-mer count -- the two things `POST /contigs` needs per contig (see
`docs/reference/http-api.md` and `src/sylphd/sketching.py::sketch_contigs`).

The tests run the built module under node (`sketch-wasm/build.sh` first):

* `sketch-wasm/tests/contigs_check.mjs` drives the wasm API directly;
* `sketch-wasm/tests/contigs_pipeline_check.mjs` runs `sketchContigs` from
  `web/sketching.js` through the real `web/sketch-worker.js`/`decompress-worker.js`
  (on node worker threads), so batching, ack/backpressure and the gzip path are
  covered too.

Every record is compared with `pysylph.Sketcher` (`.sketch_single` bytes with
the k-mer entries sorted, `.sketch_genome(...).kmers` count) and with the CLI
reference `sylphd.sketching.sketch_contigs`. The wasm is gitignored, so the
tests skip when it has not been built.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import random
import shutil
import struct
import subprocess
from pathlib import Path

import pysylph
import pytest

from sylphd.sketchcheck import parse_sketch_header
from sylphd.sketching import sketch_contigs, unique_contig_name

ROOT = Path(__file__).resolve().parents[1]
WASM = ROOT / "web" / "wasm" / "sylph_sketch.js"
CONTIGS_CHECK = ROOT / "sketch-wasm" / "tests" / "contigs_check.mjs"
PIPELINE_CHECK = ROOT / "sketch-wasm" / "tests" / "contigs_pipeline_check.mjs"
LAMBDA = ROOT / "scratch" / "taxonomy" / "phase4a" / "lambda.fasta"


@pytest.fixture(autouse=True)
def _require_node_and_wasm() -> None:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    if not WASM.exists():
        pytest.skip("web/wasm is not built (run sketch-wasm/build.sh)")


def sorted_bytes(data: bytes) -> bytes:
    """Re-emit a SequencesSketch with its k-mer entries sorted.

    The wasm writer sorts the entries (so its output is deterministic), sylph
    writes hash-map order; same helper as ``sketch-wasm/tests/compare_pysylph.py``.
    """
    (n,) = struct.unpack_from("<Q", data, 0)
    end = 8 + 12 * n
    entries = sorted(struct.iter_unpack("<QI", data[8:end]))
    return data[:8] + b"".join(struct.pack("<QI", *e) for e in entries) + data[end:]


def without_file_name(data: bytes) -> bytes:
    """A SequencesSketch with ``file_name`` blanked.

    The wasm names a record's sketch after the first word of its header; the
    caller (``sketchContigs``) makes the *request* name unique/fallback
    (``dupe_2``, ``contig-<index>``), which the serialized inner name does not
    know. The server takes names from `params`, so only the k-mer entries and
    c/k/mean matter; compare those.
    """
    (n,) = struct.unpack_from("<Q", data, 0)
    at = 8 + 12 * n
    (c, k, name_len) = struct.unpack_from("<QQQ", data, at)
    tail = data[at + 24 + name_len :]
    return data[:at] + struct.pack("<QQQ", c, k, 0) + tail


def _random(rng: random.Random, length: int) -> str:
    return "".join(rng.choice("ACGT") for _ in range(length))


def make_sequences(big: bool = False) -> list[tuple[str, str]]:
    """A FASTA records worth of edge cases (names are unique here)."""
    rng = random.Random(20240517)
    unit = _random(rng, 120)
    sequences = [
        ("random", _random(rng, 3000)),
        # the same block twice -> many repeated k-mers
        ("repeats", _random(rng, 900) + "ACGT" * 40 + _random(rng, 600) + "TTTT" * 30 + _random(rng, 400)),
        # N runs: BYTE_TO_SEQ maps N to A in both implementations
        ("nruns", _random(rng, 700) + "N" * 150 + _random(rng, 900) + "NNACGTN" * 20 + _random(rng, 300)),
        ("lower", _random(rng, 2500).lower()),
        ("short", "ACGTACGTAC"),  # 10 bp
        ("below-k", _random(rng, 20)),  # 20 bp: sketched for min_length=0
        ("exact-k", _random(rng, 31)),
        ("tandem", unit * 50),  # every k-mer repeated -> no genome-style k-mers
    ]
    if LAMBDA.exists():
        sequences.append(("lambda", "".join(LAMBDA.read_text().splitlines()[1:])))
    if big:
        sequences.append(("big", _random(rng, 2_200_000)))
    return sequences


def write_fasta(path: Path, sequences: list[tuple[str, str]], gz: bool = False) -> Path:
    text = "".join(f">{name} description of {name}\n{seq}\n" for name, seq in sequences)
    if gz:
        # two gzip members, like `cat a.fa.gz b.fa.gz` (multi-member path)
        half = len(text) // 2
        pos = text.find("\n>", half)
        if pos == -1:
            pos = text.find("\n", half)
        cut = pos + 1
        path.write_bytes(gzip.compress(text[:cut].encode()) + gzip.compress(text[cut:].encode()))
    else:
        path.write_text(text)
    return path


def run_wasm(path: Path, c: int, k: int, min_length: int, chunk: int, gz: bool = False) -> dict:
    done = subprocess.run(
        ["node", str(CONTIGS_CHECK), str(path), str(c), str(k), str(min_length), str(chunk),
         "gz" if gz else "plain"],
        capture_output=True, text=True, check=True, cwd=ROOT,
    )
    return json.loads(done.stdout)


def run_pipeline(path: Path, c: int, k: int, min_length: int) -> dict:
    done = subprocess.run(
        ["node", str(PIPELINE_CHECK), str(path), str(c), str(k), str(min_length)],
        capture_output=True, text=True, check=True, cwd=ROOT,
    )
    return json.loads(done.stdout)


def expected_records(sequences: list[tuple[str, str]], c: int, k: int, min_length: int) -> list[dict]:
    sketcher = pysylph.Sketcher(c=c, k=k)
    out = []
    for index, (name, seq) in enumerate(sequences, start=1):
        sequence = seq.upper()
        # the wasm reports the first word of the header (dnaio's record.id)
        name = name.split()[0] if name.split() else ""
        record = {
            "index": index,
            "name": name,
            "length": len(seq),
            "gcCount": sequence.count("G") + sequence.count("C"),
            "contigKmers": None,
            "sketch": None,
        }
        if len(seq) and len(seq) >= min_length:
            sample = sketcher.sketch_single(name, [sequence])
            record["sketch"] = sorted_bytes(sample.to_bytes())
            record["contigKmers"] = len(
                sketcher.sketch_genome(name, [sequence], profiling=False).kmers
            )
        out.append(record)
    return out


def compare(records: list[dict], expected: list[dict], c: int, k: int) -> None:
    assert len(records) == len(expected)
    for got, want in zip(records, expected, strict=True):
        assert got["index"] == want["index"]
        assert got["name"] == want["name"]
        assert got["length"] == want["length"], want["name"]
        assert got["gcCount"] == want["gcCount"], want["name"]
        assert got["contigKmers"] == want["contigKmers"], want["name"]
        if want["sketch"] is None:
            assert got["sketch"] is None, want["name"]
        else:
            sketch = base64.b64decode(got["sketch"])
            assert sketch == want["sketch"], f"{want['name']}: sample sketch differs"
            parsed = pysylph.SampleSketch.from_bytes(sketch)  # the server accepts it
            assert parsed.c == c and parsed.k == k
            assert parsed.n_kmers == struct.unpack_from("<Q", sketch, 0)[0]
            header = parse_sketch_header(sketch)  # ... and so does sylphd.sketchcheck
            assert (header.c, header.k, header.kmer_count, header.paired) == (c, k, parsed.n_kmers, False)


@pytest.mark.parametrize("c,k", [(200, 31), (50, 21)])
def test_records_match_pysylph(tmp_path: Path, c: int, k: int) -> None:
    sequences = make_sequences()
    min_length = 1000
    path = write_fasta(tmp_path / "contigs.fa", sequences)
    expected = expected_records(sequences, c, k, min_length)

    # odd chunk sizes split records (and CRLF-less lines) at every position
    for chunk in (4093, 997):
        result = run_wasm(path, c, k, min_length, chunk)
        compare(result["records"], expected, c, k)
        assert result["pending"] == 0
        assert result["seen"] == len(sequences)
        # records are delivered incrementally: the last one needs end_file()
        assert result["beforeEnd"] == len(sequences) - 1

    # the same input as a two-member gzip file, through the wasm gzip decoder
    gz_path = write_fasta(tmp_path / "contigs.fa.gz", sequences, gz=True)
    result = run_wasm(gz_path, c, k, min_length, 256, gz=True)
    compare(result["records"], expected, c, k)


def test_short_records_and_min_length_zero(tmp_path: Path) -> None:
    sequences = [
        ("empty", ""),
        ("below-k", _random(random.Random(7), 20)),
        ("exact-k", _random(random.Random(8), 31)),
        ("tandem", "ACGT" * 100),
    ]
    path = write_fasta(tmp_path / "short.fa", sequences)
    result = run_wasm(path, 20, 31, 0, 7)
    compare(result["records"], expected_records(sequences, 20, 31, 0), 20, 31)
    assert result["records"][0]["sketch"] is None
    assert result["records"][0]["contigKmers"] is None
    # 20 bp: a sketch exists but has no k-mers, like pysylph
    assert result["records"][1]["sketch"] is not None
    assert result["records"][1]["contigKmers"] == 0


def test_fastq_and_wrapped_fasta(tmp_path: Path) -> None:
    """FASTQ records and multi-line FASTA: the same record/first-word handling."""
    rng = random.Random(1234)
    fastq = [("fq1 x", _random(rng, 500)), ("fq2", _random(rng, 31)), ("fq3", _random(rng, 2000))]
    path = tmp_path / "reads.fq"
    path.write_text("".join(f"@{name}\n{seq}\n+\n{'I' * len(seq)}\n" for name, seq in fastq))
    result = run_wasm(path, 200, 31, 100, 13)
    compare(result["records"], expected_records(fastq, 200, 31, 100), 200, 31)

    wrapped = [("wrapped description", _random(rng, 700)), ("second", _random(rng, 120))]
    path = tmp_path / "wrapped.fa"
    path.write_text("".join(
        f">{name}\n" + "\n".join(seq[i : i + 60] for i in range(0, len(seq), 60)) + "\n"
        for name, seq in wrapped
    ))
    result = run_wasm(path, 100, 21, 100, 17)
    compare(result["records"], expected_records(wrapped, 100, 21, 100), 100, 21)


def test_matches_cli_sketch_contigs(tmp_path: Path) -> None:
    """The CLI reference (names, gc, length, both k-mer numbers, sketch bytes)."""
    sequences = make_sequences()
    path = write_fasta(tmp_path / "contigs.fa", sequences)
    expected = list(sketch_contigs(path, c=200, k=31, min_length=1000))
    result = run_wasm(path, 200, 31, 1000, 512)

    assert len(result["records"]) == len(expected)
    for got, want in zip(result["records"], expected, strict=True):
        assert got["index"] == want.index
        assert got["name"] == want.name
        assert got["length"] == want.length
        gc = round(100.0 * got["gcCount"] / want.length, 2) if want.length else 0.0
        assert gc == want.gc
        assert got["contigKmers"] == want.contig_kmers
        if want.sketch is None:
            assert got["sketch"] is None
        else:
            assert base64.b64decode(got["sketch"]) == sorted_bytes(want.sketch.to_bytes())


def test_pipeline_sketch_contigs(tmp_path: Path) -> None:
    """`sketchContigs` through the real workers: batching, acks, gzip, names."""
    rng = random.Random(99)
    sequences = make_sequences(big=True) + [
        ("dupe", _random(rng, 1500)),
        ("dupe", _random(rng, 1600)),  # unique name must become dupe_2
        ("contig-3", _random(rng, 1700)),  # a later literal dupe_2-like name
        ("", _random(rng, 1200)),  # empty header id -> contig-<index>
    ]
    path = write_fasta(tmp_path / "pipeline.fa.gz", sequences, gz=True)
    result = run_pipeline(path, 200, 31, 1000)
    expected = list(sketch_contigs(path, c=200, k=31, min_length=1000))

    stats = result["stats"]
    records = result["records"]
    assert len(records) == len(expected) == len(sequences)
    assert stats["records"] == len(sequences)
    assert stats["sketched"] == sum(1 for rec in expected if rec.sketch is not None)
    assert stats["skipped"] == len(sequences) - stats["sketched"]
    assert stats["bases"] == sum(len(seq) for _, seq in sequences)
    assert stats["chunks"] > 1  # the 2.2 Mbp record spans chunks
    assert stats["contigs"] == len(sequences)

    used: set[str] = set()
    for got, want in zip(records, expected, strict=True):
        assert got["name"] == want.name
        assert got["name"] not in used
        used.add(got["name"])
        assert got["length"] == want.length
        assert got["gc"] == want.gc
        assert got["contigKmers"] == want.contig_kmers
        if want.sketch is None:
            assert got["sketch"] is None
        else:
            # names can be rewritten by the caller (dupe_2, contig-<index>), so
            # compare everything but the inner file_name here
            assert without_file_name(base64.b64decode(got["sketch"])) == without_file_name(
                sorted_bytes(want.sketch.to_bytes())
            ), want.name

    # duplicate ids are suffixed, like the CLI
    assert [rec["name"] for rec in records if rec["headerName"] == "dupe"] == ["dupe", "dupe_2"]


NODE_NAMING = r"""
globalThis.window = globalThis;
require(process.argv[1]);
const { uniqueContigName } = window.SylphdSketch;
const counts = new Map(), used = new Set();
const raws = JSON.parse(process.argv[2]);
console.log(JSON.stringify(raws.map((raw, i) => uniqueContigName(raw, counts, used, i + 1))));
"""


def test_unique_contig_name_matches_python() -> None:
    raws = ["c", "c", "c_2", "", "c", "c_3", "d"]
    done = subprocess.run(
        ["node", "-e", NODE_NAMING, str(ROOT / "web" / "sketching.js"), json.dumps(raws)],
        capture_output=True, text=True, check=True,
    )
    js = json.loads(done.stdout)
    counts: dict[str, int] = {}
    used: set[str] = set()
    py = [unique_contig_name(raw, counts, used, i + 1) for i, raw in enumerate(raws)]
    assert js == py


def test_pipeline_cancels(tmp_path: Path) -> None:
    """cancel() rejects the promise with an AbortError and stops the workers."""
    harness = tmp_path / "cancel.mjs"
    harness.write_text(
        """
        import { readFileSync } from "node:fs";
        import { pathToFileURL } from "node:url";
        import path from "node:path";
        const [web, file] = process.argv.slice(2);
        const wasm = path.join(web, "wasm/sylph_sketch_bg.wasm");
        const realFetch = globalThis.fetch;
        globalThis.fetch = async (url) => String(url).includes("sylph_sketch_bg.wasm")
          ? new Response(readFileSync(wasm), { headers: { "content-type": "application/wasm" } })
          : realFetch(url);
        globalThis.window = globalThis;
        globalThis.self = globalThis;
        await import(pathToFileURL(path.join(web, "sketching.js")).href);
        const run = window.SylphdSketch.sketchContigs(new File([readFileSync(file)], "x.fa"),
          { c: 200, k: 31 }, () => {});
        run.cancel();
        try { await run.done; console.log("resolved"); }
        catch (e) { console.log(e.name); }
        process.exit(0);
        """
    )
    path = write_fasta(tmp_path / "one.fa", [("a", "ACGT" * 100)])
    done = subprocess.run(
        ["node", str(harness), str(ROOT / "web"), str(path)],
        capture_output=True, text=True, check=True, timeout=120,
    )
    assert done.stdout.strip() == "AbortError", done.stdout + done.stderr
