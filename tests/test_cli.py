"""CLI tests, including one real Unix-socket round trip against a subprocess."""

from __future__ import annotations

import csv
import json
import os
import socket
import sqlite3
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
from sylphd.metadata import METADATA_COLUMNS, TaxonomySource, build_metadata_db

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
    """Wait until ``path`` exists *and* accepts connections.

    ``serve`` binds the socket before uvicorn starts listening, so the file can
    exist for a moment without a listener.
    """
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
    # One sylph-tax source keyed by the whole genome file name: it overrides
    # the virtual GTDB lineage of the first genome (a later ord), so the
    # 'tax' profile has something to show that gtdb/ncbi do not.
    uhgv = directory / "uhgv.tsv"
    uhgv.write_text(
        "GCA_111111111.1_genomic.fna.gz\tvFAM-1;vOTU-1\tHostus testus\n",
        newline="",
    )
    build_metadata_db(
        [source],
        directory / "taxonomy-meta.sqlite",
        release=232,
        taxonomy=[TaxonomySource("UHGV", uhgv)],
    )


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

    def test_estimate_read_counts_tsv_value(self, server) -> None:
        """``--estimate-read-counts`` puts read counts in ``Sequence_abundance``.

        ``sylph profile --estimate-read-counts ecoli.syldb o157.sylsp`` reports
        1149 reads for o157.  The option implies ``--estimate-unknown``, so the
        coverage column is labelled ``True_cov``.
        """
        socket_path, _ = server
        result = _run(
            "profile",
            str(O157_SKETCH),
            "--socket",
            str(socket_path),
            "--estimate-read-counts",
            "--fields",
            "none",
        )
        lines = [line for line in result.stdout.splitlines() if line]
        header = lines[0].split("\t")
        assert "True_cov" in header
        assert "Eff_cov" not in header
        row = dict(zip(header, lines[1].split("\t")))
        counts = float(row["Sequence_abundance"])
        assert counts == pytest.approx(1149.0)
        assert counts.is_integer()
        # Taxonomic_abundance is still a relative abundance.
        assert float(row["Taxonomic_abundance"]) == pytest.approx(100.0)


