"""Several databases in one server (phase 1 of the multi-database design).

The pooled server must give exactly what ``pysylph.Profiler(*databases)`` gives
directly, the single-database JSON must keep its shape (goldens), and the
``Profiler`` cache must be shared between requests and threads.
"""

from __future__ import annotations

import json
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import Self

import dnaio
import pysylph
import pytest
import web_golden
from conftest import (
    CLODF13_FASTA,
    CLODF13_PLAIN,
    ECOLI_PLAIN,
    ECOLI_TWO_STAGE,
    K12_PAIRED_SKETCH,
    K12_R1,
    MIX_SKETCH,
)
from starlette.testclient import TestClient

from sylphd import cli
from sylphd.metadata import build_metadata_db
from sylphd.profiling import DatabaseSet, DatabaseSpec, _bounded_duplicate_summary
from sylphd.protocol import ProfileParams
from sylphd.server import SKETCH_CONTENT_TYPE, ServerConfig, create_app


def _post(client: TestClient, endpoint: str, payloads: list[tuple[str, bytes]], **params):
    body = {"params": json.dumps(params)}
    files = [("sketch", (f"{name}.sylsp", data)) for name, data in payloads]
    return client.post(f"/{endpoint}", data=body, files=files)


@lru_cache(maxsize=1)
def _mix() -> object:
    return pysylph.SampleSketch.load(str(MIX_SKETCH))


@lru_cache(maxsize=1)
def _k12_reads() -> list[str]:
    with dnaio.open(K12_R1) as reader:
        return [record.sequence for record in reader]


@lru_cache(maxsize=8)
def _loaded(path: Path) -> DatabaseSet:
    """A one-database :class:`DatabaseSet` around ``path``."""
    return DatabaseSet.load([DatabaseSpec(path)])


def _profiler(paths: tuple[Path, ...], **kwargs) -> pysylph.Profiler:
    return pysylph.Profiler(
        *(_loaded(path).databases[0].db for path in paths), **kwargs
    )


def _result_fields(result, mode: str) -> list[dict[str, object]]:
    """The sylph columns of a pysylph result, as the wire protocol reports them."""
    rows: list[dict[str, object]] = []
    for item in result:
        row: dict[str, object] = {
            "Genome_file": item.genome_sketch.name,
            "Adjusted_ANI": float(item.ani),
            "Containment_ind": [int(value) for value in item.containment],
        }
        if mode == "profile":
            row["Taxonomic_abundance"] = float(item.taxonomic_abundance)
            row["Sequence_abundance"] = float(item.sequence_abundance)
            row["kmers_reassigned"] = int(item.kmers_reassigned)
        rows.append(row)
    return rows


def _wire_fields(rows: list[dict[str, object]], mode: str) -> list[dict[str, object]]:
    """The same columns out of a server response."""
    projected: list[dict[str, object]] = []
    for row in rows:
        item: dict[str, object] = {
            "Genome_file": row["Genome_file"],
            "Adjusted_ANI": row["Adjusted_ANI"],
            "Containment_ind": row["Containment_ind"],
        }
        if mode == "profile":
            item["Taxonomic_abundance"] = row["Taxonomic_abundance"]
            item["Sequence_abundance"] = row["Sequence_abundance"]
            item["kmers_reassigned"] = row["kmers_reassigned"]
        projected.append(item)
    return projected


def _app(specs, **config_kwargs) -> TestClient:
    return TestClient(create_app(ServerConfig(db_paths=tuple(specs), **config_kwargs)))


