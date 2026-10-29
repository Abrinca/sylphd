"""Tests for the public-web features: ``/assembly``, limits and CORS."""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from sylphd.assembly import (
    IMPRECISE_BELOW_ANI,
    INTERPRETATION_THRESHOLDS,
    SPECIES_ANI,
    SequenceError,
    SequenceTooLarge,
    gunzip_limited,
    parse_assembly,
)
from sylphd.cli import _gtdb_species
from sylphd.limits import client_identity, parse_rate_limit
from sylphd.metadata import METADATA_COLUMNS
from sylphd.server import ServerConfig, create_app

from conftest import CLODF13_FASTA, ECOLI_TWO_STAGE, O157_SKETCH


def _raw_fasta() -> bytes:
    return gzip.decompress(CLODF13_FASTA.read_bytes())


def _gzip_fasta() -> bytes:
    return CLODF13_FASTA.read_bytes()


@pytest.fixture(scope="module")
def assembly_client(tiny_database: Path, tiny_metadata: Path):
    app = create_app(
        ServerConfig(
            db_path=tiny_database,
            meta_path=tiny_metadata,
            allow_sequences=True,
        )
    )
    with TestClient(app) as client:
        yield client


def _post_assembly(client: TestClient, body: bytes, **params):
    query = "&".join(f"{key}={value}" for key, value in params.items())
    url = "/assembly" + (f"?{query}" if query else "")
    return client.post(
        url, content=body, headers={"content-type": "application/octet-stream"}
    )


class TestAssemblyDisabled:
    def test_raw_sequences_are_refused_by_default(self) -> None:
        app = create_app(ServerConfig(db_path=ECOLI_TWO_STAGE))
        with TestClient(app) as test_client:
            response = test_client.post(
                "/assembly", content=_raw_fasta(), headers={"content-type": "text/plain"}
            )
            assert response.status_code == 404
            assert "--allow-sequences" in response.json()["error"]
            # no content type at all is a sequence upload too
            assert test_client.post("/assembly", content=_raw_fasta()).status_code == 404
            info = test_client.get("/info").json()
            assert info["limits"]["allow_sequences"] is False
            assert info["limits"]["max_sequence_mb"] is None
            # the endpoint exists for sketches (see tests/test_assembly_sketch.py)
            assert "/assembly" in info["endpoints"]
            assert "/assembly" in test_client.get("/").json()["endpoints"]