class TestMetadataTSV:
    @pytest.mark.parametrize("fields", [[], ["--fields", "all"]])
    def test_default_includes_metadata_columns(self, metadata_server, fields) -> None:
        socket_path, _ = metadata_server
        result = _run("query", str(O157_SKETCH), "--socket", str(socket_path), *fields)
        lines = [line for line in result.stdout.splitlines() if line]
        header = lines[0].split("\t")
        assert "gtdb_taxonomy" in header
        assert "genome_size" in header
        assert len(lines[1].split("\t")) == len(header)

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

    @pytest.mark.parametrize("error", [sqlite3.DatabaseError("not a database"), csv.Error("bad field")])
    def test_storage_and_parser_errors_are_cli_errors(
        self, error: Exception, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def fail(args: object) -> int:
            raise error

        monkeypatch.setattr(cli, "cmd_setup", fail)
        assert cli.main(["setup", "-m", "x.tsv", "-o", "x.sqlite"]) == 1
        assert f"error: {error}" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "args",
        [
            ["query", str(O157_SKETCH), "-o", "{dir}/missing/out.tsv"],
            ["profile", str(O157_SKETCH), "--taxprof", "{dir}/missing/p-"],
            ["profile", str(O157_SKETCH), "--taxmatrix", "{dir}/missing/m-"],
            ["profile", str(O157_SKETCH), "--taxonomy", "kraken", "--taxprof", "{dir}/p-"],
        ],
    )
    def test_output_problems_fail_before_contacting_the_server(
        self, args: list[str], tmp_path: Path
    ) -> None:
        # the socket does not exist: reaching the server would fail differently
        result = _run(
            *(a.format(dir=tmp_path) for a in args),
            "--socket",
            str(tmp_path / "none.sock"),
            check=False,
        )
        assert result.returncode in (1, 2)
        assert "cannot talk" not in result.stderr
        assert "error:" in result.stderr

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
        rows = {line.split("\t")[0]: line.split("\t") for line in gtdb[2:]}
        assert float(rows["d__Bacteria"][1]) == pytest.approx(100.0)

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

    @pytest.mark.parametrize("value", ["ani", "coverage"])
    def test_strain_level_matrices(self, taxonomy_server, value: str) -> None:
        """``ani``/``coverage`` matrices hold strain rows only, sorted.

        Same shape as ``sylph-tax merge --column ANI|Coverage``: the rank rows
        (whose value is ``NA`` in every sample) are dropped, and a strain the
        other sample does not contain is ``0``.
        """
        _, directory = taxonomy_server
        result = self._profile(
            taxonomy_server,
            "--taxonomy",
            "gtdb",
            "--taxmatrix",
            str(directory / f"{value}-"),
            "--taxmatrix-value",
            value,
        )
        assert result.returncode == 0, result.stderr
        lines = (directory / f"{value}-gtdb.tsv").read_text().splitlines()
        assert lines[0] == "clade_name\tone\ttwo"
        rows = [line.split("\t") for line in lines[1:]]
        assert rows
        assert all("|t__" in row[0] for row in rows)
        assert [row[0] for row in rows] == sorted(row[0] for row in rows)
        values = [[float(cell) for cell in row[1:]] for row in rows]
        assert all(len(cells) == 2 for cells in values)
        # ``one`` only holds the first genome: the second strain is 0 there.
        assert any(0.0 in cells for cells in values)
        assert all(max(cells) > 0.0 for cells in values)

    def test_tax_profile(self, taxonomy_server) -> None:
        """``--taxonomy tax`` writes the merged, sylph-tax-exact profile."""
        _, directory = taxonomy_server
        result = self._profile(
            taxonomy_server,
            "--taxonomy",
            "tax",
            "--taxprof",
            str(directory / "tax-"),
            "--taxmatrix",
            str(directory / "tax-"),
            "--fields",
            "none",
        )
        assert result.returncode == 0, result.stderr
        lines = (directory / "tax-one.tax.sylphmpa").read_text().splitlines()
        assert lines[0] == (
            "#SampleID\tone\tTaxonomies_used:['GTDB_r232', 'UHGV']"
        )
        rows = {line.split("\t")[0]: line.split("\t") for line in lines[2:]}
        # The UHGV key is the whole genome file name, so pass 1 wins over the
        # virtual GTDB accession.
        assert "vFAM-1|vOTU-1" in rows
        strain = "vFAM-1|vOTU-1|t__GCA_111111111.1_genomic.fna.gz"
        assert strain in rows
        assert float(rows[strain][3]) > 90.0
        assert (directory / "tax-tax.tsv").is_file()

    def test_tax_source_restricts_the_sources(self, taxonomy_server) -> None:
        _, directory = taxonomy_server
        result = self._profile(
            taxonomy_server,
            "--taxonomy",
            "tax",
            "--tax-source",
            "GTDB_r232",
            "--taxprof",
            str(directory / "src-"),
            "--fields",
            "none",
        )
        assert result.returncode == 0, result.stderr
        lines = (directory / "src-one.tax.sylphmpa").read_text().splitlines()
        assert lines[0] == "#SampleID\tone\tTaxonomies_used:['GTDB_r232']"
        assert any(line.startswith("d__Bacteria|") for line in lines[2:])
        assert not any("vFAM-1" in line for line in lines[2:])

    def test_unknown_tax_source(self, taxonomy_server) -> None:
        _, directory = taxonomy_server
        result = self._profile(
            taxonomy_server,
            "--taxonomy",
            "tax",
            "--tax-source",
            "nope",
            "--taxprof",
            str(directory / "x-"),
        )
        assert result.returncode == 1
        assert "unknown taxonomy source" in result.stderr

    def test_tax_source_needs_tax(self, taxonomy_server) -> None:
        _, directory = taxonomy_server
        result = self._profile(
            taxonomy_server,
            "--taxonomy",
            "gtdb",
            "--tax-source",
            "GTDB_r232",
            "--taxprof",
            str(directory / "x-"),
        )
        assert result.returncode == 1
        assert "--tax-source" in result.stderr

    def test_unknown_taxonomy(self, taxonomy_server) -> None:
        _, directory = taxonomy_server
        result = self._profile(
            taxonomy_server, "--taxonomy", "kraken", "--taxprof", str(directory / "x-")
        )
        assert result.returncode == 1
        assert "unknown taxonomy" in result.stderr

    def test_requires_server_metadata(self, server, tmp_path: Path) -> None:
        # The plain server has no --meta: the taxonomy flags must fail clearly.
        socket_path, _ = server
        result = _run(
            "profile",
            str(O157_SKETCH),
            "--socket",
            str(socket_path),
            "--taxprof",
            str(tmp_path / "p-"),
            check=False,
        )
        assert result.returncode == 1
        assert "no metadata database" in result.stderr