class TestDatabaseSpec:
    def test_plain_path(self) -> None:
        spec = DatabaseSpec.parse("db/gtdb.syl2db")
        assert spec.path == Path("db/gtdb.syl2db")
        assert spec.name is None
        assert spec.display_name == "gtdb.syl2db"

    def test_alias(self) -> None:
        spec = DatabaseSpec.parse("uhgv=db/uhgv.syldb")
        assert spec.path == Path("db/uhgv.syldb")
        assert spec.name == "uhgv"
        assert spec.display_name == "uhgv"

    def test_path_with_equals_is_not_an_alias(self) -> None:
        spec = DatabaseSpec.parse("/tmp/x=y/gtdb.syldb")
        assert spec.path == Path("/tmp/x=y/gtdb.syldb")
        assert spec.name is None

    def test_alias_may_contain_equals(self) -> None:
        spec = DatabaseSpec.parse("uhgv=a=b.syldb")
        assert spec.name == "uhgv"
        assert spec.path == Path("a=b.syldb")

    def test_empty_path(self) -> None:
        with pytest.raises(ValueError, match="missing path"):
            DatabaseSpec.parse("uhgv=")

    def test_duplicate_alias(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="both named 'uhgv'"):
            cli.parse_db_specs([f"uhgv={tmp_path / 'a.syldb'}", f"uhgv={tmp_path / 'b.syldb'}"])

    def test_duplicate_default_name(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="both named 'a.syldb'"):
            cli.parse_db_specs([str(tmp_path / "a.syldb"), str(tmp_path / "x" / "a.syldb")])

    def test_serve_rejects_duplicate_names(self, tmp_path: Path, capsys) -> None:
        code = cli.main(
            [
                "serve",
                "--db",
                f"x={tmp_path / 'a.syldb'}",
                "--db",
                f"x={tmp_path / 'b.syldb'}",
                "--socket",
                str(tmp_path / "s.sock"),
            ]
        )
        assert code == 2
        assert "both named 'x'" in capsys.readouterr().err


