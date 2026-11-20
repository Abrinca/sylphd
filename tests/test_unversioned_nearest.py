"""Unversioned accessions (GlobDB) and the nearest NCBI taxon of genomes without a record.

GlobDB names its genomes ``genomes/GCA_041167105.fa.gz`` with the contig
``GCA_041167105_000000000001``: no version anywhere, while GTDB's metadata is keyed
``GCA_041167105.1``.  Everything is built from tiny fixtures; nothing touches NCBI.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from sylphd import cli, ncbi_taxonomy
from sylphd.metadata import METADATA_COLUMNS, MetadataDB, accession_from_genome_name
from sylphd.metadata_build import build_metadata_db, update_metadata_db
from sylphd.ncbi_nearest import NcbiTree, consensus, lineage_clades
from sylphd.profiling import _taxprof_fields, result_rows
from sylphd.taxonomy import aggregate_rows, clade_dict, resolve_taxonomy_columns
from sylphd.taxonomy_sources import TaxonomySource

HEADER = ["accession", *METADATA_COLUMNS]
DATA = Path(__file__).parent / "data" / "ncbi"
ASSEMBLIES = (DATA / "assembly_summary_refseq.txt", DATA / "assembly_summary_genbank.txt")

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


def test_read_assemblies_resolves_an_unversioned_base() -> None:
    found = ncbi_taxonomy.read_assemblies(ASSEMBLIES, ["GCA_000002495", "GCF_000002515"])
    # the summary lists one version per base; the result is keyed by the request
    assert found["GCA_000002495"] == ncbi_taxonomy.Assembly(5141, "Neurospora crassa OR74A", "GCA_000002495.2")
    assert found["GCF_000002515"].accession == "GCF_000002515.1"
    # an unversioned request is independent of the versions other keys ask for
    found = ncbi_taxonomy.read_assemblies(ASSEMBLIES, ["GCF_000002515", "GCF_000002515.1", "GCF_000002515.2"])
    assert set(found) == {"GCF_000002515", "GCF_000002515.1"}
    assert ncbi_taxonomy.assembly_accession("GCF_003697165") == "GCF_003697165"


def _patch_ncbi(monkeypatch: pytest.MonkeyPatch, taxdump: tuple[Path, Path, Path] | None = None) -> None:
    monkeypatch.setattr(
        ncbi_taxonomy,
        "fetch_assemblies",
        lambda accessions, *, progress=None: ncbi_taxonomy.read_assemblies(ASSEMBLIES, accessions),
    )
    files = taxdump or (DATA / "rankedlineage.dmp", DATA / "nodes.dmp", DATA / "merged.dmp")
    monkeypatch.setattr(ncbi_taxonomy, "fetch_taxdump", lambda directory, *, progress=None: files)


def test_ncbi_enrichment_accepts_unversioned_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ncbi(monkeypatch)
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
        [gtdb], output, release=232, taxonomy=[TaxonomySource("G", tax)], ncbi_cache=tmp_path / "c"
    )
    counts = {s["name"]: s for s in stats["taxonomy_sources"]}["G"]["ncbi"]
    assert counts == {"gtdb": 1, "mapped": 2, "no_accession": 1, "not_in_summary": 1, "no_lineage": 0}
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

    # --update without --ncbi-taxonomy keeps rows an unversioned key still refers to,
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

#: rankedlineage.dmp columns after the name: species genus family order class phylum kingdom superkingdom.
RANKED = {
    2: ("", "", "", "", "", "", "", ""),
    10: ("", "", "", "", "", "", "", "Bacteria"),
    12: ("", "", "", "", "Clostridia", "Bacillota", "", "Bacteria"),
    13: ("", "", "", "", "Clostridia", "Bacillota", "", "Bacteria"),
    20: ("", "", "", "", "", "Pseudomonadota", "", "Bacteria"),
    30: ("", "", "", "", "Gammaproteobacteria", "Pseudomonadota", "", "Bacteria"),
    40: ("", "", "", "Burkholderiales", "Gammaproteobacteria", "Pseudomonadota", "", "Bacteria"),
    50: ("", "", "Hydrogenophilaceae", "Burkholderiales", "Gammaproteobacteria", "Pseudomonadota", "", "Bacteria"),
    51: ("", "Thiobacillus", "Hydrogenophilaceae", "Burkholderiales", "Gammaproteobacteria", "Pseudomonadota", "", "Bacteria"),
    60: ("", "", "Hydrogenophilaceae", "Burkholderiales", "Gammaproteobacteria", "Pseudomonadota", "", "Bacteria"),
    80: ("", "Collinsella", "Coriobacteriaceae", "Coriobacteriales", "Coriobacteriia", "Actinomycetota", "", "Bacteria"),
    81: ("", "Collinsella", "Coriobacteriaceae", "Coriobacteriales", "Coriobacteriia", "Actinomycetota", "", "Bacteria"),
}


def _node_line(taxid: int, parent: int, rank: str, specified: int) -> str:
    """One ``nodes.dmp`` row with the 18 documented fields."""
    fields = ["", "", rank, "", "0", "0", "11", "0", "0", "0", "0", "0", "", "", "",
              str(specified), "0", "1"]
    fields[0], fields[1] = str(taxid), str(parent)
    return "\t|\t".join(fields) + "\t|\n"


@pytest.fixture()
def taxdump(tmp_path: Path) -> tuple[Path, Path, Path]:
    nodes = tmp_path / "nodes.dmp"
    nodes.write_text("".join(_node_line(t, p, r, s) for t, p, r, _, s in NODES))
    merged = tmp_path / "merged.dmp"
    merged.write_text("999\t|\t51\t|\n")
    names = {t: n for t, _, _, n, _ in NODES}
    ranked = tmp_path / "rankedlineage.dmp"
    lines = []
    for taxid, columns in RANKED.items():
        lines.append("\t|\t".join([str(taxid), names[taxid], *columns]) + "\t|")
    ranked.write_text("\n".join(lines) + "\n")
    return ranked, nodes, merged


def test_tree_lift_and_consensus(taxdump: tuple[Path, Path, Path]) -> None:
    tree = NcbiTree.from_files(taxdump[1], taxdump[2])
    assert tree.rank(51) == "species" and tree.rank(2) == "domain" and tree.rank(52) is None
    # lift() is the raw placement: the nearest node with a major rank, even an
    # informally named species (that is what the nearest taxon reports).
    assert tree.lift(51) == 51  # a formal species is a placement
    assert tree.lift(52) == 51  # a strain votes as its species
    assert tree.lift(999) == 51  # merged
    assert tree.lift(63) == 63  # "Tepidiphilus sp."
    assert tree.lift(81) == 81  # "uncultured Collinsella sp."
    assert tree.lift(82) == 81  # a strain below it
    assert tree.lift(13) == 13  # "Clostridia bacterium"
    assert tree.lift(77133) is None  # environmental samples
    assert tree.lift(123456) is None  # not in the taxdump
    assert tree.lift(131567) is None  # nothing with a major rank above it
    # lift_formal() is the extra information: the same node for a formal taxon,
    # the genus/class above when NCBI only names the species informally.
    assert tree.lift_formal(51) == 51
    assert tree.lift_formal(52) == 51
    assert tree.lift_formal(63) == 60  # "Tepidiphilus sp." -> its genus
    assert tree.lift_formal(81) == 80  # "uncultured Collinsella sp." -> its genus
    assert tree.lift_formal(82) == 80  # a strain below it -> its genus
    assert tree.lift_formal(13) == 12  # "Clostridia bacterium" -> its class
    assert tree.lift_formal(2) == 2  # a rank other than species is formal
    # a minority outlier is ignored: three of four agree on the species
    assert consensus(tree, {51: 3, 61: 1}) == (51, 3, 4)
    # different species of one genus: the genus
    assert consensus(tree, {61: 1, 62: 1, 63: 1}) == (60, 3, 3)
    # an even split is not a majority, so the nearest node they all share
    assert consensus(tree, {51: 1, 61: 1}) == (40, 2, 2)
    # three of four in one family: the family, though the fourth is in another phylum
    assert consensus(tree, {51: 2, 61: 1, 70: 1}) == (40, 3, 4)
    # nothing in common below the domain
    assert consensus(tree, {51: 1, 70: 1}) == (2, 2, 2)
    # the consensus is the raw node: it may itself be an informal species, and
    # the formal taxon is the lift_formal() of it (see lift_formal above)
    assert consensus(tree, {81: 4}) == (81, 4, 4)
    assert consensus(tree, {13: 2}) == (13, 2, 2)
    assert consensus(tree, {}) is None


def test_a_taxdump_without_the_specified_species_column_keeps_species(
    tmp_path: Path,
) -> None:
    # 4-field rows (nothing says "informal"): lift_formal equals lift
    nodes = tmp_path / "nodes.dmp"
    nodes.write_text(
        "1\t|\t1\t|\tno rank\t|\t\t|\n"
        "2\t|\t1\t|\tsuperkingdom\t|\t\t|\n"
        "3\t|\t2\t|\tspecies\t|\t\t|\n"
    )
    merged = tmp_path / "merged.dmp"
    merged.write_text("")
    tree = NcbiTree.from_files(nodes, merged)
    assert tree.lift(3) == 3 and tree.lift_formal(3) == 3


def test_lineage_clades() -> None:
    assert lineage_clades("d__Bacteria;p__;g__Thiobacillus;s__") == ("d__Bacteria", "g__Thiobacillus")
    assert lineage_clades("vFAM-00013;root") == ()


def _members(prefix: str, lineage: str, taxids: list[int]) -> list[list[str]]:
    return [
        _row(f"RS_{prefix}{index:03d}.1", lineage, ncbi_taxid=str(taxid))
        for index, taxid in enumerate(taxids)
    ]


@pytest.fixture()
def nearest_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, taxdump: tuple[Path, Path, Path]
) -> Path:
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
        # a GTDB genome without an NCBI taxid is a target too (virtual source)
        _row("GB_GCA_500000001.1", prefix + "g__Thiobacillus;s__Thiobacillus sp"),
    ]
    gtdb = _tsv(tmp_path / "g.tsv", rows)
    tax = _taxonomy(
        tmp_path / "mags.tsv.gz",
        [
            f"MAG1\t{prefix}g__Thiobacillus;s__MOTU_1",  # genus clade -> species 51
            f"MAG2\t{prefix}g__Tepidiphilus;s__MOTU_2",  # genus 60
            f"MAG3\t{prefix}g__Unseen;s__MOTU_3",  # no genus clade: family
            f"MAG4\t{prefix}g__Envgenus;s__MOTU_4",  # uninformative members: family clade
            "MAG5\td__Bacteria;p__Unseen;c__;o__;f__;g__;s__",  # only the domain: domain
            "MAG6\td__Archaea;p__Nothing",  # no member at all
            "UHGV-1\tvFAM-1;root",  # not a GTDB-style lineage
            f"GCF_110000000\t{prefix}g__Thiobacillus;s__x",  # a real accession GTDB lacks, unmapped
            f"MAG7\t{collinsella}g__Collinsella;s__MOTU_7",  # informal species: genus 80
            f"MAG8\t{clostridia}g__Placeholder;s__MOTU_8",  # informal species: class 12
        ],
    )
    _patch_ncbi(monkeypatch, taxdump)
    output = tmp_path / "meta.sqlite"
    build_metadata_db(
        [gtdb], output, release=232, taxonomy=[TaxonomySource("Mags", tax)], ncbi_cache=tmp_path / "c"
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
    # raw taxid, rank, name, clade, members, then formal taxid, rank, name
    assert rows[("Mags", "MAG1")] == [
        51, "species", "Thiobacillus denitrificans", "g__Thiobacillus", 4,
        51, "species", "Thiobacillus denitrificans",  # formal == raw for a binomial
    ]
    assert rows[("Mags", "MAG2")][:4] == [60, "genus", "Tepidiphilus", "g__Tepidiphilus"]
    # no genus clade, or only uninformative members: the family clade, whose members
    # (4 of 7 in Tepidiphilus) have the genus as consensus
    assert rows[("Mags", "MAG3")][:4] == [60, "genus", "Tepidiphilus", "f__Hydrogenophilaceae"]
    assert rows[("Mags", "MAG4")][:4] == [60, "genus", "Tepidiphilus", "f__Hydrogenophilaceae"]
    assert rows[("Mags", "MAG5")][1] == "domain"
    assert ("Mags", "MAG6") not in rows and ("Mags", "UHGV-1") not in rows
    # The raw nearest taxon is NCBI's informal species; the formal one is above it.
    assert rows[("Mags", "MAG7")] == [
        81, "species", "uncultured Collinsella sp.", "g__Collinsella", 3,
        80, "genus", "Collinsella",
    ]
    assert rows[("Mags", "MAG8")] == [
        13, "species", "Clostridia bacterium", "g__Placeholder", 2,
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
    # MAG1..MAG8 and GCF_110000000 are GTDB-style without a record; UHGV-1 is not.
    # The ranks are the raw ones; MAG7 and MAG8 are informal species (their formal
    # taxon is the genus/class above).
    assert mags["keys"] == 9 and mags["assigned"] == 8 and mags["informal"] == 2
    assert mags["ranks"] == {"domain": 1, "genus": 3, "species": 4}
    assert sources["GTDB_r232"]["ncbi_nearest"]["assigned"] == 1
    assert sources["GTDB_r232"]["ncbi_nearest"]["informal"] == 0


def test_resolved_hits_carry_the_nearest_taxon(nearest_db: Path) -> None:
    with MetadataDB(nearest_db) as db:
        assert db.has_nearest
        assert "ncbi_nearest_taxid" in db.field_columns and "ncbi_nearest_taxid" not in db.columns
        assert "ncbi_nearest_formal_taxid" in db.field_columns
        hits = db.resolve([("genomes/MAG1.fa.gz", "MAG1_000000000001"), ("genomes/zzz.fa.gz", "zzz")])
        near = hits[0].columns()
        assert near["tax_key"] == "MAG1"
        assert (near["ncbi_nearest_taxid"], near["ncbi_nearest_rank"], near["ncbi_nearest_name"]) == (
            51, "species", "Thiobacillus denitrificans"
        )
        assert near["ncbi_nearest_clade"] == "g__Thiobacillus"
        assert str(near["ncbi_nearest_lineage"]).startswith("d__Bacteria;p__Pseudomonadota;")
        # a formally named raw taxon: the formal columns are the same node
        assert (near["ncbi_nearest_formal_taxid"], near["ncbi_nearest_formal_rank"],
                near["ncbi_nearest_formal_name"]) == (51, "species", "Thiobacillus denitrificans")
        # a miss has the columns, empty
        assert hits[1].columns()["ncbi_nearest_taxid"] is None
        assert hits[1].columns()["ncbi_nearest_formal_taxid"] is None
        rows = result_rows(
            db,
            [_fake_result("genomes/MAG1.fa.gz", "MAG1_000000000001")],
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
        assert db.resolve([("genomes/MAG1.fa.gz", "m")])[0].columns()["ncbi_nearest_taxid"] == 51
    # replacing the source drops its nearest rows (the new keys are not computed without the flag)
    replaced = tmp_path / "replaced.sqlite"
    other = _taxonomy(tmp_path / "o.gz", ["MAG1\td__Bacteria;p__Pseudomonadota"])
    update_metadata_db(nearest_db, replaced, taxonomy=[TaxonomySource("Mags", other)])
    with MetadataDB(replaced) as db:
        source = {s["name"]: s for s in db.taxonomy_sources()}["Mags"]
        assert "ncbi_nearest" not in source
        assert db.resolve([("genomes/MAG1.fa.gz", "m")])[0].columns().get("ncbi_nearest_taxid") is None


def test_setup_reports_the_nearest_taxa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    taxdump: tuple[Path, Path, Path],
) -> None:
    _patch_ncbi(monkeypatch, taxdump)
    gtdb = _tsv(
        tmp_path / "g.tsv",
        [
            *_members("GCF_100000", "d__Bacteria;p__P;f__F;g__Thiobacillus;s__a", [51, 51]),
            # NCBI only names this species informally (see the taxdump fixture)
            *_members("GCF_200000", "d__Bacteria;p__P;f__F;g__Collinsella;s__c", [81, 81]),
        ],
    )
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
            "setup", "-m", str(gtdb), "--taxonomy", f"Mags={tax}", "--ncbi-taxonomy",
            "--cache-dir", str(tmp_path / "cache"), "-o", str(tmp_path / "o.sqlite"),
        ]
    )
    err = capsys.readouterr().err
    assert code == 0, err
    assert (
        "nearest NCBI taxon: 3 of 3 keys without an NCBI record "
        "(domain 1, species 2; 1 informal, formally named above)"
    ) in err
    assert list((tmp_path / "cache" / "ncbi").iterdir()) == []  # the taxdump is gone again
    counts = json.loads(MetadataDB(tmp_path / "o.sqlite").info["ncbi_nearest"])["Mags"]
    assert counts["assigned"] == 3 and counts["informal"] == 1


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
        _hit("MAG1", 30.0, **near),
        _hit("MAG2", 10.0),  # no NCBI lineage and no nearest taxon
    ]
    clades = {c.clade: c for c in aggregate_rows(rows, "ncbi")}
    genus = "d__Bacteria|p__Pseudomonadota|c__Gammaproteobacteria|o__Burkholderiales|f__Hydrogenophilaceae|g__Thiobacillus"
    assert clades[genus].relative == 90.0  # real genome plus the nearest-placed one
    assert clades["unclassified"].relative == 10.0
    strain = clades[f"{genus}|t__MAG1"]
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
            db, [_fake_result("genomes/MAG1.fa.gz", "MAG1_000000000001")],
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
    nearest = clades[f"{species}|t__MAG1.fa.gz"].nearest
    assert nearest == {
        "taxid": 51, "rank": "species",
        "name": "Thiobacillus denitrificans", "clade": "g__Thiobacillus",
    }
