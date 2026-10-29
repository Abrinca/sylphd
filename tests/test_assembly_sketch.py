"""``POST /assembly`` with a precomputed sketch (``application/vnd.sylph.sketch``).

The sketch path must return exactly what the raw-sequence path returns for the same
assembly, and must work on a server started without ``--allow-sequences``.
"""

from __future__ import annotations

import gzip
import json
import struct
import subprocess
import sys
import time
from pathlib import Path

import pysylph
import pytest
from starlette.testclient import TestClient

from sylphd.client import SKETCH_CONTENT_TYPE
from sylphd.server import ServerConfig, create_app
from sylphd.sketching import InputError, sketch_assembly

from conftest import CLODF13_FASTA, ECOLI_TWO_STAGE, K12_R1


def _fasta() -> bytes:
    return gzip.decompress(CLODF13_FASTA.read_bytes())


def _sketch(
    tmp_path: Path, data: bytes, *, c: int = 200, k: int = 31, name: str = "assembly", suffix: str = ".fa"
):
    path = tmp_path / f"input{suffix}"
    path.write_bytes(data)
    sketch, stats = sketch_assembly(path, name, c=c, k=k)
    return sketch.to_bytes(), stats


def _post_sketch(client: TestClient, body: bytes, **params):
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return client.post(
        "/assembly" + (f"?{query}" if query else ""),
        content=body,
        headers={"content-type": SKETCH_CONTENT_TYPE},
    )


def _post_raw(client: TestClient, body: bytes, **params):
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return client.post(
        "/assembly" + (f"?{query}" if query else ""),
        content=body,
        headers={"content-type": "application/octet-stream"},
    )


@pytest.fixture(scope="module")
def sketch_only_client(tiny_database: Path, tiny_metadata: Path):
    """A server started WITHOUT --allow-sequences."""
    app = create_app(ServerConfig(db_path=tiny_database, meta_path=tiny_metadata))
    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="module")
def sequences_client(tiny_database: Path, tiny_metadata: Path):
    app = create_app(
        ServerConfig(db_path=tiny_database, meta_path=tiny_metadata, allow_sequences=True)
    )
    with TestClient(app) as client:
        yield client