class TestPooledMatchesPysylph:
    """The server must give what ``Profiler(*databases)`` gives directly."""

    @pytest.mark.parametrize(
        "paths",
        [
            (ECOLI_TWO_STAGE, CLODF13_PLAIN),
            (ECOLI_PLAIN, CLODF13_PLAIN),
            (ECOLI_TWO_STAGE, ECOLI_PLAIN, CLODF13_PLAIN),
        ],
    )
    @pytest.mark.parametrize("mode", ["profile", "query"])
    def test_same_rows(self, paths: tuple[Path, ...], mode: str) -> None:
        specs = [DatabaseSpec(path) for path in paths]
        with warnings.catch_warnings():
            # The ecoli .syldb and .syl2db hold the same genomes: pysylph warns.
            warnings.simplefilter("ignore", UserWarning)
            profiler = _profiler(paths)
            result = profiler.profile(_mix()) if mode == "profile" else profiler.query(_mix())
            expected = _result_fields(result, mode)
            with _app(specs) as client:
                response = _post(client, mode, [("mix", MIX_SKETCH.read_bytes())], mode=mode)
        assert response.status_code == 200, response.text
        assert _wire_fields(response.json()[0]["rows"], mode) == expected

    def test_pooled_info(self) -> None:
        with _app(
            [
                DatabaseSpec(ECOLI_TWO_STAGE, "gtdb"),
                DatabaseSpec(CLODF13_PLAIN, "clodf13"),
            ]
        ) as client:
            info = client.get("/info").json()
        pooled = info["database"]
        assert pooled["c"] == 200
        assert pooled["k"] == 31
        assert pooled["genomes"] == 4
        assert pooled["two_stage"] is False
        assert pooled["screen_c"] is None
        assert pooled["name"] == "gtdb, clodf13"
        assert pooled["sha256"] is None
        assert "file" not in pooled

        entries = info["databases"]
        assert [entry["name"] for entry in entries] == ["gtdb", "clodf13"]
        assert [entry["file"] for entry in entries] == ["ecoli.syl2db", "clodf13.syldb"]
        assert [entry["genomes"] for entry in entries] == [3, 1]
        assert entries[0]["screen_c"] == 3000
        assert entries[0]["two_stage"] is True
        assert entries[1]["two_stage"] is False
        assert [entry["kind"] for entry in entries] == ["two-stage", "plain"]
        # no metadata file: no database can be shown as GTDB's
        assert [entry["gtdb"] for entry in entries] == [False, False]

    def test_gtdb_flag_follows_the_genome_names(
        self, tiny_database: Path, tiny_metadata: Path, tmp_path: Path
    ) -> None:
        """A fungi/UHGV database with GTDB metadata is not labelled "GTDB r232"."""
        with dnaio.open(CLODF13_FASTA) as reader:
            sequences = [record.sequence for record in reader]
        # UHGV stores every genome in the same file name, so no GTDB accession
        genome = pysylph.Sketcher(c=200, k=31).sketch_genome(
            "votus_full.fna.gz", sequences
        )
        other = tmp_path / "uhgv.syldb"
        pysylph.Database([genome]).dump(str(other))
        with _app([DatabaseSpec(tiny_database, "tiny"), DatabaseSpec(other, "uhgv")], meta_path=tiny_metadata) as client:
            entries = client.get("/info").json()["databases"]
        assert [(entry["name"], entry["gtdb"]) for entry in entries] == [
            ("tiny", True),
            ("uhgv", False),
        ]

    def test_single_database_entries_match_database(self) -> None:
        with _app([DatabaseSpec(ECOLI_TWO_STAGE, "gtdb")]) as client:
            info = client.get("/info").json()
        (entry,) = info["databases"]
        assert entry["name"] == "gtdb"
        assert entry["file"] == "ecoli.syl2db"
        assert entry["path"] == info["database"]["path"]
        assert {key: value for key, value in entry.items() if key not in {"file", "kind", "gtdb"}} == info["database"]
        assert entry["kind"] == "two-stage"
        assert entry["gtdb"] is False  # this server has no metadata file

    def test_assembly_uses_the_pooled_view(self) -> None:
        with _app([DatabaseSpec(ECOLI_TWO_STAGE), DatabaseSpec(CLODF13_PLAIN)]) as client:
            response = client.post(
                "/assembly",
                params={"top": 5},
                content=K12_PAIRED_SKETCH.read_bytes(),
                headers={"Content-Type": SKETCH_CONTENT_TYPE},
            )
        assert response.status_code == 200, response.text
        database = response.json()["database"]
        assert database == {
            "name": "ecoli.syl2db, clodf13.syldb",
            "genomes": 4,
            "c": 200,
            "k": 31,
        }

    def test_non_gtdb_hits_get_no_metadata(self, tmp_path: Path, data_dir: Path) -> None:
        """Genome names without a GCA/GCF accession must not break the join."""
        metadata = _tiny_metadata(tmp_path)
        with _app(
            [DatabaseSpec(ECOLI_TWO_STAGE), DatabaseSpec(CLODF13_PLAIN)],
            meta_path=metadata,
        ) as client:
            response = _post(client, "profile", [("mix", MIX_SKETCH.read_bytes())])
        assert response.status_code == 200, response.text
        rows = {row["Genome_file"]: row for row in response.json()[0]["rows"]}
        clodf13 = rows["src/pysylph/tests/clodf13.fna.gz"]
        assert clodf13["accession"] is None
        assert clodf13["gtdb_taxonomy"] is None
        ecoli = rows["test_files/e.coli-K12.fasta.gz"]
        assert ecoli["accession"] is None  # the test metadata knows another genome


def _plain_database(path: Path, specifications: list[tuple[int, int, str]]) -> Path:
    """Build a plain ``.syldb`` from ``(c, k, genome name)`` triples."""
    with dnaio.open(CLODF13_FASTA) as reader:
        sequences = [record.sequence for record in reader]
    genomes = [
        pysylph.Sketcher(c=c, k=k).sketch_genome(name, sequences)
        for c, k, name in specifications
    ]
    pysylph.Database(genomes).dump(str(path))
    return path


