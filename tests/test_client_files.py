"""``SylphClient.profile_reads`` / ``query_reads`` / ``identify_assembly`` / ``classify_contigs``:
sketch a file here, send only the sketch, and get what the lower-level calls give."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest
from conftest import CLODF13_FASTA, CLODF13_PLAIN, ECOLI_TWO_STAGE, K12_R1, K12_R2, O157_SKETCH
from starlette.testclient import TestClient

from sylphd.client import ClientError, SylphClient
from sylphd.per_contig_client import PerContigOptions, PerContigRun
from sylphd.server import ServerConfig, create_app
from sylphd.sketching import InputError, SampleInput, sketch_assembly, sketch_sample


def _client(database, stack) -> SylphClient:
    test_client = stack.enter_context(TestClient(create_app(ServerConfig(db_path=database))))
    client = SylphClient(base_url="http://testserver")
    client._client = test_client  # a TestClient is an httpx.Client
    return client


@pytest.fixture
def reads_client(request):
    from contextlib import ExitStack

    with ExitStack() as stack:
        yield _client(ECOLI_TWO_STAGE, stack)


@pytest.fixture
def genome_client(request):
    from contextlib import ExitStack

    with ExitStack() as stack:
        yield _client(CLODF13_PLAIN, stack)


class TestSketchParameters:
    def test_c_and_k_come_from_the_server_and_info_is_fetched_once(self, reads_client) -> None:
        assert reads_client.sketch_parameters() == (200, 31)
        assert reads_client.database_info() is reads_client.database_info()


class TestReads:
    def test_a_pair_is_named_without_r1_and_matches_the_low_level_call(self, reads_client) -> None:
        result = reads_client.profile_reads((K12_R1, K12_R2))
        assert (result["name"], result["kind"]) == ("k12", "profile")
        assert result["rows"]
        c, k = reads_client.sketch_parameters()
        sketch = sketch_sample(SampleInput("k12", ((str(K12_R1), str(K12_R2)),)), c=c, k=k)
        assert result == reads_client.profile([("k12", sketch.to_bytes())])[0]

    def test_name_override_single_file_sketch_file_and_sample_input(self, reads_client) -> None:
        assert reads_client.profile_reads(K12_R1, name="mine")["name"] == "mine"
        assert reads_client.profile_reads(K12_R1)["name"] == "k12.R1"
        assert reads_client.profile_reads(O157_SKETCH)["name"] == "o157"  # a ready .sylsp
        sample = SampleInput("given", ((str(K12_R1), str(K12_R2)),))
        assert reads_client.profile_reads(sample)["name"] == "given"
        assert reads_client.profile_reads(sample, name="renamed")["name"] == "renamed"
        assert reads_client.profile_reads(list((K12_R1, K12_R2)))["name"] == "k12"  # a list pair

    def test_query_reads_asks_for_ani_hits(self, reads_client) -> None:
        result = reads_client.query_reads((K12_R1, K12_R2), name="k12")
        assert result["kind"] == "query" and result["name"] == "k12"

    def test_bad_input_is_a_clear_error(self, reads_client, tmp_path) -> None:
        with pytest.raises(TypeError, match="path, an .r1, r2. pair"):
            reads_client.profile_reads(42)
        with pytest.raises(TypeError):
            reads_client.profile_reads((K12_R1, K12_R2, K12_R1))
        with pytest.raises(InputError):
            reads_client.profile_reads(tmp_path / "missing.fq")


class TestAssembly:
    def test_matches_the_sketch_upload_and_names_the_assembly(self, genome_client) -> None:
        result = genome_client.identify_assembly(CLODF13_FASTA)
        assert result["query"]["name"] == "clodf13" and result["query"]["records"] == 1
        assert result["hits"] and result["hits"][0]["ani"] == pytest.approx(100.0)
        c, k = genome_client.sketch_parameters()
        sketch, stats = sketch_assembly(CLODF13_FASTA, "clodf13", c=c, k=k)
        same = genome_client.assembly_sketch(
            sketch.to_bytes(), name="clodf13",
            records=stats.records, length=stats.length, gc=stats.gc,
        )
        assert {**result, "elapsed_ms": 0} == {**same, "elapsed_ms": 0}  # only the timing differs

    def test_name_top_and_min_ani(self, genome_client) -> None:
        assert genome_client.identify_assembly(CLODF13_FASTA, name="x")["query"]["name"] == "x"
        assert genome_client.identify_assembly(CLODF13_FASTA, min_ani=70)["hits"]
        with pytest.raises(ClientError, match="min_ani"):
            genome_client.identify_assembly(CLODF13_FASTA, min_ani=100.1)
        assert len(genome_client.identify_assembly(CLODF13_FASTA, top=1)["hits"]) == 1


class TestContigs:
    def test_returns_the_run_with_its_records(self, genome_client) -> None:
        run = genome_client.classify_contigs(CLODF13_FASTA, min_length=1000)
        assert isinstance(run, PerContigRun) and len(run.records) == 1
        assert run.options.min_length == 1000
        assert run.summary()["contigs"] == 1

    def test_options_object_and_overrides(self, genome_client) -> None:
        run = genome_client.classify_contigs(
            CLODF13_FASTA, options=PerContigOptions(top=3), min_length=500
        )
        assert (run.options.top, run.options.min_length) == (3, 500)
        skipped = genome_client.classify_contigs(CLODF13_FASTA, min_length=10**6)
        assert skipped.records == [] and skipped.skipped_short.contigs == 1

    def test_unknown_option_is_an_error(self, genome_client) -> None:
        with pytest.raises(TypeError):
            genome_client.classify_contigs(CLODF13_FASTA, nonsense=1)


def test_a_sketch_over_the_upload_limit_is_refused_before_sending(reads_client) -> None:
    reads_client._info = {"database": {"c": 200, "k": 31}, "limits": {"max_upload_mb": 1}}
    with pytest.raises(ClientError, match="upload limit"):
        reads_client._check_upload(b"x" * (2 << 20))
    reads_client._check_upload(b"x" * 1024)


def test_without_pysylph_the_error_names_the_extra() -> None:
    code = textwrap.dedent(
        """
        import sys
        for name in ("pysylph", "dnaio"):
            sys.modules[name] = None
        from sylphd.client import SylphClient
        client = SylphClient(base_url="https://example.org")
        for call in (lambda: client.identify_assembly("x.fa"), lambda: client.profile_reads("x.fq"),
                     lambda: client.classify_contigs("x.fa")):
            try:
                call()
            except ImportError as error:
                assert "sylphd[client]" in str(error), error
            else:
                raise SystemExit("no ImportError")
        print("ok")
        """
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert result.stdout.strip() == "ok", result.stderr
