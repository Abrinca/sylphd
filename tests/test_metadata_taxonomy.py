"""Tests for the format 3 taxonomy tables: build, update, and the v2/v3 reader."""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
import stat
import threading
from pathlib import Path

import pytest
from compat.metadata_v2_reader import MetadataDB as V2MetadataDB
from conftest import ECOLI_TWO_STAGE
from starlette.testclient import TestClient

from sylphd import cli, download
from sylphd.catalog import DATABASES, TAXONOMIES
from sylphd.download import parse_taxonomy_spec, taxonomy_table
from sylphd.metadata import (
    METADATA_COLUMNS,
    MetadataDB,
)
from sylphd.metadata_build import build_metadata_db, update_metadata_db
from sylphd.taxonomy_sources import TaxonomySource

ROOT = Path(__file__).resolve().parent.parent

HEADER = ["accession", *METADATA_COLUMNS]


def _gtdb_row(accession: str, taxonomy: str = "d__Bacteria;p__Test") -> list[str]:
    values = {column: "none" for column in METADATA_COLUMNS}
    values["gtdb_taxonomy"] = taxonomy
    values["genome_size"] = "1000"
    return [accession, *[values[column] for column in METADATA_COLUMNS]]


@pytest.fixture()
def gtdb_source(tmp_path: Path) -> Path:
    path = tmp_path / "bac120_metadata_r232.tsv.gz"
    text = "\t".join(HEADER) + "\n"
    text += "\n".join(
        "\t".join(row)
        for row in (
            _gtdb_row("RS_GCF_000005845.2"),
            _gtdb_row("GB_GCA_964372435.1", "d__Archaea"),
        )
    )
    text += "\n"
    with gzip.open(path, "wt", newline="") as handle:
        handle.write(text)
    return path


@pytest.fixture()
def uhgv_file(tmp_path: Path) -> Path:
    """A gzip file with a header row, a host column and a repeated key."""
    path = tmp_path / "uhgv_default_metadata.tsv.gz"
    text = (
        "key\tlineage\thost\n"
        "UHGV-0405173\tvFAM-00013;root;Monodnaviria\tHomo sapiens\n"
        "UHGV-0405174\tvFAM-00014;root\t\n"
        "UHGV-0405173\tvFAM-00099;root\t\n"
    )
    with gzip.open(path, "wt", newline="") as handle:
        handle.write(text)
    return path


@pytest.fixture()
def fungi_file(tmp_path: Path) -> Path:
    """A plain file whose key collides with the GTDB accession and UHGV key."""
    path = tmp_path / "fungi_refseq_2025-10-11_metadata.tsv"
    path.write_text(
        "GCF_000005845.2\td__Bacteria;p__Test;s__X\n"
        "UHGV-0405173\td__Eukaryota;p__Fungi\n",
        newline="",
    )
    return path


def _source_ords(path: str | Path) -> list[tuple[str, int]]:
    """Return ``(name, ord)`` of the taxonomy sources, in resolution order."""
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT name, ord FROM taxonomy_source ORDER BY ord"
        ).fetchall()
    finally:
        connection.close()