class TestPlainDatabaseParameters:
    """Genomes of one plain database may differ in ``c``, never in ``k``."""

    def test_c_is_the_minimum_of_its_genomes(self, tmp_path: Path) -> None:
        path = _plain_database(
            tmp_path / "mixed-c.syldb",
            [(200, 31, "GCA_000000001.1_genomic.fna.gz"), (100, 31, "GCA_000000002.1_genomic.fna.gz")],
        )
        databases = DatabaseSet.load([DatabaseSpec(path, "mixed")])
        assert databases.c == 100
        assert databases.info()["c"] == 100
        assert databases.entries()[0]["c"] == 100
        # next to a c=200 database the pooled c is still the minimum
        pooled = DatabaseSet.load([DatabaseSpec(path, "mixed"), DatabaseSpec(ECOLI_TWO_STAGE)])
        assert pooled.c == 100

    def test_mixed_c_rejects_a_coarser_sketch(self, tmp_path: Path) -> None:
        path = _plain_database(
            tmp_path / "mixed-c.syldb",
            [(200, 31, "GCA_000000001.1_genomic.fna.gz"), (100, 31, "GCA_000000002.1_genomic.fna.gz")],
        )
        # Two databases, so the 400 has to name the limiting one.
        with _app([DatabaseSpec(path, "mixed"), DatabaseSpec(ECOLI_TWO_STAGE, "gtdb")]) as client:
            assert client.get("/info").json()["database"]["c"] == 100
            response = _post(
                client, "profile", [("k12", K12_PAIRED_SKETCH.read_bytes())]
            )
        assert response.status_code == 400
        error = response.json()["error"]
        assert "sketch c=200" in error
        assert "c=100" in error
        assert "mixed" in error

    def test_sparser_sketch_at_the_minimum_is_accepted(self, tmp_path: Path) -> None:
        path = _plain_database(
            tmp_path / "mixed-c.syldb",
            [(200, 31, "GCA_000000001.1_genomic.fna.gz"), (100, 31, "GCA_000000002.1_genomic.fna.gz")],
        )
        sketch = pysylph.Sketcher(c=100, k=31).sketch_single(
            "sparse", _k12_reads()
        )
        with _app([DatabaseSpec(path, "mixed")]) as client:
            response = _post(client, "query", [("sparse", sketch.to_bytes())])
        assert response.status_code == 200, response.text

    def test_mixed_k_is_refused(self, tmp_path: Path) -> None:
        path = _plain_database(
            tmp_path / "mixed-k.syldb",
            [(200, 31, "GCA_000000001.1_genomic.fna.gz"), (200, 21, "GCA_000000002.1_genomic.fna.gz")],
        )
        # Even as the only database: the check is inside the database.
        with pytest.raises(ValueError, match=r"mixed-k.*different k \(31 for .* and 21 for"):
            DatabaseSet.load([DatabaseSpec(path, "mixed-k")])


def _tiny_metadata(directory: Path) -> Path:
    """A metadata database with a single synthetic accession."""
    from sylphd.metadata import METADATA_COLUMNS

    source = directory / "tiny.tsv"
    values = {column: "none" for column in METADATA_COLUMNS}
    values["gtdb_taxonomy"] = "d__Bacteria;p__Test;c__Test"
    source.write_text(
        "\t".join(["accession", *METADATA_COLUMNS])
        + "\n"
        + "\t".join(["GCA_000000001.1", *[values[column] for column in METADATA_COLUMNS]])
        + "\n"
    )
    output = directory / "meta.sqlite"
    build_metadata_db([source], output, release=232)
    return output


class _FakeClient:
    """A stand-in for ``SylphClient`` with a canned ``/info`` document."""

    def __init__(self, info: dict[str, object]) -> None:
        self._info = info

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def info(self) -> dict[str, object]:
        return self._info


def _info_document(databases: list[dict[str, object]]) -> dict[str, object]:
    return {
        "database": {
            "name": ", ".join(str(entry["name"]) for entry in databases),
            "path": "<path>",
            "size": 1,
            "sha256": None,
            "genomes": sum(int(entry["genomes"]) for entry in databases),
            "c": 200,
            "k": 31,
            "screen_c": None,
            "two_stage": False,
        },
        "databases": databases,
        "metadata": None,
        "limits": {
            "max_jobs": 2,
            "max_upload_mb": 512,
            "max_sketches": 4096,
            "allow_sequences": False,
            "max_sequence_mb": None,
            "rate_limit": None,
            "max_queue": None,
        },
        "version": "0.0.0",
        "pysylph_version": "0.0.0",
        "python_version": "3.14.0",
    }


