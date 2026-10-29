"""Tests for the Starlette application, in process with ``TestClient``."""

from __future__ import annotations

import json
import socket
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import dnaio
import pysylph
import pytest
import uvicorn
from conftest import CLODF13_FASTA, ECOLI_TWO_STAGE, K12_R1, MIX_SKETCH, O157_SKETCH
from starlette.testclient import TestClient

from sylphd.metadata import METADATA_COLUMNS
from sylphd.protocol import ProfileParams, columns_for_mode, format_row, format_value
from sylphd.server import (
    ServerConfig,
    _socket_identity,
    _unlink_socket_if_owned,
    bind_unix_socket,
    create_app,
    prepare_unix_socket,
    serve,
)


def _reads(path):
    with dnaio.open(path) as reader:
        return [record.sequence for record in reader]


def _post(client: TestClient, endpoint: str, payloads: list[tuple[str, bytes]], **params):
    body = {"params": json.dumps(params)}
    files = [("sketch", (f"{name}.sylsp", data)) for name, data in payloads]
    return client.post(f"/{endpoint}", data=body, files=files)


@pytest.fixture(scope="module")
def client():
    app = create_app(ServerConfig(db_path=ECOLI_TWO_STAGE))
    with TestClient(app) as test_client:
        yield test_client


class TestBasics:
    def test_health(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_index(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "/profile" in response.json()["endpoints"]

    def test_info(self, client: TestClient) -> None:
        info = client.get("/info").json()
        assert info["database"]["name"] == "ecoli.syl2db"
        assert info["database"]["c"] == 200
        assert info["database"]["k"] == 31
        assert info["database"]["screen_c"] == 3000
        assert info["database"]["genomes"] == 3
        assert info["database"]["two_stage"] is True
        assert info["database"]["size"] > 0
        assert info["metadata"] is None
        assert info["limits"]["max_jobs"] == 2
        assert info["pysylph_version"]
        assert info["python_version"].startswith("3.")

    def test_unknown_route(self, client: TestClient) -> None:
        response = client.get("/nope")
        assert response.status_code == 404
        assert "error" in response.json()


class TestProfileQuery:
    def test_query(self, client: TestClient) -> None:
        payload = O157_SKETCH.read_bytes()
        response = _post(client, "query", [("o157", payload)], names=["o157"])
        assert response.status_code == 200
        samples = response.json()
        assert len(samples) == 1
        assert samples[0]["name"] == "o157"
        assert samples[0]["kind"] == "query"
        rows = samples[0]["rows"]
        assert len(rows) == 3
        assert rows[0]["Sample_file"] == "o157"
        assert rows[0]["Adjusted_ANI"] == pytest.approx(99.79, abs=0.01)
        assert rows[0]["Naive_ANI"] == pytest.approx(96.02, abs=0.01)
        assert rows[0]["Containment_ind"] == [6208, 21899]
        assert "Taxonomic_abundance" not in rows[0]
        assert "kmers_reassigned" not in rows[0]
        assert rows[0]["accession"] is None

    def test_profile(self, client: TestClient) -> None:
        payload = O157_SKETCH.read_bytes()
        response = _post(client, "profile", [("o157", payload)], names=["o157"])
        assert response.status_code == 200
        rows = response.json()[0]["rows"]
        assert len(rows) == 1
        assert rows[0]["Taxonomic_abundance"] == pytest.approx(100.0)
        assert rows[0]["Sequence_abundance"] == pytest.approx(100.0)
        assert rows[0]["kmers_reassigned"] == 0

    def test_estimate_read_counts(self, client: TestClient) -> None:
        """``estimate_read_counts`` reports read counts and implies ``True_cov``.

        ``sylph profile --estimate-read-counts ecoli.syldb o157.sylsp`` reports
        1149 reads for o157; the same value as pysylph's direct call, and it is
        integral (the library rounds it).
        """
        payload = O157_SKETCH.read_bytes()
        response = _post(
            client,
            "profile",
            [("o157", payload)],
            names=["o157"],
            estimate_read_counts=True,
        )
        assert response.status_code == 200
        rows = response.json()[0]["rows"]
        assert len(rows) == 1
        assert rows[0]["Taxonomic_abundance"] == pytest.approx(100.0)
        assert rows[0]["Sequence_abundance"] == pytest.approx(1149.0)
        assert float(rows[0]["Sequence_abundance"]).is_integer()

        params = ProfileParams(estimate_read_counts=True)
        assert "True_cov" in columns_for_mode("profile", params)
        assert "Eff_cov" not in columns_for_mode("profile", params)

    def test_parameters_are_forwarded(self, client: TestClient) -> None:
        payload = O157_SKETCH.read_bytes()
        response = _post(
            client, "query", [("o157", payload)], names=["o157"], minimum_ani=99.0
        )
        assert response.status_code == 200
        assert len(response.json()[0]["rows"]) == 1

    def test_multiple_samples_keep_order(self, client: TestClient) -> None:
        o157 = O157_SKETCH.read_bytes()
        mix = MIX_SKETCH.read_bytes()
        response = _post(
            client, "query", [("first", o157), ("second", mix)], names=["first", "second"]
        )
        assert response.status_code == 200
        names = [sample["name"] for sample in response.json()]
        assert names == ["first", "second"]
        assert all(sample["rows"] for sample in response.json())

    def test_name_falls_back_to_filename(self, client: TestClient) -> None:
        response = _post(client, "query", [("from-file", O157_SKETCH.read_bytes())])
        assert response.json()[0]["name"] == "from-file"


class TestErrors:
    def test_requires_multipart(self, client: TestClient) -> None:
        response = client.post("/query", json={})
        assert response.status_code == 415
        assert "multipart" in response.json()["error"]

    def test_missing_sketch(self, client: TestClient) -> None:
        response = client.post("/query", files={"params": (None, "{}")})
        assert response.status_code == 400
        assert "sketch" in response.json()["error"]

    def test_invalid_json(self, client: TestClient) -> None:
        response = client.post(
            "/query", data={"params": "{"}, files=[("sketch", ("x", b"x"))]
        )
        assert response.status_code == 400
        assert "invalid JSON" in response.json()["error"]

    def test_params_not_object(self, client: TestClient) -> None:
        response = client.post(
            "/query", data={"params": "[1]"}, files=[("sketch", ("x", b"x"))]
        )
        assert response.status_code == 400
        assert "JSON object" in response.json()["error"]

    def test_unknown_parameter(self, client: TestClient) -> None:
        response = _post(
            client, "query", [("x", b"x")], bogus=1
        )
        assert response.status_code == 400
        assert "unknown parameter" in response.json()["error"]

    @pytest.mark.parametrize(
        "params, needle",
        [
            ({"minimum_ani": "high"}, "finite number"),
            ({"minimum_ani": 120}, "<= 100"),
            ({"min_contain": -1}, ">= 0"),
            ({"min_contain": 1.5}, "integer"),
            ({"estimate_unknown": "maybe"}, "boolean"),
            ({"estimate_read_counts": "maybe"}, "boolean"),
        ],
    )
    def test_bad_parameter_values(self, client: TestClient, params, needle) -> None:
        response = _post(client, "query", [("x", b"x")], **params)
        assert response.status_code == 400
        assert needle in response.json()["error"]

    def test_mode_mismatch(self, client: TestClient) -> None:
        response = _post(client, "query", [("x", b"x")], mode="profile")
        assert response.status_code == 400
        assert "mode" in response.json()["error"]

    def test_names_count_mismatch(self, client: TestClient) -> None:
        response = _post(client, "query", [("x", b"x")], names=[])
        assert response.status_code == 400
        assert "entries" in response.json()["error"]

    @pytest.mark.parametrize("name", ["a\tb", "a\nb", "a\x00b", "x" * 1025])
    def test_bad_sample_name(self, client: TestClient, name: str) -> None:
        response = _post(client, "query", [("x", b"x")], names=[name])
        assert response.status_code == 400
        assert "sample name" in response.json()["error"]

    def test_invalid_sketch(self, client: TestClient) -> None:
        response = _post(client, "query", [("bad", b"not a sketch")])
        assert response.status_code == 400
        assert "not a valid sample sketch" in response.json()["error"]

    def test_allocation_bomb_sketch_returns_400(self, client: TestClient) -> None:
        # Eight bytes declaring 100 million k-mers: sylph's deserializer would
        # reserve the memory up front and abort the process.  The validator
        # rejects it from the length prefix instead.
        response = _post(client, "query", [("bomb", struct.pack("<Q", 100_000_000))])
        assert response.status_code == 400
        assert "declares" in response.json()["error"]
        # The server is still alive and still profiles normal sketches.
        assert client.get("/health").status_code == 200

        assert _post(client, "query", [("o157", O157_SKETCH.read_bytes())]).status_code == 200

    def test_k_mismatch(self, client: TestClient) -> None:
        sketch = pysylph.Sketcher(c=200, k=21).sketch_single("wrong-k", _reads(K12_R1))
        response = _post(client, "query", [("wrong-k", sketch.to_bytes())])
        assert response.status_code == 400
        assert "does not match the database k" in response.json()["error"]

    def test_c_too_sparse(self, client: TestClient) -> None:
        sketch = pysylph.Sketcher(c=400, k=31).sketch_single("sparse", _reads(K12_R1))
        response = _post(client, "query", [("sparse", sketch.to_bytes())])
        assert response.status_code == 400
        assert "sparser" in response.json()["error"]

    def test_plain_database_advertises_the_smallest_genome_c(self, tmp_path: Path) -> None:
        """A plain database's ``c`` is the minimum over its genomes.

        The genomes below were sketched at c=400 and c=200, so ``/info`` says
        200 and a c=300 sketch is refused by the header check instead of being
        sent to pysylph, which would reject it while naming only the genome.
        """
        with dnaio.open(CLODF13_FASTA) as reader:
            sequences = [record.sequence for record in reader]
        long_reads = sequences[0]
        sketcher400 = pysylph.Sketcher(c=400, k=31)
        sketcher200 = pysylph.Sketcher(c=200, k=31)
        database = pysylph.Database(
            [
                sketcher400.sketch_genome("GCA_000000001.1_genomic.fna.gz", sequences),
                sketcher200.sketch_genome(
                    "GCA_000000002.1_genomic.fna.gz", [long_reads[: len(long_reads) // 2]]
                ),
            ]
        )
        db_path = tmp_path / "mixed-c.syldb"
        database.dump(str(db_path))

        app = create_app(ServerConfig(db_path=db_path))
        sketch = pysylph.Sketcher(c=300, k=31).sketch_single("mixed", _reads(K12_R1))
        with TestClient(app) as mixed:
            assert mixed.get("/info").json()["database"]["c"] == 200
            response = _post(mixed, "profile", [("mixed", sketch.to_bytes())])
            assert response.status_code == 400
            error = response.json()["error"]
            assert "sketch c=300" in error and "database c=200" in error
            # Still a healthy server: the next request is answered normally.
            assert mixed.get("/health").status_code == 200

    def test_profiler_runtime_error_is_400(self, client: TestClient, monkeypatch) -> None:
        """A ``ValueError`` from ``profile``/``query`` is also a 400.

        sylphd's header check catches ``c``/``k`` mismatches first; pysylph can
        still refuse a sketch per genome, and that is the client's bad request.
        """

        class Refuse:
            def profile(self, sketch):
                raise ValueError(
                    "c parameter for sample (300) > c parameter for genome x (200)"
                )

        monkeypatch.setattr(
            client.app.state.database, "profiler", lambda params: Refuse()
        )
        response = _post(client, "profile", [("o157", O157_SKETCH.read_bytes())])
        assert response.status_code == 400
        assert "cannot profile this sample" in response.json()["error"]
        assert "c parameter for sample" in response.json()["error"]

    def test_profiler_construction_error_is_400(
        self, client: TestClient, monkeypatch
    ) -> None:
        """A ``ValueError`` from the ``Profiler`` constructor is also a 400."""

        def refuse(*args, **kwargs):
            raise ValueError("k parameter for sample (31) != k parameter for database (21)")

        # Profilers are cached per parameter set; drop the cached one so this
        # request has to build (and the patched constructor is called).
        client.app.state.database.clear_profilers()
        monkeypatch.setattr("sylphd.profiling.pysylph.Profiler", refuse)
        response = _post(client, "profile", [("o157", O157_SKETCH.read_bytes())])
        assert response.status_code == 400
        assert "k parameter for sample" in response.json()["error"]

    def test_unknown_fields(self, client: TestClient) -> None:
        response = _post(client, "query", [("o157", O157_SKETCH.read_bytes())], fields=["nope"])
        # no metadata database on this server: any non-empty fields request fails
        assert response.status_code == 400
        assert "metadata" in response.json()["error"]

    def test_empty_fields_are_allowed_without_metadata(self, client: TestClient) -> None:
        response = _post(client, "query", [("o157", O157_SKETCH.read_bytes())], fields=[])
        assert response.status_code == 200

    def test_upload_limit(self) -> None:
        app = create_app(ServerConfig(db_path=ECOLI_TWO_STAGE, max_upload_mb=1))
        with TestClient(app) as limited:
            response = limited.post(
                "/query",
                data={"params": "{}"},
                files=[("sketch", ("big", b"x" * (2 * 1024 * 1024)))],
            )
        assert response.status_code == 413
        assert "limit" in response.json()["error"]

    def test_too_many_sketches_is_400(self, client: TestClient) -> None:
        files = [("sketch", (f"s{i}", b"x")) for i in range(4097)]
        response = client.post("/query", data={"params": "{}"}, files=files)
        assert response.status_code == 400
        assert "Too many files" in response.json()["error"]

    def test_non_file_sketch_part(self, client: TestClient) -> None:
        response = client.post(
            "/query",
            data={"params": "{}"},
            files={"sketch": (None, "not a file")},
        )
        assert response.status_code == 400
        assert "file uploads" in response.json()["error"]

    @pytest.mark.parametrize(
        "params, needle",
        [
            ({"minimum_ani": float("inf")}, "finite number"),
            ({"minimum_ani": float("nan")}, "finite number"),
            ({"minimum_ani": 10**400}, "finite number"),
            ({"min_number_kmers": 2**63}, "<= 9223372036854775807"),
        ],
    )
    def test_unrepresentable_parameter_values(
        self, client: TestClient, params, needle
    ) -> None:
        response = _post(client, "query", [("x", b"x")], **params)
        assert response.status_code == 400
        assert needle in response.json()["error"]

    def test_unexpected_profiling_error_returns_json_500(
        self, client: TestClient, monkeypatch
    ) -> None:
        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr("sylphd.server.run_sample", boom)
        response = _post(client, "query", [("o157", O157_SKETCH.read_bytes())])
        assert response.status_code == 500
        assert "profiling failed" in response.json()["error"]


class TestMetadataJoin:
    def test_metadata_columns(self, tiny_database: Path, tiny_metadata: Path) -> None:
        app = create_app(ServerConfig(db_path=tiny_database, meta_path=tiny_metadata))
        with TestClient(app) as joined:
            info = joined.get("/info").json()
            assert info["metadata"]["rows"] == 1
            assert info["metadata"]["release"] == "232"

            response = _post(
                joined, "query", [("mix", MIX_SKETCH.read_bytes())], names=["mix"]
            )
            assert response.status_code == 200
            row = response.json()[0]["rows"][0]
            assert row["accession"] == "GCA_000000001.1"
            assert row["gtdb_taxonomy"] == "d__Bacteria;p__Test;c__Test"
            assert row["genome_size"] == 9957
            assert row["mimag_high_quality"] == 1
            assert row["ncbi_taxid"] is None

            # select a subset
            response = _post(
                joined,
                "query",
                [("mix", MIX_SKETCH.read_bytes())],
                names=["mix"],
                fields=["gtdb_taxonomy", "genome_size"],
            )
            row = response.json()[0]["rows"][0]
            assert set(row) & set(METADATA_COLUMNS) == {"gtdb_taxonomy", "genome_size"}

            # unknown column
            response = _post(
                joined,
                "query",
                [("mix", MIX_SKETCH.read_bytes())],
                names=["mix"],
                fields=["nope"],
            )
            assert response.status_code == 400


class TestFormatting:
    def test_format_value(self) -> None:
        assert format_value("Adjusted_ANI", 99.789) == "99.79"
        assert format_value("Eff_cov", 0.3597986) == "0.360"
        assert format_value("Median_cov", 1.0) == "1"
        assert format_value("ANI_5-95_percentile", [99.6437, 99.9452]) == "99.64-99.95"
        assert format_value("Containment_ind", [6208, 21899]) == "6208/21899"
        assert format_value("Taxonomic_abundance", 100.0) == "100.0000"
        assert format_value("Eff_lambda", None) == ""
        assert format_value("Sample_file", "x") == "x"
        assert format_value("kmers_reassigned", 0) == "0"

    def test_true_cov_header_uses_eff_cov_value(self) -> None:
        row = {"Eff_cov": 0.3597986}
        assert format_row(row, ["True_cov"]) == "0.360"


class TestUnixSocketCleanup:
    def test_replaced_socket_is_not_unlinked(self, tmp_path: Path) -> None:
        path = tmp_path / "s.sock"
        sock = bind_unix_socket(path)
        identity = _socket_identity(path)
        assert identity is not None
        sock.close()
        path.unlink()
        path.write_text("replacement")
        _unlink_socket_if_owned(path, identity)
        assert path.exists()
        assert path.read_text() == "replacement"

    def test_owned_socket_is_unlinked(self, tmp_path: Path) -> None:
        path = tmp_path / "s.sock"
        sock = bind_unix_socket(path)
        identity = _socket_identity(path)
        assert identity is not None
        sock.close()
        _unlink_socket_if_owned(path, identity)
        assert not path.exists()

    def test_prepare_unix_socket_refuses_live(self, tmp_path: Path) -> None:
        path = tmp_path / "live.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        listener.listen(1)
        try:
            with pytest.raises(RuntimeError, match="already listening"):
                prepare_unix_socket(path)
        finally:
            listener.close()
            path.unlink()

    def test_prepare_unix_socket_removes_stale(self, tmp_path: Path) -> None:
        path = tmp_path / "stale.sock"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        sock.close()
        prepare_unix_socket(path)
        assert not path.exists()


class TestServe:
    def test_uvicorn_does_not_rewrite_client_from_proxy_headers(self, monkeypatch) -> None:
        calls = []
        monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
        serve(ServerConfig(db_path=ECOLI_TWO_STAGE), tcp=("127.0.0.1", 0), app=object())
        assert calls[0]["proxy_headers"] is False


class TestConcurrency:
    def test_health_responsive_while_jobs_busy(
        self, client: TestClient, monkeypatch
    ) -> None:
        started = threading.Event()
        release = threading.Event()

        def blocking_run_sample(*args, **kwargs):
            started.set()
            release.wait(timeout=10)
            return {"name": "blocked", "kind": "query", "rows": []}

        monkeypatch.setattr("sylphd.server.run_sample", blocking_run_sample)
        app = create_app(
            ServerConfig(db_path=ECOLI_TWO_STAGE, max_jobs=1),
            database=client.app.state.database,
        )
        with TestClient(app) as test_client, ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                test_client.post,
                "/query",
                data={"params": "{}"},
                files=[("sketch", ("o157.sylsp", O157_SKETCH.read_bytes()))],
            )
            assert started.wait(timeout=5)
            health = test_client.get("/health")
            assert health.status_code == 200
            release.set()
            response = future.result(timeout=10)
            assert response.status_code == 200

    def test_unexpected_failure_is_logged_but_not_sent_to_the_client(
        self, client: TestClient, monkeypatch, caplog
    ) -> None:
        def broken_run_sample(*args, **kwargs):
            raise RuntimeError("secret internal detail")

        monkeypatch.setattr("sylphd.server.run_sample", broken_run_sample)
        with caplog.at_level("ERROR", logger="uvicorn.error"):
            response = _post(client, "query", [("o157", O157_SKETCH.read_bytes())])
        assert response.status_code == 500
        assert response.json() == {"error": "profiling failed"}
        assert "secret internal detail" in caplog.text

    def test_semaphore_released_after_error(self, client: TestClient) -> None:
        app = create_app(
            ServerConfig(db_path=ECOLI_TWO_STAGE, max_jobs=1),
            database=client.app.state.database,
        )
        with TestClient(app) as test_client:
            bad = _post(test_client, "query", [("bad", b"not a sketch")])
            assert bad.status_code == 400
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    _post,
                    test_client,
                    "query",
                    [("o157", O157_SKETCH.read_bytes())],
                )
                good = future.result(timeout=10)
            assert good.status_code == 200
