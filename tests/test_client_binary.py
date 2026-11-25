"""End-to-end tests of the native ``sylphd-client`` binary.

The query commands of ``sylphd`` are the binary's: Python owns only the command name
and forwards (``tests/test_entry.py``). So the command line is tested here, against a
live server on a temporary Unix socket (and a fake HTTP server for the ``--json``
passthrough and the ``api_version`` check).

The binary is looked up through ``$SYLPHD_CLIENT_BIN``, the venv (``uv sync`` builds
``sylphd-bin`` from ``rust/sylphd-client``) or ``rust/target/{release,debug}``. A
missing binary skips the module; ``SYLPHD_REQUIRE_CLIENT_BIN=1`` (CI) makes it a
failure instead. The Rust unit tests cover the address table, retries and parsing;
these tests run the real thing end to end.
"""

from __future__ import annotations

import json
import os
import random
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import dnaio
import pysylph
import pytest
from conftest import CLODF13_FASTA, K12_R1, K12_R2, require_client_binary

REPO = Path(__file__).resolve().parent.parent

BIN = require_client_binary()


def _run(*args: str | Path, timeout: float = 600, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(BIN), *map(str, args)],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **env} if env else None,
    )


def _run_sylphd(*args: str | Path, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    """The Python command, which forwards the query commands to the binary."""
    return subprocess.run(
        [sys.executable, "-m", "sylphd", *map(str, args)],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _wait_for_socket(path: Path, timeout: float = 120.0) -> None:
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
def _serve(directory: Path, *args: str):
    socket_path = directory / "sylphd.sock"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "sylphd",
            "serve",
            "--socket",
            str(socket_path),
            "--log-level",
            "warning",
            *args,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
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
def server(tmp_path_factory, tiny_database: Path, tiny_metadata: Path):
    with _serve(
        tmp_path_factory.mktemp("client-binary-server"),
        "--db",
        str(tiny_database),
        "--meta",
        str(tiny_metadata),
        "--allow-sequences",
    ) as socket_path:
        yield socket_path


@pytest.fixture(scope="module")
def token_server(tmp_path_factory, tiny_database: Path):
    """A server with one privileged token: a wrong token earns a warning."""
    directory = tmp_path_factory.mktemp("client-binary-tokens")
    tokens = directory / "tokens"
    tokens.write_text(TOKEN + "\n")
    with _serve(
        directory,
        "--db",
        str(tiny_database),
        "--privileged-token-file",
        str(tokens),
    ) as socket_path:
        yield socket_path


@pytest.fixture(scope="module")
def one_contig_server(tmp_path_factory, tiny_database: Path, tiny_metadata: Path):
    """A server that takes one contig per ``/contigs`` request: every run is multi-batch."""
    with _serve(
        tmp_path_factory.mktemp("client-binary-one-contig"),
        "--db",
        str(tiny_database),
        "--meta",
        str(tiny_metadata),
        "--max-contigs",
        "1",
    ) as socket_path:
        yield socket_path


TOKEN = "t0ken-" + "x" * 24


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------


def test_info_text(server: Path) -> None:
    result = _run("info", "--socket", server)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    lines = result.stdout.splitlines()
    assert any(line.startswith("database: ") for line in lines)
    assert any("c: 200  k: 31" in line for line in lines)
    assert any(line.startswith("versions: sylphd ") for line in lines)
    assert any(line.startswith("metadata: ") for line in lines)
    assert any(line.startswith("limits: max-jobs ") for line in lines)
    assert any(line.startswith("usage statistics: ") for line in lines)
    # No operator messages on this server: no "messages:" block.
    assert "messages:" not in result.stdout


def test_info_json(server: Path) -> None:
    result = _run("info", "--json", "--socket", server)
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["api_version"] == 1
    assert document["database"]["c"] == 200
    assert document["database"]["k"] == 31
    assert document["messages"] == []
    assert document["metadata"]["release"] == "232"


# ---------------------------------------------------------------------------
# profile / query
# ---------------------------------------------------------------------------


def test_profile_tsv(server: Path) -> None:
    result = _run("profile", "--socket", server, CLODF13_FASTA)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    header, *rows = result.stdout.splitlines()
    columns = header.split("\t")
    assert columns[:17] == [
        "Sample_file",
        "Genome_file",
        "Taxonomic_abundance",
        "Sequence_abundance",
        "Adjusted_ANI",
        "Eff_cov",
        "ANI_5-95_percentile",
        "Eff_lambda",
        "Lambda_5-95_percentile",
        "Median_cov",
        "Mean_cov_geq1",
        "Containment_ind",
        "Naive_ANI",
        "kmers_reassigned",
        "Contig_name",
        "accession",
        "genome_id",
    ]
    # Then the metadata columns of the loaded database, in its order.
    assert columns[17] == "gtdb_taxonomy"
    assert len(columns) == len(set(columns))
    (row,) = [line.split("\t") for line in rows]
    assert row[:6] == ["clodf13", "GCA_000000001.1_genomic.fna.gz", "100.0000", "100.0000", "100.00", "1.000"]
    assert row[15] == "GCA_000000001.1"
    assert row[16] == "GCA_000000001.1"
    assert row[17].startswith("d__Bacteria")


def test_profile_json_is_a_document_and_renders_identically(server: Path, tmp_path: Path) -> None:
    output = tmp_path / "profile.json"
    saved = _run("profile", "--socket", server, CLODF13_FASTA, "--json", "-o", output)
    assert saved.returncode == 0, saved.stderr
    # --json writes only the document: no summary, no notes on stdout.
    assert saved.stderr == ""
    document = json.loads(output.read_text())
    assert document["api_version"] == 1
    assert isinstance(document["table"]["data"], list)
    assert [sample["name"] for sample in document["samples"]] == ["clodf13"]
    assert document["table"]["columns"][:3] == [
        "Sample_file",
        "Genome_file",
        "Taxonomic_abundance",
    ]
    # The offline renderer is the same code as the online one.
    tsv = _run("profile", "--socket", server, CLODF13_FASTA)
    rendered = _run("render", output)
    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stdout == tsv.stdout


def test_json_samples_carry_sketch_statistics_and_client_counts(server: Path) -> None:
    """FASTQ input: the client sends reads/bases; the server adds the sketch header's values."""
    document = json.loads(_run("profile", "--socket", server, K12_R1, "--json").stdout)
    (sample,) = document["samples"]
    assert set(sample) == {"name", "kmers", "mean_read_length", "paired", "reads", "bases"}
    with dnaio.open(K12_R1) as reader:
        records = [record.sequence for record in reader]
    assert sample["reads"] == len(records)
    assert sample["bases"] == sum(len(sequence) for sequence in records)
    assert sample["paired"] is False
    assert sample["kmers"] > 0
    assert sample["mean_read_length"] == pytest.approx(sample["bases"] / sample["reads"])


def test_paired_reads_count_a_pair_once(server: Path) -> None:
    document = json.loads(_run("profile", "--socket", server, "-1", K12_R1, "-2", K12_R2, "--json").stdout)
    (sample,) = document["samples"]
    assert sample["paired"] is True
    with dnaio.open(K12_R1) as reader:
        pairs = sum(1 for _ in reader)
    assert sample["reads"] == pairs


def test_sylsp_input_has_no_read_counts(server: Path, tmp_path: Path) -> None:
    with dnaio.open(CLODF13_FASTA) as reader:
        sequences = [record.sequence for record in reader]
    sketch_path = tmp_path / "clodf13.sylsp"
    sketch_path.write_bytes(pysylph.Sketcher(c=200, k=31).sketch_single("clodf13", sequences).to_bytes())
    document = json.loads(_run("profile", "--socket", server, sketch_path, "--json").stdout)
    (sample,) = document["samples"]
    assert sample["reads"] is None and sample["bases"] is None
    assert sample["kmers"] > 0


def test_a_sample_without_hits_is_named_on_stderr(server: Path) -> None:
    """R1 alone can give no hits: the summary says so, with the sketch's k-mers and the reads."""
    sketch = pysylph.Sketcher(c=200, k=31).sketch_single("empty", ["ACGT" * 200])
    path = Path(server).parent / "empty.sylsp"
    path.write_bytes(sketch.to_bytes())
    result = _run("profile", "--socket", server, path)
    assert result.returncode == 0, result.stderr
    assert result.stderr.startswith("sylphd: no hits for empty (")
    assert "k-mers" in result.stderr and "reads" not in result.stderr  # a .sylsp has no read count
    assert len(result.stderr.splitlines()) == 1


def test_fastq_without_hits_reports_kmers_and_reads_on_stderr(server: Path) -> None:
    """The database holds one other genome, so K12 reads have no hits: the line says how many reads."""
    result = _run("profile", "--socket", server, K12_R1)
    assert result.returncode == 0, result.stderr
    (line,) = result.stderr.splitlines()
    assert line.startswith("sylphd: no hits for ")
    assert " k-mers, " in line and line.endswith(" reads)")


def test_json_passthrough_stdout(server: Path) -> None:
    """``--json`` without ``-o`` prints the server's document, unchanged."""
    document = json.loads(_run("profile", "--socket", server, CLODF13_FASTA, "--json").stdout)
    assert document["api_version"] == 1
    assert set(document) >= {"api_version", "params", "samples", "table", "caveats"}


def test_query_with_a_sylsp_sketch(server: Path, tmp_path: Path) -> None:
    with dnaio.open(CLODF13_FASTA) as reader:
        sequences = [record.sequence for record in reader]
    sketch = pysylph.Sketcher(c=200, k=31).sketch_single("clodf13", sequences)
    sketch_path = tmp_path / "clodf13.sylsp"
    sketch_path.write_bytes(sketch.to_bytes())

    result = _run("query", "--socket", server, sketch_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0].startswith("Sample_file\tGenome_file\tAdjusted_ANI")
    assert "GCA_000000001.1" in result.stdout

    # A .sylsp is read, not sketched: one sample, named after the file.
    document = json.loads(_run("query", "--socket", server, sketch_path, "--json").stdout)
    assert [sample["name"] for sample in document["samples"]] == ["clodf13"]


def test_gzipped_paired_reads_and_sample_names(server: Path) -> None:
    # k12 does not hit the plasmid database (too few k-mers), so the samples
    # without hits are what the document must still list.
    result = _run(
        "profile", "--socket", server, "-1", K12_R1, "-2", K12_R2, "--json"
    )
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    # A paired file name loses its _R1/_R2 suffix.
    assert [sample["name"] for sample in document["samples"]] == ["k12"]
    assert document["table"]["data"] == []

    # --sample NAME:FILE[,FILE] names it explicitly; R1+R2 is a pair.
    named = _run(
        "profile",
        "--socket",
        server,
        "--sample",
        f"pair:{K12_R1}+{K12_R2}",
        "--json",
    )
    assert named.returncode == 0, named.stderr
    assert [sample["name"] for sample in json.loads(named.stdout)["samples"]] == ["pair"]


def test_fields_and_taxonomy_flags(server: Path, tmp_path: Path) -> None:
    # --fields selects metadata columns (and drops the rest).
    result = _run(
        "profile", "--socket", server, CLODF13_FASTA, "--fields", "gtdb_taxonomy", "--json"
    )
    assert result.returncode == 0, result.stderr
    table = json.loads(result.stdout)["table"]
    assert table["columns"][-1] == "gtdb_taxonomy"
    assert table["data"][0][-1].startswith("d__Bacteria")

    # --fields none keeps the fixed columns only.
    none = _run("profile", "--socket", server, CLODF13_FASTA, "--fields", "none", "--json")
    assert none.returncode == 0, none.stderr
    assert json.loads(none.stdout)["table"]["columns"][-1] == "genome_id"

    # An unknown field is a server-side 400, so a client error (1).
    bad = _run("profile", "--socket", server, CLODF13_FASTA, "--fields", "nope")
    assert bad.returncode == 1
    assert "nope" in bad.stderr


def test_taxprof_files(server: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    result = _run(
        "profile", "--socket", server, CLODF13_FASTA, "--taxonomy", "gtdb", "--taxprof", str(out) + "/"
    )
    assert result.returncode == 0, result.stderr
    (path,) = sorted(out.glob("*.sylphmpa"))
    assert path.name == "clodf13.gtdb.sylphmpa"
    assert f"wrote {out}/clodf13.gtdb.sylphmpa" in result.stderr

    # The file is the document's taxprof text, byte for byte. The server builds
    # it only when the request asks for it, i.e. with --taxprof.
    second = tmp_path / "second"
    second.mkdir()
    document = json.loads(
        _run(
            "profile",
            "--socket",
            server,
            CLODF13_FASTA,
            "--taxonomy",
            "gtdb",
            "--taxprof",
            str(second) + "/",
            "--json",
        ).stdout
    )
    text = document["taxprof"]["clodf13"]["gtdb"]["sylphmpa"]
    assert path.read_text() == text
    assert "clodf13" in text.splitlines()[0] or text.splitlines()[0].startswith("#SampleID")


def test_taxprof_adds_no_columns_to_the_table(server: Path, tmp_path: Path) -> None:
    """The server aggregates from its own columns: the table keeps the requested fields,
    and the client sends ``fields`` and ``taxprof`` as given."""
    out = tmp_path / "out"
    out.mkdir()
    common = ("profile", "--socket", server, CLODF13_FASTA, "--fields", "none", "--taxonomy", "gtdb,ncbi")
    plain = _run(*common)
    with_taxprof = _run(*common, "--taxprof", str(out) + "/")
    assert plain.returncode == with_taxprof.returncode == 0, with_taxprof.stderr
    assert with_taxprof.stdout == plain.stdout
    assert "gtdb_taxonomy" not in plain.stdout.splitlines()[0]
    assert sorted(path.name for path in out.glob("*.sylphmpa")) == [
        "clodf13.gtdb.sylphmpa",
        "clodf13.ncbi.sylphmpa",
    ]
    # A typo fails before anything is sketched or sent.
    bad = _run(*common[:-1], "bogus", "--taxprof", str(out) + "/")
    assert bad.returncode == 1
    assert "taxonom" in bad.stderr


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


def test_assembly_tsv_and_summary(server: Path) -> None:
    result = _run("assembly", "--socket", server, CLODF13_FASTA)
    assert result.returncode == 0, result.stderr
    header, *rows = result.stdout.splitlines()
    assert header.split("\t") == [
        "rank",
        "ani",
        "relation",
        "shared_rank",
        "accession",
        "genome_id",
        "taxon",
        "gtdb_species",
        "taxonomy_source",
        "ncbi_organism_name",
        "ncbi_taxid",
    ]
    (row,) = [line.split("\t") for line in rows]
    assert row[:5] == ["1", "100.00", "same species", "species", "GCA_000000001.1"]
    assert row[6] == "Test"  # the last named rank
    assert row[7] == ""  # the fixture lineage has no s__ rank
    assert "assigned to species Test" in result.stderr

    # --no-local-sketch uploads the raw (gzip) sequence: same hits, same summary.
    raw = _run("assembly", "--socket", server, CLODF13_FASTA, "--no-local-sketch")
    assert raw.returncode == 0, raw.stderr
    assert raw.stdout == result.stdout
    assert raw.stderr == result.stderr

    # --quiet drops the summary line.
    quiet = _run("assembly", "--socket", server, CLODF13_FASTA, "--quiet")
    assert quiet.returncode == 0, quiet.stderr
    assert quiet.stderr == ""


def test_assembly_json_and_render(server: Path, tmp_path: Path) -> None:
    output = tmp_path / "assembly.json"
    saved = _run("assembly", "--socket", server, CLODF13_FASTA, "--json", "-o", output)
    assert saved.returncode == 0, saved.stderr
    assert saved.stderr == ""
    document = json.loads(output.read_text())
    assert document["api_version"] == 1
    assert document["query"]["input"] == "sketch"
    assert (document["query"]["records"], document["query"]["length"]) == (1, 9957)
    hits = [dict(zip(document["table"]["columns"], values)) for values in document["table"]["data"]]
    assert hits[0]["accession"] == "GCA_000000001.1"
    rendered = _run("render", output)
    assert rendered.returncode == 0, rendered.stderr
    tsv = _run("assembly", "--socket", server, CLODF13_FASTA)
    assert rendered.stdout == tsv.stdout
    assert rendered.stderr == tsv.stderr


#: The fixture genome is 9 957 bp, so the "long" contig needs a lowered floor.
CONTIG_FLOOR = 5000


def _contig_fasta(tmp_path: Path, genome: str) -> Path:
    """One classifiable contig, one below the length floor, one without k-mers."""
    path = tmp_path / "contigs.fa"
    path.write_text(
        "".join(
            f">{name}\n{sequence}\n"
            for name, sequence in [
                ("long", genome[1000:9000]),  # 8 000 bp
                ("medium", genome[6000:9000]),  # 3 000 bp
                ("short", genome[9000:9500]),  # 500 bp
                # a tandem repeat has no genome-style k-mers: 0 < 10
                ("repetitive", "ACGT" * 7_500),
            ]
        ),
        newline="",
    )
    return path


@pytest.fixture(scope="module")
def genome() -> str:
    with dnaio.open(CLODF13_FASTA) as reader:
        return "".join(record.sequence for record in reader)


def test_per_contig_tsv_and_skip_counters(server: Path, genome: str, tmp_path: Path) -> None:
    path = _contig_fasta(tmp_path, genome)
    result = _run(
        "assembly",
        "--socket",
        server,
        path,
        "--per-contig",
        "--min-contig-length",
        str(CONTIG_FLOOR),
        "--fields",
        "none",
    )
    assert result.returncode == 0, result.stderr
    header, *rows = result.stdout.splitlines()
    assert header.split("\t")[0] == "contig"
    assert header.split("\t")[-1] == "consensus_taxon"
    assert [line.split("\t")[0] for line in rows] == ["long"]

    summary = result.stderr
    # Three of four records never reach the server, each for its own reason.
    assert "per-contig: 1 of 4 contig(s)" in summary
    assert f"2 contigs (3.5 kb) below {CONTIG_FLOOR} bp not classified" in summary
    assert "1 contigs (30.0 kb) with too few k-mers not classified" in summary


def test_per_contig_json_is_the_run_document(server: Path, genome: str, tmp_path: Path) -> None:
    path = _contig_fasta(tmp_path, genome)
    output = tmp_path / "contigs.json"
    saved = _run(
        "assembly",
        "--socket",
        server,
        path,
        "--per-contig",
        "--min-contig-length",
        str(CONTIG_FLOOR),
        "--json",
        "-o",
        output,
    )
    assert saved.returncode == 0, saved.stderr
    document = json.loads(output.read_text())
    assert document["api_version"] == 1
    assert document["summary"]["queried_contigs"] == 1
    assert document["summary"]["contigs"] == 4
    assert document["summary"]["skipped_short"] == {"contigs": 2, "bp": 3_500}
    assert document["summary"]["skipped_few_kmers"] == {"contigs": 1, "bp": 30_000}
    assert document["summary"]["min_contig_length"] == CONTIG_FLOOR
    assert list(document["contigs"]) == ["long"]

    rendered = _run("render", output)
    assert rendered.returncode == 0, rendered.stderr
    tsv = _run(
        "assembly",
        "--socket",
        server,
        path,
        "--per-contig",
        "--min-contig-length",
        str(CONTIG_FLOOR),
    )
    assert rendered.stdout == tsv.stdout
    assert rendered.stderr == tsv.stderr


PER_CONTIG_KEYS = [
    "api_version",
    "params",
    "table",
    "contigs",
    "summary",
    "caveats",
    "database",
    "thresholds",
    "elapsed_ms",
]


def _per_contig_json(socket_path: Path, path: Path, *options: str) -> dict:
    result = _run("assembly", "--socket", socket_path, path, "--per-contig", "--json", *options)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_per_contig_batches_merge_into_the_single_request_run(
    server: Path, one_contig_server: Path, genome: str, tmp_path: Path
) -> None:
    """Two contigs in two requests (``--max-contigs 1``) give the run of one request."""
    path = _contig_fasta(tmp_path, genome)
    options = ("--min-contig-length", "1000")
    single = _per_contig_json(server, path, *options)
    batched = _per_contig_json(one_contig_server, path, *options)
    assert single["summary"]["queried_contigs"] == 2  # long and medium
    assert list(batched) == list(single) == PER_CONTIG_KEYS
    for document in (single, batched):
        document.pop("elapsed_ms")
    # The server's database summary and thresholds are the same too: one document, two ways.
    assert batched == single
    assert list(batched["contigs"]) == ["long", "medium"]

    rendered = [
        _run("assembly", "--socket", socket_path, path, "--per-contig", *options)
        for socket_path in (server, one_contig_server)
    ]
    assert rendered[0].returncode == rendered[1].returncode == 0
    assert rendered[0].stdout == rendered[1].stdout
    assert rendered[0].stderr == rendered[1].stderr


def test_per_contig_empty_run_has_the_shape_of_a_run_with_batches(
    server: Path, genome: str, tmp_path: Path
) -> None:
    """No contig reaches the server: the run is built from the request and /info alone."""
    path = _contig_fasta(tmp_path, genome)
    full = _per_contig_json(server, path, "--min-contig-length", "1000")
    empty = _per_contig_json(server, path, "--min-contig-length", "1000000")
    assert empty["summary"]["queried_contigs"] == 0
    assert empty["table"]["data"] == []
    assert empty["contigs"] == {}
    assert list(empty) == list(full) == PER_CONTIG_KEYS
    assert list(empty["params"]) == list(full["params"])
    assert empty["params"] == full["params"]
    assert empty["database"] == full["database"]
    assert empty["thresholds"] == full["thresholds"]
    assert empty["table"]["columns"] == full["table"]["columns"]
    assert list(empty["summary"]) == list(full["summary"])
    assert empty["elapsed_ms"] == 0.0


def test_per_contig_options(server: Path, tmp_path: Path) -> None:
    path = _contig_fasta(tmp_path, _genome())
    # --min-contig-length moves the floor, --fields adds the metadata columns.
    result = _run(
        "assembly",
        "--socket",
        server,
        path,
        "--per-contig",
        "--min-contig-length",
        "1",
        "--fields",
        "gtdb_taxonomy",
    )
    assert result.returncode == 0, result.stderr
    columns = result.stdout.splitlines()[0].split("\t")
    assert "gtdb_taxonomy" in columns
    # The medium contig is sent too; the 500 bp record and the repetitive one
    # have too few genome-style k-mers and are counted by the client instead.
    assert "per-contig: 2 of 4 contig(s)" in result.stderr
    assert "2 contigs (30.5 kb) with too few k-mers not classified" in result.stderr


def _genome() -> str:
    with dnaio.open(CLODF13_FASTA) as reader:
        return "".join(record.sequence for record in reader)


# ---------------------------------------------------------------------------
# errors, exit codes and the token warning
# ---------------------------------------------------------------------------


def test_exit_codes(server: Path, tmp_path: Path) -> None:
    # 2: no input samples (usage/input error).
    missing = _run("profile", "--socket", server)
    assert missing.returncode == 2
    assert "no input samples" in missing.stderr

    # 2: --per-contig always sketches locally.
    per_contig = _run(
        "assembly", "--socket", server, CLODF13_FASTA, "--per-contig", "--no-local-sketch"
    )
    assert per_contig.returncode == 2
    assert "--no-local-sketch cannot be used" in per_contig.stderr

    # 2: an option that only --per-contig takes.
    misplaced = _run("assembly", "--socket", server, CLODF13_FASTA, "--consensus-z", "2")
    assert misplaced.returncode == 2
    assert "--per-contig" in misplaced.stderr

    # 1: no server at that socket.
    unreachable = _run("info", "--socket", tmp_path / "missing.sock")
    assert unreachable.returncode == 1
    assert "cannot talk to sylphd on" in unreachable.stderr

    # 2: unknown command.
    unknown = _run("nope")
    assert unknown.returncode == 2


def test_a_wrong_token_is_warned_about(token_server: Path, tmp_path: Path) -> None:
    tokens = tmp_path / "wrong"
    tokens.write_text("y" * 30 + "\n")
    result = _run("info", "--socket", token_server, "--token-file", tokens)
    assert result.returncode == 0, result.stderr
    assert "does not accept this access token" in result.stderr

    # The right token: no warning.
    right = tmp_path / "right"
    right.write_text(TOKEN + "\n")
    ok = _run("info", "--socket", token_server, "--token-file", right)
    assert ok.returncode == 0, ok.stderr
    assert "access token" not in ok.stderr

    # A missing token file is a client error, with the path in the message.
    gap = _run("info", "--socket", token_server, "--token-file", tmp_path / "missing")
    assert gap.returncode == 1
    assert "cannot read token file" in gap.stderr


# ---------------------------------------------------------------------------
# fake HTTP server: --json passthrough and the api_version check
# ---------------------------------------------------------------------------

PASSTHROUGH_DOCUMENT = json.dumps(
    json.loads((REPO / "rust" / "sylphd-core" / "tests" / "data" / "profile_golden.json").read_text())[
        "cases"
    ][0]["document"]
)


def _info_document(api_version: int = 1) -> dict:
    return {
        "api_version": api_version,
        "service": "sylphd",
        "version": "0.7.0",
        "pysylph_version": "test",
        "python_version": "3.14",
        "database": {
            "name": "fake",
            "size": 1,
            "sha256": None,
            "genomes": 1,
            "c": 200,
            "k": 31,
            "screen_c": None,
            "two_stage": False,
        },
        "databases": [],
        "metadata": None,
        "limits": {
            "max_jobs": 1,
            "max_upload_mb": 512,
            "max_sketches": 4096,
            "max_contigs": 4096,
            "max_params_bytes": 1048576,
            "allow_sequences": False,
            "max_sequence_mb": None,
            "rate_limit": None,
            "max_queue": None,
        },
        "usage": {"enabled": False},
        "endpoints": ["/info", "/messages", "/profile", "/query"],
    }


@contextmanager
def _fake_server(info_document: dict, response_document: str, messages: tuple[int, str] | None = None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # keep pytest output clean
            pass

        def _send(self, status: int, body: str) -> None:
            payload = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            if self.path == "/info":
                self._send(200, json.dumps(info_document))
            elif self.path == "/messages":
                status, body = messages or (200, '{"messages": []}')
                self._send(status, body)
            else:
                self._send(404, '{"error": "not found"}')

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            self.rfile.read(length)
            self._send(200, response_document)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=10)


def test_json_passthrough_is_the_server_document() -> None:
    with _fake_server(_info_document(), PASSTHROUGH_DOCUMENT) as url:
        result = _run("profile", "--url", url, K12_R1, "--json")
    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip("\n") == PASSTHROUGH_DOCUMENT


@pytest.mark.parametrize(
    "messages",
    [(500, '{"error": "boom"}'), (404, "{}"), (200, "<html>not json</html>"), (200, '{"messages": 5}')],
    ids=["500", "404", "garbage", "shape"],
)
def test_a_broken_messages_endpoint_is_silent(messages: tuple[int, str]) -> None:
    """Operator messages are a nicety: the command works without them."""
    with _fake_server(_info_document(), PASSTHROUGH_DOCUMENT, messages=messages) as url:
        result = _run("profile", "--url", url, K12_R1)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_api_version_mismatch_fails_before_sketching() -> None:
    with _fake_server(_info_document(api_version=2), PASSTHROUGH_DOCUMENT) as url:
        result = _run("profile", "--url", url, K12_R1)
    assert result.returncode == 1
    assert "server API version 2 is newer than this client's (1): update sylphd" in result.stderr
    assert "update sylphd" in result.stderr
    # Nothing was sketched, so the profile response was never requested:
    assert result.stdout == ""


def test_render_golden_documents(tmp_path: Path) -> None:
    golden = json.loads(
        (REPO / "rust" / "sylphd-core" / "tests" / "data" / "assembly_golden.json").read_text()
    )
    case = golden["cases"][0]
    path = tmp_path / "assembly.json"
    path.write_text(json.dumps(case["document"]))
    result = _run("render", path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == case["expected_tsv"]
    assert result.stderr == case["expected_stderr"]

    # A document from another API version is refused.
    document = dict(case["document"])
    document["api_version"] = 99
    path.write_text(json.dumps(document))
    mismatch = _run("render", path)
    assert mismatch.returncode == 1
    assert "document API version 99 is newer" in mismatch.stderr


# ---------------------------------------------------------------------------
# the `sylphd` command forwards to the binary
# ---------------------------------------------------------------------------


def test_sylphd_forwards_the_query_commands(server: Path) -> None:
    forwarded = _run_sylphd("info", "--socket", server)
    direct = _run("info", "--socket", server)
    assert forwarded.returncode == 0, forwarded.stderr
    assert forwarded.stdout == direct.stdout
    assert forwarded.stderr == direct.stderr


def test_sylphd_propagates_the_exit_code(server: Path) -> None:
    forwarded = _run_sylphd("profile", "--socket", server)
    assert forwarded.returncode == 2
    assert "no input samples" in forwarded.stderr

    unreachable = _run_sylphd("info", "--socket", server.parent / "missing.sock")
    assert unreachable.returncode == 1


def test_long_names_and_gzip_passthrough(server: Path, tmp_path: Path) -> None:
    """A gzip file and a long sample name survive the forwarding unchanged."""
    name = "sample-" + "x" * 120
    result = _run_sylphd(
        "profile", "--socket", server, CLODF13_FASTA, "--sample-name", name, "--json"
    )
    assert result.returncode == 0, result.stderr
    assert [sample["name"] for sample in json.loads(result.stdout)["samples"]] == [name]


def test_gzip_input_is_read_without_a_python_unzipper(server: Path, tmp_path: Path) -> None:
    path = tmp_path / "two-members.fa.gz"
    member = CLODF13_FASTA.read_bytes()
    path.write_bytes(member + member)
    with dnaio.open(path) as reader:
        records = sum(1 for _ in reader)
    assert records == 2
    result = _run("assembly", "--socket", server, path)
    assert result.returncode == 0, result.stderr
    assert result.stdout  # both records are sketchable


def test_random_genome_fasta_is_accepted(server: Path, tmp_path: Path) -> None:
    rng = random.Random(7)
    path = tmp_path / "random.fa"
    path.write_text(">random\n" + "".join(rng.choice("ACGT") for _ in range(30_000)) + "\n")
    result = _run("assembly", "--socket", server, path)
    assert result.returncode == 0, result.stderr
    # No hit, but a well-formed answer: the header plus the summary line.
    assert result.stdout.splitlines()[0].startswith("rank\t")
    assert "no hit" in result.stderr or "no species" in result.stderr


# ---------------------------------------------------------------------------
# sylphd.run: the ~20-line Python helper
# ---------------------------------------------------------------------------


def test_run_returns_the_document(server: Path) -> None:
    from sylphd import run

    document = run("profile", CLODF13_FASTA, "--socket", server)
    assert document["api_version"] == 1
    assert [sample["name"] for sample in document["samples"]] == ["clodf13"]
    assert document["table"]["columns"][:2] == ["Sample_file", "Genome_file"]

    # --json is added, so it works the same if the caller passes it.
    again = run("profile", CLODF13_FASTA, "--socket", server, "--json")
    assert again == document


def test_run_passes_stderr_through(server: Path, tmp_path: Path, capsys) -> None:
    from sylphd import run

    # --json keeps the binary's stderr to the notes (the TSV summary is part of
    # the rendering); the "wrote" note of --taxprof is one of them.
    out = tmp_path / "out"
    out.mkdir()
    run(
        "profile",
        CLODF13_FASTA,
        "--socket",
        server,
        "--taxonomy",
        "gtdb",
        "--taxprof",
        str(out) + "/",
    )
    assert f"wrote {out}/clodf13.gtdb.sylphmpa" in capsys.readouterr().err


def test_run_raises_with_the_stderr(server: Path, tmp_path: Path) -> None:
    from sylphd import ClientRunError, run

    with pytest.raises(ClientRunError) as caught:
        run("info", "--socket", tmp_path / "missing.sock")
    assert caught.value.returncode == 1
    assert "cannot talk to sylphd on" in str(caught.value)
    assert "cannot talk to sylphd on" in caught.value.stderr


def test_run_rejects_what_it_cannot_do(tmp_path: Path) -> None:
    from sylphd import run

    with pytest.raises(ValueError, match="as its first argument"):
        run("render", tmp_path / "document.json")
    with pytest.raises(ValueError, match="as its first argument"):
        run()
    with pytest.raises(ValueError, match="do not pass -o"):
        run("profile", "x.fq", "-o", tmp_path / "out.json")
