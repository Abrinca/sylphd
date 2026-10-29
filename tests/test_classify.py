"""Lineage-based decision engine for ``POST /assembly`` (pure functions)."""

from __future__ import annotations

from typing import Any

import pytest

from sylphd.assembly import (
    FUNGI_CAVEAT,
    GENERIC_CAVEAT,
    VIRUS_CAVEAT,
    Lineage,
    classify_hits,
    parse_gtdb_taxonomy,
    parse_lineage,
    shared_lineage_rank,
    shared_rank,
    species_caveat,
)

BACT = "d__Bacteria;p__Bacillota;c__Bacilli;o__Lactobacillales;"
LACTO = BACT + "f__Streptococcaceae;g__Lactococcus;"
LACTIS = LACTO + "s__Lactococcus lactis"
CREMORIS = LACTO + "s__Lactococcus cremoris"
STREP = BACT + "f__Streptococcaceae;g__Streptococcus;s__Streptococcus pyogenes"
LACTOBAC = BACT + "f__Lactobacillaceae;g__Lactobacillus;s__Lactobacillus delbrueckii"
BACILLUS = "d__Bacteria;p__Bacillota;c__Bacilli;o__Bacillales;f__Bacillaceae;g__B;s__B subtilis"
CLOSTRIDIA = "d__Bacteria;p__Bacillota_A;c__Clostridia;o__Lachnospirales;f__F;g__G;s__S"
ECOLI = "d__Bacteria;p__Pseudomonadota;c__Gammaproteobacteria;o__Enterobacterales;f__E;g__Escherichia;s__Escherichia coli"
ARCHAEON = "d__Archaea;p__Methanobacteriota;c__C;o__O;f__F;g__G;s__S"


def hit(ani: float, taxonomy: str | None, accession: str = "GCA_1.1") -> dict[str, Any]:
    return {"ani": ani, "accession": accession, "gtdb_taxonomy": taxonomy}


def relations(hits: list[dict[str, Any]]) -> list[str]:
    return [h["relation"] for h in hits]


def test_threshold_uses_unrounded_ani() -> None:
    hits = [hit(94.996, LACTIS)]
    assignment, closest = classify_hits(hits)
    assert assignment is None
    assert hits[0]["ani_species_level"] is False
    assert hits[0]["ani"] == closest["ani"] == 95.0


def test_parse_gtdb_taxonomy() -> None:
    assert parse_gtdb_taxonomy("d__Bacteria; p__Bacillota ;g__;s__") == {
        "d": "Bacteria",
        "p": "Bacillota",
    }
    assert parse_gtdb_taxonomy(None) == {}
    assert parse_gtdb_taxonomy("garbage;x__y") == {}


def test_shared_rank_needs_agreeing_parents() -> None:
    a = parse_gtdb_taxonomy(LACTIS)
    assert shared_rank(a, a) == "species"
    assert shared_rank(a, parse_gtdb_taxonomy(CREMORIS)) == "genus"
    assert shared_rank(a, parse_gtdb_taxonomy(STREP)) == "family"
    assert shared_rank(a, parse_gtdb_taxonomy(LACTOBAC)) == "order"
    assert shared_rank(a, parse_gtdb_taxonomy(BACILLUS)) == "class"
    assert shared_rank(a, parse_gtdb_taxonomy(ECOLI)) == "domain"
    assert shared_rank(a, parse_gtdb_taxonomy(ARCHAEON)) is None
    # same genus name under a different family is not a shared genus
    other = parse_gtdb_taxonomy(LACTIS.replace("f__Streptococcaceae", "f__Other"))
    assert shared_rank(a, other) == "order"


