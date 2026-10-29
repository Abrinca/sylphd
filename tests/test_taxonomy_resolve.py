"""Hit → lineage resolution (phase 3): probe rules, precedence, ``tax_*`` rows.

``sylph_tax.sylph_to_taxprof`` looks a hit up under a fixed list of key
guesses; :func:`sylphd.metadata.sylphtax_probes` must reproduce that list
(``docs/project/multi-db-design.md`` B3) and :meth:`MetadataDB.resolve` must
apply sylph-tax's precedence: within a pass the first probe that exists wins,
a pass-1 match overrides a pass-0 match, and for a key in several sources the
lineage comes from the largest ``ord`` while the host comes from the largest
``ord`` that has one.

The parity tests against the real sylph-tax live in
``tests/test_sylphtax_parity.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import ECOLI_TWO_STAGE, MIX_SKETCH
from starlette.testclient import TestClient

from sylphd.metadata import (
    HIT_TAXONOMY_COLUMNS,
    METADATA_COLUMNS,
    MetadataDB,
    SylphTaxProbes,
    TaxonomySource,
    build_metadata_db,
    sylphtax_probes,
)

# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------


class TestProbes:
    """Every rule of the probe table, with realistic database names."""

    def test_gtdb_two_stage_path(self) -> None:
        probes = sylphtax_probes(
            "gtdb_genomes_reps_r232/database/GCA/047/199/115/"
            "GCA_047199115.1_genomic.fna.gz",
            "GCA_047199115.1 ctg1",
        )
        # pass 0 cuts "_genomic" off the basename and the path stays irrelevant
        assert probes.pass0[:4] == (
            "GCA_047199115.1",
            "GCA_047199115.1.gz",
            "GCA_047199115",
            "GCA_047199115.1",
        )
        # pass 1 keeps the whole basename
        assert probes.pass1[0] == "GCA_047199115.1_genomic.fna.gz"
        assert probes.pass1[3] == "GCA_047199115.1"

    def test_asm_in_the_basename(self) -> None:
        probes = sylphtax_probes(
            "GCF_000005845.2_ASM584v2_genomic.fna.gz", "NC_000913.3"
        )
        # "ASM" wins over "_genomic": the split is at "_ASM"
        assert probes.pass0[0] == "GCF_000005845.2"
        assert probes.pass0[2] == "GCF_000005845"
        assert probes.pass1[0] == "GCF_000005845.2_ASM584v2_genomic.fna.gz"

    def test_asm_anywhere_in_the_path_cuts_the_basename(self) -> None:
        """``"ASM" in file_name`` looks at the full path, not the basename."""
        probes = sylphtax_probes("/db/asm_versions/fungi.fna", "contig")
        # the basename has no "_ASM", so the split returns the basename whole
        assert probes.pass0[0] == "fungi.fna"
        probes = sylphtax_probes("/db/ASM/fungi_ASM1.fna", "contig")
        assert probes.pass0[0] == "fungi"
        # "ASM" only in the path: the basename is *not* cut at "_genomic"
        probes = sylphtax_probes("/db/ASM/fungi_genomic.fna", "contig")
        assert probes.pass0[0] == "fungi_genomic.fna"

    def test_fungi_refseq_keeps_the_assembly_name(self) -> None:
        """The fungal keys are not normalised: ``_genomic`` is cut, nothing else."""
        probes = sylphtax_probes(
            "ncbi_dataset/data/GCF_010093815.1/GCF_010093815.1_Psehy1_genomic.fna",
            "NW_024067585.1",
        )
        assert probes.pass0[0] == "GCF_010093815.1_Psehy1"
        assert probes.pass0[2] == "GCF_010093815"

    def test_fungi_bare_accession(self) -> None:
        probes = sylphtax_probes(
            "ncbi_dataset/data/GCF_000142805.1/GCF_000142805.1_genomic.fna.gz",
            "NW_1",
        )
        assert probes.pass0[0] == "GCF_000142805.1"
        # the pass-0 glob drops the version: what GlobDB keys look like
        assert probes.pass0[2] == "GCF_000142805"

    def test_uhgv_contig_probes(self) -> None:
        """UHGV names every genome ``votus_full.fna.gz``; only the contig works."""
        probes = sylphtax_probes("votus_full.fna.gz", "UHGV-0320751 species=vOTU-125334")
        assert probes.pass0[3] == "UHGV-0320751"
        assert probes.pass1[3] == "UHGV-0320751"
        # the file probes are constant for a UHGV database and never match
        assert probes.pass0[0] == "votus_full.fna.gz"
        assert probes.pass0[2] == "votus_full"

    def test_imgvr_pipe_separated_contig(self) -> None:
        probes = sylphtax_probes(
            "IMGVR_UViG_2504643025_000001|Ga0525864_0000001",
            "IMGVR_UViG_2504643025_000001|Ga0525864_0000001|Ga0525864",
        )
        # pass 0 keeps the part before the first "|" ...
        assert probes.pass0[3] == "IMGVR_UViG_2504643025_000001"
        # ... pass 1 has no "|" rule, so only the space split applies
        assert probes.pass1[3] == "IMGVR_UViG_2504643025_000001|Ga0525864_0000001|Ga0525864"

    def test_pass0_contig_splits_on_a_space_only(self) -> None:
        probes = sylphtax_probes("x.fna", "UHGV-1\tspecies=vOTU-9")
        assert probes.pass0[3] == "UHGV-1\tspecies=vOTU-9"
        assert probes.pass1[3] == "UHGV-1"

    def test_suffix_probes_come_last_and_in_order(self) -> None:
        probes = sylphtax_probes("sample.v1.fna", "c")
        assert probes.pass0[4:] == ("sample.v1.fna", "sample.v1.fna", "sample.v1")
        probes = sylphtax_probes("sample.v1.fasta", "c")
        assert probes.pass0[4:] == ("sample.v1", "sample.v1", "sample.v1.fasta")

    def test_no_taxonomy_uses_pass1_values(self) -> None:
        probes = sylphtax_probes("/x/GCF_1_genomic.fna.gz", "ctg 1")
        assert probes.no_taxonomy == "NO_TAXONOMY;t__GCF_1_genomic.fna.gz:ctg"

    def test_empty_names_do_not_raise(self) -> None:
        probes = sylphtax_probes("", "")
        assert isinstance(probes, SylphTaxProbes)
        assert probes.contig_name == ""
        assert probes.no_taxonomy == "NO_TAXONOMY;t__:"

    def test_keys_are_unique_and_ordered(self) -> None:
        probes = sylphtax_probes("a_genomic.fna.gz", "c")
        keys = probes.probe_keys()
        assert len(keys) == len(set(keys))
        # pass 0 first, then the pass-1 keys it did not already have
        assert keys[:2] == probes.pass0[:2]
        assert set(keys) == set(probes.pass0) | set(probes.pass1)


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def _write_source(path: Path, rows: list[tuple[str, str, str | None]]) -> Path:
    path.write_text(
        "".join(
            f"{key}\t{lineage}\t{host}\n" if host is not None else f"{key}\t{lineage}\n"
            for key, lineage, host in rows
        ),
        newline="",
    )
    return path


@pytest.fixture()
def resolve_db(tmp_path: Path) -> Path:
    """A metadata file with three sources, no GTDB metadata."""
    first = _write_source(
        tmp_path / "first.tsv",
        [
            ("K_ord", "one;lineage", "Host1"),
            ("K_host", "one;host", "Host1"),
            ("K_filtered", "one;filtered", "Host1"),
            ("K_shared_host", "one;shared", "Host1"),
            ("S", "pass0;asm", None),
            ("K_gf", "pass0;gf", None),
            ("k.fa.gz", "pass0;gfgz", None),
            ("Y", "pass0;glob", None),
            ("C", "pass0;contig", None),
            ("Z.v1", "pass0;split-fa", None),
            ("GCF_003697165", "glob;version-stripped", None),
            ("UHGV-0320751", "vFAM-1;vOTU-1", None),
            ("IMGVR_UViG_2504643025_000001", "r__Viruses;k__X", None),
            # the empty key is a valid key (setup stores it, sylph-tax accepts it)
            ("", "root;empty-key", "Host-empty"),
        ],
    )
    second = _write_source(
        tmp_path / "second.tsv",
        [
            ("K_ord", "two;lineage", None),
            ("K_host", "two;host", None),
            ("K_filtered", "two;filtered", None),
            ("K_shared_host", "two;shared", "Host2"),
            ("S_ASM1_genomic.fna.gz", "pass1;override", None),
        ],
    )
    third = _write_source(tmp_path / "third.tsv", [("K_shared_host", "three", None)])
    output = tmp_path / "meta.sqlite"
    build_metadata_db(
        [],
        output,
        taxonomy=[
            TaxonomySource("first", first),
            TaxonomySource("second", second),
            TaxonomySource("third", third),
        ],
    )
    return output


def _taxonomies(db: MetadataDB, hits: list[tuple[str, str]], **kwargs):
    return db.resolve(hits, **kwargs)


class TestResolve:
    def test_pass1_overrides_pass0(self, resolve_db: Path) -> None:
        """``sylph_to_taxprof.main`` has no ``break``: the later pass wins."""
        with MetadataDB(resolve_db) as db:
            (taxonomy,) = _taxonomies(
                db, [("S_ASM1_genomic.fna.gz", "contig")]
            )
        assert taxonomy.key == "S_ASM1_genomic.fna.gz"
        assert taxonomy.lineage == "pass1;override"
        assert taxonomy.source == "second"

    def test_pass0_match_survives_a_pass1_miss(self, resolve_db: Path) -> None:
        with MetadataDB(resolve_db) as db:
            (taxonomy,) = _taxonomies(db, [("K_gf_genomic.fna.gz", "nope")])
        assert taxonomy.key == "K_gf"
        assert taxonomy.lineage == "pass0;gf"

    def test_gf_before_glob_and_contig(self, resolve_db: Path) -> None:
        # "C|x" keeps the pass-1 contig probe out of the way: pass 0's gf wins.
        with MetadataDB(resolve_db) as db:
            (taxonomy,) = _taxonomies(db, [("K_gf_genomic.fna.gz", "C|x")])
        assert taxonomy.key == "K_gf"

    def test_gf_gz_before_glob(self, resolve_db: Path) -> None:
        with MetadataDB(resolve_db) as db:
            (taxonomy,) = _taxonomies(db, [("k.fa", "C")])
        # pass 0: "k.fa" (miss), "k.fa.gz" (hit) before "k" (glob)
        assert taxonomy.key == "k.fa.gz"
        assert taxonomy.lineage == "pass0;gfgz"

    def test_glob_before_contig(self, resolve_db: Path) -> None:
        # pass 0: gf "Y.fna", glob "Y", contig "C"; pass 1 finds "C|x" neither.
        with MetadataDB(resolve_db) as db:
            (taxonomy,) = _taxonomies(db, [("Y.fna", "C|x")])
        assert taxonomy.key == "Y"
        assert taxonomy.lineage == "pass0;glob"

    def test_contig_before_the_suffix_splits(self, resolve_db: Path) -> None:
        with MetadataDB(resolve_db) as db:
            (taxonomy,) = _taxonomies(db, [("Z.v1.fasta", "C")])
        assert taxonomy.key == "C"
        assert taxonomy.lineage == "pass0;contig"

    def test_glob_resolves_a_version_stripped_key(self, resolve_db: Path) -> None:
        with MetadataDB(resolve_db) as db:
            (taxonomy,) = _taxonomies(
                db, [("GCF_003697165.2_genomic.fna.gz", "ctg")]
            )
        assert taxonomy.key == "GCF_003697165"
        assert taxonomy.lineage == "glob;version-stripped"

    def test_uhgv_contig(self, resolve_db: Path) -> None:
        with MetadataDB(resolve_db) as db:
            (taxonomy,) = _taxonomies(
                db, [("votus_full.fna.gz", "UHGV-0320751 species=vOTU-125334")]
            )
        assert taxonomy.key == "UHGV-0320751"
        assert taxonomy.lineage == "vFAM-1;vOTU-1"

    def test_imgvr_contig_before_the_pipe(self, resolve_db: Path) -> None:
        with MetadataDB(resolve_db) as db:
            (taxonomy,) = _taxonomies(
                db,
                [
                    (
                        "IMGVR_UViG_2504643025_000001|Ga0525864_0000001",
                        "IMGVR_UViG_2504643025_000001|Ga0525864_0000001",
                    )
                ],
            )
        assert taxonomy.key == "IMGVR_UViG_2504643025_000001"

    def test_largest_ord_wins(self, resolve_db: Path) -> None:
        with MetadataDB(resolve_db) as db:
            (taxonomy,) = _taxonomies(db, [("K_ord_genomic.fna.gz", "c")])
        assert taxonomy.source == "second"
        assert taxonomy.lineage == "two;lineage"
        # the host is chosen independently: "first" is the last source with one
        assert taxonomy.host == "Host1"

    def test_host_comes_from_the_last_source_that_has_one(
        self, resolve_db: Path
    ) -> None:
        """Lineage and host are chosen independently by ``ord`` (B3)."""
        with MetadataDB(resolve_db) as db:
            (taxonomy,) = _taxonomies(db, [("K_host_genomic.fna.gz", "c")])
        assert taxonomy.lineage == "two;host"
        # ... even though the row without a host is the later one
        assert taxonomy.host == "Host1"
        assert taxonomy.source == "second"

    def test_a_later_host_replaces_an_earlier_one(self, resolve_db: Path) -> None:
        with MetadataDB(resolve_db) as db:
            (taxonomy,) = _taxonomies(db, [("K_shared_host_genomic.fna.gz", "c")])
        assert taxonomy.lineage == "three"
        assert taxonomy.host == "Host2"  # third has no host column
        assert taxonomy.source == "third"

    def test_sources_restrict_the_search(self, resolve_db: Path) -> None:
        with MetadataDB(resolve_db) as db:
            (taxonomy,) = _taxonomies(
                db, [("K_filtered_genomic.fna.gz", "c")], sources=["first"]
            )
            (other,) = _taxonomies(
                db, [("K_filtered_genomic.fna.gz", "c")], sources=["second"]
            )
        assert taxonomy.lineage == "one;filtered"
        assert taxonomy.host == "Host1"
        assert other.lineage == "two;filtered"

    def test_a_key_only_in_an_excluded_source_is_a_miss(
        self, resolve_db: Path
    ) -> None:
        with MetadataDB(resolve_db) as db:
            (taxonomy,) = _taxonomies(
                db, [("S_ASM1_genomic.fna.gz", "c")], sources=["first"]
            )
        # "S" exists in "first", and pass 1 now has nothing to override it
        assert taxonomy.key == "S"
        assert taxonomy.lineage == "pass0;asm"

    def test_a_miss_is_all_none(self, resolve_db: Path) -> None:
        with MetadataDB(resolve_db) as db:
            (taxonomy,) = _taxonomies(db, [("nothing.fna.gz", "ctg")])
        assert taxonomy.columns() == {
            "tax_source": None,
            "tax_key": None,
            "tax_lineage": None,
            "tax_host": None,
        }

    def test_the_empty_key_resolves(self, resolve_db: Path) -> None:
        """A pass-0 contig probe can be "" (``Contig_name`` starting with "|")."""
        with MetadataDB(resolve_db) as db:
            (taxonomy,) = _taxonomies(db, [("unknown.fna", "|extra")])
        assert taxonomy.key == ""
        assert (taxonomy.source, taxonomy.lineage, taxonomy.host) == (
            "first",
            "root;empty-key",
            "Host-empty",
        )

    def test_case_sensitivity(self, resolve_db: Path) -> None:
        with MetadataDB(resolve_db) as db:
            # GTDB accessions are normalised upstream, sylph-tax keys never are
            (taxonomy,) = _taxonomies(db, [("k_gf_genomic.fna.gz", "c")])
        assert taxonomy.lineage is None

    def test_many_hits_are_batched(self, resolve_db: Path) -> None:
        """>500 distinct keys per sample exercise the chunked lookups."""
        hits = [(f"genome-{index}_genomic.fna.gz", "c") for index in range(1200)]
        with MetadataDB(resolve_db) as db:
            assert all(taxon.lineage is None for taxon in db.resolve(hits))
            first = _write_source(
                Path(resolve_db).parent / "extra.tsv",
                [(f"genome-{index}", f"l{index}", None) for index in range(1200)],
            )
            # a second file in the same directory, added by rebuilding
            sources = [
                TaxonomySource(name, Path(resolve_db).parent / f"{name}.tsv")
                for name in ("first", "second", "third")
            ]
            output = Path(resolve_db).parent / "many.sqlite"
            build_metadata_db(
                [], output, taxonomy=[*sources, TaxonomySource("extra", first)]
            )
        with MetadataDB(output) as db:
            resolved = db.resolve(hits)
        assert len(resolved) == 1200
        assert [taxon.key for taxon in resolved] == [
            f"genome-{index}" for index in range(1200)
        ]

    def test_empty_hit_list(self, resolve_db: Path) -> None:
        with MetadataDB(resolve_db) as db:
            assert db.resolve([]) == []

    def test_virtual_gtdb_source(self, tmp_path: Path) -> None:
        values = {column: "none" for column in METADATA_COLUMNS}
        values["gtdb_taxonomy"] = "d__Bacteria;p__Test"
        source = tmp_path / "gtdb.tsv"
        source.write_text(
            "\t".join(["accession", *METADATA_COLUMNS])
            + "\n"
            + "\t".join(["GCA_000000001.1", *[values[c] for c in METADATA_COLUMNS]])
            + "\n"
        )
        output = tmp_path / "meta.sqlite"
        build_metadata_db([source], output, release=232)
        with MetadataDB(output) as db:
            assert db.taxonomy_columns == HIT_TAXONOMY_COLUMNS  # virtual source
            (taxonomy,) = db.resolve([("GCA_000000001.1_genomic.fna.gz", "c")])
        assert taxonomy.lineage == "d__Bacteria;p__Test"
        assert taxonomy.source == "GTDB_r232"

    def test_gtdb_source_loses_to_a_later_file(self, tmp_path: Path) -> None:
        values = {column: "none" for column in METADATA_COLUMNS}
        values["gtdb_taxonomy"] = "d__Bacteria;p__From_metadata"
        source = tmp_path / "gtdb.tsv"
        source.write_text(
            "\t".join(["accession", *METADATA_COLUMNS])
            + "\n"
            + "\t".join(["GCA_000000001.1", *[values[c] for c in METADATA_COLUMNS]])
            + "\n"
        )
        other = _write_source(
            tmp_path / "other.tsv", [("GCA_000000001.1", "later;lineage", "Host")]
        )
        output = tmp_path / "meta.sqlite"
        build_metadata_db(
            [source], output, release=232, taxonomy=[TaxonomySource("other", other)]
        )
        with MetadataDB(output) as db:
            (taxonomy,) = db.resolve([("GCA_000000001.1_genomic.fna.gz", "c")])
            (gtdb_only,) = db.resolve(
                [("GCA_000000001.1_genomic.fna.gz", "c")], sources=["GTDB_r232"]
            )
        assert (taxonomy.source, taxonomy.lineage) == ("other", "later;lineage")
        assert taxonomy.host == "Host"
        assert gtdb_only.lineage == "d__Bacteria;p__From_metadata"


# ---------------------------------------------------------------------------
# the server puts the four columns on every row
# ---------------------------------------------------------------------------


def _source_metadata(tmp_path: Path) -> Path:
    """Taxonomy sources keyed by the genome names of the ecoli test database."""
    first = _write_source(
        tmp_path / "first.tsv",
        [
            ("e.coli-K12.fasta.gz", "vFAM-1;vOTU-K12", "Homo sapiens"),
            ("clodf13.fna.gz", "vFAM-2;vOTU-CLO", None),
        ],
    )
    second = _write_source(
        tmp_path / "second.tsv",
        [("e.coli-K12.fasta.gz", "vFAM-9;vOTU-OTHER", "Mus musculus")],
    )
    output = tmp_path / "meta.sqlite"
    build_metadata_db(
        [],
        output,
        taxonomy=[
            TaxonomySource("first", first),
            TaxonomySource("second", second),
        ],
    )
    return output


def _post(client: TestClient, payloads: list[tuple[str, bytes]], **params):
    return client.post(
        "/profile",
        data={"params": json.dumps(params)},
        files=[("sketch", (f"{name}.sylsp", data)) for name, data in payloads],
    )


def _app(meta_path: Path):
    from sylphd.server import ServerConfig, create_app

    return TestClient(create_app(ServerConfig(db_path=ECOLI_TWO_STAGE, meta_path=meta_path)))


class TestServerRows:
    def test_info_exposes_the_tax_columns(self, tmp_path: Path) -> None:
        metadata = _source_metadata(tmp_path)
        with _app(metadata) as client:
            columns = client.get("/info").json()["metadata"]["columns"]
        assert columns[-4:] == list(HIT_TAXONOMY_COLUMNS)

    def test_profile_rows_carry_the_taxonomy(self, tmp_path: Path) -> None:
        metadata = _source_metadata(tmp_path)
        with _app(metadata) as client:
            response = _post(client, [("mix", MIX_SKETCH.read_bytes())])
        assert response.status_code == 200, response.text
        rows = response.json()[0]["rows"]
        by_genome = {row["Genome_file"]: row for row in rows}
        ecoli = by_genome["test_files/e.coli-K12.fasta.gz"]
        assert ecoli["tax_source"] == "second"
        assert ecoli["tax_key"] == "e.coli-K12.fasta.gz"
        assert ecoli["tax_lineage"] == "vFAM-9;vOTU-OTHER"
        assert ecoli["tax_host"] == "Mus musculus"
        # the strain level of the tax profile is the key, not the accession
        assert ecoli["accession"] is None

    def test_tax_sources_param_restricts_resolution(self, tmp_path: Path) -> None:
        metadata = _source_metadata(tmp_path)
        with _app(metadata) as client:
            response = _post(
                client,
                [("mix", MIX_SKETCH.read_bytes())],
                tax_sources=["first"],
            )
        assert response.status_code == 200, response.text
        rows = {row["Genome_file"]: row for row in response.json()[0]["rows"]}
        ecoli = rows["test_files/e.coli-K12.fasta.gz"]
        assert (ecoli["tax_source"], ecoli["tax_lineage"]) == (
            "first",
            "vFAM-1;vOTU-K12",
        )

    def test_unknown_tax_source_is_a_400(self, tmp_path: Path) -> None:
        metadata = _source_metadata(tmp_path)
        with _app(metadata) as client:
            response = _post(
                client, [("mix", MIX_SKETCH.read_bytes())], tax_sources=["nope"]
            )
        assert response.status_code == 400
        error = response.json()["error"]
        assert "unknown taxonomy source" in error
        assert "first" in error and "second" in error

    def test_hits_without_taxonomy_do_not_break(self, tmp_path: Path) -> None:
        """The mix sample's genomes need not all be in the sources."""
        metadata = _source_metadata(tmp_path)
        with _app(metadata) as client:
            response = _post(
                client,
                [("mix", MIX_SKETCH.read_bytes())],
                fields=["genome_size", *HIT_TAXONOMY_COLUMNS],
            )
        assert response.status_code == 200, response.text
        assert response.json()[0]["rows"]

    def test_fields_none_leaves_the_tax_columns_out(self, tmp_path: Path) -> None:
        metadata = _source_metadata(tmp_path)
        with _app(metadata) as client:
            response = _post(client, [("mix", MIX_SKETCH.read_bytes())], fields=[])
        row = response.json()[0]["rows"][0]
        assert "tax_lineage" not in row
        assert "gtdb_taxonomy" not in row

    def test_assembly_hits_carry_the_taxonomy(self, tmp_path: Path) -> None:
        metadata = _source_metadata(tmp_path)
        from sylphd.server import SKETCH_CONTENT_TYPE

        with _app(metadata) as client:
            response = client.post(
                "/assembly",
                params={"top": 5, "min_ani": 80.0},
                content=MIX_SKETCH.read_bytes(),
                headers={"Content-Type": SKETCH_CONTENT_TYPE},
            )
        assert response.status_code == 200, response.text
        hits = {hit["genome_file"]: hit for hit in response.json()["hits"]}
        ecoli = hits["test_files/e.coli-K12.fasta.gz"]
        assert ecoli["tax_lineage"] == "vFAM-9;vOTU-OTHER"
        assert ecoli["tax_host"] == "Mus musculus"
