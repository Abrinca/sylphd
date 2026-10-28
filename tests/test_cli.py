"""CLI tests, including one real Unix-socket round trip against a subprocess."""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import dnaio
import pysylph
import pytest

from sylphd import cli
from sylphd.client import ClientError, connect
from sylphd.metadata import METADATA_COLUMNS, build_metadata_db

from conftest import (
    CLODF13_FASTA,
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


@pytest.fixture(scope="module")
def metadata_server(tmp_path_factory):
    directory = tmp_path_factory.mktemp("meta-server")
    source = directory / "meta.tsv"
    header = ["accession", *METADATA_COLUMNS]
    values = {column: "none" for column in METADATA_COLUMNS}
    values["genome_size"] = "42"
    source.write_text(
        "\t".join(header)
        + "\n"
        + "\t".join(["GCA_1", *[values[column] for column in METADATA_COLUMNS]])
        + "\n"
    )
    meta_path = directory / "meta.sqlite"
    build_metadata_db([source], meta_path, release=232)
    socket_path = directory / "sylphd.sock"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "sylphd",
            "serve",
            "--db",
            str(ECOLI_TWO_STAGE),
            "--meta",
            str(meta_path),
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


GTDB_TAXONOMY = {
    "GCA_111111111.1": (
        "d__Bacteria;p__Bacillota;c__Clostridia;o__Clostridiales;"
        "f__Clostridiaceae;g__Clostridium;s__Clostridium difficile"
    ),
    "GCA_222222222.1": (
        "d__Bacteria;p__Pseudomonadota;c__Gammaproteobacteria;"
        "o__Enterobacterales;f__Enterobacteriaceae;g__Escherichia;"
        "s__Escherichia coli"
    ),
}
NCBI_TAXONOMY = {
    # Internal gaps must be filled with "unclassified <ancestor>".
    "GCA_111111111.1": (
        "d__Bacteria;p__Bacillota;c__Clostridia;o__;f__;"
        "g__Candidatus Avispirillum;s__"
    ),
    "GCA_222222222.1": (
        "d__Bacteria;p__Pseudomonadota;c__Gammaproteobacteria;"
        "o__Enterobacterales;f__Enterobacteriaceae;g__Escherichia;"
        "s__Escherichia coli"
    ),
}


def _build_taxonomy_db(directory: Path) -> None:
    """A two-genome database, two sample sketches and a metadata SQLite."""
    with dnaio.open(CLODF13_FASTA) as handle:
        sequence = next(iter(handle)).sequence
    half = len(sequence) // 2
    sketcher = pysylph.Sketcher(c=200, k=31)
    genomes = [
        sketcher.sketch_genome(f"{accession}_genomic.fna.gz", [part])
        for accession, part in (
            ("GCA_111111111.1", sequence[:half]),
            ("GCA_222222222.1", sequence[half:]),
        )
    ]
    pysylph.Database(genomes).dump(str(directory / "taxonomy.syldb"))

    def sketch(name: str, sequence_part: str) -> None:
        reads = [
            sequence_part[start : start + 1000]
            for start in range(0, len(sequence_part) - 1000, 1000)
        ]
        sample = sketcher.sketch_single(name, reads)
        (directory / f"{name}.sylsp").write_bytes(sample.to_bytes())

    sketch("one", sequence[:half])
    sketch("two", sequence)

    header = ["accession", *METADATA_COLUMNS]
    lines = ["\t".join(header)]
    for accession in GTDB_TAXONOMY:
        values = {column: "none" for column in METADATA_COLUMNS}
        values.update(
            gtdb_taxonomy=GTDB_TAXONOMY[accession],
            ncbi_taxonomy=NCBI_TAXONOMY[accession],
            genome_size="42",
        )
        lines.append(
            "\t".join([accession, *[values[column] for column in METADATA_COLUMNS]])
        )
    source = directory / "taxonomy-meta.tsv"
    source.write_text("\n".join(lines) + "\n")
    build_metadata_db([source], directory / "taxonomy-meta.sqlite", release=232)


@pytest.fixture(scope="module")
def taxonomy_server(tmp_path_factory):
    directory = tmp_path_factory.mktemp("taxonomy-server")
    _build_taxonomy_db(directory)
    socket_path = directory / "sylphd.sock"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "sylphd",
            "serve",
            "--db",
            str(directory / "taxonomy.syldb"),
            "--meta",
            str(directory / "taxonomy-meta.sqlite"),
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
        yield socket_path, directory
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


class TestUnixSocketRoundTrip:
    def test_allocation_bomb_does_not_kill_the_server(self, server) -> None:
        """The real ``sylphd serve`` process must survive a malicious upload."""
        socket_path, process = server
        with connect(socket_path=socket_path) as client:
            with pytest.raises(ClientError) as caught:
                client.query([("bomb", struct.pack("<Q", 100_000_000))])
            assert caught.value.status_code == 400
            assert "declares" in str(caught.value)
            # Same process, same socket: still answering.
            assert client.health()["status"] == "ok"
            assert client.query([("o157", O157_SKETCH.read_bytes())])
        assert process.poll() is None

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

    def test_estimate_unknown_tsv_value(self, server) -> None:
        socket_path, _ = server
        result = _run(
            "profile",
            str(O157_SKETCH),
            "--socket",
            str(socket_path),
            "--estimate-unknown",
            "--fields",
            "none",
        )
        lines = [line for line in result.stdout.splitlines() if line]
        header = lines[0].split("\t")
        assert "True_cov" in header
        row = dict(zip(header, lines[1].split("\t")))
        assert row["True_cov"] != ""

    def test_graceful_shutdown_removes_socket(self, server) -> None:
        socket_path, process = server
        assert socket_path.is_socket()
        process.terminate()
        process.wait(timeout=20)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and socket_path.exists():
            time.sleep(0.1)
        assert not socket_path.exists()


class TestMetadataTSV:
    def test_default_includes_metadata_columns(self, metadata_server) -> None:
        socket_path, _ = metadata_server
        result = _run("query", str(O157_SKETCH), "--socket", str(socket_path))
        lines = [line for line in result.stdout.splitlines() if line]
        header = lines[0].split("\t")
        assert "gtdb_taxonomy" in header
        assert "genome_size" in header
        assert len(lines[1].split("\t")) == len(header)

    def test_fields_all_includes_metadata_columns(self, metadata_server) -> None:
        socket_path, _ = metadata_server
        result = _run(
            "query", str(O157_SKETCH), "--socket", str(socket_path), "--fields", "all"
        )
        lines = [line for line in result.stdout.splitlines() if line]
        header = lines[0].split("\t")
        assert "gtdb_taxonomy" in header
        assert "genome_size" in header

    def test_explicit_fields(self, metadata_server) -> None:
        socket_path, _ = metadata_server
        result = _run(
            "query",
            str(O157_SKETCH),
            "--socket",
            str(socket_path),
            "--fields",
            "gtdb_taxonomy,genome_size",
        )
        lines = [line for line in result.stdout.splitlines() if line]
        header = lines[0].split("\t")
        assert "gtdb_taxonomy" in header
        assert "genome_size" in header
        assert "ncbi_taxid" not in header


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

    def test_setup_reps_only(self, tmp_path: Path) -> None:
        from sylphd.metadata import METADATA_COLUMNS, MetadataDB

        def line(accession: str, representative: str) -> str:
            values = {column: "none" for column in METADATA_COLUMNS}
            values["gtdb_representative"] = representative
            return "\t".join([accession, *[values[column] for column in METADATA_COLUMNS]])

        source = tmp_path / "meta.tsv"
        source.write_text(
            "\t".join(["accession", *METADATA_COLUMNS])
            + "\n"
            + line("GCA_1", "t")
            + "\n"
            + line("GCA_2", "f")
            + "\n"
        )
        output = tmp_path / "reps.sqlite"
        assert (
            cli.main(
                ["setup", "-m", str(source), "-o", str(output), "--reps-only", "-q"]
            )
            == 0
        )
        with MetadataDB(output) as db:
            assert db.info["reps_only"] == "true"
            assert set(db.lookup(["GCA_1", "GCA_2"])) == {"GCA_1"}

    def test_setup_help(self) -> None:
        result = _run("setup", "--help")
        assert "--reps-only" in result.stdout

    def test_download_help(self) -> None:
        result = _run("download", "--help")
        assert "--release" in result.stdout
        assert "--only" in result.stdout

    def test_serve_requires_db(self) -> None:
        result = _run("serve", check=False)
        assert result.returncode == 2
        assert "--db" in result.stderr


class TestTaxonomyOutputs:
    def _profile(self, taxonomy_server, *args: str) -> subprocess.CompletedProcess[str]:
        socket_path, directory = taxonomy_server
        return _run(
            "profile",
            str(directory / "one.sylsp"),
            str(directory / "two.sylsp"),
            "--socket",
            str(socket_path),
            *args,
            check=False,
        )

    def test_taxprof_and_taxmatrix(self, taxonomy_server) -> None:
        _, directory = taxonomy_server
        result = self._profile(
            taxonomy_server,
            "--fields",
            "none",
            "--taxprof",
            str(directory / "taxprof-"),
            "--taxmatrix",
            str(directory / "matrix-"),
        )
        assert result.returncode == 0, result.stderr

        # The main TSV is unchanged: no taxonomy columns unless --fields asks.
        header = result.stdout.splitlines()[0].split("\t")
        assert "accession" in header
        assert "gtdb_taxonomy" not in header
        assert "ncbi_taxonomy" not in header

        for sample in ("one", "two"):
            for taxonomy in ("gtdb", "ncbi"):
                assert (directory / f"taxprof-{sample}.{taxonomy}.sylphmpa").is_file()
        assert (directory / "matrix-gtdb.tsv").is_file()
        assert (directory / "matrix-ncbi.tsv").is_file()

        gtdb = (directory / "taxprof-one.gtdb.sylphmpa").read_text().splitlines()
        assert gtdb[0] == "#SampleID\tone\tTaxonomies_used:['gtdb']"
        assert gtdb[1].split("\t") == [
            "clade_name",
            "relative_abundance",
            "sequence_abundance",
            "ANI (if strain-level)",
            "Coverage (if strain-level)",
        ]
        rows = {line.split("\t")[0]: line.split("\t") for line in gtdb[2:]}
        root = rows["d__Bacteria"]
        assert float(root[1]) == pytest.approx(100.0)
        assert float(root[2]) == pytest.approx(100.0)
        assert root[3:] == ["NA", "NA"]

        species = (
            "d__Bacteria|p__Bacillota|c__Clostridia|o__Clostridiales|"
            "f__Clostridiaceae|g__Clostridium|s__Clostridium difficile"
        )
        strain = rows[species + "|t__GCA_111111111.1"]
        assert float(strain[3]) > 90.0
        assert float(strain[4]) == pytest.approx(1.0)

        ncbi = (directory / "taxprof-one.ncbi.sylphmpa").read_text().splitlines()
        assert (
            "d__Bacteria|p__Bacillota|c__Clostridia|o__unclassified Clostridia"
            in {line.split("\t")[0] for line in ncbi[2:]}
        )

        matrix = (directory / "matrix-gtdb.tsv").read_text().splitlines()
        assert matrix[0] == "clade_name\tone\ttwo"
        values = {line.split("\t")[0]: line.split("\t")[1:] for line in matrix[1:]}
        assert [float(value) for value in values["d__Bacteria"]] == pytest.approx(
            [100.0, 100.0]
        )
        assert [
            float(value) for value in values["d__Bacteria|p__Pseudomonadota"]
        ] == pytest.approx([0.0, 50.0])
        # Rows below a missing taxon are present with zero.
        assert (
            "d__Bacteria|p__Pseudomonadota|c__Gammaproteobacteria"
            in values
        )

    def test_taxonomy_selection_and_visible_fields(self, taxonomy_server) -> None:
        _, directory = taxonomy_server
        result = self._profile(
            taxonomy_server,
            "--taxonomy",
            "gtdb",
            "--fields",
            "gtdb_taxonomy",
            "--taxprof",
            str(directory / "sel-"),
            "--taxmatrix",
            str(directory / "sel-"),
        )
        assert result.returncode == 0, result.stderr
        header = result.stdout.splitlines()[0].split("\t")
        assert "gtdb_taxonomy" in header
        assert "ncbi_taxonomy" not in header
        assert "d__Bacteria" in result.stdout

        assert (directory / "sel-one.gtdb.sylphmpa").is_file()
        assert (directory / "sel-gtdb.tsv").is_file()
        assert not (directory / "sel-one.ncbi.sylphmpa").exists()
        assert not (directory / "sel-ncbi.tsv").exists()

    def test_sequence_matrix(self, taxonomy_server) -> None:
        _, directory = taxonomy_server
        result = self._profile(
            taxonomy_server,
            "--taxonomy",
            "gtdb",
            "--taxmatrix",
            str(directory / "seq-"),
            "--taxmatrix-value",
            "sequence",
        )
        assert result.returncode == 0, result.stderr
        matrix = (directory / "seq-gtdb.tsv").read_text().splitlines()
        rows = {line.split("\t")[0]: line.split("\t")[1:] for line in matrix[1:]}
        assert [float(value) for value in rows["d__Bacteria"]] == pytest.approx(
            [100.0, 100.0], rel=0.01
        )

    def test_unknown_taxonomy(self, taxonomy_server) -> None:
        _, directory = taxonomy_server
        result = self._profile(
            taxonomy_server, "--taxonomy", "kraken", "--taxprof", str(directory / "x-")
        )
        assert result.returncode == 1
        assert "unknown taxonomy" in result.stderr

    def test_requires_server_metadata(self, tmp_path: Path) -> None:
        # A server without --meta: the taxonomy flags must fail clearly.
        socket_path = tmp_path / "plain.sock"
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
            result = _run(
                "profile",
                str(O157_SKETCH),
                "--socket",
                str(socket_path),
                "--taxprof",
                str(tmp_path / "p-"),
                check=False,
            )
        finally:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        assert result.returncode == 1
        assert "no metadata database" in result.stderr

    def test_query_has_no_taxonomy_flags(self) -> None:
        result = _run(
            "query",
            str(O157_SKETCH),
            "--taxprof",
            "x-",
            "--socket",
            "/nonexistent.sock",
            check=False,
        )
        assert result.returncode == 2
        assert "unrecognized arguments" in result.stderr