class TestInfoCommand:
    def test_lists_the_databases(self, monkeypatch, capsys) -> None:
        entries = [
            {"name": "gtdb", "file": "ecoli.syl2db", "genomes": 3, "c": 200, "k": 31, "two_stage": True},
            {"name": "clodf13", "file": "clodf13.syldb", "genomes": 1, "c": 200, "k": 31, "two_stage": False},
        ]
        monkeypatch.setattr(
            cli, "_client_from_args", lambda args: _FakeClient(_info_document(entries))
        )
        assert cli.main(["info"]) == 0
        out = capsys.readouterr().out
        assert "databases: 2" in out
        assert "  gtdb: ecoli.syl2db (two-stage, c=200, k=31, 3 genomes)" in out
        assert "  clodf13: clodf13.syldb (plain, c=200, k=31, 1 genomes)" in out

    def test_single_database_output_is_unchanged(self, monkeypatch, capsys) -> None:
        entries = [
            {"name": "ecoli.syl2db", "file": "ecoli.syl2db", "genomes": 3, "c": 200, "k": 31, "two_stage": True}
        ]
        document = _info_document(entries)
        document["database"]["name"] = "ecoli.syl2db"  # type: ignore[index]
        monkeypatch.setattr(cli, "_client_from_args", lambda args: _FakeClient(document))
        assert cli.main(["info"]) == 0
        out = capsys.readouterr().out
        assert "databases:" not in out
        assert "database: ecoli.syl2db" in out


class TestServerConfig:
    """The historical positional arguments keep working next to ``db_paths``."""

    def test_positional_arguments(self, tmp_path: Path) -> None:
        meta = tmp_path / "meta.sqlite"
        config = ServerConfig(ECOLI_TWO_STAGE, meta, 4)
        assert config.db_path == ECOLI_TWO_STAGE
        assert config.meta_path == meta
        assert config.max_jobs == 4
        assert config.db_paths == (DatabaseSpec(ECOLI_TWO_STAGE),)

    def test_db_paths_is_keyword_only(self) -> None:
        config = ServerConfig(db_paths=(DatabaseSpec(ECOLI_PLAIN, "p"),))
        assert config.db_path == ECOLI_PLAIN
        assert config.db_paths[0].name == "p"

    def test_both_spellings_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="not both"):
            ServerConfig(
                db_path=ECOLI_TWO_STAGE, db_paths=(DatabaseSpec(ECOLI_PLAIN),)
            )

    def test_a_database_is_required(self) -> None:
        with pytest.raises(ValueError, match="at least one database"):
            ServerConfig()


class TestStartupChecks:
    def test_k_mismatch_refuses_to_start(self, tmp_path: Path) -> None:
        with dnaio.open(CLODF13_FASTA) as reader:
            sequences = [record.sequence for record in reader]
        genome = pysylph.Sketcher(c=200, k=21).sketch_genome("GCA_000000001.1_genomic.fna.gz", sequences)
        path = tmp_path / "k21.syldb"
        pysylph.Database([genome]).dump(str(path))

        with pytest.raises(ValueError, match=r"different k.*gtdb: k=31.*k21: k=21"):
            DatabaseSet.load([DatabaseSpec(ECOLI_TWO_STAGE, "gtdb"), DatabaseSpec(path, "k21")])

    def test_k_mismatch_is_a_cli_error(self, tmp_path: Path, capsys) -> None:
        with dnaio.open(CLODF13_FASTA) as reader:
            sequences = [record.sequence for record in reader]
        genome = pysylph.Sketcher(c=200, k=21).sketch_genome("GCA_000000001.1_genomic.fna.gz", sequences)
        path = tmp_path / "k21.syldb"
        pysylph.Database([genome]).dump(str(path))

        code = cli.main(
            [
                "serve",
                "--db",
                str(ECOLI_TWO_STAGE),
                "--db",
                str(path),
                "--socket",
                str(tmp_path / "s.sock"),
            ]
        )
        assert code == 1
        error = capsys.readouterr().err
        assert "different k" in error
        assert "k=31" in error and "k=21" in error

    def test_pooled_c_is_the_minimum(self, tmp_path: Path) -> None:
        with dnaio.open(CLODF13_FASTA) as reader:
            sequences = [record.sequence for record in reader]
        genome = pysylph.Sketcher(c=100, k=31).sketch_genome("GCA_000000001.1_genomic.fna.gz", sequences)
        path = tmp_path / "c100.syldb"
        pysylph.Database([genome]).dump(str(path))

        databases = DatabaseSet.load([DatabaseSpec(ECOLI_TWO_STAGE), DatabaseSpec(path, "sparse")])
        assert databases.c == 100
        assert databases.info()["c"] == 100