class TestAssemblyUpload:
    def test_plain_fasta(self, assembly_client: TestClient) -> None:
        response = _post_assembly(assembly_client, _raw_fasta(), top=5, min_ani=80)
        assert response.status_code == 200
        payload = response.json()

        assert set(payload) == {
            "query",
            "database",
            "params",
            "thresholds",
            "assignment",
            "closest",
            "elapsed_ms",
            "hits",
        }
        assert payload["query"]["name"] == "assembly"
        assert payload["query"]["records"] == 1
        assert payload["query"]["length"] == 9957
        assert payload["query"]["gc"] == pytest.approx(54.4, abs=0.1)
        assert payload["query"]["sketch_kmers"] > 0

        assert payload["database"]["name"] == "tiny.syldb"
        assert payload["database"]["genomes"] == 1
        assert payload["database"]["c"] == 200
        assert payload["database"]["k"] == 31
        assert payload["params"] == {"top": 5, "min_ani": 80.0}
        assert payload["thresholds"] == {"species": 95, "imprecise_below": 85}
        assert payload["elapsed_ms"] >= 0

        (hit,) = payload["hits"]
        assert hit["rank"] == 1
        assert hit["ani"] == pytest.approx(100.0, abs=0.01)
        assert hit["relation"] == "same species"
        assert hit["shared_rank"] == "species"
        assert hit["ani_species_level"] is True
        assert "interpretation" not in hit
        assert payload["assignment"]["rank"] == "species"
        assert payload["assignment"]["accession"] == "GCA_000000001.1"
        assert payload["assignment"]["ani"] == hit["ani"]
        assert payload["closest"]["accession"] == "GCA_000000001.1"
        assert hit["genome_file"] == "GCA_000000001.1_genomic.fna.gz"
        assert hit["accession"] == "GCA_000000001.1"
        assert hit["containment"] == [44, 44]
        assert hit["containment_fraction"] == 1.0
        # every metadata column is present, from the metadata database
        for column in METADATA_COLUMNS:
            assert column in hit
        assert hit["gtdb_taxonomy"] == "d__Bacteria;p__Test;c__Test"
        assert hit["genome_size"] == 9957
        assert hit["ncbi_taxid"] is None

    def test_gzip_magic_is_detected(self, assembly_client: TestClient) -> None:
        response = assembly_client.post(
            "/assembly?name=zipped",
            content=_gzip_fasta(),
            headers={"content-type": "application/gzip"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["query"]["name"] == "zipped"
        assert payload["query"]["length"] == 9957
        assert payload["hits"][0]["accession"] == "GCA_000000001.1"

    def test_fastq_is_accepted(self, assembly_client: TestClient) -> None:
        body = b"@r1\nACGTACGTACGTACGTACGTACGTACGTACGTACGTACGT\n+\n" + b"I" * 40 + b"\n"
        response = _post_assembly(assembly_client, body)
        assert response.status_code == 200
        assert response.json()["query"]["records"] == 1

    def test_wrong_content_type(self, assembly_client: TestClient) -> None:
        response = assembly_client.post(
            "/assembly",
            content=_raw_fasta(),
            headers={"content-type": "multipart/form-data"},
        )
        assert response.status_code == 415

    @pytest.mark.parametrize(
        "params",
        [
            {"top": 0},
            {"top": 51},
            {"top": "x"},
            {"min_ani": 69},
            {"min_ani": 101},
            {"min_ani": "high"},
        ],
    )
    def test_query_parameter_validation(
        self, assembly_client: TestClient, params
    ) -> None:
        response = _post_assembly(assembly_client, _raw_fasta(), **params)
        assert response.status_code == 400
        assert "error" in response.json()

    def test_min_ani_filters_hits(self, tiny_database: Path) -> None:
        app = create_app(ServerConfig(db_path=tiny_database, allow_sequences=True))
        with TestClient(app) as test_client:
            response = _post_assembly(test_client, b">a\n" + b"ACGT" * 2000 + b"\n")
            assert response.status_code == 200
            payload = response.json()
            assert payload["hits"] == []


class TestAssemblyErrors:
    @pytest.mark.parametrize(
        "body",
        [
            b"",
            b"   \n",
            b"hello world",
            b">a\n\n",
            b">a\nACGTX\n",
            b">a\n",
            b">a\nACGT\n>b\n",  # truncated second record
        ],
    )
    def test_invalid_body_is_400(self, assembly_client: TestClient, body) -> None:
        response = _post_assembly(assembly_client, body)
        assert response.status_code == 400
        assert "error" in response.json()
        # no sequence or header contents leak into the message
        assert "hello world" not in response.json()["error"]

    def test_size_limit_413(self, tiny_database: Path) -> None:
        app = create_app(
            ServerConfig(db_path=tiny_database, allow_sequences=True, max_sequence_mb=1)
        )
        with TestClient(app) as test_client:
            response = _post_assembly(test_client, b">a\n" + b"ACGT" * 600_000)
        assert response.status_code == 413
        assert "limit" in response.json()["error"]

    def test_gzip_bomb_413(self, tiny_database: Path) -> None:
        app = create_app(
            ServerConfig(db_path=tiny_database, allow_sequences=True, max_sequence_mb=1)
        )
        bomb = gzip.compress(b">a\n" + b"A" * (6 * 1024 * 1024))
        assert len(bomb) < 1024 * 1024
        with TestClient(app) as test_client:
            response = _post_assembly(test_client, bomb)
        assert response.status_code == 413
        assert "decompressed" in response.json()["error"]

    def test_invalid_gzip_400(self, assembly_client: TestClient) -> None:
        response = _post_assembly(assembly_client, b"\x1f\x8bnope")
        assert response.status_code == 400


class TestAssemblyHelpers:
    def test_parse_assembly(self) -> None:
        parsed = parse_assembly(b">a\nACGTNN\n>b\nGGCC\n")
        assert parsed.records == 2
        assert parsed.length == 10
        assert parsed.gc == 60.0
        assert parsed.sequences == ("ACGTNN", "GGCC")

    def test_thresholds_are_one_constant(self) -> None:
        assert INTERPRETATION_THRESHOLDS == {"species": 95.0, "imprecise_below": 85.0}
        assert SPECIES_ANI == 95.0 and IMPRECISE_BELOW_ANI == 85.0

    def test_gunzip_limited(self) -> None:
        data = gzip.compress(b"A" * 1000)
        assert gunzip_limited(data, 1000) == b"A" * 1000
        with pytest.raises(SequenceTooLarge):
            gunzip_limited(data, 999)
        with pytest.raises(SequenceError):
            gunzip_limited(b"not gzip", 10)

    def test_gtdb_species(self) -> None:
        assert (
            _gtdb_species("d__Bacteria;p__P;c__C;o__O;f__F;g__G;s__Escherichia coli")
            == "Escherichia coli"
        )
        assert _gtdb_species("d__Bacteria;p__P") == ""
        assert _gtdb_species(None) == ""


class TestNoAdjust:
    def test_query_reports_naive_ani_without_ci(self) -> None:
        app = create_app(ServerConfig(db_path=ECOLI_TWO_STAGE))
        with TestClient(app) as test_client:
            default = test_client.post(
                "/query",
                data={"params": json.dumps({"names": ["o157"]})},
                files=[("sketch", ("o157.sylsp", O157_SKETCH.read_bytes()))],
            ).json()[0]["rows"][0]
            naive = test_client.post(
                "/query",
                data={"params": json.dumps({"names": ["o157"], "no_adjust": True})},
                files=[("sketch", ("o157.sylsp", O157_SKETCH.read_bytes()))],
            ).json()[0]["rows"][0]

        assert default["Adjusted_ANI"] == pytest.approx(99.79, abs=0.01)
        assert naive["Adjusted_ANI"] == pytest.approx(96.02, abs=0.01)
        assert naive["Adjusted_ANI"] == pytest.approx(naive["Naive_ANI"], abs=0.005)
        assert naive["ANI_5-95_percentile"] is None
        assert default["ANI_5-95_percentile"] is not None

    def test_no_adjust_must_be_boolean(self) -> None:
        app = create_app(ServerConfig(db_path=ECOLI_TWO_STAGE))
        with TestClient(app) as test_client:
            response = test_client.post(
                "/query",
                data={"params": json.dumps({"no_adjust": "maybe"})},
                files=[("sketch", ("o157.sylsp", O157_SKETCH.read_bytes()))],
            )
        assert response.status_code == 400
        assert "boolean" in response.json()["error"]


class TestRateLimit:
    def test_429_with_retry_after(self) -> None:
        app = create_app(ServerConfig(db_path=ECOLI_TWO_STAGE, rate_limit=(2, 60.0)))
        with TestClient(app) as test_client:
            first = test_client.post("/query", json={})
            second = test_client.post("/query", json={})
            third = test_client.post("/query", json={})
            # /health is not limited
            assert test_client.get("/health").status_code == 200
        assert first.status_code == 415
        assert second.status_code == 415
        assert third.status_code == 429
        assert int(third.headers["retry-after"]) >= 1
        assert "rate limit" in third.json()["error"]

    def test_parse_rate_limit(self) -> None:
        assert parse_rate_limit("10/min") == (10, 60.0)
        assert parse_rate_limit("3/s") == (3, 1.0)
        for bad in ("10", "10/hour", "0/min", "x/min"):
            with pytest.raises(ValueError):
                parse_rate_limit(bad)


class TestClientIdentity:
    @staticmethod
    def _scope(peer: str | None, xff: str | None = None) -> dict:
        headers = []
        if xff is not None:
            headers.append((b"x-forwarded-for", xff.encode()))
        return {
            "type": "http",
            "client": None if peer is None else (peer, 1234),
            "headers": headers,
        }

    def test_unix_peer(self) -> None:
        assert client_identity(self._scope(None), frozenset()) == "unix"

    def test_xff_ignored_without_trusted_proxy(self) -> None:
        scope = self._scope("198.51.100.7", "203.0.113.9")
        assert client_identity(scope, frozenset()) == "198.51.100.7"

    def test_trusted_ip_uses_rightmost_untrusted(self) -> None:
        scope = self._scope("127.0.0.1", "1.1.1.1, 203.0.113.9")
        assert client_identity(scope, frozenset({"127.0.0.1"})) == "203.0.113.9"

    def test_trusted_unix_peer(self) -> None:
        scope = self._scope(None, "203.0.113.9")
        assert client_identity(scope, frozenset({"unix"})) == "203.0.113.9"
        assert client_identity(scope, frozenset()) == "unix"

    def test_trusted_proxy_changes_bucket(self) -> None:
        def app():
            return create_app(
                ServerConfig(
                    db_path=ECOLI_TWO_STAGE,
                    rate_limit=(1, 60.0),
                    trusted_proxies=("10.0.0.1",),
                )
            )
        with TestClient(app(), client=("10.0.0.1", 1)) as test_client:
            assert test_client.post("/query", json={}).status_code == 415
            other = test_client.post(
                "/query", json={}, headers={"x-forwarded-for": "203.0.113.9"}
            )
            assert other.status_code == 415  # different client: new bucket
            same = test_client.post(
                "/query", json={}, headers={"x-forwarded-for": "203.0.113.9"}
            )
            assert same.status_code == 429

        with TestClient(app(), client=("10.0.0.2", 1)) as test_client:
            assert test_client.post(
                "/query", json={}, headers={"x-forwarded-for": "203.0.113.9"}
            ).status_code == 415
            # the peer is not trusted, so XFF must not create a new bucket
            assert test_client.post(
                "/query", json={}, headers={"x-forwarded-for": "203.0.113.10"}
            ).status_code == 429


class TestQueueLimit:
    def test_503_with_retry_after(self, monkeypatch) -> None:
        started = threading.Event()
        release = threading.Event()

        def blocking_run_sample(*args, **kwargs):
            started.set()
            release.wait(timeout=10)
            return {"name": "blocked", "kind": "query", "rows": []}

        monkeypatch.setattr("sylphd.server._run_sample", blocking_run_sample)
        app = create_app(
            ServerConfig(db_path=ECOLI_TWO_STAGE, max_jobs=1, max_queue=0)
        )
        with TestClient(app) as test_client:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    test_client.post,
                    "/query",
                    data={"params": "{}"},
                    files=[("sketch", ("o157.sylsp", O157_SKETCH.read_bytes()))],
                )
                assert started.wait(timeout=5)
                rejected = test_client.post(
                    "/query",
                    data={"params": "{}"},
                    files=[("sketch", ("o157.sylsp", O157_SKETCH.read_bytes()))],
                )
                assert rejected.status_code == 503
                assert int(rejected.headers["retry-after"]) >= 1
                assert "busy" in rejected.json()["error"]
                release.set()
                assert future.result(timeout=10).status_code == 200


class TestCORS:
    def test_preflight_and_response_headers(self) -> None:
        origin = "https://abrinca.github.io"
        app = create_app(
            ServerConfig(db_path=ECOLI_TWO_STAGE, cors_origins=(origin,))
        )
        with TestClient(app) as test_client:
            preflight = test_client.options(
                "/query",
                headers={
                    "origin": origin,
                    "access-control-request-method": "POST",
                    "access-control-request-headers": "content-type",
                },
            )
            assert preflight.status_code == 200
            assert preflight.headers["access-control-allow-origin"] == origin
            assert "POST" in preflight.headers["access-control-allow-methods"]
            assert "content-type" in preflight.headers[
                "access-control-allow-headers"
            ].lower()

            response = test_client.post(
                "/query", json={}, headers={"origin": origin}
            )
            assert response.status_code == 415
            assert response.headers["access-control-allow-origin"] == origin
            assert "retry-after" in response.headers[
                "access-control-expose-headers"
            ].lower()

    def test_wildcard_origin(self) -> None:
        app = create_app(
            ServerConfig(db_path=ECOLI_TWO_STAGE, cors_origins=("*",))
        )
        with TestClient(app) as test_client:
            response = test_client.get(
                "/health", headers={"origin": "https://anywhere.example"}
            )
            assert response.status_code == 200
            assert response.headers["access-control-allow-origin"] == "*"

    def test_other_origin_is_not_allowed(self) -> None:
        app = create_app(
            ServerConfig(
                db_path=ECOLI_TWO_STAGE, cors_origins=("https://example.org",)
            )
        )
        with TestClient(app) as test_client:
            response = test_client.post(
                "/query", json={}, headers={"origin": "https://evil.example"}
            )
            assert "access-control-allow-origin" not in response.headers

    @pytest.mark.parametrize(
        ("origin", "allowed"),
        [
            ("http://localhost:8000", True),
            ("http://127.0.0.1:5173", True),
            ("http://0.0.0.0:8000", True),
            ("http://[::1]:8000", True),
            ("http://localhost", True),
            ("http://localhost.evil.example", False),
            ("http://127.0.0.1.evil.example:8000", False),
            ("https://example.org", False),
        ],
    )
    def test_local_origins(self, origin: str, allowed: bool) -> None:
        app = create_app(ServerConfig(db_path=ECOLI_TWO_STAGE, cors_origins=("local",)))
        with TestClient(app) as test_client:
            response = test_client.get("/health", headers={"origin": origin})
            assert ("access-control-allow-origin" in response.headers) == allowed


def _wait_for_socket(path: Path, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_socket():
            return
        time.sleep(0.1)
    raise AssertionError(f"server did not create {path} in time")


@pytest.fixture(scope="module")
def assembly_server(tmp_path_factory, tiny_database: Path, tiny_metadata: Path):
    directory = tmp_path_factory.mktemp("assembly-server")
    socket_path = directory / "sylphd.sock"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "sylphd",
            "serve",
            "--db",
            str(tiny_database),
            "--meta",
            str(tiny_metadata),
            "--socket",
            str(socket_path),
            "--allow-sequences",
            "--log-level",
            "warning",
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


class TestCLI:
    def test_assembly_command(self, assembly_server: Path, tmp_path: Path) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "sylphd",
                "assembly",
                str(CLODF13_FASTA),
                "--socket",
                str(assembly_server),
                "--top",
                "3",
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert result.returncode == 0, result.stderr
        lines = [line for line in result.stdout.splitlines() if line]
        header = lines[0].split("\t")
        assert header == [
            "rank",
            "ani",
            "relation",
            "shared_rank",
            "accession",
            "gtdb_species",
            "ncbi_organism_name",
            "ncbi_taxid",
        ]
        row = lines[1].split("\t")
        assert row[0] == "1"
        assert float(row[1]) == pytest.approx(100.0, abs=0.01)
        assert row[2] == "same species"
        assert row[3] == "species"
        assert row[4] == "GCA_000000001.1"
        assert row[5] == ""  # the test taxonomy has no s__ rank
        assert row[6] == ""

    def test_assembly_json(self, assembly_server: Path) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "sylphd",
                "assembly",
                str(CLODF13_FASTA),
                "--socket",
                str(assembly_server),
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["query"]["length"] == 9957
        assert payload["hits"][0]["relation"] == "same species"