class TestSketchUpload:
    def test_works_without_allow_sequences(self, sketch_only_client, tmp_path) -> None:
        sketch, stats = _sketch(tmp_path, _fasta())
        response = _post_sketch(
            sketch_only_client, sketch, top=5, records=stats.records, length=stats.length, gc=stats.gc
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["query"] == {
            "name": "assembly",
            "records": 1,
            "length": 9957,
            "gc": stats.gc,
            "sketch_kmers": payload["query"]["sketch_kmers"],
            "input": "sketch",
        }
        assert payload["query"]["sketch_kmers"] > 0
        (hit,) = payload["hits"]
        assert hit["accession"] == "GCA_000000001.1"
        assert hit["ani"] == pytest.approx(100.0, abs=0.01)
        assert payload["assignment"]["accession"] == "GCA_000000001.1"
        assert payload["thresholds"] == {"species": 95, "imprecise_below": 85}

    def test_identical_to_the_raw_sequence_path(
        self, sketch_only_client, sequences_client, tmp_path
    ) -> None:
        sketch, _ = _sketch(tmp_path, _fasta())
        by_sketch = _post_sketch(sketch_only_client, sketch).json()
        by_sequence = _post_raw(sequences_client, _fasta()).json()
        for payload in (by_sketch, by_sequence):
            payload.pop("elapsed_ms")
        assert by_sketch["hits"] == by_sequence["hits"]
        assert by_sketch["assignment"] == by_sequence["assignment"]
        assert by_sketch["closest"] == by_sequence["closest"]
        assert by_sketch["database"] == by_sequence["database"]
        assert by_sketch["params"] == by_sequence["params"]
        query_sketch, query_sequence = by_sketch["query"], by_sequence["query"]
        assert query_sketch["sketch_kmers"] == query_sequence["sketch_kmers"]
        assert (query_sketch["input"], query_sequence["input"]) == ("sketch", "sequence")
        # what a sketch cannot tell the server stays empty unless the client says
        assert (query_sketch["records"], query_sketch["length"], query_sketch["gc"]) == (None, None, None)

    def test_identical_on_a_two_stage_database_with_many_hits(self, tmp_path) -> None:
        import dnaio

        with dnaio.open(K12_R1) as reader:
            fastq = b"".join(
                b"@r\n" + r.sequence.encode() + b"\n+\n" + r.qualities.encode() + b"\n"
                for r in reader
            )
        sketch, _ = _sketch(tmp_path, fastq, suffix=".fq")
        config = {"db_path": ECOLI_TWO_STAGE}
        with TestClient(create_app(ServerConfig(**config))) as plain, TestClient(
            create_app(ServerConfig(**config, allow_sequences=True))
        ) as seq:
            a = _post_sketch(plain, sketch, min_ani=70, top=20).json()
            b = _post_raw(seq, fastq, min_ani=70, top=20).json()
        assert a["hits"] == b["hits"] and a["hits"]
        assert a["closest"] == b["closest"]

    def test_info_reports_sequence_support(self, sketch_only_client, sequences_client) -> None:
        info = sketch_only_client.get("/info").json()
        assert info["limits"]["allow_sequences"] is False
        assert sequences_client.get("/info").json()["limits"]["allow_sequences"] is True
        assert "/assembly" in info["endpoints"]
        assert "assembly" not in info

    def test_content_type_parameters_are_ignored(self, sketch_only_client, tmp_path) -> None:
        sketch, _ = _sketch(tmp_path, _fasta())
        response = sketch_only_client.post(
            "/assembly", content=sketch, headers={"content-type": SKETCH_CONTENT_TYPE + "; charset=binary"}
        )
        assert response.status_code == 200

    def test_sketch_limit_is_the_general_upload_limit(self, tiny_database, tmp_path) -> None:
        # 1 MB upload limit; a 2 MB body is refused even though it is "a sketch"
        app = create_app(ServerConfig(db_path=tiny_database, max_upload_mb=1))
        with TestClient(app) as client:
            response = _post_sketch(client, b"\x00" * (2 * 1024 * 1024))
        assert response.status_code == 413

    def test_not_limited_by_the_sequence_limit(self, tiny_database, tmp_path) -> None:
        # --max-sequence-mb 1 must not apply to sketches: a >1 MB sketch is accepted
        import random

        rng = random.Random(1)
        fasta = b">big\n" + "".join(rng.choice("ACGT") for _ in range(2_500_000)).encode() + b"\n"
        sketch, _ = _sketch(tmp_path, fasta, c=200)
        app = create_app(
            ServerConfig(db_path=tiny_database, allow_sequences=True, max_sequence_mb=1)
        )
        with TestClient(app) as client:
            assert _post_raw(client, fasta).status_code == 413  # raw: limited
            assert _post_sketch(client, sketch).status_code == 200


class TestSketchErrors:
    def test_empty_body(self, sketch_only_client) -> None:
        assert _post_sketch(sketch_only_client, b"").status_code == 400

    @pytest.mark.parametrize(
        "body",
        [b"not a sketch", b"\x01\x02\x03", struct.pack("<Q", 100_000_000), b">a\nACGT\n"],
    )
    def test_malformed_sketch_is_400(self, sketch_only_client, body) -> None:
        response = _post_sketch(sketch_only_client, body)
        assert response.status_code == 400
        assert "not a valid sample sketch" in response.json()["error"]

    def test_truncated_sketch_is_400(self, sketch_only_client, tmp_path) -> None:
        sketch, _ = _sketch(tmp_path, _fasta())
        assert _post_sketch(sketch_only_client, sketch[: len(sketch) // 2]).status_code == 400

    def test_k_mismatch_is_400(self, sketch_only_client, tmp_path) -> None:
        sketch, _ = _sketch(tmp_path, _fasta(), k=21)
        response = _post_sketch(sketch_only_client, sketch)
        assert response.status_code == 400
        assert "k=21" in response.json()["error"]

    def test_c_sparser_than_database_is_400(self, sketch_only_client, tmp_path) -> None:
        sketch, _ = _sketch(tmp_path, _fasta(), c=1000)
        response = _post_sketch(sketch_only_client, sketch)
        assert response.status_code == 400
        assert "sparser" in response.json()["error"]

    def test_denser_sketch_is_accepted(self, sketch_only_client, tmp_path) -> None:
        sketch, _ = _sketch(tmp_path, _fasta(), c=100)
        assert _post_sketch(sketch_only_client, sketch).status_code == 200

    @pytest.mark.parametrize(
        "params",
        [{"records": -1}, {"records": "x"}, {"length": "1.5"}, {"gc": 101}, {"gc": "x"}, {"top": 0}, {"min_ani": 60}],
    )
    def test_parameter_validation(self, sketch_only_client, tmp_path, params) -> None:
        sketch, _ = _sketch(tmp_path, _fasta())
        assert _post_sketch(sketch_only_client, sketch, **params).status_code == 400

    def test_raw_sequences_still_refused(self, sketch_only_client) -> None:
        assert _post_raw(sketch_only_client, _fasta()).status_code == 404

    def test_unknown_content_type_with_sequences_enabled(self, sequences_client) -> None:
        response = sequences_client.post(
            "/assembly", content=_fasta(), headers={"content-type": "multipart/form-data"}
        )
        assert response.status_code == 415
        assert SKETCH_CONTENT_TYPE in response.json()["error"]


def test_sketch_uploads_are_rate_limited(tiny_database, tmp_path) -> None:
    sketch, _ = _sketch(tmp_path, _fasta())
    app = create_app(ServerConfig(db_path=tiny_database, rate_limit=(1, 60.0)))
    with TestClient(app) as client:
        assert _post_sketch(client, sketch).status_code == 200
        response = _post_sketch(client, sketch)
    assert response.status_code == 429


def test_sketch_assembly_matches_the_servers_sketch(tmp_path) -> None:
    """The client-side sketch equals what the server builds from the same FASTA."""
    from sylphd.assembly import parse_assembly

    data = _fasta()
    local, stats = _sketch(tmp_path, data)
    parsed = parse_assembly(data)
    server_side = pysylph.Sketcher(c=200, k=31).sketch_single("assembly", list(parsed.sequences))
    assert pysylph.SampleSketch.from_bytes(local).kmer_counts == server_side.kmer_counts
    assert (stats.records, stats.length, stats.gc) == (parsed.records, parsed.length, parsed.gc)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _wait_for_socket(path: Path, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_socket():
            return
        time.sleep(0.1)
    raise AssertionError(f"server did not create {path} in time")


def _serve(tmp_path_factory, tiny_database, tiny_metadata, *extra: str):
    directory = tmp_path_factory.mktemp("asm-sketch-server")
    socket_path = directory / "sylphd.sock"
    process = subprocess.Popen(
        [
            sys.executable, "-m", "sylphd", "serve",
            "--db", str(tiny_database), "--meta", str(tiny_metadata),
            "--socket", str(socket_path), "--log-level", "warning", *extra,
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
def cli_sketch_only_server(tmp_path_factory, tiny_database, tiny_metadata):
    yield from _serve(tmp_path_factory, tiny_database, tiny_metadata)


@pytest.fixture(scope="module")
def cli_sequences_server(tmp_path_factory, tiny_database, tiny_metadata):
    yield from _serve(tmp_path_factory, tiny_database, tiny_metadata, "--allow-sequences")


def _assembly(socket: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "sylphd", "assembly", str(CLODF13_FASTA), "--socket", str(socket), "--json", *args],
        capture_output=True,
        text=True,
        timeout=600,
    )


class TestCLI:
    def test_default_sketches_locally(self, cli_sketch_only_server) -> None:
        result = _assembly(cli_sketch_only_server)  # the server has no --allow-sequences
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["query"]["input"] == "sketch"
        assert (payload["query"]["records"], payload["query"]["length"]) == (1, 9957)
        assert payload["hits"][0]["relation"] == "same species"

    def test_explicit_local_sketch_on_a_server_with_sequences(self, cli_sequences_server) -> None:
        local = json.loads(_assembly(cli_sequences_server, "--local-sketch").stdout)
        raw = json.loads(_assembly(cli_sequences_server, "--no-local-sketch").stdout)
        assert (local["query"]["input"], raw["query"]["input"]) == ("sketch", "sequence")
        assert local["hits"] == raw["hits"]
        assert local["assignment"] == raw["assignment"]
        assert local["query"]["gc"] == raw["query"]["gc"]
        assert local["query"]["length"] == raw["query"]["length"]

    def test_no_local_sketch_is_refused_by_a_sketch_only_server(self, cli_sketch_only_server) -> None:
        result = _assembly(cli_sketch_only_server, "--no-local-sketch")
        assert result.returncode != 0
        assert "--allow-sequences" in result.stderr  # the server's own message

    def test_table_output_with_local_sketch(self, cli_sketch_only_server) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "sylphd", "assembly", str(CLODF13_FASTA), "--socket", str(cli_sketch_only_server)],
            capture_output=True, text=True, timeout=600,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines()[1].split("\t")[:3] == ["1", "100.00", "same species"]


class TestGzipMembers:
    """The client reads gzip like the server's raw-upload path does."""

    def test_concatenated_members_are_all_read(self, tmp_path) -> None:
        member = CLODF13_FASTA.read_bytes()
        single = tmp_path / "one.fa.gz"
        single.write_bytes(member)
        double = tmp_path / "two.fa.gz"
        double.write_bytes(member + member)
        _, one = sketch_assembly(single, "x", c=200, k=31)
        _, two = sketch_assembly(double, "x", c=200, k=31)
        assert two.records == 2 * one.records

    def test_truncated_gzip_is_an_input_error(self, tmp_path) -> None:
        path = tmp_path / "cut.fa.gz"
        path.write_bytes(CLODF13_FASTA.read_bytes()[:-100])
        with pytest.raises(InputError, match="cannot sketch"):
            sketch_assembly(path, "x", c=200, k=31)
