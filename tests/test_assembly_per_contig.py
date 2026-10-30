"""``sylphd assembly --per-contig``: one sketch, ANI and hit list per record.

The classification lives on the server now (``POST /contigs``), so this file
tests both halves: the endpoint (validation, admission, the response shape, the
per-request summary) through ``TestClient``, and the command end to end through
a real ``sylphd serve`` process.

Most command tests reuse the one-genome database of ``conftest.tiny_database``
(built from ``clodf13.fna.gz``); contigs are windows of that genome, so every
long enough record has a hit.

``ani_data`` builds a small synthetic database from a 2 Mb random genome and a
3 %-mutated variant.  It is what makes the contig-relative ANI checkable: an
exact 30 kb fragment must score 100 % even though it is 1.5 % of the genome.

``contaminant_data`` is the parsimony regression: a bacterial genome that
carries a 20 kb phage segment as one of its contigs, plus the phage genome on
its own.  The phage contig is contained in both, so only the ranking can put
the phage first.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import math
import random
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import dnaio
import httpx
import pysylph
import pytest
from conftest import CLODF13_FASTA
from starlette.testclient import TestClient

from sylphd import cli, per_contig, per_contig_client
from sylphd.client import SylphClient
from sylphd.protocol import contig_json_size, contigs_json_size
from sylphd.server import ServerConfig, create_app
from sylphd.sketching import unique_contig_name


def _wait_for_socket(path: Path, timeout: float = 120.0) -> None:
    """Wait until ``path`` exists *and* accepts connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_socket():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.2)
            try:
                probe.connect(str(path))
            except (FileNotFoundError, ConnectionRefusedError):
                pass
            else:
                return
            finally:
                probe.close()
        time.sleep(0.1)
    raise AssertionError(f"server did not create {path} in time")


@contextmanager
def _serve(directory: Path, database: Path, *extra: str, meta: Path | None = None):
    """Run ``sylphd serve`` on a Unix socket for the duration of the block."""
    socket_path = directory / "sylphd.sock"
    command = [sys.executable, "-m", "sylphd", "serve", "--db", str(database)]
    if meta is not None:
        command.extend(["--meta", str(meta)])
    command.extend(["--socket", str(socket_path), "--log-level", "warning", *extra])
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        _wait_for_socket(socket_path)
        yield socket_path
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture(scope="module")
def server(tmp_path_factory, tiny_database, tiny_metadata):
    with _serve(
        tmp_path_factory.mktemp("per-contig-server"),
        tiny_database,
        meta=tiny_metadata,
    ) as socket_path:
        yield socket_path


@pytest.fixture(scope="module")
def capped_server(tmp_path_factory, tiny_database, tiny_metadata):
    """A server that accepts at most two contigs per POST /contigs."""
    with _serve(
        tmp_path_factory.mktemp("capped-server"),
        tiny_database,
        "--max-contigs",
        "2",
        meta=tiny_metadata,
    ) as socket_path:
        yield socket_path


@pytest.fixture(scope="module")
def queue_server(tmp_path_factory, tiny_database, tiny_metadata):
    """A server whose queue refuses more than two sketches per request."""
    with _serve(
        tmp_path_factory.mktemp("per-contig-queue-server"),
        tiny_database,
        "--max-jobs",
        "1",
        "--max-queue",
        "1",
        meta=tiny_metadata,
    ) as socket_path:
        yield socket_path


@dataclass(frozen=True)
class AniData:
    reference: str
    variant: str
    foreign: str
    database: Path


def _mutate(sequence: str, rate: float, seed: int) -> str:
    rng = random.Random(seed)
    bases = list(sequence)
    for position in rng.sample(range(len(bases)), round(rate * len(bases))):
        current = bases[position]
        bases[position] = rng.choice([base for base in "ACGT" if base != current])
    return "".join(bases)


@pytest.fixture(scope="session")
def ani_data(tmp_path_factory) -> AniData:
    """A 2 Mb random genome, a 3 %-mutated variant, an unrelated genome."""
    directory = tmp_path_factory.mktemp("per-contig-ani")
    rng = random.Random(20261029)
    reference = "".join(rng.choices("ACGT", k=2_000_000))
    variant = _mutate(reference, 0.03, 20261030)
    foreign = "".join(random.Random(20261031).choices("ACGT", k=2_000_000))
    sketcher = pysylph.Sketcher(c=200, k=31)
    database = directory / "ani.syldb"
    pysylph.Database(
        [
            sketcher.sketch_genome("GCA_000000900.1_genomic.fna.gz", [reference]),
            sketcher.sketch_genome("GCA_000000901.1_genomic.fna.gz", [variant]),
            sketcher.sketch_genome("GCA_000000902.1_genomic.fna.gz", [foreign]),
        ]
    ).dump(str(database))
    return AniData(
        reference=reference, variant=variant, foreign=foreign, database=database
    )


@pytest.fixture(scope="module")
def ani_server(tmp_path_factory, ani_data: AniData):
    with _serve(tmp_path_factory.mktemp("ani-server"), ani_data.database) as socket_path:
        yield socket_path


@dataclass(frozen=True)
class ConsensusData:
    reference: str
    sibling: str
    other: str
    database: Path
    metadata: Path


@pytest.fixture(scope="session")
def consensus_data(tmp_path_factory) -> ConsensusData:
    """Two indistinguishable genomes of one genus, plus a third species.

    ``GCA_000001000.1`` and ``GCA_000001001.1`` differ by 10 substitutions in
    1 Mb (0.001 % ANI), so no contig can tell them apart and every assignment
    to one of them is a coin flip.  ``GCA_000001002.1`` is 3 % away: its own
    species (``Bacillus cereus``) of the same genus.
    """
    from sylphd.metadata import METADATA_COLUMNS
    from sylphd.metadata_build import build_metadata_db

    directory = tmp_path_factory.mktemp("per-contig-consensus")
    reference = "".join(random.Random(20261101).choices("ACGT", k=1_000_000))
    sibling = _mutate(reference, 0.00001, 20261102)
    other = _mutate(reference, 0.03, 20261103)
    sketcher = pysylph.Sketcher(c=200, k=31)
    database = directory / "consensus.syldb"
    pysylph.Database(
        [
            sketcher.sketch_genome("GCA_000001000.1_genomic.fna.gz", [reference]),
            sketcher.sketch_genome("GCA_000001001.1_genomic.fna.gz", [sibling]),
            sketcher.sketch_genome("GCA_000001002.1_genomic.fna.gz", [other]),
        ]
    ).dump(str(database))

    prefix = "d__Bacteria;p__Bacillota;c__Bacilli;o__Bacillales;f__Bacillaceae;g__Bacillus"
    taxonomy = {
        "GCA_000001000.1": f"{prefix};s__Bacillus subtilis_A",
        "GCA_000001001.1": f"{prefix};s__Bacillus velezensis",
        "GCA_000001002.1": f"{prefix};s__Bacillus cereus",
    }
    source = directory / "consensus.tsv"
    lines = ["\t".join(["accession", *METADATA_COLUMNS])]
    for accession, lineage in taxonomy.items():
        values = {column: "" for column in METADATA_COLUMNS}
        values["gtdb_taxonomy"] = lineage
        lines.append("\t".join([accession, *[values[c] for c in METADATA_COLUMNS]]))
    source.write_text("\n".join(lines) + "\n")
    metadata = directory / "consensus.sqlite"
    build_metadata_db([source], metadata, release=232)
    return ConsensusData(
        reference=reference,
        sibling=sibling,
        other=other,
        database=database,
        metadata=metadata,
    )


@pytest.fixture(scope="module")
def consensus_server(tmp_path_factory, consensus_data: ConsensusData):
    with _serve(
        tmp_path_factory.mktemp("consensus-server"),
        consensus_data.database,
        meta=consensus_data.metadata,
    ) as socket_path:
        yield socket_path


@pytest.fixture(scope="session")
def small_c_database(tmp_path_factory) -> Path:
    """``clodf13`` at ``c=20``: ~6 kB sketches, so a 1 MB upload limit binds."""
    with dnaio.open(CLODF13_FASTA) as reader:
        contigs = [record.sequence for record in reader]
    sketch = pysylph.Sketcher(c=20, k=31).sketch_genome(
        "GCA_000000001.1_genomic.fna.gz", contigs
    )
    path = tmp_path_factory.mktemp("small-c") / "small-c.syldb"
    pysylph.Database([sketch]).dump(str(path))
    return path


@pytest.fixture(scope="module")
def upload_limited_server(tmp_path_factory, small_c_database: Path):
    with _serve(
        tmp_path_factory.mktemp("upload-limited-server"),
        small_c_database,
        "--max-upload-mb",
        "1",
    ) as socket_path:
        yield socket_path


@pytest.fixture(scope="module")
def genome() -> str:
    with dnaio.open(CLODF13_FASTA) as reader:
        return next(iter(reader)).sequence


@pytest.fixture
def fragmented(tmp_path: Path, genome: str) -> Path:
    """Three long contigs (one id repeated), one short, one low-complexity."""
    path = tmp_path / "fragmented.fa"
    path.write_text(
        ">c1 first window\n"
        + genome[:6000]
        + "\n>c2 second window\n"
        + genome[-6000:]
        + "\n>c1 duplicate id\n"
        + genome[1000:7000]
        + "\n>short one\n"
        + genome[:500]
        + "\n>tiny low complexity\n"
        + "A" * 3000
        + "\n"
    )
    return path


def _write_fasta(path: Path, records: list[tuple[str, str]], width: int = 80) -> Path:
    with path.open("w") as handle:
        for name, sequence in records:
            handle.write(f">{name}\n")
            for start in range(0, len(sequence), width):
                handle.write(sequence[start : start + width] + "\n")
    return path


def _run(*args: object, check: bool = True) -> int:
    code = cli.main([str(arg) for arg in args])
    if check:
        assert code == 0
    return code