def _to_v2(path: str | Path) -> Path:
    """Turn a built file into what format 2 wrote: no taxonomy tables, version 2."""
    path = Path(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP INDEX IF EXISTS taxonomy_entry_key")
        for table in (
            "taxonomy_entry",
            "taxonomy_source",
            "taxonomy_overlap",
            "dict_tax_lineage",
            "dict_tax_host",
        ):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        for key in ("taxonomy_sources", "sylphtax_rules"):
            connection.execute("DELETE FROM info WHERE key = ?", (key,))
        connection.execute("UPDATE info SET value = '2' WHERE key = 'format_version'")
        connection.commit()
    finally:
        connection.close()
    return path


class TestBuild:
    def test_v3_sources_and_entries(
        self, tmp_path: Path, gtdb_source: Path, uhgv_file: Path, fungi_file: Path
    ) -> None:
        output = tmp_path / "meta.sqlite"
        stats = build_metadata_db(
            [gtdb_source],
            output,
            release=232,
            taxonomy=[
                TaxonomySource("UHGV_default", uhgv_file),
                TaxonomySource("FungiRefSeq-latest", fungi_file),
            ],
        )
        assert stats["rows"] == 2
        assert [source["name"] for source in stats["taxonomy_sources"]] == [
            "GTDB_r232",
            "UHGV_default",
            "FungiRefSeq-latest",
        ]

        with MetadataDB(output) as db:
            assert db.format_version == "3"
            assert db.has_taxonomy
            sources = db.taxonomy_sources()
            assert [source["name"] for source in sources] == [
                "GTDB_r232",
                "UHGV_default",
                "FungiRefSeq-latest",
            ]
            assert [source["kind"] for source in sources] == [
                "metadata",
                "sylphtax",
                "sylphtax",
            ]
            assert sources[0]["keys"] == 2  # every GTDB row has a lineage
            assert sources[0]["md5"] is None  # virtual: nothing of its own
            # the header row is an entry and the repeated key collapses
            assert sources[1]["keys"] == 3
            assert sources[1]["has_host"] is True
            assert sources[2]["has_host"] is False
            assert len(sources[1]["md5"]) == 32
            # Public (/info) description: a local path is reduced to its base name.
            assert sources[1]["origin"] == uhgv_file.name

            summary = db.summary()
            assert summary["sylphtax_rules"] == "1.9.2"
            assert [source["name"] for source in summary["taxonomy_sources"]] == [
                "GTDB_r232",
                "UHGV_default",
                "FungiRefSeq-latest",
            ]
            assert json.loads(db.info["taxonomy_sources"])[1]["name"] == "UHGV_default"

        connection = sqlite3.connect(output)
        try:
            # ord is the setup order, the virtual GTDB source first
            assert connection.execute(
                "SELECT name, ord FROM taxonomy_source ORDER BY ord"
            ).fetchall() == [("GTDB_r232", 0), ("UHGV_default", 1), ("FungiRefSeq-latest", 2)]
            # keys are verbatim (case sensitive), the header row is an entry
            keys = {
                (name, key, lineage, host)
                for name, key, lineage, host in connection.execute(
                    "SELECT s.name, e.key, l.value, h.value FROM taxonomy_entry AS e "
                    "JOIN taxonomy_source AS s ON s.id = e.source_id "
                    "LEFT JOIN dict_tax_lineage AS l ON l.id = e.lineage "
                    "LEFT JOIN dict_tax_host AS h ON h.id = e.host"
                )
            }
            # a repeated key keeps the last lineage and the host of the earlier
            # row (sylph-tax only replaces the host when a row has one)
            assert ("UHGV_default", "UHGV-0405173", "vFAM-00099;root", "Homo sapiens") in keys
            assert not ("UHGV_default", "UHGV-0405173", "vFAM-00013;root;Monodnaviria", None) in keys
            assert ("UHGV_default", "UHGV-0405174", "vFAM-00014;root", None) in keys
            assert ("UHGV_default", "key", "lineage", "host") in keys
            # the same key in two sources is kept in both: resolution picks by ord
            assert ("FungiRefSeq-latest", "GCF_000005845.2", "d__Bacteria;p__Test;s__X", None) in keys
            assert ("FungiRefSeq-latest", "UHGV-0405173", "d__Eukaryota;p__Fungi", None) in keys
        finally:
            connection.close()

    def test_only_taxonomy_is_allowed(self, tmp_path: Path, fungi_file: Path) -> None:
        output = tmp_path / "meta.sqlite"
        stats = build_metadata_db(
            [], output, taxonomy=[TaxonomySource("FungiRefSeq-latest", fungi_file)]
        )
        assert stats["rows"] == 0
        with MetadataDB(output) as db:
            assert [source["name"] for source in db.taxonomy_sources()] == [
                "FungiRefSeq-latest"
            ]

    def test_gtdb_catalogue_source_of_the_same_release_is_skipped(
        self, tmp_path: Path, gtdb_source: Path, fungi_file: Path
    ) -> None:
        output = tmp_path / "meta.sqlite"
        stats = build_metadata_db(
            [gtdb_source],
            output,
            release=232,
            taxonomy=[
                TaxonomySource("GTDB_r232", fungi_file, from_catalogue=True),
                TaxonomySource("FungiRefSeq-latest", fungi_file),
            ],
        )
        assert stats["skipped_taxonomy"] == ["GTDB_r232"]
        with MetadataDB(output) as db:
            # only the virtual source and the non-GTDB file: no second copy of
            # the GTDB lineages
            assert [source["name"] for source in db.taxonomy_sources()] == [
                "GTDB_r232",
                "FungiRefSeq-latest",
            ]
            assert db.taxonomy_sources()[0]["kind"] == "metadata"
            assert [source["keys"] for source in db.taxonomy_sources()][1:] == [2]
            # the skipped source does not leave a hole in the order
            assert _source_ords(output) == [("GTDB_r232", 0), ("FungiRefSeq-latest", 1)]

    @pytest.mark.parametrize(
        "source_name",
        [
            "GTDB_r214",  # a different release: a different taxonomy
            "GTDB_r232_custom",  # a local name that only shares the prefix
        ],
    )
    def test_other_gtdb_like_sources_are_imported(
        self,
        tmp_path: Path,
        gtdb_source: Path,
        fungi_file: Path,
        source_name: str,
    ) -> None:
        output = tmp_path / "meta.sqlite"
        stats = build_metadata_db(
            [gtdb_source],
            output,
            release=232,
            taxonomy=[
                TaxonomySource(source_name, fungi_file, from_catalogue=True)
            ],
        )
        assert stats["skipped_taxonomy"] == []
        with MetadataDB(output) as db:
            assert [source["name"] for source in db.taxonomy_sources()] == [
                "GTDB_r232",
                source_name,
            ]

    def test_a_local_gtdb_file_is_imported(
        self, tmp_path: Path, gtdb_source: Path, fungi_file: Path
    ) -> None:
        """A local ``NAME=PATH`` file is never treated as the catalogue one."""
        output = tmp_path / "meta.sqlite"
        stats = build_metadata_db(
            [gtdb_source],
            output,
            release=232,
            taxonomy=[TaxonomySource("GTDB_from_file", fungi_file)],
        )
        assert stats["skipped_taxonomy"] == []
        with MetadataDB(output) as db:
            sources = db.taxonomy_sources()
            assert [(source["name"], source["kind"]) for source in sources] == [
                ("GTDB_r232", "metadata"),
                ("GTDB_from_file", "sylphtax"),
            ]
            assert sources[0]["keys"] == 2  # the virtual source is intact

    def test_a_local_file_may_not_use_the_virtual_name(
        self, tmp_path: Path, gtdb_source: Path, fungi_file: Path
    ) -> None:
        """Naming a local file GTDB_r232 would drop the virtual source: refuse."""
        output = tmp_path / "meta.sqlite"
        with pytest.raises(ValueError, match="virtual GTDB source"):
            build_metadata_db(
                [gtdb_source],
                output,
                release=232,
                taxonomy=[TaxonomySource("GTDB_r232", fungi_file)],
            )
        assert not output.exists()

    def test_no_skip_without_a_release(
        self, tmp_path: Path, gtdb_source: Path, fungi_file: Path
    ) -> None:
        """Without --release the virtual source is ``GTDB``; nothing can match."""
        output = tmp_path / "meta.sqlite"
        stats = build_metadata_db(
            [gtdb_source],
            output,
            taxonomy=[TaxonomySource("GTDB_r232", fungi_file, from_catalogue=True)],
        )
        assert stats["skipped_taxonomy"] == []
        with MetadataDB(output) as db:
            assert [source["name"] for source in db.taxonomy_sources()] == [
                "GTDB",
                "GTDB_r232",
            ]

    def test_missing_taxonomy_file_keeps_old_database(
        self, tmp_path: Path, gtdb_source: Path, fungi_file: Path
    ) -> None:
        output = tmp_path / "meta.sqlite"
        build_metadata_db([gtdb_source], output, release=232)
        before = output.read_bytes()
        with pytest.raises(FileNotFoundError):
            build_metadata_db(
                [gtdb_source],
                output,
                release=232,
                force=True,
                taxonomy=[TaxonomySource("gone", tmp_path / "gone.tsv")],
            )
        assert output.read_bytes() == before
        assert sorted(path.name for path in tmp_path.glob("meta.sqlite*")) == ["meta.sqlite"]

    def test_build_leaves_an_unrelated_tmp_file_alone(
        self, tmp_path: Path, gtdb_source: Path
    ) -> None:
        """The build must not unlink a ``<output>.tmp`` that is not its own."""
        decoy = tmp_path / "meta.sqlite.tmp"
        decoy.write_bytes(b"do not touch")
        build_metadata_db([gtdb_source], tmp_path / "meta.sqlite", release=232)
        assert decoy.read_bytes() == b"do not touch"


def _sylphtax_reference(
    rows: list[tuple[str, str, str | None]],
) -> dict[str, tuple[str, str | None]]:
    """What sylph-tax's two dicts end up with, for parity checks.

    ``sylph_to_taxprof.py`` assigns ``genome_to_taxonomy[key]`` on every row and
    ``genome_to_additional_data[key]`` only for a row with a third column.
    """
    lineages: dict[str, str] = {}
    hosts: dict[str, str] = {}
    for key, lineage, host in rows:
        lineages[key] = lineage
        if host is not None:
            hosts[key] = host
    return {key: (lineage, hosts.get(key)) for key, lineage in lineages.items()}


def _stored_entries(path: str | Path) -> dict[tuple[str, str], tuple[str | None, str | None]]:
    connection = sqlite3.connect(path)
    try:
        return {
            (name, key): (lineage, host)
            for name, key, lineage, host in connection.execute(
                "SELECT s.name, e.key, l.value, h.value FROM taxonomy_entry AS e "
                "JOIN taxonomy_source AS s ON s.id = e.source_id "
                "LEFT JOIN dict_tax_lineage AS l ON l.id = e.lineage "
                "LEFT JOIN dict_tax_host AS h ON h.id = e.host"
            )
        }
    finally:
        connection.close()


class TestDuplicateKeys:
    """A repeated key must end up as in sylph-tax's dicts (item B2/phase-3 input)."""

    def test_reference_helper_matches_the_documented_rules(self) -> None:
        rows = [
            ("k", "first", "H1"),
            ("k", "second", None),
            ("j", "only", "H2"),
            ("j", "later", "H3"),
        ]
        assert _sylphtax_reference(rows) == {
            "k": ("second", "H1"),
            "j": ("later", "H3"),
        }

    def test_duplicates_across_the_batch_boundary(
        self, tmp_path: Path
    ) -> None:
        """Duplicates inside, across and around the 20,000-row batch boundary."""
        rows: list[tuple[str, str, str | None]] = []
        rows.append(("K_early", "l1", "H_early"))
        rows.append(("K_before_batch", "l2", "H_batch"))
        for index in range(19_998):
            rows.append((f"U{index}", f"l{index}", None))
        # rows[20_000] and rows[20_001]: the split of the first batch
        rows.append(("K_before_batch", "l3", None))  # keeps H, no host column
        rows.append(("K_boundary", "b1", "H_boundary"))
        rows.append(("K_early", "l4", None))  # keeps H_early
        rows.append(("K_boundary", "b2", "H_boundary2"))
        for index in range(20_000):
            rows.append((f"V{index}", f"v{index}", None))
        rows.append(("K_boundary", "b3", None))  # keeps H_boundary2
        rows.append(("K_early", "l5", "H_late"))  # host replaced

        path = tmp_path / "dup.tsv"
        path.write_text(
            "".join(
                f"{key}\t{lineage}\t{host}\n" if host is not None else f"{key}\t{lineage}\n"
                for key, lineage, host in rows
            ),
            newline="",
        )
        output = tmp_path / "meta.sqlite"
        build_metadata_db([], output, taxonomy=[TaxonomySource("dup", path)])

        reference = _sylphtax_reference(rows)
        stored = {key: value for (_, key), value in _stored_entries(output).items()}
        assert stored == reference
        assert stored["K_early"] == ("l5", "H_late")
        assert stored["K_before_batch"] == ("l3", "H_batch")
        assert stored["K_boundary"] == ("b3", "H_boundary2")

    def test_hosts_are_kept_per_source(self, tmp_path: Path) -> None:
        """Each source keeps its own host row; phase 3 resolves across sources."""
        first = tmp_path / "first.tsv"
        first.write_text("K\tone\tH1\n", newline="")
        second = tmp_path / "second.tsv"
        second.write_text("K\ttwo\n", newline="")
        output = tmp_path / "meta.sqlite"
        build_metadata_db(
            [],
            output,
            taxonomy=[
                TaxonomySource("first", first),
                TaxonomySource("second", second),
            ],
        )
        assert _stored_entries(output) == {
            ("first", "K"): ("one", "H1"),
            ("second", "K"): ("two", None),
        }


class TestSylphTaxParsing:
    """The parser mirrors ``sylph_to_taxprof.py`` and reports file and line."""

    def test_blank_line_is_an_error(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.tsv"
        path.write_text("a\tlineage\n\nb\tlineage\n", newline="")
        with pytest.raises(ValueError, match=r"broken\.tsv: line 2: blank line"):
            build_metadata_db(
                [], tmp_path / "meta.sqlite", taxonomy=[TaxonomySource("bad", path)]
            )

    def test_line_without_a_tab_is_an_error(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.tsv.gz"
        with gzip.open(path, "wt", newline="") as handle:
            handle.write("a\tlineage\nno-tab-here\n")
        with pytest.raises(ValueError, match=r"line 2: .*no tab"):
            build_metadata_db(
                [], tmp_path / "meta.sqlite", taxonomy=[TaxonomySource("bad", path)]
            )

    def test_an_empty_key_is_accepted(self, tmp_path: Path) -> None:
        """sylph-tax stores an empty key; so do we (it never matches a probe)."""
        path = tmp_path / "empty.tsv"
        path.write_text("\tvFAM-1;root\tHost\n", newline="")
        output = tmp_path / "meta.sqlite"
        build_metadata_db([], output, taxonomy=[TaxonomySource("empty", path)])
        assert _stored_entries(output) == {("empty", ""): ("vFAM-1;root", "Host")}

    def test_a_trailing_tab_is_not_a_host(self, tmp_path: Path) -> None:
        path = tmp_path / "trailing.tsv"
        path.write_text("K\tone\tH1\nK\ttwo\t\n", newline="")
        output = tmp_path / "meta.sqlite"
        build_metadata_db([], output, taxonomy=[TaxonomySource("t", path)])
        assert _stored_entries(output) == {("t", "K"): ("two", "H1")}


class TestTemporaryFiles:
    """Builds and updates use a unique exclusive sibling, never ``<output>.tmp``."""

    def test_update_can_read_a_file_named_like_the_old_temp(
        self, tmp_path: Path, gtdb_source: Path, uhgv_file: Path
    ) -> None:
        source = _to_v2(
            build_metadata_db([gtdb_source], tmp_path / "out.sqlite.tmp", release=232)[
                "path"
            ]
        )
        before = source.read_bytes()
        stats = update_metadata_db(
            source,
            tmp_path / "out.sqlite",
            taxonomy=[TaxonomySource("UHGV_default", uhgv_file)],
        )
        assert source.read_bytes() == before  # its own input was not deleted
        assert Path(str(stats["path"])) == tmp_path / "out.sqlite"
        with MetadataDB(stats["path"]) as db:
            assert db.format_version == "3"

    def test_an_unrelated_tmp_file_survives_an_update(
        self, tmp_path: Path, gtdb_source: Path, uhgv_file: Path
    ) -> None:
        path = _to_v2(
            build_metadata_db([gtdb_source], tmp_path / "meta.sqlite", release=232)["path"]
        )
        decoy = tmp_path / "meta.sqlite.tmp"
        decoy.write_bytes(b"do not touch")
        update_metadata_db(
            path, path, taxonomy=[TaxonomySource("UHGV_default", uhgv_file)]
        )
        assert decoy.read_bytes() == b"do not touch"

    def test_failed_update_removes_only_its_own_temp(
        self, tmp_path: Path, gtdb_source: Path, uhgv_file: Path
    ) -> None:
        path = _to_v2(
            build_metadata_db([gtdb_source], tmp_path / "meta.sqlite", release=232)["path"]
        )
        decoy = tmp_path / "meta.sqlite.tmp"
        decoy.write_bytes(b"do not touch")
        with pytest.raises(FileNotFoundError):
            update_metadata_db(
                path, path, taxonomy=[TaxonomySource("gone", tmp_path / "gone.tsv")]
            )
        assert decoy.read_bytes() == b"do not touch"
        assert sorted(p.name for p in tmp_path.glob("meta.sqlite*")) == [
            "meta.sqlite",
            "meta.sqlite.tmp",
        ]

    def test_concurrent_builds_do_not_clobber_each_other(
        self, tmp_path: Path, gtdb_source: Path
    ) -> None:
        output = tmp_path / "meta.sqlite"
        errors: list[BaseException] = []

        def build() -> None:
            try:
                build_metadata_db([gtdb_source], output, release=232, force=True)
            except BaseException as error:  # noqa: BLE001 - reported below
                errors.append(error)

        threads = [threading.Thread(target=build) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert errors == []
        with MetadataDB(output) as db:
            assert db.summary()["rows"] == 2
        # each run cleaned up its own temp file; no fixed <output>.tmp exists
        assert sorted(path.name for path in tmp_path.glob("meta.sqlite*")) == [
            "meta.sqlite"
        ]


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


class TestPermissions:
    def test_a_fresh_build_keeps_the_umask_mode(
        self, tmp_path: Path, gtdb_source: Path
    ) -> None:
        output = tmp_path / "meta.sqlite"
        build_metadata_db([gtdb_source], output, release=232)
        assert stat.S_IMODE(output.stat().st_mode) == 0o666 & ~_umask()

    @pytest.mark.parametrize("mode", [0o600, 0o644])
    def test_an_in_place_update_keeps_the_mode(
        self, tmp_path: Path, gtdb_source: Path, uhgv_file: Path, mode: int
    ) -> None:
        path = _to_v2(
            build_metadata_db([gtdb_source], tmp_path / "meta.sqlite", release=232)["path"]
        )
        path.chmod(mode)
        update_metadata_db(
            path, path, taxonomy=[TaxonomySource("UHGV_default", uhgv_file)]
        )
        assert stat.S_IMODE(path.stat().st_mode) == mode
        with MetadataDB(path) as db:
            assert db.format_version == "3"

    def test_an_update_to_a_new_path_uses_the_umask_mode(
        self, tmp_path: Path, gtdb_source: Path, uhgv_file: Path
    ) -> None:
        source = _to_v2(
            build_metadata_db([gtdb_source], tmp_path / "v2.sqlite", release=232)["path"]
        )
        source.chmod(0o600)
        output = tmp_path / "v3.sqlite"
        update_metadata_db(
            source, output, taxonomy=[TaxonomySource("UHGV_default", uhgv_file)]
        )
        assert stat.S_IMODE(output.stat().st_mode) == 0o666 & ~_umask()



class TestReaderVersions:
    def test_v2_file_loads_and_serves_unchanged(
        self, tmp_path: Path, gtdb_source: Path, uhgv_file: Path
    ) -> None:
        output = _to_v2(build_metadata_db(
            [gtdb_source],
            tmp_path / "meta.sqlite",
            release=232,
            taxonomy=[TaxonomySource("UHGV_default", uhgv_file)],
        )["path"])
        with MetadataDB(output) as db:
            assert db.format_version == "2"
            assert db.has_taxonomy is False
            assert db.taxonomy_sources() == []
            summary = db.summary()
            assert "taxonomy_sources" not in summary
            assert "sylphtax_rules" not in summary
            found = db.lookup(["GCF_000005845.2"])
        assert found["GCF_000005845.2"]["gtdb_taxonomy"] == "d__Bacteria;p__Test"

        from sylphd.server import ServerConfig, create_app

        app = create_app(ServerConfig(db_path=ECOLI_TWO_STAGE, meta_path=output))
        with TestClient(app) as client:
            metadata = client.get("/info").json()["metadata"]
            assert metadata["format_version"] == "2"
            assert "taxonomy_sources" not in metadata
            assert metadata["rows"] == 2

    def test_v2_file_serves_metadata_columns(
        self, tmp_path: Path, tiny_database: Path
    ) -> None:
        from conftest import CLODF13_FASTA

        from sylphd.server import ServerConfig, create_app
        from sylphd.sketching import SampleInput, sketch_sample

        source = tmp_path / "one.tsv"
        row = _gtdb_row("GCA_000000001.1", "d__Bacteria;p__Test;c__Test")
        row[METADATA_COLUMNS.index("genome_size") + 1] = "9957"
        source.write_text("\t".join(HEADER) + "\n" + "\t".join(row) + "\n")
        path = _to_v2(build_metadata_db([source], tmp_path / "meta.sqlite")["path"])

        sketch = sketch_sample(SampleInput("clodf13", ((str(CLODF13_FASTA),),)), c=200, k=31)
        app = create_app(ServerConfig(db_path=tiny_database, meta_path=path))
        with TestClient(app) as client:
            assert client.get("/info").json()["metadata"]["format_version"] == "2"
            response = client.post(
                "/query",
                data={"params": "{}"},
                files={"sketch": ("clodf13.sylsp", sketch.to_bytes())},
            )
        assert response.status_code == 200, response.text
        rows = response.json()[0]["rows"]
        assert rows
        assert rows[0]["genome_size"] == 9957
        assert rows[0]["gtdb_taxonomy"] == "d__Bacteria;p__Test;c__Test"

    def test_v3_file_reports_sources_on_info(
        self, tmp_path: Path, gtdb_source: Path, uhgv_file: Path
    ) -> None:
        output = tmp_path / "meta.sqlite"
        build_metadata_db(
            [gtdb_source],
            output,
            release=232,
            taxonomy=[TaxonomySource("UHGV_default", uhgv_file)],
        )
        from sylphd.server import ServerConfig, create_app

        app = create_app(ServerConfig(db_path=ECOLI_TWO_STAGE, meta_path=output))
        with TestClient(app) as client:
            metadata = client.get("/info").json()["metadata"]
        assert metadata["format_version"] == "3"
        assert [(source["name"], source["keys"]) for source in metadata["taxonomy_sources"]] == [
            ("GTDB_r232", 2),
            ("UHGV_default", 3),
        ]

    def test_old_reader_opens_v3(
        self, tmp_path: Path, gtdb_source: Path, uhgv_file: Path
    ) -> None:
        """A format 2 reader (frozen from e72b807) must open a format 3 file.

        The frozen copy lives in ``tests/compat/metadata_v2_reader.py``; reading
        it from git (``HEAD``) would turn this test into a no-op as soon as
        format 3 is committed.
        """
        output = tmp_path / "meta.sqlite"
        build_metadata_db(
            [gtdb_source],
            output,
            release=232,
            taxonomy=[TaxonomySource("UHGV_default", uhgv_file)],
        )
        with V2MetadataDB(output) as db:
            assert db.columns == METADATA_COLUMNS
            assert db.summary()["rows"] == 2
            # the old reader reports the file's version, it does not know 3
            assert db.summary()["format_version"] == "3"
            assert "taxonomy_sources" not in db.summary()
            assert db.lookup(["GCF_000005845.2"])["GCF_000005845.2"]["gtdb_taxonomy"] == (
                "d__Bacteria;p__Test"
            )


class TestUpdate:
    def test_v2_to_v3_migration(self, tmp_path: Path, gtdb_source: Path, uhgv_file: Path) -> None:
        v2 = _to_v2(
            build_metadata_db([gtdb_source], tmp_path / "v2.sqlite", release=232)["path"]
        )
        before = v2.read_bytes()
        output = tmp_path / "v3.sqlite"
        stats = update_metadata_db(
            v2, output, taxonomy=[TaxonomySource("UHGV_default", uhgv_file)]
        )
        assert stats["rows"] == 2
        assert v2.read_bytes() == before  # the source is untouched
        with MetadataDB(output) as db:
            assert db.format_version == "3"
            assert [source["name"] for source in db.taxonomy_sources()] == [
                "GTDB_r232",
                "UHGV_default",
            ]
            assert db.taxonomy_sources()[0]["kind"] == "metadata"
            assert db.taxonomy_sources()[0]["keys"] == 2
            assert db.lookup(["GCF_000005845.2"])["GCF_000005845.2"]["genome_size"] == 1000

    def test_update_in_place(self, tmp_path: Path, gtdb_source: Path, uhgv_file: Path) -> None:
        path = _to_v2(
            build_metadata_db([gtdb_source], tmp_path / "meta.sqlite", release=232)["path"]
        )
        update_metadata_db(path, path, taxonomy=[TaxonomySource("UHGV_default", uhgv_file)])
        with MetadataDB(path) as db:
            assert db.format_version == "3"
            assert [source["name"] for source in db.taxonomy_sources()] == [
                "GTDB_r232",
                "UHGV_default",
            ]
        assert not list(tmp_path.glob("meta.sqlite*"))[1:]

    def test_update_adds_and_replaces_sources(
        self, tmp_path: Path, gtdb_source: Path, uhgv_file: Path, fungi_file: Path
    ) -> None:
        path = tmp_path / "meta.sqlite"
        build_metadata_db(
            [gtdb_source],
            path,
            release=232,
            taxonomy=[TaxonomySource("UHGV_default", uhgv_file)],
        )
        update_metadata_db(
            path, path, taxonomy=[TaxonomySource("FungiRefSeq-latest", fungi_file)]
        )
        with MetadataDB(path) as db:
            assert [source["name"] for source in db.taxonomy_sources()] == [
                "GTDB_r232",
                "UHGV_default",
                "FungiRefSeq-latest",
            ]

        # Re-importing a source replaces it instead of adding a second one.
        smaller = tmp_path / "smaller.tsv"
        smaller.write_text("UHGV-1\troot\n", newline="")
        update_metadata_db(
            path, path, taxonomy=[TaxonomySource("UHGV_default", smaller)]
        )
        with MetadataDB(path) as db:
            sources = db.taxonomy_sources()
            assert [source["name"] for source in sources] == [
                "GTDB_r232",
                "UHGV_default",
                "FungiRefSeq-latest",
            ]
            assert sources[1]["keys"] == 1

    def test_update_does_not_replace_the_virtual_source(
        self, tmp_path: Path, gtdb_source: Path, fungi_file: Path
    ) -> None:
        """The redundant catalogue GTDB file must not replace the virtual source."""
        path = _to_v2(
            build_metadata_db([gtdb_source], tmp_path / "meta.sqlite", release=232)["path"]
        )
        stats = update_metadata_db(
            path,
            path,
            taxonomy=[
                TaxonomySource("GTDB_r232", fungi_file, from_catalogue=True),
                TaxonomySource("FungiRefSeq-latest", fungi_file),
            ],
        )
        assert stats["skipped_taxonomy"] == ["GTDB_r232"]
        with MetadataDB(path) as db:
            sources = db.taxonomy_sources()
            assert [(source["name"], source["kind"]) for source in sources] == [
                ("GTDB_r232", "metadata"),
                ("FungiRefSeq-latest", "sylphtax"),
            ]
            assert sources[0]["keys"] == 2  # the virtual source still resolves GTDB

    def test_update_imports_a_different_gtdb_release(
        self, tmp_path: Path, gtdb_source: Path, fungi_file: Path
    ) -> None:
        path = _to_v2(
            build_metadata_db([gtdb_source], tmp_path / "meta.sqlite", release=232)["path"]
        )
        stats = update_metadata_db(
            path,
            path,
            taxonomy=[TaxonomySource("GTDB_r214", fungi_file, from_catalogue=True)],
        )
        assert stats["skipped_taxonomy"] == []
        with MetadataDB(path) as db:
            assert [source["name"] for source in db.taxonomy_sources()] == [
                "GTDB_r232",
                "GTDB_r214",
            ]

    def test_failed_update_keeps_the_file(
        self, tmp_path: Path, gtdb_source: Path, uhgv_file: Path
    ) -> None:
        path = _to_v2(
            build_metadata_db([gtdb_source], tmp_path / "meta.sqlite", release=232)["path"]
        )
        before = path.read_bytes()
        with pytest.raises(FileNotFoundError):
            update_metadata_db(
                path, path, taxonomy=[TaxonomySource("gone", tmp_path / "gone.tsv")]
            )
        assert path.read_bytes() == before
        assert not list(tmp_path.glob("*.tmp"))

    def test_update_refuses_a_foreign_file(self, tmp_path: Path, uhgv_file: Path) -> None:
        foreign = tmp_path / "foreign.sqlite"
        connection = sqlite3.connect(foreign)
        connection.execute("CREATE TABLE something (x)")
        connection.commit()
        connection.close()
        with pytest.raises(ValueError, match="not a sylphd metadata database"):
            update_metadata_db(
                foreign,
                tmp_path / "out.sqlite",
                taxonomy=[TaxonomySource("UHGV_default", uhgv_file)],
            )
        assert not (tmp_path / "out.sqlite").exists()


def _key_file(path: Path, keys: int, prefix: str, extra: str = "") -> Path:
    """A sylph-tax file with ``keys`` rows ``<prefix>-01`` ... plus ``extra`` lines."""
    text = "".join(f"{prefix}-{index:02d}\tlineage-{index}\n" for index in range(1, keys + 1))
    path.write_text(text + extra, newline="")
    return path


class TestOverlaps:
    """Overlapping sources are allowed but counted, warned about, and reported."""

    def _two_sources(self, tmp_path: Path) -> tuple[Path, Path]:
        first = _key_file(tmp_path / "first.tsv", 7, "SHARED")
        # keys 3..9 overlap with `first` (7 keys), `ONLY-01` does not
        second = _key_file(
            tmp_path / "second.tsv", 9, "SHARED", extra="ONLY-01\tlineage\n"
        )
        return first, second

    def test_two_imported_sources_warn_once_per_pair(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        first, second = self._two_sources(tmp_path)
        output = tmp_path / "meta.sqlite"
        stats = build_metadata_db(
            [],
            output,
            taxonomy=[
                TaxonomySource("First", first),
                TaxonomySource("Second", second),
            ],
        )
        captured = capsys.readouterr()
        assert captured.err.count("warning") == 1  # exactly one warning for the pair
        warning = captured.err.strip()
        assert "'First'" in warning and "'Second'" in warning
        assert "7 key(s)" in warning
        # the first five shared keys, sorted
        assert "examples: SHARED-01, SHARED-02, SHARED-03, SHARED-04, SHARED-05" in warning
        assert "the source imported later (Second) wins for these keys" in warning
        assert "import only one of them, or rebuild without one" in warning

        assert stats["taxonomy_overlaps"] == [
            {
                "source": "Second",
                "other": "First",
                "keys": 7,
                "examples": ["SHARED-01", "SHARED-02", "SHARED-03", "SHARED-04", "SHARED-05"],
                "later": "Second",
            }
        ]
        with MetadataDB(output) as db:
            outputs = db.taxonomy_overlaps()
        assert outputs == [
            {
                "sources": ["First", "Second"],
                "keys": 7,
                "examples": ["SHARED-01", "SHARED-02", "SHARED-03", "SHARED-04", "SHARED-05"],
            }
        ]

    def test_overlap_with_the_virtual_gtdb_source(
        self, tmp_path: Path, gtdb_source: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The virtual source is counted on the accession, as ``resolve`` looks it up."""
        path = tmp_path / "local.tsv"
        path.write_text(
            "GCF_000005845.2\tvFAM-1;root\n"
            "GCF_964372435.1\tvFAM-2;root\n"  # not an accession of the file
            "SHARED-01\tvFAM-3;root\n",
            newline="",
        )
        output = tmp_path / "meta.sqlite"
        stats = build_metadata_db(
            [gtdb_source],
            output,
            release=232,
            taxonomy=[TaxonomySource("Local", path)],
        )
        warning = capsys.readouterr().err
        assert "'GTDB_r232'" in warning and "'Local'" in warning
        assert "1 key(s)" in warning
        assert "examples: GCF_000005845.2" in warning
        assert "the source imported later (Local) wins" in warning
        assert stats["taxonomy_overlaps"][0]["other"] == "GTDB_r232"
        with MetadataDB(output) as db:
            assert db.taxonomy_overlaps() == [
                {
                    "sources": ["GTDB_r232", "Local"],
                    "keys": 1,
                    "examples": ["GCF_000005845.2"],
                }
            ]
        # the counts are in the file, in both directions
        connection = sqlite3.connect(output)
        try:
            assert sorted(connection.execute("SELECT keys FROM taxonomy_overlap")) == [
                (1,),
                (1,),
            ]
        finally:
            connection.close()

    def test_disjoint_sources_do_not_warn(
        self, tmp_path: Path, gtdb_source: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        first = _key_file(tmp_path / "first.tsv", 3, "AAA")
        second = _key_file(tmp_path / "second.tsv", 4, "BBB")
        output = tmp_path / "meta.sqlite"
        stats = build_metadata_db(
            [gtdb_source],
            output,
            release=232,
            taxonomy=[
                TaxonomySource("First", first),
                TaxonomySource("Second", second),
            ],
        )
        captured = capsys.readouterr()
        assert "warning" not in captured.err
        assert "warning" not in captured.out
        assert stats["taxonomy_overlaps"] == []
        with MetadataDB(output) as db:
            assert db.taxonomy_overlaps() == []

    def test_update_warns_about_the_source_it_adds(
        self, tmp_path: Path, gtdb_source: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        first, second = self._two_sources(tmp_path)
        path = tmp_path / "meta.sqlite"
        build_metadata_db(
            [gtdb_source],
            path,
            release=232,
            taxonomy=[TaxonomySource("First", first)],
        )
        capsys.readouterr()  # drop the build's own (empty) output
        stats = update_metadata_db(
            path, path, taxonomy=[TaxonomySource("Second", second)]
        )
        warning = capsys.readouterr().err
        assert warning.count("warning") == 1
        assert "'First'" in warning and "'Second'" in warning
        assert "7 key(s)" in warning
        assert stats["taxonomy_overlaps"][0]["later"] == "Second"
        with MetadataDB(path) as db:
            sources = db.taxonomy_sources()
            assert [source["name"] for source in sources] == [
                "GTDB_r232",
                "First",
                "Second",
            ]
            assert db.taxonomy_overlaps() == [
                {
                    "sources": ["First", "Second"],
                    "keys": 7,
                    "examples": ["SHARED-01", "SHARED-02", "SHARED-03", "SHARED-04", "SHARED-05"],
                }
            ]

    def test_replacing_a_source_refreshes_the_counts(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        first, second = self._two_sources(tmp_path)
        path = tmp_path / "meta.sqlite"
        build_metadata_db(
            [],
            path,
            taxonomy=[TaxonomySource("First", first), TaxonomySource("Second", second)],
        )
        capsys.readouterr()  # the build's own overlap warning
        disconnected = _key_file(tmp_path / "disjoint.tsv", 2, "NOPE")
        stats = update_metadata_db(
            path, path, taxonomy=[TaxonomySource("Second", disconnected)]
        )
        assert stats["taxonomy_overlaps"] == []  # the replacement is disjoint
        with MetadataDB(path) as db:
            assert db.taxonomy_overlaps() == []
        assert "warning" not in capsys.readouterr().err

    def test_an_old_v3_file_gains_the_counts_on_update(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A file written before ``taxonomy_overlap`` reads and is filled by --update."""
        first, second = self._two_sources(tmp_path)
        path = tmp_path / "meta.sqlite"
        build_metadata_db(
            [],
            path,
            taxonomy=[TaxonomySource("First", first), TaxonomySource("Second", second)],
        )
        # what the first release of format 3 wrote: entries but no overlap table
        connection = sqlite3.connect(path)
        connection.execute("DROP TABLE taxonomy_overlap")
        connection.commit()
        connection.close()
        with MetadataDB(path) as db:
            assert db.taxonomy_overlaps() == []
        capsys.readouterr()
        stats = update_metadata_db(path, path)
        assert stats["taxonomy_overlaps"] == []  # nothing imported, so no warning
        with MetadataDB(path) as db:
            assert db.taxonomy_overlaps() == [
                {
                    "sources": ["First", "Second"],
                    "keys": 7,
                    "examples": ["SHARED-01", "SHARED-02", "SHARED-03", "SHARED-04", "SHARED-05"],
                }
            ]
        assert "warning" not in capsys.readouterr().err

    def test_info_reports_the_counts(
        self, tmp_path: Path, gtdb_source: Path
    ) -> None:
        first, second = self._two_sources(tmp_path)
        path = tmp_path / "meta.sqlite"
        build_metadata_db(
            [gtdb_source],
            path,
            release=232,
            taxonomy=[TaxonomySource("First", first), TaxonomySource("Second", second)],
        )
        from sylphd.server import ServerConfig, create_app

        app = create_app(ServerConfig(db_path=ECOLI_TWO_STAGE, meta_path=path))
        with TestClient(app) as client:
            metadata = client.get("/info").json()["metadata"]
        assert [source["name"] for source in metadata["taxonomy_sources"]] == [
            "GTDB_r232",
            "First",
            "Second",
        ]
        assert metadata["taxonomy_overlaps"] == [
            {
                "sources": ["First", "Second"],
                "keys": 7,
                "examples": ["SHARED-01", "SHARED-02", "SHARED-03", "SHARED-04", "SHARED-05"],
            }
        ]

    def test_sylphd_info_prints_the_counts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """``sylphd info`` follows the JSON, so a fake client covers the layout."""
        document = {
            "database": {
                "name": "db",
                "size": 1,
                "genomes": 1,
                "c": 200,
                "k": 31,
                "screen_c": None,
                "two_stage": False,
            },
            "metadata": {
                "file": "meta.sqlite",
                "release": "232",
                "rows": 2,
                "built": "2026-01-01T00:00:00+00:00",
                "columns": ["gtdb_taxonomy"],
                "taxonomy_sources": [
                    {"name": "First", "kind": "sylphtax", "keys": 9},
                    {"name": "Second", "kind": "sylphtax", "keys": 10},
                ],
                "taxonomy_overlaps": [
                    {"sources": ["First", "Second"], "keys": 1234, "examples": ["K1", "K2"]},
                ],
            },
            "limits": {},
            "version": "0.0.0",
            "pysylph_version": "0.0.0",
            "python_version": "3.14.0",
        }

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *info: object) -> None:
                return None

            def info(self) -> dict[str, object]:
                return document

        monkeypatch.setattr(cli, "_client_from_args", lambda args: FakeClient())
        assert cli.main(["info"]) == 0
        out = capsys.readouterr().out
        assert "    First: 9 keys" in out
        assert "    Second: 10 keys" in out
        assert "path:" not in out
        assert "metadata: meta.sqlite" in out
        assert "  taxonomy overlaps: 1" in out
        assert "    First and Second: 1,234 shared keys; examples: K1, K2; the source imported later wins" in out


class TestCliSpecs:
    def test_catalogue_name(self) -> None:
        spec = parse_taxonomy_spec("UHGV_ictv")
        assert spec.name == "UHGV_ictv"
        assert spec.path is None
        assert spec.download is True

    def test_name_equals_path(self, tmp_path: Path) -> None:
        source = tmp_path / "uhgv_default_metadata.tsv.gz"
        source.write_text("k\tl\n")
        spec = parse_taxonomy_spec(f"UHGV={source}")
        assert spec.name == "UHGV"
        assert spec.path == source
        assert spec.download is False

    def test_bare_path(self, tmp_path: Path) -> None:
        source = tmp_path / "fungi_refseq_2025-10-11_metadata.tsv.gz"
        source.write_text("k\tl\n")
        spec = parse_taxonomy_spec(str(source))
        assert spec.name == "fungi_refseq_2025-10-11_metadata"
        assert spec.path == source

    def test_errors(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="unknown taxonomy"):
            parse_taxonomy_spec("UHGV_nope")
        with pytest.raises(FileNotFoundError):
            parse_taxonomy_spec("name=/nope/file.tsv")
        with pytest.raises(FileNotFoundError):
            parse_taxonomy_spec(str(tmp_path / "nope.tsv"))
        with pytest.raises(ValueError, match="empty"):
            parse_taxonomy_spec("  ")

    def test_setup_with_a_local_path(
        self, tmp_path: Path, gtdb_source: Path, uhgv_file: Path, capsys
    ) -> None:
        output = tmp_path / "meta.sqlite"
        code = cli.main(
            [
                "setup",
                "-m",
                str(gtdb_source),
                "--taxonomy",
                f"UHGV={uhgv_file}",
                "-o",
                str(output),
                "--release",
                "232",
                "-q",
            ]
        )
        assert code == 0, capsys.readouterr().err
        with MetadataDB(output) as db:
            assert [source["name"] for source in db.taxonomy_sources()] == [
                "GTDB_r232",
                "UHGV",
            ]

    def test_update_needs_no_metadata_files(
        self, tmp_path: Path, gtdb_source: Path, uhgv_file: Path, capsys
    ) -> None:
        path = _to_v2(
            build_metadata_db([gtdb_source], tmp_path / "meta.sqlite", release=232)["path"]
        )
        code = cli.main(
            ["setup", "--update", "--taxonomy", str(uhgv_file), "-o", str(path), "-q"]
        )
        assert code == 0, capsys.readouterr().err
        with MetadataDB(path) as db:
            assert db.format_version == "3"
            assert [source["name"] for source in db.taxonomy_sources()] == [
                "GTDB_r232",
                "uhgv_default_metadata",
            ]

    def test_setup_downloads_a_sylphtax_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        called: dict[str, object] = {}

        def fake_download(entry, directory, **kwargs):
            called["name"] = entry.name
            called["directory"] = Path(directory)
            path = Path(directory) / entry.filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("UHGV-1\tvFAM-1\tHost\n")
            return path

        monkeypatch.setattr(cli, "download_taxonomy", fake_download)
        output = tmp_path / "meta.sqlite"
        code = cli.main(
            ["setup", "--taxonomy", "UHGV_ictv", "-o", str(output), "-q"]
        )
        assert code == 0, capsys.readouterr().err
        assert called["name"] == "UHGV_ictv"
        # the default cache directory is <output dir>/taxonomy
        assert called["directory"] == tmp_path / "taxonomy"
        with MetadataDB(output) as db:
            sources = db.taxonomy_sources()
            assert [source["name"] for source in sources] == ["UHGV_ictv"]
            assert sources[0]["origin"] == TAXONOMIES["UHGV_ictv"].mirrors[0]

    def test_setup_skips_the_redundant_catalogue_gtdb_file(
        self,
        tmp_path: Path,
        gtdb_source: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys,
    ) -> None:
        """The CLI marks catalogue names, so the same-release GTDB file is skipped."""

        def fake_download(entry, directory, **kwargs):
            path = Path(directory) / entry.filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("GCA_1\td__Bacteria\n", newline="")
            return path

        monkeypatch.setattr(cli, "download_taxonomy", fake_download)
        output = tmp_path / "meta.sqlite"
        code = cli.main(
            [
                "setup",
                "-m",
                str(gtdb_source),
                "--taxonomy",
                "GTDB_r232",
                "--release",
                "232",
                "-o",
                str(output),
            ]
        )
        captured = capsys.readouterr()
        assert code == 0, captured.err
        assert "GTDB_r232: skipped" in captured.err
        with MetadataDB(output) as db:
            assert [
                (source["name"], source["kind"]) for source in db.taxonomy_sources()
            ] == [("GTDB_r232", "metadata")]

    def test_duplicate_names_are_refused(
        self, tmp_path: Path, gtdb_source: Path, uhgv_file: Path
    ) -> None:
        code = cli.main(
            [
                "setup",
                "--taxonomy",
                f"same={uhgv_file}",
                "--taxonomy",
                f"same={uhgv_file}",
                "-o",
                str(tmp_path / "meta.sqlite"),
                "-q",
            ]
        )
        assert code == 1

    def test_setup_needs_a_source(self, tmp_path: Path) -> None:
        assert cli.main(["setup", "-o", str(tmp_path / "meta.sqlite"), "-q"]) == 1


class TestCatalogue:
    def test_entries_are_consistent(self) -> None:
        assert TAXONOMIES
        size_only = []
        for name, entry in TAXONOMIES.items():
            assert entry.name == name
            assert entry.filename.endswith(".tsv.gz")
            assert entry.mirrors
            for mirror in entry.mirrors:
                assert mirror.startswith(("http://", "https://"))
                assert mirror.endswith("/" + entry.filename)
            assert entry.size is None or entry.size > 0
            assert entry.md5 is None or len(entry.md5) == 32
            # Only GlobDB r232 may be checked by size alone: its publisher
            # offers no checksum.  Every other entry must be pinned by MD5
            # (download_taxonomy refuses an entry without one).
            if entry.md5 is None:
                size_only.append(name)
                assert entry.size is not None
            if entry.size_only:
                assert entry.md5 is None
                assert entry.size is not None
            # the pairing table of the design note
            assert download.paired_databases(name) or name in {
                "GlobDB_r226",
                "FungiRefSeq-2024-07-25",
            }
        assert size_only == ["GlobDB_r232"]

    def test_target_pairings(self) -> None:
        assert DATABASES["gtdb-r232-c200-dbv2.syl2db"].taxonomy == "GTDB_r232"
        assert DATABASES["uhgv_c200_dbv1.syldb"].taxonomy == "UHGV_default"
        assert DATABASES["uhgv_c200_dbv1.syldb"].taxonomy_alternatives == (
            "UHGV_ictv",
        )
        assert DATABASES["fungi-refseq-2025-10-11-c200-dbv1.syldb"].taxonomy == (
            "FungiRefSeq-latest"
        )
        assert DATABASES["uhgg_all_c200_v0.3.0.syldb"].taxonomy is None

    def test_table(self) -> None:
        text = taxonomy_table()
        assert "UHGV_default" in text
        assert "FungiRefSeq-latest" in text
        assert "GTDB_r232" in text

    def test_lookup_error(self) -> None:
        with pytest.raises(ValueError, match="unknown taxonomy"):
            download.lookup_taxonomy("nope")

