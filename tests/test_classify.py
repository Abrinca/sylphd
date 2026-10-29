"""Lineage-based decision engine for ``POST /assembly`` (pure functions)."""

from __future__ import annotations

from typing import Any

import pytest

from sylphd.assembly import classify_hits, parse_gtdb_taxonomy, shared_rank

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
    }
    assert closest == assignment
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