def test_lactococcus_like_query_is_assigned_and_ranked_by_lineage() -> None:
    hits = [
        hit(98.65, LACTIS, "GCF_L"),
        hit(97.0, CREMORIS, "GCF_C"),
        hit(90.1, STREP),
        hit(88.0, LACTOBAC),
        hit(87.5, BACILLUS),
        hit(86.96, "d__Bacteria;p__Bacillota;c__Bacilli;o__Lactobacillales;f__Carnobacteriaceae;g__Caccenecus;s__Caccenecus sp"),
        hit(86.24, CLOSTRIDIA),
        hit(85.2, ARCHAEON),
    ]
    assignment, closest = classify_hits(hits)
    assert assignment == {
        "rank": "species",
        "taxon": "Lactococcus lactis",
        "gtdb_taxonomy": LACTIS,
        "accession": "GCF_L",
        "ani": 98.65,
        "taxonomy": {
            "domain": "Bacteria",
            "phylum": "Bacillota",
            "class": "Bacilli",
            "order": "Lactobacillales",
            "family": "Streptococcaceae",
            "genus": "Lactococcus",
            "species": "Lactococcus lactis",
        },
        "taxonomy_source": "GTDB",
    }
    assert closest == assignment
    assert "note" not in assignment  # the 95 % threshold is GTDB's own
    assert relations(hits) == [
        "same species",
        "same genus",
        "same family",
        "same order",
        "same class",
        "same order",
        "same domain",
        "different domain",
    ]
    assert [h["shared_rank"] for h in hits] == [
        "species", "genus", "family", "order", "class", "order", "domain", None,
    ]
    # the second hit reaches the species threshold but is another GTDB species
    assert [h["ani_species_level"] for h in hits[:3]] == [True, True, False]


@pytest.mark.parametrize(
    "ani, assigned",
    [(95.0, True), (94.99, False), (100.0, True), (70.0, False)],
)
def test_species_threshold_is_the_only_hard_boundary(ani: float, assigned: bool) -> None:
    hits = [hit(ani, LACTIS)]
    assignment, closest = classify_hits(hits)
    assert (assignment is not None) is assigned
    assert closest is not None and closest["taxon"] == "Lactococcus lactis"
    assert hits[0]["relation"] == ("same species" if assigned else "closest relative")
    assert hits[0]["shared_rank"] == ("species" if assigned else None)
    assert hits[0]["ani_species_level"] is assigned


def test_no_assignment_describes_hits_relative_to_the_closest() -> None:
    hits = [
        hit(85.48, LACTIS),
        hit(84.0, CREMORIS),
        hit(83.0, STREP),
        hit(80.0, CLOSTRIDIA),
        hit(79.0, ARCHAEON),
    ]
    assignment, closest = classify_hits(hits)
    assert assignment is None
    assert closest is not None and closest["taxon"] == "Lactococcus lactis"
    assert closest["ani"] == 85.48
    assert relations(hits) == [
        "closest relative",
        "same genus as closest relative",
        "same family as closest relative",
        "same domain as closest relative",
        "different domain from closest relative",
    ]
    assert [h["shared_rank"] for h in hits] == [None, "genus", "family", "domain", None]
    assert not any(h["ani_species_level"] for h in hits)


def test_hit_at_species_ani_with_other_species_name_is_flagged() -> None:
    hits = [hit(96.0, LACTIS), hit(95.2, CREMORIS)]
    assignment, _ = classify_hits(hits)
    assert assignment is not None and assignment["taxon"] == "Lactococcus lactis"
    assert hits[1]["relation"] == "same genus"
    assert hits[1]["shared_rank"] == "genus"
    assert hits[1]["ani_species_level"] is True


def test_same_species_name_below_species_ani_keeps_the_lineage_label() -> None:
    hits = [hit(97.0, LACTIS, "A"), hit(93.0, LACTIS, "B")]
    classify_hits(hits)
    assert hits[1]["relation"] == "same species"
    assert hits[1]["ani_species_level"] is False


def test_without_metadata_falls_back_to_ani_only() -> None:
    hits = [hit(98.0, None), hit(91.0, None), hit(95.0, None)]
    hits.sort(key=lambda h: -h["ani"])
    assignment, closest = classify_hits(hits)
    assert relations(hits) == ["same species", "same species", "related"]
    assert [h["shared_rank"] for h in hits] == ["species", "species", None]
    assert assignment == {
        "rank": "species",
        "taxon": None,
        "gtdb_taxonomy": None,
        "accession": "GCA_1.1",
        "ani": 98.0,
    }
    assert closest is not None and closest["taxon"] is None


def test_without_metadata_and_no_species_hit_has_no_assignment() -> None:
    hits = [hit(88.0, None), hit(86.0, None)]
    assignment, _ = classify_hits(hits)
    assert assignment is None
    assert relations(hits) == ["related", "related"]


def test_hit_without_lineage_among_classified_hits() -> None:
    hits = [hit(97.0, LACTIS), hit(88.0, None)]
    classify_hits(hits)
    assert hits[1]["relation"] == "unknown lineage"
    assert hits[1]["shared_rank"] is None


def test_no_hits() -> None:
    assert classify_hits([]) == (None, None)


# -- generic lineages (design note B6) ------------------------------------