class TestNonGtdbClassification:
    """The 95 % caveat for a best hit outside GTDB (design note B6)."""

    def _half(self, tmp_path: Path, records: int = 1) -> Path:
        with dnaio.open(CLODF13_FASTA) as handle:
            sequence = next(iter(handle)).sequence
        half = sequence[: len(sequence) // 2]
        size = len(half) // records
        path = tmp_path / "half.fasta"
        path.write_text(
            "".join(
                f">part{index}\n{half[index * size : (index + 1) * size]}\n"
                for index in range(records)
            )
        )
        return path

    def test_assembly_warns_and_shows_the_generic_taxon(
        self, taxonomy_server, tmp_path
    ) -> None:
        socket_path, _ = taxonomy_server
        result = _run(
            "assembly", str(self._half(tmp_path)), "--socket", str(socket_path)
        )
        assert result.returncode == 0, result.stderr
        header, row = result.stdout.splitlines()[0].split("\t"), result.stdout.splitlines()[1]
        assert header[6:9] == ["taxon", "gtdb_species", "taxonomy_source"]
        # the fixture genome is in GTDB metadata too, but the UHGV source
        # resolved this hit, so UHGV's lineage is the one that counts
        assert row.split("\t")[6:9] == ["vOTU-1", "Clostridium difficile", "UHGV"]
        assert "assigned to vOTU-1 at the 95 % species threshold" in result.stderr
        assert result.stderr.count("warning:") == 1
        assert "UHGV (viruses)" in result.stderr

    def test_quiet_suppresses_the_caveat(self, taxonomy_server, tmp_path) -> None:
        socket_path, _ = taxonomy_server
        result = _run(
            "assembly",
            str(self._half(tmp_path)),
            "--socket",
            str(socket_path),
            "--quiet",
        )
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""

    def test_json_carries_the_note_and_no_stderr(
        self, taxonomy_server, tmp_path
    ) -> None:
        socket_path, _ = taxonomy_server
        result = _run(
            "assembly",
            str(self._half(tmp_path)),
            "--socket",
            str(socket_path),
            "--json",
        )
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        payload = json.loads(result.stdout)
        assert payload["assignment"]["taxon"] == "vOTU-1"
        assert payload["assignment"]["taxonomy"] == {
            "level 1": "vFAM-1",
            "level 2": "vOTU-1",
        }
        assert payload["assignment"]["taxonomy_source"] == "UHGV"
        assert payload["assignment"]["note"].startswith("UHGV (viruses)")
        assert payload["closest"] == payload["assignment"]
        (hit,) = payload["hits"]
        assert hit["taxonomy_source"] == "UHGV"
        assert hit["taxonomy"]["level 2"] == "vOTU-1"
        assert hit["shared_rank"] == "species"

    def test_per_contig_prints_the_caveat_once(self, taxonomy_server, tmp_path) -> None:
        socket_path, _ = taxonomy_server
        result = _run(
            "assembly",
            str(self._half(tmp_path, records=2)),
            "--per-contig",
            "--min-contig-length",
            "1000",
            "--socket",
            str(socket_path),
        )
        assert result.returncode == 0, result.stderr
        assert len(result.stdout.splitlines()) == 3  # header + two contigs
        assert result.stderr.count("UHGV (viruses)") == 1
        assert result.stderr.count("\n") == 3  # query line, consensus, caveat

    def test_tax_profile_prints_the_caveat_once(self, taxonomy_server) -> None:
        socket_path, directory = taxonomy_server
        result = _run(
            "profile",
            str(directory / "one.sylsp"),
            str(directory / "two.sylsp"),
            "--socket",
            str(socket_path),
            "--taxonomy",
            "tax",
            "--taxprof",
            str(directory / "warn-"),
        )
        assert result.returncode == 0, result.stderr
        assert result.stderr.count("UHGV (viruses)") == 1
        # A GTDB-only profile has nothing to warn about.
        gtdb = _run(
            "profile",
            str(directory / "one.sylsp"),
            "--socket",
            str(socket_path),
            "--taxonomy",
            "gtdb",
            "--taxprof",
            str(directory / "quiet-"),
        )
        assert gtdb.returncode == 0, gtdb.stderr
        assert "warning:" not in gtdb.stderr


class TestShutdown:
    # Last on purpose: it terminates the module-scoped ``server``.
    def test_graceful_shutdown_removes_socket(self, server) -> None:
        socket_path, process = server
        assert socket_path.is_socket()
        process.terminate()
        process.wait(timeout=20)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and socket_path.exists():
            time.sleep(0.1)
        assert not socket_path.exists()