@pytest.fixture(scope="module")
def sparse_client(tmp_path_factory):
    """A server whose smallest c is 100, next to the c=200 ecoli database."""
    with dnaio.open(CLODF13_FASTA) as reader:
        sequences = [record.sequence for record in reader]
    genome = pysylph.Sketcher(c=100, k=31).sketch_genome(
        "GCA_000000001.1_genomic.fna.gz", sequences
    )
    directory = tmp_path_factory.mktemp("sparse")
    path = directory / "c100.syldb"
    pysylph.Database([genome]).dump(str(path))
    with _app([DatabaseSpec(path, "sparse"), DatabaseSpec(ECOLI_TWO_STAGE, "gtdb")]) as client:
        yield client


class TestSketchCompatibility:

    def test_c_above_the_minimum_names_the_database(self, sparse_client) -> None:
        response = _post(
            sparse_client, "profile", [("k12", K12_PAIRED_SKETCH.read_bytes())]
        )
        assert response.status_code == 400
        error = response.json()["error"]
        assert "sketch c=200" in error
        assert "c=100" in error
        assert "sparse" in error
        assert "gtdb" not in error

    def test_wrong_k_names_the_databases(self) -> None:
        # ecoli.syl2db is k=31; a k=21 sketch with plausible c is rejected
        # before decoding, and the error lists the databases.
        with dnaio.open(CLODF13_FASTA) as reader:
            sequences = [record.sequence for record in reader]
        sketch = pysylph.Sketcher(c=200, k=21).sketch_single("k21-sample", sequences)
        with _app(
            [DatabaseSpec(ECOLI_TWO_STAGE, "gtdb"), DatabaseSpec(CLODF13_PLAIN, "clodf13")]
        ) as client:
            response = _post(client, "query", [("k21", sketch.to_bytes())])
        assert response.status_code == 400
        error = response.json()["error"]
        assert "sketch k=21" in error
        assert "gtdb: k=31" in error and "clodf13: k=31" in error


class TestSingleDatabaseGoldens:
    """The single-database JSON keeps the shape the web app reads."""

    def test_info(self) -> None:
        with _app([DatabaseSpec(ECOLI_TWO_STAGE)]) as client:
            info = client.get("/info").json()
        assert "databases" in info
        info.pop("databases")
        # Additive since the golden was captured: ``/contigs`` is one more
        # POST endpoint and ``max_params_bytes``/``max_contigs`` two more
        # limits.  The web app reads none of them, so the shape it depends on
        # is unchanged.
        info["endpoints"].remove("/contigs")
        info["limits"].pop("max_params_bytes")
        info["limits"].pop("max_contigs")
        assert web_golden.normalise(info) == web_golden.load("info")

    def test_assembly(self) -> None:
        with _app([DatabaseSpec(ECOLI_TWO_STAGE)]) as client:
            response = client.post(
                "/assembly",
                params={"top": 10, "min_ani": 80.0, "name": "golden"},
                content=K12_PAIRED_SKETCH.read_bytes(),
                headers={"Content-Type": SKETCH_CONTENT_TYPE},
            )
        assert response.status_code == 200, response.text
        assert web_golden.normalise(response.json()) == web_golden.load("assembly")