#: A UHGV_default lineage: prefix-less, the vOTU is the finest unit.
UHGV_FAMILY = "vFAM-00013;vSUBFAM-00074;vGENUS-00044;vSUBGEN-00024;vOTU-052719"
UHGV_SPECIES = "vFAM-00013;vSUBFAM-00074;vGENUS-00044;vSUBGEN-00024;vOTU-052720"
#: An ICTV lineage of the same database, starting with ``root``.
ICTV = "root;Monodnaviria;Sangervirae;Phixviricota;Malgrandaviricetes;Petitvirales;Microviridae"
#: A fungal RefSeq lineage: no domain, kingdom first.
FUNGI = "k__Eukaryota;p__Ascomycota;c__Sordariomycetes;o__Magnaporthales;f__Pyriculariaceae;g__Pyricularia;s__Pyricularia oryzae"


def source_hit(
    ani: float,
    lineage: str | None,
    source: str | None,
    accession: str | None = None,
) -> dict[str, Any]:
    return {
        "ani": ani,
        "accession": accession,
        "gtdb_taxonomy": None,
        "tax_lineage": lineage,
        "tax_source": source,
    }


def test_parse_lineage_understands_every_source() -> None:
    assert parse_lineage(LACTIS).taxonomy == {
        "domain": "Bacteria",
        "phylum": "Bacillota",
        "class": "Bacilli",
        "order": "Lactobacillales",
        "family": "Streptococcaceae",
        "genus": "Lactococcus",
        "species": "Lactococcus lactis",
    }
    levels = parse_lineage(UHGV_FAMILY)
    assert levels.taxonomy == {
        "level 1": "vFAM-00013",
        "level 2": "vSUBFAM-00074",
        "level 3": "vGENUS-00044",
        "level 4": "vSUBGEN-00024",
        "level 5": "vOTU-052719",
    }
    assert levels.levels and levels.taxon == "vOTU-052719"
    assert levels.species is None and levels.species_key == "level 5"
    # "root" is not a taxon: the ICTV ranks are levels too
    ictv = parse_lineage(ICTV)
    assert ictv.ranks[0] == ("level 1", "Monodnaviria")
    assert len(ictv) == 6
    fungi = parse_lineage(FUNGI)
    assert fungi.ranks[0] == ("k", "Eukaryota")
    assert fungi.taxon == "Pyricularia oryzae" and fungi.species_key == "s"
    assert parse_lineage(None) == Lineage()
    assert parse_lineage("") == Lineage()
    assert parse_lineage("d__Bacteria;p__X;g__;s__").taxonomy == {
        "domain": "Bacteria",
        "phylum": "X",
    }


def test_shared_lineage_rank_is_generic() -> None:
    a = parse_lineage(UHGV_FAMILY)
    b = parse_lineage(UHGV_SPECIES)
    assert shared_lineage_rank(a, b) == "level 4"
    assert shared_lineage_rank(a, a) == "level 5"
    assert shared_lineage_rank(a, parse_lineage("vOTHER")) is None
    assert shared_lineage_rank(
        parse_lineage(LACTIS), parse_lineage(CREMORIS)
    ) == "genus"


def test_non_gtdb_best_hit_is_assigned_to_the_last_named_rank() -> None:
    hits = [source_hit(98.0, UHGV_FAMILY, "UHGV_default")]
    assignment, closest = classify_hits(hits)
    assert assignment == {
        "rank": "species",
        "taxon": "vOTU-052719",
        "gtdb_taxonomy": None,
        "accession": None,
        "ani": 98.0,
        "taxonomy": {
            "level 1": "vFAM-00013",
            "level 2": "vSUBFAM-00074",
            "level 3": "vGENUS-00044",
            "level 4": "vSUBGEN-00024",
            "level 5": "vOTU-052719",
        },
        "taxonomy_source": "UHGV_default",
        "note": VIRUS_CAVEAT,
    }
    assert closest == assignment
    assert hits[0]["taxonomy"]["level 5"] == "vOTU-052719"
    assert hits[0]["taxonomy_source"] == "UHGV_default"
    assert hits[0]["relation"] == "same species"


def test_icv_and_imgvr_sources_are_viruses_too() -> None:
    for lineage, source in ((ICTV, "UHGV_ictv"), (UHGV_FAMILY, "IMGVR_4.1")):
        assignment, _ = classify_hits([source_hit(99.0, lineage, source)])
        assert assignment is not None and assignment["note"] == VIRUS_CAVEAT


