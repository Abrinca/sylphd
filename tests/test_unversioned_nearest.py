"""Unversioned accessions (GlobDB) and the nearest NCBI taxon of genomes without a record.

GlobDB names its genomes ``genomes/GCA_041167105.fa.gz`` with the contig
``GCA_041167105_000000000001``: no version anywhere, while GTDB's metadata is keyed
``GCA_041167105.1``.  Everything is built from tiny fixtures (the omnitax file too);
nothing touches NCBI.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from taxfile import write_taxonomy_file

from sylphd import cli
from sylphd.metadata import METADATA_COLUMNS, MetadataDB, accession_from_genome_name
from sylphd.metadata_build import build_metadata_db, update_metadata_db
from sylphd.profiling import _taxprof_fields, result_rows
from sylphd.taxprofile import aggregate_rows, clade_dict, resolve_taxonomy_columns
from sylphd.taxonomy_nearest import lineage_clades
from sylphd.taxonomy_sources import TaxonomySource, assembly_accession

HEADER = ["accession", *METADATA_COLUMNS]

LINEAGE = "d__Bacteria;p__Pseudomonadota;c__Gammaproteobacteria;o__Burkholderiales;"


def _row(accession: str, lineage: str | None = "d__Bacteria;p__Test", **overrides: str) -> list[str]:
    values = {column: "none" for column in METADATA_COLUMNS}
    values["gtdb_taxonomy"] = lineage if lineage is not None else "none"
    values.update(overrides)
    return [accession, *[values[column] for column in METADATA_COLUMNS]]


def _tsv(path: Path, rows: list[list[str]]) -> Path:
    path.write_text("\n".join("\t".join(r) for r in [HEADER, *rows]) + "\n", newline="")
    return path


def _taxonomy(path: Path, lines: list[str]) -> Path:
    with gzip.open(path, "wt", newline="") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# 2a: unversioned accessions
# ---------------------------------------------------------------------------


def test_globdb_name_shapes() -> None:
    # The shapes of globdb_r232_sylph_v2_c200.syl2db: only NCBI genomes have an accession.
    assert accession_from_genome_name("genomes/GCA_041167105.fa.gz") == "GCA_041167105"
    assert accession_from_genome_name("genomes/MOTU40_002958.fa.gz") is None
    assert accession_from_genome_name("genomes/MGYG000511636.fa.gz") is None
    # Versioned names are unchanged.
    assert accession_from_genome_name("GCA_041167105.1_genomic.fna.gz") == "GCA_041167105.1"


@pytest.fixture()
def gtdb_db(tmp_path: Path) -> Path:
    gtdb = _tsv(
        tmp_path / "gtdb.tsv",
        [
            _row("GB_GCA_041167105.1", LINEAGE + "f__A;g__Alpha;s__Alpha one", ncbi_taxid="111"),
            _row("RS_GCF_003697165.1", LINEAGE + "f__B;g__Beta;s__Beta one", ncbi_taxid="222"),
            # Two GTDB versions of one base: an unversioned name must not guess.
            _row("GB_GCA_000000010.1", LINEAGE + "f__C;g__Gamma;s__Gamma one", ncbi_taxid="333"),
            _row("GB_GCA_000000010.2", LINEAGE + "f__C;g__Gamma;s__Gamma two", ncbi_taxid="334"),
        ],
    )
    taxonomy = _taxonomy(
        tmp_path / "globdb.tsv.gz",
        [
            f"GCA_041167105\t{LINEAGE}f__A;g__Alpha;s__Alpha one",
            f"GCF_003697165\t{LINEAGE}f__B;g__Beta;s__Beta one",
            f"GCA_000000010\t{LINEAGE}f__C;g__Gamma;s__Gamma one",
            f"MOTU40_002958\t{LINEAGE}f__D;g__Delta;s__MOTU_1",
        ],
    )
    output = tmp_path / "meta.sqlite"
    build_metadata_db(
        [gtdb], output, release=232, taxonomy=[TaxonomySource("GlobDB_r232", taxonomy)]
    )
    return output


def _fake_result(name: str, contig: str) -> SimpleNamespace:
    return SimpleNamespace(
        genome_sketch=SimpleNamespace(name=name, description=contig),
        ani=99.0,
        coverage=1.0,
        ani_ci=(98.0, 100.0),
        effective_lambda=1.0,
        lambda_status="ok",
        lambda_ci=(0.9, 1.1),
        median_coverage=1.0,
        mean_coverage=1.0,
        containment=(10, 10),
        ani_naive=99.0,
        taxonomic_abundance=1.0,
        sequence_abundance=1.0,
        kmers_reassigned=0,
    )


def test_unversioned_lookup_resolves_the_single_version(gtdb_db: Path) -> None:
    with MetadataDB(gtdb_db) as db:
        found = db.lookup(["GCA_041167105", "GCF_003697165", "GCA_000000010", "GCA_999"])
        # keyed by what was asked, with the row of the only version
        assert found["GCA_041167105"]["ncbi_taxid"] == 111
        assert found["GCA_041167105"]["gtdb_taxonomy"].endswith("s__Alpha one")
        assert found["GCF_003697165"]["ncbi_taxid"] == 222
        # two versions: ambiguous, no guess; and no such genome
        assert "GCA_000000010" not in found
        assert "GCA_999" not in found
        # a version that does exist still resolves exactly
        assert db.lookup(["GCA_000000010.2"])["GCA_000000010.2"]["ncbi_taxid"] == 334
        # an unversioned name identifies a GTDB database, an ambiguous one does not
        assert db.contains_accessions(["GCA_041167105"]) is True
        assert db.contains_accessions(["GCA_000000010"]) is False
        assert db.contains_accessions(["MOTU40_002958"]) is False


def test_hit_rows_of_a_globdb_database_get_gtdb_metadata(gtdb_db: Path) -> None:
    results = [
        _fake_result("genomes/GCA_041167105.fa.gz", "GCA_041167105_000000000001"),
        _fake_result("genomes/MOTU40_002958.fa.gz", "MOTU40_002958_000000000007"),
        _fake_result("genomes/MGYG000511636.fa.gz", "MGYG000511636_1"),
    ]
    with MetadataDB(gtdb_db) as db:
        rows = result_rows(db, results, "sample", fields=["gtdb_taxonomy", "ncbi_taxid", "tax_source", "tax_key"])
    assert rows[0]["accession"] == "GCA_041167105"
    assert rows[0]["ncbi_taxid"] == 111
    assert rows[0]["gtdb_taxonomy"].endswith("s__Alpha one")
    assert rows[0]["tax_source"] == "GlobDB_r232"
    assert rows[0]["tax_key"] == "GCA_041167105"
    # MAGs have no accession and no GTDB row, but their key resolves in the source
    assert rows[1]["accession"] is None and rows[1]["ncbi_taxid"] is None
    assert rows[1]["tax_key"] == "MOTU40_002958"
    assert rows[2]["tax_source"] is None


def test_overlap_counts_the_unversioned_keys(gtdb_db: Path) -> None:
    with MetadataDB(gtdb_db) as db:
        (pair,) = db.taxonomy_overlaps()
        assert pair["sources"] == ["GTDB_r232", "GlobDB_r232"]
        # GCA_041167105 and GCF_003697165; GCA_000000010 is ambiguous (two versions)
        assert pair["keys"] == 2
        assert pair["examples"] == ["GCA_041167105", "GCF_003697165"]


def test_summary_warns_about_the_unversioned_overlap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gtdb = _tsv(tmp_path / "g.tsv", [_row("RS_GCF_003697165.1")])
    tax = _taxonomy(tmp_path / "t.tsv.gz", ["GCF_003697165\td__Bacteria;p__Test"])
    code = cli.main(
        ["setup", "-m", str(gtdb), "--taxonomy", f"GlobDB_r232={tax}", "-o", str(tmp_path / "o.sqlite")]
    )
    assert code == 0
    assert "share 1 key(s)" in capsys.readouterr().err


#: What the NCBI side of the enrichment test knows: GenBank's .2 and RefSeq's .1 of two bases.
ASSEMBLIES = [
    ("GCA_000002495.2", 5141, "Neurospora crassa OR74A"),
    ("GCF_000002515.1", 4932, "Saccharomyces cerevisiae"),
]
FUNGI = [
    (1, 1, "no rank", "root", 0),
    (2759, 1, "superkingdom", "Eukaryota", 0),
    (4932, 2759, "species", "Saccharomyces cerevisiae", 1),
    (5141, 2759, "species", "Neurospora crassa", 1),
]


def test_unversioned_genomes_resolve_as_omnitax_resolves_them(tmp_path: Path) -> None:
    import omnitax

    path = write_taxonomy_file(tmp_path / "t.sqlite", FUNGI, assemblies=ASSEMBLIES, gtdb=[], links=[])
    with omnitax.open(path, "ncbi") as ncbi:
        found = ncbi.links(["GCA_000002495", "GCF_000002515", "GCF_000002515.2", "GCF_999999999"])
    # the file lists one version per base; the answer is keyed by the request
    assert found["GCA_000002495"].key == "GCA_000002495.2" and found["GCA_000002495"].taxon.id == "5141"
    assert found["GCF_000002515"].key == "GCF_000002515.1"
    assert found["GCF_000002515.2"].key == "GCF_000002515.1"  # the only version of that number
    assert "GCF_999999999" not in found
    # GCA_ and GCF_ are different genomes
    with omnitax.open(path, "ncbi") as ncbi:
        assert ncbi.links(["GCF_000002495", "GCA_000002515"]) == {}
    assert assembly_accession("GCF_003697165") == "GCF_003697165"


def test_ncbi_enrichment_accepts_unversioned_keys(tmp_path: Path) -> None:
    taxonomy_db = write_taxonomy_file(tmp_path / "t.sqlite", FUNGI, assemblies=ASSEMBLIES)
    gtdb = _tsv(tmp_path / "g.tsv", [_row("RS_GCF_000005845.2", ncbi_taxid="562")])
    tax = _taxonomy(
        tmp_path / "t.tsv.gz",
        [
            "GCF_000005845\td__Bacteria;p__Test",  # GTDB covers it (GCF_000005845.2)
            "GCA_000002495\td__Eukaryota;p__Ascomycota",  # GenBank only, not in GTDB
            "GCF_000002515\td__Eukaryota;p__Ascomycota",  # RefSeq .1, not in GTDB
            "GCF_999999999\td__Eukaryota",  # not in the summaries
            "MOTU1\td__Bacteria",
        ],
    )
    output = tmp_path / "meta.sqlite"
    stats = build_metadata_db(
        [gtdb], output, release=232, taxonomy=[TaxonomySource("G", tax)], taxonomy_db=taxonomy_db
    )
    counts = {s["name"]: s for s in stats["taxonomy_sources"]}["G"]["ncbi"]
    assert counts == {"gtdb": 1, "mapped": 2, "no_accession": 1, "not_in_summary": 1, "ambiguous": 0}
    with MetadataDB(output) as db:
        # stored under the summary's versioned accession, found through the unversioned name
        found = db.lookup(["GCA_000002495", "GCA_000002495.2", "GCF_000005845"])
        assert found["GCA_000002495"]["ncbi_taxid"] == 5141
        assert found["GCA_000002495.2"]["ncbi_taxid"] == 5141
        assert found["GCF_000005845"]["ncbi_taxid"] == 562  # GTDB's own row, never replaced
        assert db.lookup(["GCF_000002515"])["GCF_000002515"]["ncbi_taxid"] == 4932
    with closing(sqlite3.connect(output)) as connection:
        rows = [r[0] for r in connection.execute("SELECT accession FROM metadata_data ORDER BY 1")]
    assert rows == ["GCA_000002495.2", "GCF_000002515.1", "GCF_000005845.2"]

    # --update without --taxonomy-db keeps rows an unversioned key still refers to,
    # and drops them with the source.
    kept = tmp_path / "kept.sqlite"
    update_metadata_db(output, kept, taxonomy=[TaxonomySource("Other", tax)])
    with MetadataDB(kept) as db:
        assert "GCA_000002495" in db.lookup(["GCA_000002495"])
    small = _taxonomy(tmp_path / "s.tsv.gz", ["GCA_000002495\td__Eukaryota"])
    pruned = tmp_path / "pruned.sqlite"
    update_metadata_db(output, pruned, taxonomy=[TaxonomySource("G", small)])
    with closing(sqlite3.connect(pruned)) as connection:
        rows = [r[0] for r in connection.execute("SELECT accession FROM metadata_data ORDER BY 1")]
    assert rows == ["GCA_000002495.2", "GCF_000005845.2"]

    # A versioned key keeps only the row of its own accession: the row of another
    # version of its base goes, because no unversioned key asks for the base.
    versioned = _taxonomy(tmp_path / "v.tsv.gz", ["GCA_000002495.1\td__Eukaryota"])
    other = tmp_path / "other.sqlite"
    update_metadata_db(output, other, taxonomy=[TaxonomySource("G", versioned)])
    with closing(sqlite3.connect(other)) as connection:
        rows = [r[0] for r in connection.execute("SELECT accession FROM metadata_data ORDER BY 1")]
    assert rows == ["GCF_000005845.2"]


# ---------------------------------------------------------------------------
# 2b: the nearest NCBI taxon
# ---------------------------------------------------------------------------

#: taxid, parent, rank, name, specified_species -- a tiny NCBI tree.
#: ``specified_species`` is ``nodes.dmp``'s own flag: 1 when a species in the
#: node's lineage has a formal name.  ``"Tepidiphilus sp."``, ``"Bacillus sp."``,
#: ``"uncultured Collinsella sp."`` and ``"Clostridia bacterium"`` are NCBI's
#: informal species; the rest are binomials (or not species).
NODES = [
    (1, 1, "no rank", "root", 0),
    (131567, 1, "no rank", "cellular organisms", 0),
    (2, 131567, "superkingdom", "Bacteria", 0),
    (10, 2, "phylum", "Pseudomonadota", 0),
    (11, 2, "phylum", "Bacillota", 0),
    (12, 11, "class", "Clostridia", 0),
    (13, 12, "species", "Clostridia bacterium", 0),
    (20, 10, "class", "Gammaproteobacteria", 0),
    (30, 20, "order", "Burkholderiales", 0),
    (40, 30, "family", "Hydrogenophilaceae", 0),
    (41, 92, "family", "Coriobacteriaceae", 0),
    (50, 40, "genus", "Thiobacillus", 0),
    (51, 50, "species", "Thiobacillus denitrificans", 1),
    (52, 51, "no rank", "Thiobacillus denitrificans ATCC 25259", 1),
    (60, 40, "genus", "Tepidiphilus", 0),
    (61, 60, "species", "Tepidiphilus margaritifer", 1),
    (62, 60, "species", "Tepidiphilus thermophilus", 1),
    (63, 60, "species", "Tepidiphilus sp.", 0),
    (70, 11, "species", "Bacillus sp.", 0),
    (80, 41, "genus", "Collinsella", 0),
    (81, 80, "species", "uncultured Collinsella sp.", 0),
    (82, 81, "no rank", "uncultured Collinsella sp. SC03", 0),
    (90, 2, "phylum", "Actinomycetota", 0),
    (91, 90, "class", "Coriobacteriia", 0),
    (92, 91, "order", "Coriobacteriales", 0),
    (48479, 1, "no rank", "environmental samples", 0),
    (77133, 48479, "species", "uncultured bacterium", 0),
]

def test_lineage_clades() -> None:
    assert lineage_clades("d__Bacteria;p__;g__Thiobacillus;s__") == ("d__Bacteria", "g__Thiobacillus")
    assert lineage_clades("vFAM-00013;root") == ()


def _members(prefix: str, lineage: str, taxids: list[int]) -> list[list[str]]:
    return [
        _row(f"RS_{prefix}{index:03d}.1", lineage, ncbi_taxid=str(taxid))
        for index, taxid in enumerate(taxids)
    ]


def _taxonomy_db(directory: Path, rows: list[list[str]]) -> Path:
    """The omnitax file for GTDB metadata ``rows``: NCBI (``NODES``) with the taxids of the rows as GTDB's links,
    and GTDB with their lineages."""
    accessions = [(row[0], row[1 + METADATA_COLUMNS.index("gtdb_taxonomy")], row[1 + METADATA_COLUMNS.index("ncbi_taxid")]) for row in rows]
    return write_taxonomy_file(
        directory / "taxonomies.sqlite",
        NODES,
        gtdb=[(accession, lineage) for accession, lineage, _ in accessions],
        links=[(accession, int(taxid)) for accession, _, taxid in accessions if taxid != "none"],
    )


@pytest.fixture()
def nearest_db(tmp_path: Path) -> Path:
    prefix = "d__Bacteria;p__Pseudomonadota;c__Gammaproteobacteria;o__Burkholderiales;f__Hydrogenophilaceae;"
    collinsella = "d__Bacteria;p__Actinomycetota;c__Coriobacteriia;o__Coriobacteriales;f__Coriobacteriaceae;"
    clostridia = "d__Bacteria;p__Bacillota;c__Clostridia;o__O;f__F;"
    rows = [
        *_members("GCF_100000", prefix + "g__Thiobacillus;s__Thiobacillus denitrificans", [52, 51, 51, 61]),
        *_members("GCF_200000", prefix + "g__Tepidiphilus;s__Tepidiphilus a", [61, 62, 63]),
        *_members("GCF_300000", prefix + "g__Envgenus;s__Env a", [77133]),
        # other phyla keep the domain from having a majority node
        *_members("GCF_400000", "d__Bacteria;p__Bacillota;c__C;o__O;f__Fam;g__G;s__S", [70] * 7),
        # NCBI only knows these species informally, so they vote at the genus ...
        *_members("GCF_600000", collinsella + "g__Collinsella;s__Collinsella sp", [81, 81, 82]),
        # ... or at the class ("Clostridia bacterium" hangs directly below it).
        *_members("GCF_700000", clostridia + "g__Placeholder;s__Placeholder sp", [13, 13]),
        # one species of 20 genomes next to one of a single genome: a leaf votes once, whatever its genomes
        *_members("GCF_800000", prefix + "g__Heavy;s__Heavy a", [51] * 20),
        *_members("GCF_810000", prefix + "g__Heavy;s__Heavy b", [61]),
        # a clade whose members all agree on one species (a strain votes as its species)
        *_members("GCF_900000", prefix + "g__Pure;s__Pure a", [51, 51, 52]),
        # a GTDB genome without an NCBI taxid is a target too (virtual source)
        _row("GB_GCA_500000001.1", prefix + "g__Thiobacillus;s__Thiobacillus sp"),
    ]
    gtdb = _tsv(tmp_path / "g.tsv", rows)
    tax = _taxonomy(
        tmp_path / "mags.tsv.gz",
        [
            f"MAG1\t{prefix}g__Thiobacillus;s__MOTU_1",  # genus clade: one leaf, whose genomes are split over two species and a genus
            f"MAG2\t{prefix}g__Tepidiphilus;s__MOTU_2",  # genus 60
            f"MAG3\t{prefix}g__Unseen;s__MOTU_3",  # no genus clade: family
            f"MAG4\t{prefix}g__Envgenus;s__MOTU_4",  # uninformative members: family clade
            "MAG5\td__Bacteria;p__Unseen;c__;o__;f__;g__;s__",  # only the domain: domain
            "MAG6\td__Archaea;p__Nothing",  # no member at all
            "UHGV-1\tvFAM-1;root",  # not a GTDB-style lineage
            f"GCF_110000000\t{prefix}g__Thiobacillus;s__x",  # a real accession GTDB lacks, unmapped
            f"MAG7\t{collinsella}g__Collinsella;s__MOTU_7",  # informal species: genus 80
            f"MAG8\t{clostridia}g__Placeholder;s__MOTU_8",  # informal species: class 12
            f"MAG9\t{prefix}g__Heavy;s__MOTU_9",  # the heavy species does not outvote the light one
            f"MAG10\t{prefix}g__Pure;s__MOTU_10",  # all members agree: species 51
        ],
    )
    output = tmp_path / "meta.sqlite"
    build_metadata_db(
        [gtdb], output, release=232, taxonomy=[TaxonomySource("Mags", tax)], taxonomy_db=_taxonomy_db(tmp_path, rows)
    )
    return output


def test_nearest_taxa_of_keys_without_a_record(nearest_db: Path) -> None:
    with closing(sqlite3.connect(nearest_db)) as connection:
        rows = {
            (source, key): rest
            for source, key, *rest in connection.execute(
                "SELECT s.name, n.key, t.taxid, t.rank, t.name, n.clade, n.members, "
                "f.taxid, f.rank, f.name "
                "FROM taxonomy_nearest AS n JOIN taxonomy_source AS s ON s.id = n.source_id "
                "JOIN ncbi_nearest_taxon AS t ON t.taxid = n.taxid "
                "JOIN ncbi_nearest_taxon AS f ON f.taxid = n.formal_taxid"
            )
        }
    # raw taxid, rank, name, clade, members (the voting leaves), then formal taxid, rank, name
    assert rows[("Mags", "MAG10")] == [
        51, "species", "Thiobacillus denitrificans", "g__Pure", 1,
        51, "species", "Thiobacillus denitrificans",  # formal == raw for a binomial
    ]
    # its one leaf has 4 genomes: a strain and two of species 51 (3 / 4) and one of Tepidiphilus margaritifer: no
    # node below the family has more than 0.9 of the weight (the old majority rule said species 51)
    assert rows[("Mags", "MAG1")] == [
        40, "family", "Hydrogenophilaceae", "g__Thiobacillus", 1,
        40, "family", "Hydrogenophilaceae",
    ]
    # a leaf votes once, whatever its genomes: 20 genomes of species 51 do not outvote the single one of 61
    assert rows[("Mags", "MAG9")][:5] == [40, "family", "Hydrogenophilaceae", "g__Heavy", 2]
    assert rows[("Mags", "MAG2")][:4] == [60, "genus", "Tepidiphilus", "g__Tepidiphilus"]
    # no genus clade, or only uninformative members: the family clade, whose voting leaves
    # (Thiobacillus, Tepidiphilus, Heavy a and b, Pure; Env a is uninformative) have the family as consensus
    assert rows[("Mags", "MAG3")][:5] == [40, "family", "Hydrogenophilaceae", "f__Hydrogenophilaceae", 5]
    assert rows[("Mags", "MAG4")][:4] == [40, "family", "Hydrogenophilaceae", "f__Hydrogenophilaceae"]
    assert rows[("Mags", "MAG5")][1] == "domain"
    assert ("Mags", "MAG6") not in rows and ("Mags", "UHGV-1") not in rows
    # Votes on NCBI's informal species are lifted to their formal ancestor and abstain below it, so an all-informal
    # clade is placed at that ancestor (it was the informal species, with the formal taxon above it).
    assert rows[("Mags", "MAG7")] == [
        80, "genus", "Collinsella", "g__Collinsella", 1,
        80, "genus", "Collinsella",
    ]
    assert rows[("Mags", "MAG8")] == [
        12, "class", "Clostridia", "g__Placeholder", 1,
        12, "class", "Clostridia",
    ]
    # the virtual GTDB source: a genome without a taxid
    assert rows[("GTDB_r232", "GCA_500000001.1")][3] == "g__Thiobacillus"
    # ncbi_taxid itself is untouched: only real assembly taxids are stored
    with closing(sqlite3.connect(nearest_db)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM metadata_data WHERE accession LIKE 'MAG%'"
        ).fetchone()[0] == 0


def test_nearest_counts_by_source_and_rank(nearest_db: Path) -> None:
    with MetadataDB(nearest_db) as db:
        sources = {s["name"]: s for s in db.taxonomy_sources()}
    mags = sources["Mags"]["ncbi_nearest"]
    # MAG1..MAG10 and GCF_110000000 are GTDB-style without a record; UHGV-1 is not.
    # The ranks are the raw ones; MAG7 and MAG8 (all votes on informal species) are placed
    # at the formal genus/class, so no raw taxon is informal.
    assert mags["keys"] == 11 and mags["assigned"] == 10 and mags["informal"] == 0
    assert mags["ranks"] == {"domain": 1, "family": 5, "genus": 2, "class": 1, "species": 1}
    assert sources["GTDB_r232"]["ncbi_nearest"]["assigned"] == 1
    assert sources["GTDB_r232"]["ncbi_nearest"]["informal"] == 0


def test_resolved_hits_carry_the_nearest_taxon(nearest_db: Path) -> None:
    with MetadataDB(nearest_db) as db:
        assert db.has_nearest
        assert "ncbi_nearest_taxid" in db.field_columns and "ncbi_nearest_taxid" not in db.columns
        assert "ncbi_nearest_formal_taxid" in db.field_columns
        hits = db.resolve([("genomes/MAG10.fa.gz", "MAG10_000000000001"), ("genomes/zzz.fa.gz", "zzz")])
        near = hits[0].columns()
        assert near["tax_key"] == "MAG10"
        assert (near["ncbi_nearest_taxid"], near["ncbi_nearest_rank"], near["ncbi_nearest_name"]) == (
            51, "species", "Thiobacillus denitrificans"
        )
        assert near["ncbi_nearest_clade"] == "g__Pure"
        assert str(near["ncbi_nearest_lineage"]).startswith("d__Bacteria;p__Pseudomonadota;")
        # a formally named raw taxon: the formal columns are the same node
        assert (near["ncbi_nearest_formal_taxid"], near["ncbi_nearest_formal_rank"],
                near["ncbi_nearest_formal_name"]) == (51, "species", "Thiobacillus denitrificans")
        # a miss has the columns, empty
        assert hits[1].columns()["ncbi_nearest_taxid"] is None
        assert hits[1].columns()["ncbi_nearest_formal_taxid"] is None
        rows = result_rows(
            db,
            [_fake_result("genomes/MAG10.fa.gz", "MAG10_000000000001")],
            "s",
            fields=["ncbi_taxid", "ncbi_nearest_taxid", "ncbi_nearest_rank"],
        )
        assert rows[0]["ncbi_taxid"] is None and rows[0]["ncbi_nearest_taxid"] == 51
        assert db.summary()["taxonomy_sources"][-1]["ncbi_nearest"]["assigned"] >= 1


def test_files_without_nearest_taxa_are_unchanged(gtdb_db: Path) -> None:
    with MetadataDB(gtdb_db) as db:
        assert not db.has_nearest
        assert not any(c.startswith("ncbi_nearest") for c in db.field_columns)
        assert "ncbi_nearest_taxid" not in db.resolve([("genomes/MOTU40_002958.fa.gz", "x")])[0].columns()


def test_update_keeps_and_replaces_nearest_taxa(
    nearest_db: Path, tmp_path: Path
) -> None:
    kept = tmp_path / "kept.sqlite"
    update_metadata_db(nearest_db, kept, taxonomy=[TaxonomySource("Extra", _taxonomy(tmp_path / "e.gz", ["E1\td__Bacteria"]))])
    with MetadataDB(kept) as db:
        assert db.resolve([("genomes/MAG10.fa.gz", "m")])[0].columns()["ncbi_nearest_taxid"] == 51
    # replacing the source drops its nearest rows (the new keys are not computed without the flag)
    replaced = tmp_path / "replaced.sqlite"
    other = _taxonomy(tmp_path / "o.gz", ["MAG10\td__Bacteria;p__Pseudomonadota"])
    update_metadata_db(nearest_db, replaced, taxonomy=[TaxonomySource("Mags", other)])
    with MetadataDB(replaced) as db:
        source = {s["name"]: s for s in db.taxonomy_sources()}["Mags"]
        assert "ncbi_nearest" not in source
        assert db.resolve([("genomes/MAG10.fa.gz", "m")])[0].columns().get("ncbi_nearest_taxid") is None


def test_setup_reports_the_nearest_taxa(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rows = [
        *_members("GCF_100000", "d__Bacteria;p__P;f__F;g__Thiobacillus;s__a", [51, 51]),
        # NCBI only names this species informally (see NODES)
        *_members("GCF_200000", "d__Bacteria;p__P;f__F;g__Collinsella;s__c", [81, 81]),
    ]
    gtdb = _tsv(tmp_path / "g.tsv", rows)
    tax = _taxonomy(
        tmp_path / "t.gz",
        [
            "MAG1\td__Bacteria;p__P;f__F;g__Thiobacillus;s__MOTU",
            "MAG2\td__Bacteria;p__P;f__F;g__Collinsella;s__MOTU",
            "GCF_110000000\td__Bacteria;p__Q",
        ],
    )
    code = cli.main(
        [
            "setup", "-m", str(gtdb), "--taxonomy", f"Mags={tax}", "--taxonomy-db", str(_taxonomy_db(tmp_path, rows)),
            "--cache-dir", str(tmp_path / "cache"), "-o", str(tmp_path / "o.sqlite"),
        ]
    )
    err = capsys.readouterr().err
    assert code == 0, err
    assert (
        "nearest NCBI taxon: 3 of 3 keys without an NCBI record "
        "(domain 1, genus 1, species 1)"
    ) in err
    counts = json.loads(MetadataDB(tmp_path / "o.sqlite").info["ncbi_nearest"])["Mags"]
    assert counts["assigned"] == 3 and counts["informal"] == 0


# ---------------------------------------------------------------------------
# the NCBI profile
# ---------------------------------------------------------------------------

REAL = "d__Bacteria;p__Pseudomonadota;c__Gammaproteobacteria;o__Burkholderiales;f__Hydrogenophilaceae;g__Thiobacillus;s__Thiobacillus denitrificans"
NEAR = "d__Bacteria;p__Pseudomonadota;c__Gammaproteobacteria;o__Burkholderiales;f__Hydrogenophilaceae;g__Thiobacillus;s__"


def _hit(accession: str, abundance: float, **columns: object) -> dict[str, object]:
    return {
        "accession": accession,
        "Genome_file": f"{accession}.fa.gz",
        "Taxonomic_abundance": abundance,
        "Sequence_abundance": abundance,
        "Adjusted_ANI": 99.0,
        "Eff_cov": 1.0,
        **columns,
    }


def test_ncbi_profile_places_hits_at_their_nearest_node() -> None:
    near = {
        "ncbi_nearest_lineage": NEAR,
        "ncbi_nearest_taxid": 50,
        "ncbi_nearest_rank": "genus",
        "ncbi_nearest_name": "Thiobacillus",
        "ncbi_nearest_clade": "g__Thiobacillus",
    }
    rows = [
        _hit("GCF_1.1", 60.0, ncbi_taxonomy=REAL),
        _hit("MAG10", 30.0, **near),
        _hit("MAG2", 10.0),  # no NCBI lineage and no nearest taxon
    ]
    clades = {c.clade: c for c in aggregate_rows(rows, "ncbi")}
    genus = "d__Bacteria|p__Pseudomonadota|c__Gammaproteobacteria|o__Burkholderiales|f__Hydrogenophilaceae|g__Thiobacillus"
    assert clades[genus].relative == 90.0  # real genome plus the nearest-placed one
    assert clades["unclassified"].relative == 10.0
    strain = clades[f"{genus}|t__MAG10"]
    assert strain.nearest == {"taxid": 50, "rank": "genus", "name": "Thiobacillus", "clade": "g__Thiobacillus"}
    assert clade_dict(strain)["nearest"]["rank"] == "genus"
    # the real genome's strain and every other clade carry no mark
    assert clades[f"{genus}|s__Thiobacillus denitrificans|t__GCF_1.1"].nearest is None
    assert "nearest" not in clade_dict(clades[genus])
    # gtdb is not affected by an NCBI fallback
    assert not any(c.nearest for c in aggregate_rows(rows, "gtdb"))


def test_ncbi_profile_is_unchanged_without_the_fallback_columns() -> None:
    rows = [_hit("GCF_1.1", 60.0, ncbi_taxonomy=REAL), _hit("MAG2", 40.0)]
    with_columns = [{**row, "ncbi_nearest_lineage": None, "ncbi_nearest_taxid": None} for row in rows]
    assert aggregate_rows(with_columns, "ncbi") == aggregate_rows(rows, "ncbi")


def test_ncbi_profile_requests_the_nearest_columns_when_the_server_has_them() -> None:
    metadata = {
        "columns": ["gtdb_taxonomy", "ncbi_taxonomy", "ncbi_nearest_lineage", "ncbi_nearest_taxid"],
        "taxonomy_sources": [],
    }
    assert resolve_taxonomy_columns(["ncbi"], metadata) == [
        "ncbi_taxonomy", "ncbi_nearest_lineage", "ncbi_nearest_taxid"
    ]
    # an older file without them still works
    assert resolve_taxonomy_columns(["ncbi"], {"columns": ["ncbi_taxonomy"], "taxonomy_sources": []}) == ["ncbi_taxonomy"]


def test_ncbi_profile_falls_back_end_to_end_from_the_metadata(nearest_db: Path) -> None:
    with MetadataDB(nearest_db) as db:
        # a profile request adds the columns it needs, and the MAG is not "unclassified"
        fields = _taxprof_fields(["ncbi"], db, None)
        assert "ncbi_taxonomy" in fields and "ncbi_nearest_lineage" in fields
        assert "ncbi_nearest_formal_taxid" in fields
        rows = result_rows(
            db, [_fake_result("genomes/MAG10.fa.gz", "MAG10_000000000001")],
            "s", mode="profile", fields=fields,
        )
        assert rows[0]["ncbi_taxid"] is None
        clades = {clade.clade: clade for clade in aggregate_rows(rows, "ncbi")}
    genus = (
        "d__Bacteria|p__Pseudomonadota|c__Gammaproteobacteria|o__Burkholderiales"
        "|f__Hydrogenophilaceae|g__Thiobacillus"
    )
    assert "unclassified" not in clades
    species = f"{genus}|s__Thiobacillus denitrificans"
    assert clades[species].relative == 1.0
    nearest = clades[f"{species}|t__MAG10.fa.gz"].nearest
    assert nearest == {
        "taxid": 51, "rank": "species",
        "name": "Thiobacillus denitrificans", "clade": "g__Pure",
    }