class TestProfilerCache:
    def test_second_request_reuses_the_profiler(self) -> None:
        app = create_app(ServerConfig(db_paths=(DatabaseSpec(ECOLI_TWO_STAGE),)))
        databases = app.state.database
        assert databases.profiler_builds == 0
        with TestClient(app) as client:
            first = _post(client, "profile", [("mix", MIX_SKETCH.read_bytes())])
            second = _post(client, "profile", [("mix", MIX_SKETCH.read_bytes())])
        assert first.status_code == second.status_code == 200
        assert databases.profiler_builds == 1

    def test_profiler_objects_are_shared(self) -> None:
        databases = _loaded(ECOLI_TWO_STAGE)
        profiler = databases.profiler(ProfileParams())
        assert databases.profiler(ProfileParams()) is profiler
        assert databases.profiler(ProfileParams(minimum_ani=90.0)) is not profiler
        assert databases.profiler_builds == 2

    def test_cache_is_bounded(self) -> None:
        databases = DatabaseSet.load([DatabaseSpec(ECOLI_TWO_STAGE)], cache_size=2)
        for minimum_ani in (80.0, 85.0, 90.0):
            databases.profiler(ProfileParams(minimum_ani=minimum_ani))
        assert databases.profiler_builds == 3
        assert len(databases._profilers) == 2
        # the oldest key was evicted and is rebuilt
        databases.profiler(ProfileParams(minimum_ani=80.0))
        assert databases.profiler_builds == 4

    def test_duplicate_warning_is_logged_once(self, caplog) -> None:
        """pysylph warns at every construction; the server logs it once."""
        databases = DatabaseSet.load(
            [DatabaseSpec(ECOLI_TWO_STAGE), DatabaseSpec(ECOLI_PLAIN)]
        )
        with caplog.at_level("WARNING", logger="sylphd"):
            databases.profiler(ProfileParams())
            databases.profiler(ProfileParams(minimum_ani=90.0))
        messages = [
            record.getMessage()
            for record in caplog.records
            if "appear more than once" in record.getMessage()
        ]
        assert len(messages) == 1
        assert "3 genome(s)" in messages[0]
        assert "test_files/e.coli" in messages[0]
        assert len(messages[0]) < 300

    def test_bounded_duplicate_summary(self) -> None:
        message = (
            "250 genome(s) appear more than once across the databases (same file "
            'name and contig name): [("a", "b"), ("c", "d")]. This can cause '
            "inconsistent reassignment results; consider removing the duplicates "
            "from one of the databases."
        )
        summary = _bounded_duplicate_summary(message, limit=80)
        assert summary.startswith("250 genome(s) appear more than once")
        assert len(summary) <= 80

    def test_profiler_is_shared_across_threads(self) -> None:
        """pysylph allows concurrent ``query``/``profile`` on one Profiler."""
        databases = DatabaseSet.load(
            [DatabaseSpec(ECOLI_TWO_STAGE), DatabaseSpec(CLODF13_PLAIN)]
        )
        profiler = databases.profiler(ProfileParams())
        sample = _mix()
        expected = {
            "profile": _result_fields(profiler.profile(sample), "profile"),
            "query": _result_fields(profiler.query(sample), "query"),
        }
        barrier = threading.Barrier(8)
        failures: list[str] = []

        def worker(index: int) -> None:
            mode = "profile" if index % 2 else "query"
            barrier.wait()
            for _ in range(5):
                got = _result_fields(
                    profiler.profile(sample) if mode == "profile" else profiler.query(sample),
                    mode,
                )
                if got != expected[mode]:
                    failures.append(f"thread {index}: {mode} differs")

        with ThreadPoolExecutor(8) as pool:
            list(pool.map(worker, range(8)))
        assert failures == []