def _rows(
    capsys: pytest.CaptureFixture[str],
) -> tuple[list[str], list[dict[str, str]], str]:
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line]
    header = lines[0].split("\t")
    return header, [dict(zip(header, line.split("\t"))) for line in lines[1:]], captured.err


# ---------------------------------------------------------------------------
# table, statuses and JSON
# ---------------------------------------------------------------------------


def test_one_row_per_contig_and_statuses(server, fragmented, capsys) -> None:
    _run(
        "assembly",
        fragmented,
        "--per-contig",
        "--min-contig-length",
        "1000",
        "--socket",
        server,
    )
    header, rows, err = _rows(capsys)
    assert header[: len(per_contig_client.COLUMNS)] == list(per_contig_client.COLUMNS)
    assert "gtdb_taxonomy" in header  # metadata fields follow the leading columns

    by_id = {row["contig"]: row for row in rows}
    # skipped records are counted, not listed: no rows for "short" or "tiny"
    assert set(by_id) == {"c1", "c2", "c1_2"}
    for name in ("c1", "c2", "c1_2"):
        row = by_id[name]
        assert row["status"] == "ok"
        assert row["rank"] == "1"
        # an exact window of the reference genome is 100 % by construction,
        # even though it is only 60 % of the genome
        assert float(row["ani"]) == pytest.approx(100.0, abs=0.05)
        # the reference-denominator ANI is a coverage-style number, at best
        assert float(row["reference_ani"]) <= float(row["ani"])
        assert row["shared_kmers"] == row["contig_kmers"]
        assert float(row["explained_fraction"]) == pytest.approx(1.0, abs=0.01)
        assert row["relation"] == "same species"
        assert row["shared_rank"] == "species"
        assert row["ani_species_level"] == "true"
        assert row["accession"] == "GCA_000000001.1"
        assert 0.0 <= float(row["gc"]) <= 100.0
        # the deepest rank the fixture lineage has is the class
        assert row["n_close"] == "1"
        assert row["consensus_rank"] == "class"
        assert row["consensus_taxon"] == "Test"
    # the fixture taxonomy has no s__ rank, so the derived species is empty
    assert by_id["c1"]["gtdb_species"] == ""

    assert "per-contig:" in err
    assert "1 contigs (500 bp) below 1000 bp not classified" in err
    assert "1 contigs (3.0 kb) with too few k-mers not classified" in err
    assert "bp by consensus: Test (class) 18,000 bp (100.0 %)" in err


def test_tsv_header_is_stable(server, genome, tmp_path, capsys) -> None:
    """The leading columns are a contract: compare against the literal list."""
    path = tmp_path / "one.fa"
    path.write_text(">c\n" + genome + "\n")
    _run(
        "assembly",
        path,
        "--per-contig",
        "--min-contig-length",
        "1000",
        "--socket",
        server,
        "--fields",
        "none",
    )
    header, rows, _ = _rows(capsys)
    assert header == [
        "contig",
        "contig_length",
        "gc",
        "status",
        "rank",
        "ani",
        "shared_kmers",
        "contig_kmers",
        "explained_fraction",
        "reference_ani",
        "relation",
        "shared_rank",
        "ani_species_level",
        "accession",
        "genome_id",
        "gtdb_species",
        "taxon",
        "n_close",
        "consensus_rank",
        "consensus_taxon",
    ]
    assert len(rows[0]) == len(header)


