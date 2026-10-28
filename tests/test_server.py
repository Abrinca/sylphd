"""Tests for the Starlette application, in process with ``TestClient``."""

from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anyio
import dnaio
import httpx
import pysylph
import pytest
from starlette.testclient import TestClient

from sylphd import cli
from sylphd.metadata import METADATA_COLUMNS, build_metadata_db
from sylphd.protocol import ProfileParams, format_row, format_value
from sylphd.server import (
    LoadedDatabase,
    ServerConfig,
    _socket_identity,
    _unlink_socket_if_owned,
    bind_unix_socket,
    create_app,
    prepare_unix_socket,
)

from conftest import ECOLI_TWO_STAGE, K12_R1, MIX_SKETCH, O157_SKETCH


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


class TestTerms:
    def test_terms_url_is_reported(self) -> None:
        terms = "https://abrinca.github.io/sylphd/terms.html"
        app = create_app(ServerConfig(db_path=ECOLI_TWO_STAGE, terms_url=terms))
        with TestClient(app) as test_client:
            assert test_client.get("/info").json()["terms_url"] == terms

    def test_serve_accepts_terms_url(self) -> None:
        args = cli.build_parser().parse_args(
            [
                "serve",
                "--db",
                "db.syl2db",
                "--terms-url",
                "https://example.org/terms",
            ]
        )
        assert args.terms_url == "https://example.org/terms"


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
        assert info["terms_url"] is None
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

    def test_profile_estimate_unknown_header(self, client: TestClient) -> None:
        # estimate_unknown only changes the label in the TSV header; JSON keeps Eff_cov.
        response = _post(
            client,
            "profile",
            [("o157", O157_SKETCH.read_bytes())],
            names=["o157"],
            estimate_unknown=True,
        )
        assert response.status_code == 200
        assert "Eff_cov" in response.json()[0]["rows"][0]


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

    def test_invalid_sketch(self, client: TestClient) -> None:
        response = _post(client, "query", [("bad", b"not a sketch")])
        assert response.status_code == 400
        assert "not a valid sample sketch" in response.json()["error"]

    @pytest.mark.parametrize("count", [100_000_000, 2**63])
    def test_allocation_bomb_sketch_returns_400(
        self, client: TestClient, count: int
    ) -> None:
        # Eight bytes declaring ``count`` k-mers: sylph's deserializer would
        # reserve the memory up front and abort the process.  The validator
        # rejects it from the length prefix instead.
        response = _post(client, "query", [("bomb", struct.pack("<Q", count))])
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

        monkeypatch.setattr("sylphd.server._run_sample", boom)
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

    def test_old_format_metadata_table(
        self, tiny_database: Path, tiny_metadata: Path, tmp_path: Path
    ) -> None:
        # Format 1 files store everything in a plain ``metadata`` table; the
        # server must keep serving them without a rebuild.
        old_path = tmp_path / "old-meta.sqlite"
        connection = sqlite3.connect(old_path)
        try:
            connection.execute("ATTACH DATABASE ? AS built", (str(tiny_metadata),))
            connection.execute("CREATE TABLE metadata AS SELECT * FROM built.metadata")
            connection.execute(
                "CREATE TABLE info (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO info SELECT key, value FROM built.info")
            # format 1 has no ``reps_only`` key and recorded version 1
            connection.execute("DELETE FROM info WHERE key = 'reps_only'")
            connection.execute(
                "UPDATE info SET value = '1' WHERE key = 'format_version'"
            )
            connection.commit()
            connection.execute("DETACH DATABASE built")
        finally:
            connection.close()

        app = create_app(ServerConfig(db_path=tiny_database, meta_path=old_path))
        with TestClient(app) as joined:
            info = joined.get("/info").json()
            assert info["metadata"]["rows"] == 1
            assert info["metadata"]["format_version"] == "1"
            response = _post(
                joined, "query", [("mix", MIX_SKETCH.read_bytes())], names=["mix"]
            )
            assert response.status_code == 200
            row = response.json()[0]["rows"][0]
            assert row["gtdb_taxonomy"] == "d__Bacteria;p__Test;c__Test"
            assert row["genome_size"] == 9957
            assert row["ncbi_taxid"] is None


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

        monkeypatch.setattr("sylphd.server._run_sample", blocking_run_sample)
        app = create_app(
            ServerConfig(db_path=ECOLI_TWO_STAGE, max_jobs=1),
            database=client.app.state.database,
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
                health = test_client.get("/health")
                assert health.status_code == 200
                release.set()
                response = future.result(timeout=10)
                assert response.status_code == 200

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

    def test_semaphore_released_after_cancellation(
        self, client: TestClient, monkeypatch
    ) -> None:
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking_run_sample(*args, **kwargs):
            started.set()
            release.wait(timeout=10)
            finished.set()
            return {"name": "blocked", "kind": "query", "rows": []}

        monkeypatch.setattr("sylphd.server._run_sample", blocking_run_sample)
        app = create_app(
            ServerConfig(db_path=ECOLI_TWO_STAGE, max_jobs=1),
            database=client.app.state.database,
        )

        async def run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as ac:
                task = asyncio.create_task(
                    ac.post(
                        "/query",
                        data={"params": "{}"},
                        files=[
                            ("sketch", ("o157.sylsp", O157_SKETCH.read_bytes()))
                        ],
                    )
                )
                await anyio.to_thread.run_sync(started.wait, 5)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                release.set()
                await anyio.to_thread.run_sync(finished.wait, 5)
                response = await ac.post(
                    "/query",
                    data={"params": "{}"},
                    files=[("sketch", ("o157.sylsp", O157_SKETCH.read_bytes()))],
                )
                assert response.status_code == 200

        anyio.run(run)
