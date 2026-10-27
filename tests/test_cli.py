"""CLI tests, including one real Unix-socket round trip against a subprocess."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pysylph
import pytest

from sylphd import cli

from conftest import (
    ECOLI_TWO_STAGE,
    K12_PAIRED_SKETCH,
    K12_R1,
    K12_R2,
    O157_SKETCH,
)


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "sylphd", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=600,
    )


def _wait_for_socket(path: Path, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_socket():
            return
        time.sleep(0.1)
    raise AssertionError(f"server did not create {path} in time")


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    directory = tmp_path_factory.mktemp("server")
    socket_path = directory / "sylphd.sock"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "sylphd",
            "serve",
            "--db",
            str(ECOLI_TWO_STAGE),
            "--socket",
            str(socket_path),
            "--log-level",
            "warning",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_socket(socket_path)
        yield socket_path, process
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


class TestUnixSocketRoundTrip:
    def test_info(self, server) -> None:
        socket_path, _ = server
        result = _run("info", "--socket", str(socket_path), "--json")
        info = json.loads(result.stdout)
        assert info["database"]["c"] == 200
        assert info["database"]["k"] == 31

    def test_profile_matches_pysylph(self, server) -> None:
        socket_path, _ = server
        result = _run(
            "profile", str(O157_SKETCH), "--socket", str(socket_path), "--fields", "none"
        )
        lines = [line for line in result.stdout.splitlines() if line]
        header = lines[0].split("\t")
        assert "Taxonomic_abundance" in header
        assert "accession" in header
        assert len(lines) == 2
        row = dict(zip(header, lines[1].split("\t")))

        database = pysylph.TwoStageDatabase.load(str(ECOLI_TWO_STAGE))
        sample = pysylph.SampleSketch.load(str(O157_SKETCH))
        direct = pysylph.Profiler(database).profile(sample)[0]

        assert row["Sample_file"] == "o157"
        assert row["Genome_file"] == direct.genome_sketch.name
        assert float(row["Adjusted_ANI"]) == pytest.approx(direct.ani, abs=0.005)
        assert float(row["Naive_ANI"]) == pytest.approx(direct.ani_naive, abs=0.005)
        assert float(row["Eff_cov"]) == pytest.approx(direct.coverage, abs=0.0005)
        assert row["Containment_ind"] == (
            f"{direct.containment[0]}/{direct.containment[1]}"
        )
        assert float(row["Taxonomic_abundance"]) == pytest.approx(
            direct.taxonomic_abundance, abs=0.0001
        )
        assert int(row["kmers_reassigned"]) == direct.kmers_reassigned
        assert row["Contig_name"] == direct.genome_sketch.description

    def test_query_paired_reads(self, server) -> None:
        socket_path, _ = server
        result = _run(
            "query",
            "--socket",
            str(socket_path),
            "-1",
            str(K12_R1),
            "-2",
            str(K12_R2),
            "--fields",
            "none",
        )
        lines = [line for line in result.stdout.splitlines() if line]
        header = lines[0].split("\t")
        assert "Taxonomic_abundance" not in header
        assert len(lines) >= 2
        names = {line.split("\t")[0] for line in lines[1:]}
        assert names == {"k12"}

    def test_multiple_samples(self, server) -> None:
        socket_path, _ = server
        result = _run(
            "query",
            str(O157_SKETCH),
            str(K12_PAIRED_SKETCH),
            "--socket",
            str(socket_path),
            "--fields",
            "none",
        )
        lines = [line for line in result.stdout.splitlines() if line]
        header = lines[0].split("\t")
        rows = [dict(zip(header, line.split("\t"))) for line in lines[1:]]
        assert {row["Sample_file"] for row in rows} == {"o157", "k12.paired"}

    def test_json_output(self, server) -> None:
        socket_path, _ = server
        result = _run(
            "query",
            str(O157_SKETCH),
            "--socket",
            str(socket_path),
            "--format",
            "json",
        )
        payload = json.loads(result.stdout)
        assert payload[0]["kind"] == "query"
        assert len(payload[0]["rows"]) == 3

    def test_graceful_shutdown_removes_socket(self, server) -> None:
        socket_path, process = server
        assert socket_path.is_socket()
        process.terminate()
        process.wait(timeout=20)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and socket_path.exists():
            time.sleep(0.1)
        assert not socket_path.exists()


class TestLocalCLI:
    def test_no_server(self, tmp_path: Path) -> None:
        socket_path = tmp_path / "missing.sock"
        result = _run(
            "info", "--socket", str(socket_path), check=False
        )
        assert result.returncode == 1
        assert "cannot talk to sylphd" in result.stderr

    def test_sample_name_requires_one_sample(self) -> None:
        result = _run(
            "query",
            str(O157_SKETCH),
            str(K12_PAIRED_SKETCH),
            "--sample-name",
            "x",
            "--socket",
            "/nonexistent.sock",
            check=False,
        )
        assert result.returncode == 2
        assert "--sample-name" in result.stderr

    def test_duplicate_sample_names(self) -> None:
        result = _run(
            "query",
            str(O157_SKETCH),
            str(O157_SKETCH),
            "--socket",
            "/nonexistent.sock",
            check=False,
        )
        assert result.returncode == 2
        assert "duplicate sample name" in result.stderr

    def test_setup_command(self, tmp_path: Path) -> None:
        from sylphd.metadata import METADATA_COLUMNS

        source = tmp_path / "meta.tsv"
        header = ["accession", *METADATA_COLUMNS]
        values = {column: "none" for column in METADATA_COLUMNS}
        values["genome_size"] = "42"
        source.write_text(
            "\t".join(header)
            + "\n"
            + "\t".join(["GCA_1", *[values[column] for column in METADATA_COLUMNS]])
            + "\n"
        )
        output = tmp_path / "meta.sqlite"
        assert cli.main(["setup", "-m", str(source), "-o", str(output), "-q"]) == 0
        assert output.is_file()

    def test_download_help(self) -> None:
        result = _run("download", "--help")
        assert "--release" in result.stdout
        assert "--only" in result.stdout

    def test_serve_requires_db(self) -> None:
        result = _run("serve", check=False)
        assert result.returncode == 2
        assert "--db" in result.stderr