def test_zero_genome_kmers_is_counted_not_listed(server, tmp_path, capsys) -> None:
    """A tandem repeat has a sample sketch but no genome-style k-mers: no ANI.

    Such a contig is not sent: ``contig_kmers`` is the denominator of the ANI,
    so the endpoint requires it to be positive.  It is counted in
    ``skipped_few_kmers`` and is neither a JSON record nor a TSV row.
    """
    rng = random.Random(424242)
    unit = "".join(rng.choices("ACGT", k=5000))
    path = _write_fasta(tmp_path / "repeat.fa", [("repeat", unit * 4)])

    _run(
        "assembly",
        path,
        "--per-contig",
        "--socket",
        server,
        "--fields",
        "none",
        "--json",
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["contigs"] == []
    summary = payload["summary"]
    assert summary["contigs"] == 1  # counted in the totals
    assert summary["skipped_short"] == {"contigs": 0, "bp": 0}
    assert summary["skipped_few_kmers"] == {"contigs": 1, "bp": 20_000}
    assert summary["queried_contigs"] == 0


def test_short_contigs_are_skipped_by_default(server, genome, tmp_path, capsys) -> None:
    path = tmp_path / "one.fa"
    path.write_text(">c\n" + genome + "\n")
    _run("assembly", path, "--per-contig", "--socket", server)
    header, rows, err = _rows(capsys)
    assert header[:4] == ["contig", "contig_length", "gc", "status"]
    assert rows == []  # 9957 bp < 20000: counted, not listed
    assert "0 of 1 contig(s)" in err
    assert "1 contigs (10.0 kb) below 20000 bp not classified" in err


def test_json_document(server, fragmented, tmp_path, capsys) -> None:
    output = tmp_path / "contigs.json"
    _run(
        "assembly",
        fragmented,
        "--per-contig",
        "--min-contig-length",
        "1000",
        "--socket",
        server,
        "--json",
        "-o",
        output,
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""  # the summary is in the document, not on stderr
    payload = json.loads(output.read_text())
    assert payload["query"] == {
        "file": str(fragmented),
        "per_contig": True,
    }
    assert payload["params"] == {
        "top": 1,  # per-contig default
        "min_ani": 80.0,
        "min_contig_length": 1000,
        "consensus_ratio": 1.5,
        "consensus_z": 2.0,
        "no_adjust": True,
        "minimum_ani": 0.0,
        "screen_ani": 0.0,
        "min_contain": 3,
        "min_number_kmers": 10,
    }
    assert payload["thresholds"] == {"species": 95, "imprecise_below": 85}

    by_id = {contig["id"]: contig for contig in payload["contigs"]}
    assert by_id["c1"]["status"] == "ok"
    assert by_id["c1"]["index"] == 1
    assert by_id["c1"]["length"] == 6000
    assert by_id["c1"]["sketch_kmers"] >= 10
    assert by_id["c1"]["contig_kmers"] > 0
    hit = by_id["c1"]["hits"][0]
    assert hit["ani"] == pytest.approx(100.0, abs=0.05)
    assert hit["shared_kmers"] == hit["contig_kmers"] == by_id["c1"]["contig_kmers"]
    assert hit["explained_fraction"] == pytest.approx(1.0, abs=0.01)
    assert hit["reference_ani"] <= hit["ani"]
    assert by_id["c1"]["assignment"]["accession"] == "GCA_000000001.1"
    assert by_id["c1"]["closest"]["accession"] == "GCA_000000001.1"
    assert by_id["c1"]["hits"][0]["ani_species_level"] is True
    assert set(by_id) == {"c1", "c2", "c1_2"}
    # input record numbers are preserved, so skipped records leave gaps
    assert by_id["c1"]["index"] == 1

    summary = payload["summary"]
    assert summary["contigs"] == 5
    assert summary["queried_contigs"] == 3
    assert summary["skipped_short"] == {"contigs": 1, "bp": 500}
    assert summary["skipped_few_kmers"] == {"contigs": 1, "bp": 3000}
    assert summary["consensus_bp"][0]["taxon"] == "Test"
    assert summary["consensus_bp"][0]["rank"] == "class"
    assert summary["consensus_bp"][0]["fraction"] == 1.0


# ---------------------------------------------------------------------------
# chunking
# ---------------------------------------------------------------------------


def test_chunk_size_for() -> None:
    """A /contigs request is one job, so jobs/queue no longer bound a batch."""
    limits = {
        "max_sketches": 4096,
        "max_contigs": 4096,
        "max_jobs": 2,
        "max_queue": 0,
    }
    assert per_contig_client.chunk_size_for({"limits": limits}) == 4096
    limits["max_contigs"] = 500
    assert per_contig_client.chunk_size_for({"limits": limits}) == 500
    assert per_contig_client.chunk_size_for({}) == 4096


def test_client_batches_by_the_advertised_max_contigs(
    capped_server, genome, tmp_path, monkeypatch, capsys
) -> None:
    """Five contigs against --max-contigs 2: three sequential requests (2+2+1)."""
    records = [(f"c{index}", genome) for index in range(5)]
    path = _write_fasta(tmp_path / "five.fa", records)
    calls: list[list[str]] = []
    original = SylphClient.contigs

    def counting(self, samples, **kwargs):
        calls.append([name for name, _ in samples])
        return original(self, samples, **kwargs)

    monkeypatch.setattr(SylphClient, "contigs", counting)
    _run(
        "assembly",
        path,
        "--per-contig",
        "--min-contig-length",
        "1000",
        "--socket",
        capped_server,
        "--fields",
        "none",
        "--quiet",
    )
    rows = _rows(capsys)[1]
    assert [len(batch) for batch in calls] == [2, 2, 1]
    assert [name for batch in calls for name in batch] == [f"c{i}" for i in range(5)]
    assert len(rows) == 5
    assert {row["status"] for row in rows} == {"ok"}


def test_params_budget_counts_json_escapes() -> None:
    """The running params size is exact, including non-ASCII names."""
    specs = [
        {"name": "é" * 100, "contig_kmers": 12, "length": 3000, "gc": 51.25},
        {"name": "plain", "contig_kmers": 7, "length": 2000, "gc": 0.0},
        {"name": "x" * 1000, "contig_kmers": 100, "length": 25_000, "gc": 49.5},
    ]
    batch = per_contig_client._Batch(("gtdb_taxonomy",))
    for spec in specs:
        batch.add({"id": spec["name"]}, spec, b"")
    assert batch.params_bytes == contigs_json_size(specs, fields=("gtdb_taxonomy",))
    assert per_contig_client.contig_json_size(specs[0]) == contig_json_size(specs[0])


def test_params_budget_follows_the_server_limit() -> None:
    """A server advertising a smaller params part gets smaller batches."""
    assert per_contig_client.params_budget({}) == per_contig_client.DEFAULT_PARAMS_BUDGET
    small = {"limits": {"max_params_bytes": 100 * 1024}}
    assert per_contig_client.params_budget(small) == 90 * 1024
    roomy = {"limits": {"max_params_bytes": 8 * 1024 * 1024}}
    assert per_contig_client.params_budget(roomy) == per_contig_client.DEFAULT_PARAMS_BUDGET


def test_requests_are_one_job_regardless_of_the_queue_limit(
    queue_server, fragmented, monkeypatch, capsys
) -> None:
    """--max-jobs 1 --max-queue 1 still takes all contigs in one POST."""
    calls: list[list[str]] = []
    original = SylphClient.contigs

    def counting(self, samples, **kwargs):
        calls.append([name for name, _ in samples])
        return original(self, samples, **kwargs)

    monkeypatch.setattr(SylphClient, "contigs", counting)
    _run(
        "assembly",
        fragmented,
        "--per-contig",
        "--min-contig-length",
        "1000",
        "--socket",
        queue_server,
        "--fields",
        "none",
        "--quiet",
    )
    rows = _rows(capsys)[1]
    # One request is one job now; jobs and queue slots no longer cut the batch.
    assert [len(batch) for batch in calls] == [3]
    sent = [name for batch in calls for name in batch]
    assert sent == ["c1", "c2", "c1_2"]  # request order is record order
    assert {row["contig"] for row in rows} == {"c1", "c2", "c1_2"}


def test_long_names_split_chunks_below_the_params_limit(
    server, genome, tmp_path, monkeypatch, capsys
) -> None:
    """The JSON params part repeats every name; 1000 x ~1 kB must be chunked."""
    records = [(f"c{index}_" + "x" * 1000, genome) for index in range(1000)]
    path = _write_fasta(tmp_path / "long-names.fa", records)

    batches: list[tuple[list[str], list[dict[str, object]], dict[str, object]]] = []
    original = SylphClient.contigs

    def counting(self, samples, *, contigs, options=None, **kwargs):
        batches.append(
            ([name for name, _ in samples], list(contigs), dict(options or {}))
        )
        return original(self, samples, contigs=contigs, options=options, **kwargs)

    monkeypatch.setattr(SylphClient, "contigs", counting)
    _run(
        "assembly",
        path,
        "--per-contig",
        "--min-contig-length",
        "1000",
        "--socket",
        server,
        "--fields",
        "none",
        "--quiet",
    )
    rows = _rows(capsys)[1]
    assert len(batches) > 1
    for names, specs, options in batches:
        assert names == [spec["name"] for spec in specs]
        size = contigs_json_size(specs, options=options, fields=())
        assert size <= per_contig_client.DEFAULT_PARAMS_BUDGET
    assert len(rows) == len(records)
    assert {row["status"] for row in rows} == {"ok"}


def test_single_oversized_sketch_is_rejected(
    upload_limited_server, tmp_path, capsys
) -> None:
    """3 Mb at c=20 is ~1.8 MB of sketch: over the server's 1 MB upload limit."""
    rng = random.Random(99)
    sequence = "".join(rng.choices("ACGT", k=3_000_000))
    path = _write_fasta(tmp_path / "huge.fa", [("huge", sequence)])

    code = cli.main(
        [
            "assembly",
            str(path),
            "--per-contig",
            "--socket",
            str(upload_limited_server),
            "--fields",
            "none",
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "huge" in err
    assert "upload limit" in err


def test_upload_limited_server_splits_requests(
    upload_limited_server, genome, tmp_path, monkeypatch, capsys
) -> None:
    """220 x ~6 kB sketches against --max-upload-mb 1: split, never a 413."""
    records = [(f"c{index}", genome) for index in range(220)]
    path = _write_fasta(tmp_path / "many.fa", records)

    calls: list[list[str]] = []
    original = SylphClient.contigs

    def counting(self, samples, **kwargs):
        calls.append([name for name, _ in samples])
        return original(self, samples, **kwargs)

    monkeypatch.setattr(SylphClient, "contigs", counting)
    _run(
        "assembly",
        path,
        "--per-contig",
        "--min-contig-length",
        "1000",
        "--socket",
        upload_limited_server,
        "--fields",
        "none",
        "--quiet",
    )
    rows = _rows(capsys)[1]
    assert len(calls) > 1  # one request would exceed 1 MB
    assert len(rows) == len(records)
    assert {row["status"] for row in rows} == {"ok"}


# ---------------------------------------------------------------------------
# realistic per-contig ANI
# ---------------------------------------------------------------------------


def test_contig_relative_ani_on_exact_and_mutated_fragments(
    ani_server, ani_data: AniData, tmp_path, capsys
) -> None:
    exact = ani_data.reference[123_456:153_456]
    mutated = _mutate(ani_data.reference[500_000:530_000], 0.03, 4242)
    path = _write_fasta(tmp_path / "fragments.fa", [("exact", exact), ("mutated", mutated)])

    _run(
        "assembly", path, "--per-contig", "--socket", ani_server, "--fields", "none"
    )
    _, rows, _ = _rows(capsys)
    by_id = {row["contig"]: row for row in rows}

    exact_row = by_id["exact"]
    assert exact_row["status"] == "ok"
    assert exact_row["accession"] == "GCA_000000900.1"
    assert float(exact_row["ani"]) == pytest.approx(100.0, abs=0.05)
    assert exact_row["shared_kmers"] == exact_row["contig_kmers"]
    assert float(exact_row["explained_fraction"]) == pytest.approx(1.0, abs=0.01)

    mutated_row = by_id["mutated"]
    assert mutated_row["status"] == "ok"
    assert mutated_row["accession"] == "GCA_000000900.1"
    assert float(mutated_row["ani"]) == pytest.approx(97.0, abs=1.0)
    # the old (reference-denominator) number is far too low to be an ANI
    assert float(mutated_row["reference_ani"]) < 90.0
    assert 0.2 < float(mutated_row["explained_fraction"]) < 0.6


def test_multiple_hits_are_ranked_by_shared_kmers(
    ani_server, ani_data: AniData, tmp_path, capsys
) -> None:
    exact = ani_data.reference[900_000:930_000]
    path = _write_fasta(tmp_path / "one.fa", [("contig1", exact)])
    _run(
        "assembly",
        path,
        "--per-contig",
        "--top",
        "2",
        "--socket",
        ani_server,
        "--fields",
        "none",
    )
    _, rows, _ = _rows(capsys)
    assert [row["rank"] for row in rows] == ["1", "2"]
    top, second = rows
    assert top["accession"] == "GCA_000000900.1"
    assert second["accession"] == "GCA_000000901.1"
    assert float(top["ani"]) > float(second["ani"])
    assert int(top["shared_kmers"]) > int(second["shared_kmers"])
    assert top["shared_kmers"] == top["contig_kmers"]


def test_min_ani_is_applied_to_the_contig_relative_ani(
    ani_server, ani_data: AniData, tmp_path, capsys
) -> None:
    mutated = _mutate(ani_data.reference[1_000_000:1_040_000], 0.03, 5150)
    path = _write_fasta(tmp_path / "mutated.fa", [("contig1", mutated)])

    _run(
        "assembly",
        path,
        "--per-contig",
        "--min-ani",
        "99",
        "--socket",
        ani_server,
        "--fields",
        "none",
        "--quiet",
    )
    (row,) = _rows(capsys)[1]
    assert row["status"] == "no_hit"  # ~97 % ANI, filtered client-side

    _run(
        "assembly",
        path,
        "--per-contig",
        "--min-ani",
        "90",
        "--socket",
        ani_server,
        "--fields",
        "none",
        "--quiet",
    )
    (row,) = _rows(capsys)[1]
    assert row["status"] == "ok"
    assert float(row["ani"]) == pytest.approx(97.0, abs=1.0)


# ---------------------------------------------------------------------------
# taxonomic consensus
# ---------------------------------------------------------------------------


def _taxonomy(
    species: str, genus: str = "Bacillus", family: str = "Bacillaceae"
) -> str:
    return (
        "d__Bacteria;p__Bacillota;c__Bacilli;o__Bacillales;"
        f"f__{family};g__{genus};s__{species}"
    )


def _consensus_hit(
    ani: float,
    *,
    contig_kmers: int = 400,
    species: str | None = None,
    genus: str = "Bacillus",
    taxonomy: str | None = None,
    accession: str = "GCA_000000001.1",
) -> dict[str, object]:
    """One hit as :func:`per_contig.consensus` sees it (``shared`` from ANI)."""
    shared = round(contig_kmers * (ani / 100.0) ** 31)
    if taxonomy is None and species is not None:
        taxonomy = _taxonomy(species, genus)
    return {
        "ani": ani,
        "shared_kmers": shared,
        "gtdb_taxonomy": taxonomy,
        "accession": accession,
    }


def _query_row(
    shared: int,
    *,
    accession: str,
    species: str,
    genus: str = "Bacillus",
    total: int = 10_000,
) -> dict[str, object]:
    """One ``POST /query`` row as the server returns it."""
    return {
        "Containment_ind": [shared, total],
        "Naive_ANI": 85.0,
        "gtdb_taxonomy": _taxonomy(species, genus),
        "accession": accession,
        "Genome_file": f"{accession}_genomic.fna.gz",
    }


def test_min_ani_never_changes_the_consensus() -> None:
    """--min-ani hides rows; it must not turn an ambiguous genus into a species."""
    rows = [
        _query_row(85, accession="GCA_000000001.1", species="Bacillus subtilis"),
        _query_row(84, accession="GCA_000000002.1", species="Bacillus velezensis"),
    ]
    loose = per_contig.contig_hits(
        rows,
        k=31,
        contig_kmers=85,
        options=per_contig.ContigOptions(min_ani=80.0, top=2),
    )
    strict = per_contig.contig_hits(
        rows,
        k=31,
        contig_kmers=85,
        options=per_contig.ContigOptions(min_ani=100.0, top=2),
    )
    assert [hit["accession"] for hit in loose[0]] == [
        "GCA_000000001.1",
        "GCA_000000002.1",
    ]
    assert [hit["accession"] for hit in strict[0]] == ["GCA_000000001.1"]
    for hits, assignment, closest, summary in (loose, strict):
        assert hits and assignment is not None and closest is not None
        assert summary is not None
        assert (summary["rank"], summary["taxon"]) == ("genus", "Bacillus")
        assert summary["n_close"] == 2

    # Nothing passes --min-ani: the contig has no hits and no consensus, even
    # though the complete set would have produced one.
    below = per_contig.contig_hits(
        rows[1:], k=31, contig_kmers=85, options=per_contig.ContigOptions(min_ani=100.0)
    )
    assert below == ([], None, None, None)


def test_exact_ties_are_canonical() -> None:
    """Equal evidence is ordered by accession, not by the backend's row order."""
    first = _query_row(50, accession="GCA_000000009.1", species="Bacillus subtilis")
    second = _query_row(50, accession="GCA_000000001.1", species="Bacillus velezensis")
    options = per_contig.ContigOptions(top=2)
    forward = per_contig.contig_hits([first, second], k=31, contig_kmers=85, options=options)
    reverse = per_contig.contig_hits([second, first], k=31, contig_kmers=85, options=options)
    assert [hit["accession"] for hit in forward[0]] == [
        "GCA_000000001.1",
        "GCA_000000009.1",
    ]
    assert forward == reverse


def test_ani_noise_matches_the_binomial_formula() -> None:
    assert per_contig.ani_noise(100, 100, 31) == 0.0  # exact match: no noise
    assert per_contig.ani_noise(0, 100, 31) == math.inf  # no estimate at all
    assert per_contig.ani_noise(5, 0, 31) == math.inf
    expected = (100 / 31) * math.sqrt((1 - 0.95) / (100 * 0.95))
    assert per_contig.ani_noise(95, 100, 31) == pytest.approx(expected)
    # more k-mers, less noise (the sd is ~1/sqrt(n))
    assert per_contig.ani_noise(950, 1000, 31) < per_contig.ani_noise(95, 100, 31)


def test_consensus_of_a_single_species_is_that_species() -> None:
    result = per_contig.consensus(
        [_consensus_hit(100.0, species="Bacillus subtilis")], k=31, contig_kmers=400
    )
    assert result == {
        "rank": "species",
        "taxon": "Bacillus subtilis",
        "n_close": 1,
        "species": ["Bacillus subtilis"],
    }


def test_species_consensus_needs_the_species_threshold() -> None:
    """A lone close species at 91.75 % is a genus call, not a species call."""
    result = per_contig.consensus(
        [
            _consensus_hit(
                91.75, species="Leptospira alexanderi", genus="Leptospira", accession="A"
            )
        ],
        k=31,
        contig_kmers=440,
    )
    assert (result["rank"], result["taxon"]) == ("genus", "Leptospira")
    assert result["n_close"] == 1
    assert result["species"] == ["Leptospira alexanderi"]

    at_threshold = per_contig.consensus(
        [
            _consensus_hit(
                95.0, species="Leptospira alexanderi", genus="Leptospira", accession="A"
            )
        ],
        k=31,
        contig_kmers=440,
    )
    assert (at_threshold["rank"], at_threshold["taxon"]) == (
        "species",
        "Leptospira alexanderi",
    )


def test_consensus_99_9_vs_99_1_stays_at_species() -> None:
    """A 0.8-point gap at 99 % is further than the noise: the best species holds."""
    result = per_contig.consensus(
        [
            _consensus_hit(99.9, species="Bacillus subtilis", accession="A"),
            _consensus_hit(99.1, species="Bacillus velezensis", accession="B"),
        ],
        k=31,
        contig_kmers=400,
    )
    assert result["n_close"] == 1
    assert (result["rank"], result["taxon"]) == ("species", "Bacillus subtilis")


def test_consensus_89_9_vs_89_1_widens_to_genus() -> None:
    """At 89 %, a 0.8-point gap is within ``ratio`` of a 10-point divergence."""
    result = per_contig.consensus(
        [
            _consensus_hit(89.9, species="Bacillus subtilis", accession="A"),
            _consensus_hit(89.1, species="Bacillus velezensis", accession="B"),
        ],
        k=31,
        contig_kmers=400,
    )
    assert result["n_close"] == 2
    assert (result["rank"], result["taxon"]) == ("genus", "Bacillus")
    assert result["species"] == ["Bacillus subtilis", "Bacillus velezensis"]


def test_consensus_reduces_to_the_best_hit_per_species() -> None:
    """Three hits of two species: n_close counts species, not rows."""
    result = per_contig.consensus(
        [
            _consensus_hit(99.9, species="Bacillus subtilis", accession="A"),
            _consensus_hit(99.5, species="Bacillus subtilis", accession="A2"),
            _consensus_hit(99.8, species="Bacillus velezensis", accession="B"),
        ],
        k=31,
        contig_kmers=400,
    )
    assert result["n_close"] == 2
    assert result["species"] == ["Bacillus subtilis", "Bacillus velezensis"]


def test_consensus_noise_widens_for_a_short_contig() -> None:
    """The same two hits: genus evidence at 10 k-mers, species at 1000."""
    weak = per_contig.consensus(
        [
            _consensus_hit(99.0, contig_kmers=10, species="A species", accession="A"),
            _consensus_hit(95.0, contig_kmers=10, species="B species", accession="B"),
        ],
        k=31,
        contig_kmers=10,
    )
    strong = per_contig.consensus(
        [
            _consensus_hit(99.0, contig_kmers=1000, species="A species", accession="A"),
            _consensus_hit(95.0, contig_kmers=1000, species="B species", accession="B"),
        ],
        k=31,
        contig_kmers=1000,
    )
    assert weak["n_close"] == 2
    assert strong["n_close"] == 1
    assert strong["rank"] == "species"


def test_consensus_ratio_and_z_control_the_band() -> None:
    hits = [
        _consensus_hit(97.0, species="Bacillus subtilis", accession="A"),
        _consensus_hit(95.5, species="Bacillus velezensis", accession="B"),
    ]
    wide = per_contig.consensus(hits, k=31, contig_kmers=400)
    narrow = per_contig.consensus(hits, k=31, contig_kmers=400, ratio=1.0, z=0.0)
    assert wide["rank"] == "genus"
    assert narrow["rank"] == "species"


def test_consensus_without_taxonomy_falls_back_to_empty() -> None:
    assert per_contig.consensus([], k=31, contig_kmers=400) is None
    alone = per_contig.consensus(
        [_consensus_hit(100.0, taxonomy=None, accession="GCA_000000009.1")],
        k=31,
        contig_kmers=400,
    )
    assert (alone["rank"], alone["taxon"]) == (None, None)
    assert alone["n_close"] == 1
    assert alone["species"] == ["GCA_000000009.1"]
    # a close species without a lineage cannot be placed: consensus is empty
    mixed = per_contig.consensus(
        [
            _consensus_hit(99.9, species="Bacillus subtilis", accession="A"),
            _consensus_hit(99.8, taxonomy=None, accession="GCA_000000009.1"),
        ],
        k=31,
        contig_kmers=400,
    )
    assert mixed["n_close"] == 2
    assert (mixed["rank"], mixed["taxon"]) == (None, None)


def _source_hit(
    ani: float,
    lineage: str,
    source: str,
    *,
    contig_kmers: int = 400,
    accession: str | None = None,
) -> dict[str, object]:
    """One non-GTDB hit as :func:`per_contig.consensus` sees it."""
    shared = round(contig_kmers * (ani / 100.0) ** 31)
    return {
        "ani": ani,
        "shared_kmers": shared,
        "gtdb_taxonomy": None,
        "tax_lineage": lineage,
        "tax_source": source,
        "accession": accession,
    }


def test_consensus_of_prefix_less_lineages_uses_levels() -> None:
    """UHGV lineages have no rank prefixes: the consensus is a ``level N``."""
    family = "vFAM-00013;vSUBFAM-00074;vGENUS-00044;vSUBGEN-00024"
    result = per_contig.consensus(
        [
            _source_hit(99.9, f"{family};vOTU-052719", "UHGV_default", accession="A"),
            _source_hit(99.8, f"{family};vOTU-052720", "UHGV_default", accession="B"),
        ],
        k=31,
        contig_kmers=400,
    )
    assert (result["rank"], result["taxon"]) == ("level 4", "vSUBGEN-00024")
    assert result["n_close"] == 2
    assert result["species"] == ["A", "B"]


def test_prefix_less_consensus_caps_below_the_species_threshold() -> None:
    """A lone vOTU at 91 % is capped at the level above it, like genus for GTDB."""
    lineage = "vFAM-00013;vSUBFAM-00074;vGENUS-00044;vSUBGEN-00024;vOTU-052719"
    result = per_contig.consensus(
        [_source_hit(91.0, lineage, "UHGV_default", accession="A")],
        k=31,
        contig_kmers=440,
    )
    assert (result["rank"], result["taxon"]) == ("level 4", "vSUBGEN-00024")
    at_threshold = per_contig.consensus(
        [_source_hit(95.0, lineage, "UHGV_default", accession="A")],
        k=31,
        contig_kmers=440,
    )
    assert (at_threshold["rank"], at_threshold["taxon"]) == ("level 5", "vOTU-052719")


def test_consensus_of_fungal_lineages_starts_at_the_kingdom() -> None:
    result = per_contig.consensus(
        [
            _source_hit(
                99.0,
                "k__Eukaryota;p__Ascomycota;c__Sordariomycetes;o__Magnaporthales;"
                "f__Pyriculariaceae;g__Pyricularia;s__Pyricularia oryzae",
                "FungiRefSeq-latest",
                accession="GCF_000002495.2_MG8",
            )
        ],
        k=31,
        contig_kmers=400,
    )
    assert (result["rank"], result["taxon"]) == ("species", "Pyricularia oryzae")
    assert result["species"] == ["Pyricularia oryzae"]


def test_consensus_across_sources_is_mixed_sources() -> None:
    """Lineages of two sources cannot be intersected (design note B6)."""
    result = per_contig.consensus(
        [
            _source_hit(99.9, "vFAM-1;vOTU-1", "UHGV_default", accession="A"),
            _source_hit(
                99.8,
                "k__Eukaryota;p__Ascomycota;s__Pyricularia oryzae",
                "FungiRefSeq-latest",
                accession="B",
            ),
        ],
        k=31,
        contig_kmers=400,
    )
    assert (result["rank"], result["taxon"]) == (None, None)
    assert result["n_close"] == 2
    assert result["mixed_sources"] is True
    assert "mixed_sources" not in per_contig.consensus(
        [_source_hit(99.9, "vFAM-1;vOTU-1", "UHGV_default", accession="A")],
        k=31,
        contig_kmers=400,
    )


def test_contig_hits_classify_and_report_a_non_gtdb_hit() -> None:
    """A ``/query`` row resolved by a sylph-tax source gets its generic taxon."""
    rows = [
        {
            "Containment_ind": [85, 10_000],
            "Naive_ANI": 90.0,
            "gtdb_taxonomy": None,
            "tax_lineage": "vFAM-1;vOTU-9",
            "tax_source": "UHGV_default",
            "accession": None,
            "Genome_file": "votus_full.fna.gz",
        }
    ]
    hits, assignment, closest, summary = per_contig.contig_hits(
        rows, k=31, contig_kmers=85, options=per_contig.ContigOptions()
    )
    assert assignment is not None and closest is not None
    assert assignment["taxon"] == "vOTU-9"
    assert assignment["note"].startswith("UHGV_default (viruses)")
    assert hits[0]["taxon"] == "vOTU-9"
    assert hits[0]["taxonomy"] == {"level 1": "vFAM-1", "level 2": "vOTU-9"}
    assert hits[0]["taxonomy_source"] == "UHGV_default"
    assert summary is not None and (summary["rank"], summary["taxon"]) == (
        "level 2",
        "vOTU-9",
    )


def test_consensus_columns_and_json(consensus_server, consensus_data, tmp_path, capsys) -> None:
    """Columns after ``gtdb_species``, and the same values in the JSON."""
    host = consensus_data.reference[100_000:130_000]
    other = consensus_data.other[100_000:130_000]
    path = _write_fasta(tmp_path / "consensus.fa", [("host", host), ("other", other)])

    _run(
        "assembly",
        path,
        "--per-contig",
        "--socket",
        consensus_server,
        "--fields",
        "none",
    )
    header, rows, err = _rows(capsys)
    assert header[-3:] == ["n_close", "consensus_rank", "consensus_taxon"]
    by_id = {row["contig"]: row for row in rows}
    assert by_id["host"]["status"] == "ok"
    assert by_id["host"]["consensus_rank"] == "genus"
    assert by_id["host"]["consensus_taxon"] == "Bacillus"
    assert by_id["host"]["n_close"] == "2"  # two ~identical genomes, one genus
    assert by_id["other"]["consensus_rank"] == "species"
    assert by_id["other"]["consensus_taxon"] == "Bacillus cereus"
    assert by_id["other"]["n_close"] == "1"
    assert "bp by consensus: Bacillus (genus) 30,000 bp (50.0 %)" in err
    assert "Bacillus cereus (species) 30,000 bp (50.0 %)" in err

    _run(
        "assembly",
        path,
        "--per-contig",
        "--socket",
        consensus_server,
        "--json",
    )
    payload = json.loads(capsys.readouterr().out)
    by_id = {contig["id"]: contig for contig in payload["contigs"]}
    assert by_id["host"]["consensus"]["rank"] == "genus"
    assert by_id["host"]["consensus"]["taxon"] == "Bacillus"
    assert by_id["host"]["consensus"]["n_close"] == 2
    # order follows the hits, which two ~identical genomes may swap
    assert set(by_id["host"]["consensus"]["species"]) == {
        "Bacillus subtilis_A",
        "Bacillus velezensis",
    }
    assert by_id["other"]["consensus"]["rank"] == "species"
    assert payload["summary"]["consensus_bp"][0]["rank"] in {"genus", "species"}


def test_consensus_options_are_reported(
    consensus_server, consensus_data, tmp_path, capsys
) -> None:
    """``--consensus-ratio``/``--consensus-z`` reach the rule and the document."""
    path = _write_fasta(
        tmp_path / "one.fa", [("host", consensus_data.reference[100_000:130_000])]
    )
    _run(
        "assembly",
        path,
        "--per-contig",
        "--socket",
        consensus_server,
        "--consensus-ratio",
        "4",
        "--consensus-z",
        "0",
        "--json",
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["params"]["consensus_ratio"] == 4.0
    assert payload["params"]["consensus_z"] == 0.0
    (contig,) = payload["contigs"]
    assert contig["consensus"]["rank"] == "genus"


# ---------------------------------------------------------------------------
# inputs and output modes
# ---------------------------------------------------------------------------


def test_gzip_input(server, genome, tmp_path, capsys) -> None:
    path = tmp_path / "fragmented.fa.gz"
    with gzip.open(path, "wt") as handle:
        handle.write(">c1\n" + genome[:6000] + "\n>c2\n" + genome[-6000:] + "\n")
    _run(
        "assembly",
        path,
        "--per-contig",
        "--min-contig-length",
        "1000",
        "--socket",
        server,
        "--fields",
        "none",
    )
    _, rows, _ = _rows(capsys)
    assert [row["contig"] for row in rows] == ["c1", "c2"]
    assert [row["status"] for row in rows] == ["ok", "ok"]
    assert all(float(row["ani"]) == pytest.approx(100.0, abs=0.05) for row in rows)


def test_empty_input_is_an_input_error(server, tmp_path, capsys) -> None:
    path = tmp_path / "empty.fa"
    path.write_text("")
    assert cli.main(["assembly", str(path), "--per-contig", "--socket", str(server)]) == 1
    err = capsys.readouterr().err
    assert "contains no sequences" in err


def test_invalid_sample_name_names_the_record(server, genome, tmp_path, capsys) -> None:
    path = tmp_path / "bad-name.fa"
    path.write_text(">c" + "x" * 1100 + "\n" + genome + "\n")
    assert cli.main(["assembly", str(path), "--per-contig", "--socket", str(server)]) == 1
    err = capsys.readouterr().err
    assert "record #1" in err
    assert "too long" in err


def test_sent_contig_without_a_hit(server, tmp_path, capsys) -> None:
    random_sequence = "".join(random.Random(3).choices("ACGT", k=20_000))
    path = _write_fasta(tmp_path / "random.fa", [("unrelated", random_sequence)])
    _run(
        "assembly",
        path,
        "--per-contig",
        "--socket",
        server,
        "--fields",
        "none",
        "--json",
    )
    payload = json.loads(capsys.readouterr().out)
    (contig,) = payload["contigs"]
    assert contig["status"] == "no_hit"
    assert contig["hits"] == []
    assert "reason" not in contig  # it was sent, the database just has nothing
    assert payload["summary"]["no_hit_contigs"] == 1


def test_fastq_records(server, genome, tmp_path, capsys) -> None:
    path = tmp_path / "fragmented.fq"
    path.write_text(
        "@c1\n" + genome[:6000] + "\n+\n" + "I" * 6000 + "\n"
        "@c2\n" + genome[-6000:] + "\n+\n" + "I" * 6000 + "\n"
    )
    _run(
        "assembly", path, "--per-contig", "--min-contig-length", "1000",
        "--socket", server, "--fields", "none",
    )
    _, rows, _ = _rows(capsys)
    assert [row["contig"] for row in rows] == ["c1", "c2"]
    assert [row["status"] for row in rows] == ["ok", "ok"]


def test_duplicate_fields_are_requested_and_printed_once(
    server, fragmented, capsys
) -> None:
    """``--fields a,a`` must not duplicate the request list or a TSV column."""
    _run(
        "assembly",
        fragmented,
        "--per-contig",
        "--min-contig-length",
        "1000",
        "--socket",
        server,
        "--fields",
        "gtdb_taxonomy,gtdb_taxonomy",
        "--quiet",
    )
    header, rows, _ = _rows(capsys)
    assert header.count("gtdb_taxonomy") == 1
    assert rows and rows[0]["gtdb_taxonomy"]


def test_fields_none_hides_metadata_but_keeps_relations(
    server, fragmented, capsys
) -> None:
    _run(
        "assembly",
        fragmented,
        "--per-contig",
        "--min-contig-length",
        "1000",
        "--socket",
        server,
        "--fields",
        "none",
        "--json",
        "--quiet",
    )
    payload = json.loads(capsys.readouterr().out)
    hit = next(c for c in payload["contigs"] if c["id"] == "c1")["hits"][0]
    assert "gtdb_taxonomy" not in hit  # not displayed
    assert hit["relation"] == "same species"  # but still classified with lineage


def test_single_assembly_json_output_file(server, genome, tmp_path, capsys) -> None:
    path = tmp_path / "one.fa"
    path.write_text(">c\n" + genome + "\n")
    output = tmp_path / "assembly.json"
    _run("assembly", path, "--socket", server, "--json", "-o", output)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""  # JSON mode never writes the summary
    payload = json.loads(output.read_text())
    assert payload["hits"][0]["accession"] == "GCA_000000001.1"


def test_options_of_the_single_assembly_still_work(server, genome, tmp_path, capsys) -> None:
    path = tmp_path / "one.fa"
    path.write_text(">c\n" + genome + "\n")
    # --top default is 10 without --per-contig; the flag combination is checked
    assert _run("assembly", path, "--socket", server, "--quiet") == 0
    assert capsys.readouterr().err == ""


def test_per_contig_rejects_incompatible_options(genome, tmp_path, capsys) -> None:
    path = tmp_path / "one.fa"
    path.write_text(">c\n" + genome + "\n")
    assert cli.main(["assembly", str(path), "--per-contig", "--no-local-sketch"]) == 2
    assert "--no-local-sketch" in capsys.readouterr().err
    assert cli.main(["assembly", str(path), "--per-contig", "--name", "x"]) == 2
    assert "--name" in capsys.readouterr().err
    assert cli.main(["assembly", str(path), "--fields", "none"]) == 2
    assert "--per-contig" in capsys.readouterr().err
    assert cli.main(["assembly", str(path), "--min-contig-length", "5"]) == 2
    assert "--per-contig" in capsys.readouterr().err
    assert cli.main(["assembly", str(path), "--consensus-ratio", "2"]) == 2
    assert "--consensus-ratio is only available with --per-contig" in capsys.readouterr().err
    assert cli.main(["assembly", str(path), "--consensus-z", "3"]) == 2
    assert "--consensus-z is only available with --per-contig" in capsys.readouterr().err


def test_consensus_option_range_is_validated(genome, tmp_path, capsys) -> None:
    path = tmp_path / "one.fa"
    path.write_text(">c\n" + genome + "\n")
    with pytest.raises(SystemExit) as ratio:
        cli.main(["assembly", str(path), "--per-contig", "--consensus-ratio", "0.5"])
    assert ratio.value.code == 2
    assert "must be >= 1" in capsys.readouterr().err
    with pytest.raises(SystemExit) as z:
        cli.main(["assembly", str(path), "--per-contig", "--consensus-z", "-1"])
    assert z.value.code == 2
    assert "must be >= 0" in capsys.readouterr().err
    with pytest.raises(SystemExit) as infinite:
        cli.main(["assembly", str(path), "--per-contig", "--consensus-ratio", "inf"])
    assert infinite.value.code == 2
    assert "must be >= 1" in capsys.readouterr().err


def test_unique_contig_names() -> None:
    counts: dict[str, int] = {}
    used: set[str] = set()
    names = [
        "c",
        "c",
        "c_2",
        "c",
        "",
    ]
    assert [
        unique_contig_name(raw, counts, used, index)
        for index, raw in enumerate(names, start=1)
    ] == ["c", "c_2", "c_2_2", "c_3", "contig-5"]


def test_unreadable_input_is_an_input_error(server, tmp_path, capsys) -> None:
    missing = tmp_path / "missing.fa"
    # the server is up, so the failure is the input, not the connection
    assert cli.main(["assembly", str(missing), "--per-contig", "--socket", str(server)]) == 1
    err = capsys.readouterr().err
    assert "cannot sketch" in err
    assert "missing.fa" in err


# ---------------------------------------------------------------------------
# POST /contigs
# ---------------------------------------------------------------------------


def _sketch_contig(sequence: str, name: str) -> tuple[bytes, int, float]:
    """The sample sketch of one contig, its genome-style count and its GC."""
    sketcher = pysylph.Sketcher(c=200, k=31)
    sample = sketcher.sketch_single(name, [sequence])
    kmers = len(sketcher.sketch_genome(name, [sequence], profiling=False).kmers)
    gc = round(100.0 * (sequence.count("G") + sequence.count("C")) / len(sequence), 2)
    return sample.to_bytes(), kmers, gc


def _contig_spec(name: str, sequence: str, kmers: int, gc: float) -> dict[str, object]:
    return {"name": name, "contig_kmers": kmers, "length": len(sequence), "gc": gc}


def _post_contigs(
    client: TestClient,
    payloads: list[tuple[str, bytes]],
    params: dict[str, object],
):
    return client.post(
        "/contigs",
        data={"params": json.dumps(params)},
        files=[("sketch", (f"{name}.sylsp", data)) for name, data in payloads],
    )


@pytest.fixture(scope="module")
def endpoint_data(ani_data: AniData) -> tuple[bytes, dict[str, object]]:
    """One 40 kb exact fragment of the fixture genome, as a sketch and a spec."""
    sequence = ani_data.reference[100_000:140_000]
    payload, kmers, gc = _sketch_contig(sequence, "contig1")
    return payload, _contig_spec("contig1", sequence, kmers, gc)


@pytest.fixture(scope="module")
def endpoint_app(ani_data: AniData):
    with TestClient(create_app(ServerConfig(db_path=ani_data.database))) as client:
        yield client


def test_info_advertises_the_contigs_endpoint(endpoint_app: TestClient) -> None:
    info = endpoint_app.get("/info").json()
    assert "/contigs" in info["endpoints"]
    assert info["limits"]["max_params_bytes"] == 1024 * 1024
    assert info["limits"]["max_contigs"] == 4096


def test_max_contigs_is_advertised_and_enforced(ani_data: AniData) -> None:
    """``--max-contigs`` bounds one request and /info tells clients the bound."""
    with TestClient(
        create_app(ServerConfig(db_path=ani_data.database, max_contigs=2))
    ) as client:
        assert client.get("/info").json()["limits"]["max_contigs"] == 2
        payloads = []
        specs = []
        for index in range(3):
            sequence = ani_data.reference[index * 5000 : index * 5000 + 40_000]
            payload, kmers, gc = _sketch_contig(sequence, f"c{index}")
            payloads.append((f"c{index}", payload))
            specs.append(_contig_spec(f"c{index}", sequence, kmers, gc))

        too_many = _post_contigs(client, payloads, {"contigs": specs})
        assert too_many.status_code == 400
        error = too_many.json()["error"]
        assert "too many contigs" in error
        assert "at most 2 contigs" in error
        assert "limits.max_contigs" in error

        accepted = _post_contigs(client, payloads[:2], {"contigs": specs[:2]})
        assert accepted.status_code == 200
        assert len(accepted.json()["contigs"]) == 2


def test_max_contigs_stops_parsing_before_the_rest_is_spooled(
    ani_data: AniData,
) -> None:
    """The parser stops at the (N+1)th file part instead of reading the body.

    The body is streamed lazily and far larger than the limit; if the parser
    kept going (as it did with ``max_files=limit + 1`` until the explicit
    check), every upload would be written to a temporary file. Counting the
    chunks the app pulls shows it stops right after the first refused part.
    """
    boundary = "----sylphdcontigstest"

    def part(index: int) -> bytes:
        return (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="sketch"; '
            f'filename="c{index}.sylsp"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + b"x\r\n"

    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="params"\r\n\r\n{}\r\n'
    ).encode()
    body += b"".join(part(index) for index in range(300))
    body += f"--{boundary}--\r\n".encode()

    chunk_size = 256
    total_chunks = math.ceil(len(body) / chunk_size)
    pulled = 0

    async def chunks():
        nonlocal pulled
        for start in range(0, len(body), chunk_size):
            pulled += 1
            yield body[start : start + chunk_size]

    async def post():
        app = create_app(ServerConfig(db_path=ani_data.database, max_contigs=2))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return await client.post(
                "/contigs",
                content=chunks(),
                headers={
                    "content-type": f"multipart/form-data; boundary={boundary}"
                },
            )

    response = asyncio.run(post())
    assert response.status_code == 400
    error = response.json()["error"]
    assert "too many contigs" in error
    assert "at most 2 contigs" in error
    assert "limits.max_contigs" in error
    # Two accepted parts fit in a couple of chunks; the remaining ~300 parts
    # were never read.
    assert 0 < pulled < total_chunks // 10, (pulled, total_chunks)


def test_floor_contigs_never_reach_the_profiler(
    endpoint_app: TestClient, monkeypatch
) -> None:
    """A sketch below the sample k-mer floor is answered without a query."""
    sequence = "".join(random.Random(6).choices("ACGT", k=300))
    payload, kmers, gc = _sketch_contig(sequence, "tiny")
    calls = []

    def counting(*args, **kwargs):
        calls.append(args)
        raise AssertionError("the profiler ran for a floor contig")

    monkeypatch.setattr("sylphd.server.run_contig_query", counting)
    response = _post_contigs(
        endpoint_app,
        [("tiny", payload)],
        {"contigs": [_contig_spec("tiny", sequence, kmers, gc)]},
    )
    assert response.status_code == 200
    (result,) = response.json()["contigs"]
    assert result["status"] == "too_few_kmers"
    assert calls == []


def test_contig_kmers_zero_is_rejected_before_profiling(
    endpoint_app: TestClient, monkeypatch
) -> None:
    """A taste of ``contig_kmers == 0`` is a 400, never a query."""
    sequence = "A" * 1000
    payload, _, gc = _sketch_contig(sequence, "zero")
    monkeypatch.setattr(
        "sylphd.server.run_contig_query",
        lambda *args, **kwargs: pytest.fail("the profiler ran"),
    )
    response = _post_contigs(
        endpoint_app,
        [("zero", payload)],
        {"contigs": [_contig_spec("zero", sequence, 0, gc)]},
    )
    assert response.status_code == 400
    assert "contig_kmers must be between" in response.json()["error"]


def test_contigs_endpoint_response(endpoint_app, endpoint_data) -> None:
    payload, spec = endpoint_data
    response = _post_contigs(
        endpoint_app, [("contig1", payload)], {"contigs": [spec], "top": 2, "fields": []}
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "contigs",
        "summary",
        "caveats",
        "database",
        "params",
        "thresholds",
        "elapsed_ms",
    }
    assert body["database"] == {
        "name": ani_name(endpoint_app),
        "genomes": 3,
        "c": 200,
        "k": 31,
    }
    assert body["params"] == {
        "top": 2,
        "min_ani": 80.0,
        "consensus_ratio": 1.5,
        "consensus_z": 2.0,
        "fields": [],
    }
    assert body["thresholds"] == {"species": 95, "imprecise_below": 85}
    assert body["caveats"] == []
    assert body["elapsed_ms"] >= 0

    (result,) = body["contigs"]
    assert result["name"] == "contig1"
    assert result["status"] == "ok"
    assert result["length"] == spec["length"]
    assert result["contig_kmers"] == spec["contig_kmers"]
    assert result["sketch_kmers"] >= result["contig_kmers"]
    assert result["assignment"]["accession"] == "GCA_000000900.1"
    assert result["closest"]["accession"] == "GCA_000000900.1"

    hit = result["hits"][0]
    assert hit["rank"] == 1
    assert hit["accession"] == "GCA_000000900.1"
    assert hit["ani"] == pytest.approx(100.0, abs=0.05)
    assert hit["shared_kmers"] == hit["contig_kmers"] == spec["contig_kmers"]
    assert hit["explained_fraction"] == pytest.approx(1.0, abs=0.01)
    assert hit["reference_containment"] == [spec["contig_kmers"], hit["reference_containment"][1]]
    assert hit["taxon"] is None  # no metadata on this server
    assert "gtdb_taxonomy" not in hit  # and no metadata columns were requested

    summary = body["summary"]
    assert summary["contigs"] == 1
    assert summary["bp"] == spec["length"]
    assert summary["queried_contigs"] == 1
    assert summary["queried_bp"] == spec["length"]
    assert summary["too_few_kmers_contigs"] == 0
    assert summary["no_hit_contigs"] == 0
    assert summary["consensus_bp"] == [
        {"rank": None, "taxon": "unassigned", "bp": spec["length"], "fraction": 1.0}
    ]


def ani_name(client: TestClient) -> str:
    return client.get("/info").json()["database"]["name"]


def test_contigs_endpoint_too_few_kmers(endpoint_app: TestClient) -> None:
    """A sketch below the k-mer floor is reported, not queried."""
    sequence = "".join(random.Random(5).choices("ACGT", k=300))
    payload, kmers, gc = _sketch_contig(sequence, "tiny")
    assert kmers >= 1
    response = _post_contigs(
        endpoint_app,
        [("tiny", payload)],
        {"contigs": [_contig_spec("tiny", sequence, kmers, gc)]},
    )
    assert response.status_code == 200
    body = response.json()
    (result,) = body["contigs"]
    assert result["status"] == "too_few_kmers"
    assert "too few k-mers" in result["reason"]
    assert result["hits"] == []
    assert result["consensus"] is None
    assert body["summary"]["too_few_kmers_contigs"] == 1
    assert body["summary"]["queried_contigs"] == 0


def test_slow_scoring_stays_off_the_event_loop(
    ani_data: AniData, endpoint_data, monkeypatch
) -> None:
    """Scoring runs in the worker too: /health answers during a /contigs job."""
    payload, spec = endpoint_data
    started = threading.Event()
    release = threading.Event()
    original = per_contig.contig_result

    def slow_scorer(*args, **kwargs):
        started.set()
        assert release.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr("sylphd.server.per_contig.contig_result", slow_scorer)
    app = create_app(ServerConfig(db_path=ani_data.database))
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            _post_contigs, client, [("contig1", payload)], {"contigs": [spec]}
        )
        assert started.wait(timeout=5)
        began = time.monotonic()
        response = client.get("/health")
        elapsed = time.monotonic() - began
        release.set()
        assert response.status_code == 200
        assert elapsed < 2.0
        assert future.result(timeout=30).status_code == 200


def test_duplicate_fields_are_deduplicated(
    tiny_database, tiny_metadata, genome
) -> None:
    """Repeated metadata columns are fetched once and echoed once."""
    with TestClient(
        create_app(ServerConfig(db_path=tiny_database, meta_path=tiny_metadata))
    ) as client:
        sequence = genome[:40_000]
        payload, kmers, gc = _sketch_contig(sequence, "c1")
        response = _post_contigs(
            client,
            [("c1", payload)],
            {
                "contigs": [_contig_spec("c1", sequence, kmers, gc)],
                "fields": ["gtdb_taxonomy", "gtdb_taxonomy"],
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["params"]["fields"] == ["gtdb_taxonomy"]
    assert body["contigs"][0]["hits"][0]["gtdb_taxonomy"]


def test_contigs_endpoint_requires_multipart(endpoint_app: TestClient) -> None:
    response = endpoint_app.post("/contigs", content=b"x")
    assert response.status_code == 415
    assert "multipart" in response.json()["error"]


def test_contigs_endpoint_needs_a_sketch(endpoint_app: TestClient) -> None:
    response = endpoint_app.post(
        "/contigs", files={"params": (None, json.dumps({"contigs": []}))}
    )
    assert response.status_code == 400
    assert "no 'sketch' part" in response.json()["error"]


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"contigs": None}, "'params.contigs' must be a list"),
        (
            {"contigs": [{"name": "contig1", "length": 40000, "gc": 50.0}]},
            "contig_kmers must be an integer",
        ),
        ({"contigs": [{"name": "contig1", "contig_kmers": 0, "length": 40000, "gc": 50.0}]}, "must be between 1 and"),
        ({"contigs": [{"name": "contig1", "contig_kmers": 40_000, "length": 40000, "gc": 50.0}]}, "at most"),
        ({"contigs": [{"name": "a\tb", "contig_kmers": 100, "length": 40000, "gc": 50.0}]}, "control character"),
        ({"contigs": [{"name": "", "contig_kmers": 100, "length": 40000, "gc": 50.0}]}, "is empty"),
        ({"contigs": [{"name": "c", "contig_kmers": 100, "length": 0, "gc": 50.0}]}, "must be between 1 and"),
        ({"contigs": [{"name": "c", "contig_kmers": 100, "length": 40000, "gc": 101}]}, "gc must be between"),
        ({"contigs": [{"name": "c", "contig_kmers": 100, "length": 40000, "gc": 50.0, "x": 1}]}, "unknown key"),
        ({"contigs": [{"name": "c", "contig_kmers": "100", "length": 40000, "gc": 50.0}]}, "must be an integer"),
        ({"top": 0}, "'top' must be >= 1"),
        ({"top": 51}, "'top' must be <= 50"),
        ({"min_ani": 50}, "'min_ani' must be >= 70"),
        ({"consensus_ratio": 0.5}, "'consensus_ratio' must be >= 1"),
        ({"consensus_z": -1}, "'consensus_z' must be >= 0"),
        ({"mode": "query"}, "unknown parameter"),
        ({"fields": ["nope"]}, "no metadata database"),
        ({"tax_sources": ["nope"]}, "no sylph-tax taxonomy sources"),
    ],
)
def test_contigs_endpoint_rejects_bad_input(
    endpoint_app: TestClient, endpoint_data, overrides, needle: str
) -> None:
    payload, spec = endpoint_data
    params = {"contigs": [spec], **overrides}
    response = _post_contigs(endpoint_app, [("contig1", payload)], params)
    assert response.status_code == 400, response.text
    assert needle in response.json()["error"]


def test_contigs_endpoint_counts_must_match_the_sketches(
    endpoint_app: TestClient, endpoint_data
) -> None:
    payload, spec = endpoint_data
    response = _post_contigs(
        endpoint_app, [("contig1", payload)], {"contigs": [spec, spec]}
    )
    assert response.status_code == 400
    assert "2 entries for 1 sketches" in response.json()["error"]


def test_contigs_endpoint_rejects_duplicate_names(
    endpoint_app: TestClient, endpoint_data
) -> None:
    payload, spec = endpoint_data
    response = _post_contigs(
        endpoint_app,
        [("contig1", payload), ("contig1", payload)],
        {"contigs": [spec, spec]},
    )
    assert response.status_code == 400
    assert "repeats 'contig1'" in response.json()["error"]


def test_contigs_request_is_one_job(ani_data: AniData, endpoint_data) -> None:
    """--max-jobs 1 --max-queue 1 admits a whole request, not one job per contig."""
    payload, spec = endpoint_data
    specs = [dict(spec, name=f"contig{index}") for index in range(1, 6)]
    app = create_app(ServerConfig(db_path=ani_data.database, max_jobs=1, max_queue=1))
    with TestClient(app) as client:
        response = _post_contigs(
            client,
            [(item["name"], payload) for item in specs],
            {"contigs": specs},
        )
        assert response.status_code == 200, response.text
        assert [result["name"] for result in response.json()["contigs"]] == [
            item["name"] for item in specs
        ]


def test_100_contigs_are_one_admitted_request(
    ani_data: AniData, endpoint_data
) -> None:
    """The public policy (--max-jobs 2 --max-queue 16) takes 100 contigs at once."""
    payload, spec = endpoint_data
    specs = [dict(spec, name=f"contig{index}") for index in range(1, 101)]
    app = create_app(
        ServerConfig(
            db_path=ani_data.database,
            max_jobs=2,
            max_queue=16,
            rate_limit=(1, 60.0),
        )
    )
    with TestClient(app) as client:
        response = _post_contigs(
            client,
            [(item["name"], payload) for item in specs],
            {"contigs": specs},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["contigs"]) == 100
        assert body["summary"]["contigs"] == 100
        # The whole request was one rate-limited POST: a second one is refused.
        assert client.get("/info").status_code == 200
        limited = _post_contigs(client, [("contig1", payload)], {"contigs": [spec]})
        assert limited.status_code == 429


def test_queue_full_refuses_a_concurrent_contigs_request(
    ani_data: AniData, endpoint_data, monkeypatch
) -> None:
    """A second request while the one job slot is busy is a 503."""
    payload, spec = endpoint_data
    second = dict(spec, name="contig2")
    started = threading.Event()
    release = threading.Event()

    def blocking_query(*args, **kwargs):
        started.set()
        release.wait(timeout=10)
        return {"sketch_kmers": 10, "rows": []}

    monkeypatch.setattr("sylphd.server.run_contig_query", blocking_query)
    app = create_app(ServerConfig(db_path=ani_data.database, max_jobs=1, max_queue=0))
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            _post_contigs,
            client,
            [("contig1", payload), ("contig2", payload)],
            {"contigs": [spec, second]},
        )
        assert started.wait(timeout=5)
        rejected = _post_contigs(client, [("contig1", payload)], {"contigs": [spec]})
        assert rejected.status_code == 503
        assert rejected.headers["retry-after"] == "1"
        assert "busy" in rejected.json()["error"]
        release.set()
        assert future.result(timeout=10).status_code == 200


# ---------------------------------------------------------------------------
# parsimony: a contaminant contig inside a reference genome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContaminantData:
    host: str
    phage: str
    database: Path


@pytest.fixture(scope="session")
def contaminant_data(tmp_path_factory) -> ContaminantData:
    """A bacterium that carries a 20 kb phage contig, plus the phage genome.

    ``GCA_000000020.1`` holds the bacterial sequence and, as one more contig,
    a 20 kb random "phage"; ``GCA_000000021.1`` is that phage on its own.  A
    contig taken from the phage is therefore *contained* in both genomes, and
    only the genome sizes can tell which one it belongs to.
    """
    directory = tmp_path_factory.mktemp("contaminant")
    host = "".join(random.Random(20261110).choices("ACGT", k=300_000))
    phage = "".join(random.Random(20261111).choices("ACGT", k=20_000))
    sketcher = pysylph.Sketcher(c=200, k=31)
    database = directory / "contaminant.syldb"
    pysylph.Database(
        [
            sketcher.sketch_genome(
                "GCA_000000020.1_genomic.fna.gz", [host, phage]
            ),
            sketcher.sketch_genome("GCA_000000021.1_genomic.fna.gz", [phage]),
        ]
    ).dump(str(database))
    return ContaminantData(host=host, phage=phage, database=database)


@pytest.fixture(scope="module")
def contaminant_server(tmp_path_factory, contaminant_data: ContaminantData):
    with _serve(
        tmp_path_factory.mktemp("contaminant-server"), contaminant_data.database
    ) as socket_path:
        yield socket_path


def test_parsimony_ranks_the_phage_genome_first(
    contaminant_server, contaminant_data: ContaminantData, tmp_path, capsys
) -> None:
    """The contig is in both genomes; the one that is only the phage wins."""
    path = _write_fasta(
        tmp_path / "phage.fa",
        [
            ("phage", contaminant_data.phage),
            ("host", contaminant_data.host[:20_000]),
        ],
    )
    _run(
        "assembly",
        path,
        "--per-contig",
        "--top",
        "2",
        "--socket",
        contaminant_server,
        "--fields",
        "none",
        "--quiet",
    )
    _, rows, _ = _rows(capsys)
    by_contig: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_contig.setdefault(row["contig"], []).append(row)

    phage_rows = by_contig["phage"]
    assert [row["rank"] for row in phage_rows] == ["1", "2"]
    assert phage_rows[0]["accession"] == "GCA_000000021.1"
    assert phage_rows[1]["accession"] == "GCA_000000020.1"
    # both contain the whole contig; only the reference size separates them
    assert phage_rows[0]["ani"] == phage_rows[1]["ani"]
    assert phage_rows[0]["explained_fraction"] == phage_rows[1]["explained_fraction"]
    assert float(phage_rows[0]["reference_ani"]) > 99.0
    assert float(phage_rows[1]["reference_ani"]) < 95.0

    host_rows = by_contig["host"]
    assert [row["accession"] for row in host_rows] == ["GCA_000000020.1"]


def _contig_row(
    shared: int,
    total: int,
    *,
    accession: str,
    taxonomy: str | None = None,
    tax_lineage: str | None = None,
    tax_source: str | None = None,
) -> dict[str, object]:
    """One hit row as the profiler returns it to :func:`per_contig.contig_hits`."""
    return {
        "Containment_ind": [shared, total],
        "Naive_ANI": 100.0 * (shared / total) ** (1 / 31),
        "gtdb_taxonomy": taxonomy,
        "tax_lineage": tax_lineage,
        "tax_source": tax_source,
        "accession": accession,
        "Genome_file": f"{accession}_genomic.fna.gz",
    }


def test_parsimony_only_reorders_hits_inside_the_noise_band() -> None:
    """A hit further than the close band keeps its evidence order."""
    rows = [
        # 100 % of the contig, a 10 Mb genome (the contaminant case)
        _contig_row(83, 10_271, accession="GCA_000001000.1"),
        # 99.96 % of the contig, the phage genome itself
        _contig_row(82, 210, accession="GCA_000001001.1"),
        # far outside the band: a genuinely worse hit with a small genome
        _contig_row(40, 100, accession="GCA_000001002.1"),
    ]
    hits, assignment, _, _ = per_contig.contig_hits(
        rows, k=31, contig_kmers=83, options=per_contig.ContigOptions(top=3)
    )
    assert [hit["accession"] for hit in hits] == [
        "GCA_000001001.1",  # parsimony: 210 reference k-mers vs 10 271 at the same ANI
        "GCA_000001000.1",
        "GCA_000001002.1",  # outside the close band: evidence order
    ]
    assert assignment["accession"] == "GCA_000001001.1"


def test_parsimony_keeps_a_non_saturated_best_hit_first() -> None:
    """97 % ANI is a real distance; a smaller genome at 95 % must stay second.

    Both hits are inside the close band, so the plain rule would have moved the
    tiny reference first.  Only hits that explain >= 95 % of the contig (i.e.
    are near-completely contained, >= ~99.83 % ANI) may be reordered.
    """
    rows = [
        # 97.0 % of the contig, a 5 000-k-mer reference
        _contig_row(78, 5_000, accession="GCA_000002000.1"),
        # 95.0 % of the contig, a 300-k-mer reference
        _contig_row(41, 300, accession="GCA_000002001.1"),
    ]
    hits, assignment, _, _ = per_contig.contig_hits(
        rows, k=31, contig_kmers=200, options=per_contig.ContigOptions(top=2)
    )
    assert [hit["accession"] for hit in hits] == [
        "GCA_000002000.1",
        "GCA_000002001.1",
    ]
    assert hits[0]["ani"] == pytest.approx(97.0, abs=0.15)
    assert hits[1]["ani"] == pytest.approx(95.0, abs=0.15)
    assert hits[1]["reference_ani"] > hits[0]["reference_ani"]
    assert hits[0]["explained_fraction"] < per_contig.PARSIMONY_MIN_EXPLAINED
    assert assignment["accession"] == "GCA_000002000.1"
    # the pair *is* inside the close band: the saturation condition is what
    # keeps the evidence order
    best_ani = 100 * (78 / 200) ** (1 / 31)
    other_ani = 100 * (41 / 200) ** (1 / 31)
    assert per_contig.is_close(
        other_ani,
        per_contig.ani_noise(41, 200, 31),
        best_ani=best_ani,
        best_sd=per_contig.ani_noise(78, 200, 31),
    )


def test_containment_only_hits_still_block_the_consensus() -> None:
    """The owner's choice: a close set spanning sources stays mixed.

    The larger containing genome is *not* dropped from the consensus, so a
    phage contig whose close set spans UHGV and GTDB has no consensus, even
    though parsimony now ranks the UHGV hit first.
    """
    rows = [
        _contig_row(
            83,
            10_271,
            accession="GCA_000001000.1",
            taxonomy="d__Bacteria;p__Bacillota;c__Bacilli;o__Bacillales;"
            "f__Bacillaceae;g__Bacillus;s__Bacillus subtilis",
        ),
        _contig_row(
            82,
            210,
            accession="",
            tax_lineage="vFAM-1;vOTU-9",
            tax_source="UHGV_default",
        ),
    ]
    hits, assignment, _, summary = per_contig.contig_hits(
        rows, k=31, contig_kmers=83, options=per_contig.ContigOptions(top=2)
    )
    assert hits[0]["taxon"] == "vOTU-9"
    assert assignment["taxon"] == "vOTU-9"
    assert summary["mixed_sources"] is True
    assert summary["n_close"] == 2
    assert (summary["rank"], summary["taxon"]) == (None, None)


# ---------------------------------------------------------------------------
# summaries of several requests
# ---------------------------------------------------------------------------


def _summary_record(
    name: str, length: int, *, consensus: dict[str, object] | None
) -> dict[str, object]:
    return {
        "name": name,
        "length": length,
        "status": "ok" if consensus is not None else "no_hit",
        "hits": [{"rank": 1}] if consensus is not None else [],
        "consensus": consensus,
    }


def test_chunk_summaries_add_up_to_the_whole_run() -> None:
    records = [
        _summary_record("a", 10_000, consensus={"rank": "genus", "taxon": "Bacillus"}),
        _summary_record("b", 20_000, consensus={"rank": "species", "taxon": "X"}),
        _summary_record("c", 30_000, consensus=None),
        _summary_record("d", 40_000, consensus={"rank": "species", "taxon": "X"}),
    ]
    parts = [
        per_contig.summarize(records[:2]),
        per_contig.summarize(records[2:]),
    ]
    merged = per_contig.finalize_summary(per_contig.merge_summaries(parts))
    assert merged == per_contig.finalize_summary(per_contig.summarize(records))
    assert merged["contigs"] == 4
    assert merged["queried_bp"] == 100_000
    assert merged["no_hit_contigs"] == 1
    # every taxon of every chunk survives, not just the top five per chunk
    assert [(item["rank"], item["taxon"]) for item in merged["consensus_bp"]] == [
        ("species", "X"),  # 20 000 + 40 000 bp
        (None, "unassigned"),  # 30 000 bp
        ("genus", "Bacillus"),  # 10 000 bp
    ]


def test_summary_counts_unsent_chunks_and_truncates_the_breakdown() -> None:
    records = [
        _summary_record(f"c{index}", 1_000, consensus={"rank": "genus", "taxon": f"G{index}"})
        for index in range(7)
    ]
    summary = per_contig.finalize_summary(per_contig.summarize(records))
    assert len(summary["consensus_bp"]) == 5
    assert summary["consensus_bp"][0]["fraction"] == pytest.approx(1 / 7, abs=1e-4)