def test_fungi_get_the_fungal_caveat() -> None:
    hits = [source_hit(97.0, FUNGI, "FungiRefSeq-latest")]
    assignment, closest = classify_hits(hits)
    assert assignment is not None
    assert assignment["taxon"] == "Pyricularia oryzae"
    assert assignment["taxonomy"]["kingdom"] == "Eukaryota"
    assert assignment["note"] == FUNGI_CAVEAT
    assert closest is not None and closest["note"] == FUNGI_CAVEAT
    # k__Fungi identifies a fungal lineage even under another source name
    assert species_caveat(parse_lineage("k__Fungi;p__X", source="Local")) == FUNGI_CAVEAT


def test_other_sources_get_the_generic_caveat() -> None:
    lineage = "d__Bacteria;p__Bacillota;c__Bacilli"
    hits = [source_hit(97.0, lineage, "SoilSMAG")]
    assignment, _ = classify_hits(hits)
    assert assignment is not None
    assert assignment["note"] == GENERIC_CAVEAT.format(source="SoilSMAG")


def test_below_the_threshold_the_note_is_on_the_closest_hit() -> None:
    hits = [source_hit(90.0, UHGV_FAMILY, "UHGV_default")]
    assignment, closest = classify_hits(hits)
    assert assignment is None
    assert closest is not None and closest["note"] == VIRUS_CAVEAT
    assert closest["taxon"] == "vOTU-052719"


def test_hits_of_two_sources_have_no_shared_rank() -> None:
    hits = [
        source_hit(98.0, UHGV_FAMILY, "UHGV_default"),
        source_hit(97.0, UHGV_SPECIES, "UHGV_default"),
        source_hit(96.0, FUNGI, "FungiRefSeq-latest"),
        hit(95.5, LACTIS, "GCF_L"),
        source_hit(95.0, UHGV_FAMILY, "UHGV_ictv"),
    ]
    assignment, _ = classify_hits(hits)
    assert assignment is not None and assignment["taxon"] == "vOTU-052719"
    assert [h["shared_rank"] for h in hits] == ["species", "level 4", None, None, None]
    assert relations(hits) == [
        "same species",
        "same level 4",
        "different source",
        "different source",
        "different source",
    ]
    # the hit's own taxonomy is still reported: only the *relation* is unknown
    assert hits[2]["taxonomy_source"] == "FungiRefSeq-latest"
    assert hits[3]["taxonomy_source"] == "GTDB"
    # a GTDB hit compares with GTDB hits of another tax_source: one family
    gtdb = [hit(97.0, LACTIS), {**hit(96.0, CREMORIS), "tax_source": "GTDB_r214"}]
    classify_hits(gtdb)
    assert gtdb[1]["shared_rank"] == "genus"


def test_without_assignment_a_cross_source_hit_says_so() -> None:
    hits = [
        source_hit(90.0, UHGV_FAMILY, "UHGV_default"),
        source_hit(89.0, FUNGI, "FungiRefSeq-latest"),
    ]
    classify_hits(hits)
    assert hits[1]["relation"] == "different source from closest relative"
    assert hits[1]["shared_rank"] is None


def test_taxonomy_keys_are_absent_without_a_lineage() -> None:
    hits = [hit(98.0, None)]
    assignment, _ = classify_hits(hits)
    assert assignment is not None
    assert "taxonomy" not in assignment and "note" not in assignment
    assert "taxonomy" not in hits[0] and "taxonomy_source" not in hits[0]


def test_a_non_gtdb_source_wins_over_gtdb_metadata() -> None:
    """The source that resolved the hit says which taxonomy it belongs to."""
    hits = [
        {
            "ani": 99.0,
            "accession": "GCA_1.1",
            "gtdb_taxonomy": LACTIS,
            "tax_lineage": UHGV_FAMILY,
            "tax_source": "UHGV_default",
        },
        {**hit(98.0, LACTIS, "GCA_2.1"), "tax_source": "GTDB_r232"},
    ]
    assignment, _ = classify_hits(hits)
    assert assignment is not None
    assert assignment["taxon"] == "vOTU-052719"
    assert assignment["gtdb_taxonomy"] is None  # it is not a GTDB call
    assert hits[0]["taxonomy_source"] == "UHGV_default"
    assert hits[1]["taxonomy_source"] == "GTDB_r232"
    assert hits[1]["relation"] == "different source"

