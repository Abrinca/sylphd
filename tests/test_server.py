"""Tests for the Starlette application, in process with ``TestClient``."""

from __future__ import annotations

import json
from pathlib import Path

import dnaio
import pysylph
import pytest
from starlette.testclient import TestClient

from sylphd.metadata import METADATA_COLUMNS, build_metadata_db
from sylphd.protocol import ProfileParams, format_value
from sylphd.server import LoadedDatabase, ServerConfig, create_app

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


@pytest.fixture(scope="module")
def tiny_database(tmp_path_factory) -> Path:
    directory = tmp_path_factory.mktemp("tiny")
    contigs = []
    with dnaio.open(Path(__file__).parent / "data" / "clodf13.fna.gz") as reader:
        contigs.extend(record.sequence for record in reader)
    genome = pysylph.Sketcher(c=200, k=31).sketch_genome(
        "GCA_000000001.1_genomic.fna.gz", contigs
    )
    database = pysylph.Database([genome])
    path = directory / "tiny.syldb"
    database.dump(str(path))
    return path


@pytest.fixture(scope="module")
def tiny_metadata(tmp_path_factory, tiny_database) -> Path:
    directory = tmp_path_factory.mktemp("meta")
    source = directory / "tiny.tsv"
    header = ["accession", *METADATA_COLUMNS]
    values = {column: "none" for column in METADATA_COLUMNS}
    values.update(
        gtdb_taxonomy="d__Bacteria;p__Test;c__Test",
        genome_size="9957",
        gc_percentage="51.5",
        mimag_high_quality="t",
    )
    row = ["GCA_000000001.1", *[values[column] for column in METADATA_COLUMNS]]
    source.write_text("\t".join(header) + "\n" + "\t".join(row) + "\n")
    output = directory / "meta.sqlite"
    build_metadata_db([source], output, release=232)
    return output


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
