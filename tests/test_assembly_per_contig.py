"""The per-contig classification: ``POST /contigs`` and what the server makes of it.

The command that sends the contigs (``sylphd assembly --per-contig``) lives in the
native client now; its end-to-end tests are in ``tests/test_client_binary.py``.
This file keeps the server half: the endpoint (validation, admission, the response
shape, the per-request summary) through ``TestClient``, the contig-relative ANI,
the consensus and the parsimony ranking that ``per_contig`` computes.

``ani_data`` builds a small synthetic database from a 2 Mb random genome and a
3 %-mutated variant.  It is what makes the contig-relative ANI checkable: an
exact 30 kb fragment must score 100 % even though it is 1.5 % of the genome.

``contaminant_data`` is the parsimony regression: a bacterial genome that
carries a 20 kb phage segment as one of its contigs, plus the phage genome on
its own.  The phage contig is contained in both, so only the ranking can put
the phage first.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import dnaio
import httpx
import pysylph
import pytest
from conftest import CLODF13_FASTA
from starlette.testclient import TestClient

from sylphd import per_contig
from sylphd.server import ServerConfig, create_app


@dataclass(frozen=True)
class AniData:
    reference: str
    variant: str
    foreign: str
    database: Path


def _mutate(sequence: str, rate: float, seed: int) -> str:
    rng = random.Random(seed)
    bases = list(sequence)
    for position in rng.sample(range(len(bases)), round(rate * len(bases))):
        current = bases[position]
        bases[position] = rng.choice([base for base in "ACGT" if base != current])
    return "".join(bases)


@pytest.fixture(scope="session")
def ani_data(tmp_path_factory) -> AniData:
    """A 2 Mb random genome, a 3 %-mutated variant, an unrelated genome."""
    directory = tmp_path_factory.mktemp("per-contig-ani")
    rng = random.Random(20261029)
    reference = "".join(rng.choices("ACGT", k=2_000_000))
    variant = _mutate(reference, 0.03, 20261030)
    foreign = "".join(random.Random(20261031).choices("ACGT", k=2_000_000))
    sketcher = pysylph.Sketcher(c=200, k=31)
    database = directory / "ani.syldb"
    pysylph.Database(
        [
            sketcher.sketch_genome("GCA_000000900.1_genomic.fna.gz", [reference]),
            sketcher.sketch_genome("GCA_000000901.1_genomic.fna.gz", [variant]),
            sketcher.sketch_genome("GCA_000000902.1_genomic.fna.gz", [foreign]),
        ]
    ).dump(str(database))
    return AniData(
        reference=reference, variant=variant, foreign=foreign, database=database
    )


@pytest.fixture(scope="module")
def genome() -> str:
    with dnaio.open(CLODF13_FASTA) as reader:
        return next(iter(reader)).sequence


@pytest.fixture
def fragmented(tmp_path: Path, genome: str) -> Path:
    """Three long contigs (one id repeated), one short, one low-complexity."""
    path = tmp_path / "fragmented.fa"
    path.write_text(
        ">c1 first window\n"
        + genome[:6000]
        + "\n>c2 second window\n"
        + genome[-6000:]
        + "\n>c1 duplicate id\n"
        + genome[1000:7000]
        + "\n>short one\n"
        + genome[:500]
        + "\n>tiny low complexity\n"
        + "A" * 3000
        + "\n"
    )
    return path


# ---------------------------------------------------------------------------
# table, statuses and JSON
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# chunking
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# realistic per-contig ANI
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# taxonomic consensus
# ---------------------------------------------------------------------------


def _taxonomy(
    species: str, genus: str = "Bacillus", family: str = "Bacillaceae"
) -> str:
    return (
        "d__Bacteria;p__Bacillota;c__Bacilli;o__Bacillales;"
        f"f__{family};g__{genus};s__{species}"
    )


def _consensus_hit(
    ani: float,
    *,
    contig_kmers: int = 400,
    species: str | None = None,
    genus: str = "Bacillus",
    taxonomy: str | None = None,
    accession: str = "GCA_000000001.1",
) -> dict[str, object]:
    """One hit as :func:`per_contig.consensus` sees it (``shared`` from ANI)."""
    shared = round(contig_kmers * (ani / 100.0) ** 31)
    if taxonomy is None and species is not None:
        taxonomy = _taxonomy(species, genus)
    return {
        "ani": ani,
        "shared_kmers": shared,
        "gtdb_taxonomy": taxonomy,
        "accession": accession,
    }


def _query_row(
    shared: int,
    *,
    accession: str,
    species: str,
    genus: str = "Bacillus",
    total: int = 10_000,
) -> dict[str, object]:
    """One ``POST /query`` row as the server returns it."""
    return {
        "Containment_ind": [shared, total],
        "Naive_ANI": 85.0,
        "gtdb_taxonomy": _taxonomy(species, genus),
        "accession": accession,
        "Genome_file": f"{accession}_genomic.fna.gz",
    }


def test_min_ani_never_changes_the_consensus() -> None:
    """--min-ani hides rows; it must not turn an ambiguous genus into a species."""
    rows = [
        _query_row(85, accession="GCA_000000001.1", species="Bacillus subtilis"),
        _query_row(84, accession="GCA_000000002.1", species="Bacillus velezensis"),
    ]
    loose = per_contig.contig_hits(
        rows,
        k=31,
        contig_kmers=85,
        options=per_contig.ContigOptions(min_ani=80.0, top=2),
    )
    strict = per_contig.contig_hits(
        rows,
        k=31,
        contig_kmers=85,
        options=per_contig.ContigOptions(min_ani=100.0, top=2),
    )
    assert [hit["accession"] for hit in loose[0]] == [
        "GCA_000000001.1",
        "GCA_000000002.1",
    ]
    assert [hit["accession"] for hit in strict[0]] == ["GCA_000000001.1"]
    for hits, assignment, closest, summary in (loose, strict):
        assert hits and assignment is not None and closest is not None
        assert summary is not None
        assert (summary["rank"], summary["taxon"]) == ("genus", "Bacillus")
        assert summary["n_close"] == 2

    # Nothing passes --min-ani: the contig has no hits and no consensus, even
    # though the complete set would have produced one.
    below = per_contig.contig_hits(
        rows[1:], k=31, contig_kmers=85, options=per_contig.ContigOptions(min_ani=100.0)
    )
    assert below == ([], None, None, None)


def test_exact_ties_are_canonical() -> None:
    """Equal evidence is ordered by accession, not by the backend's row order."""
    first = _query_row(50, accession="GCA_000000009.1", species="Bacillus subtilis")
    second = _query_row(50, accession="GCA_000000001.1", species="Bacillus velezensis")
    options = per_contig.ContigOptions(top=2)
    forward = per_contig.contig_hits([first, second], k=31, contig_kmers=85, options=options)
    reverse = per_contig.contig_hits([second, first], k=31, contig_kmers=85, options=options)
    assert [hit["accession"] for hit in forward[0]] == [
        "GCA_000000001.1",
        "GCA_000000009.1",
    ]
    assert forward == reverse


def test_ani_noise_matches_the_binomial_formula() -> None:
    assert per_contig.ani_noise(100, 100, 31) == 0.0  # exact match: no noise
    assert per_contig.ani_noise(0, 100, 31) == math.inf  # no estimate at all
    assert per_contig.ani_noise(5, 0, 31) == math.inf
    expected = (100 / 31) * math.sqrt((1 - 0.95) / (100 * 0.95))
    assert per_contig.ani_noise(95, 100, 31) == pytest.approx(expected)
    # more k-mers, less noise (the sd is ~1/sqrt(n))
    assert per_contig.ani_noise(950, 1000, 31) < per_contig.ani_noise(95, 100, 31)


def test_consensus_of_a_single_species_is_that_species() -> None:
    result = per_contig.consensus(
        [_consensus_hit(100.0, species="Bacillus subtilis")], k=31, contig_kmers=400
    )
    assert result == {
        "rank": "species",
        "taxon": "Bacillus subtilis",
        "n_close": 1,
        "species": ["Bacillus subtilis"],
    }


def test_species_consensus_needs_the_species_threshold() -> None:
    """A lone close species at 91.75 % is a genus call, not a species call."""
    result = per_contig.consensus(
        [
            _consensus_hit(
                91.75, species="Leptospira alexanderi", genus="Leptospira", accession="A"
            )
        ],
        k=31,
        contig_kmers=440,
    )
    assert (result["rank"], result["taxon"]) == ("genus", "Leptospira")
    assert result["n_close"] == 1
    assert result["species"] == ["Leptospira alexanderi"]

    at_threshold = per_contig.consensus(
        [
            _consensus_hit(
                95.0, species="Leptospira alexanderi", genus="Leptospira", accession="A"
            )
        ],
        k=31,
        contig_kmers=440,
    )
    assert (at_threshold["rank"], at_threshold["taxon"]) == (
        "species",
        "Leptospira alexanderi",
    )


def test_consensus_99_9_vs_99_1_stays_at_species() -> None:
    """A 0.8-point gap at 99 % is further than the noise: the best species holds."""
    result = per_contig.consensus(
        [
            _consensus_hit(99.9, species="Bacillus subtilis", accession="A"),
            _consensus_hit(99.1, species="Bacillus velezensis", accession="B"),
        ],
        k=31,
        contig_kmers=400,
    )
    assert result["n_close"] == 1
    assert (result["rank"], result["taxon"]) == ("species", "Bacillus subtilis")


def test_consensus_89_9_vs_89_1_widens_to_genus() -> None:
    """At 89 %, a 0.8-point gap is within ``ratio`` of a 10-point divergence."""
    result = per_contig.consensus(
        [
            _consensus_hit(89.9, species="Bacillus subtilis", accession="A"),
            _consensus_hit(89.1, species="Bacillus velezensis", accession="B"),
        ],
        k=31,
        contig_kmers=400,
    )
    assert result["n_close"] == 2
    assert (result["rank"], result["taxon"]) == ("genus", "Bacillus")
    assert result["species"] == ["Bacillus subtilis", "Bacillus velezensis"]


def test_consensus_reduces_to_the_best_hit_per_species() -> None:
    """Three hits of two species: n_close counts species, not rows."""
    result = per_contig.consensus(
        [
            _consensus_hit(99.9, species="Bacillus subtilis", accession="A"),
            _consensus_hit(99.5, species="Bacillus subtilis", accession="A2"),
            _consensus_hit(99.8, species="Bacillus velezensis", accession="B"),
        ],
        k=31,
        contig_kmers=400,
    )
    assert result["n_close"] == 2
    assert result["species"] == ["Bacillus subtilis", "Bacillus velezensis"]


def test_consensus_noise_widens_for_a_short_contig() -> None:
    """The same two hits: genus evidence at 10 k-mers, species at 1000."""
    weak = per_contig.consensus(
        [
            _consensus_hit(99.0, contig_kmers=10, species="A species", accession="A"),
            _consensus_hit(95.0, contig_kmers=10, species="B species", accession="B"),
        ],
        k=31,
        contig_kmers=10,
    )
    strong = per_contig.consensus(
        [
            _consensus_hit(99.0, contig_kmers=1000, species="A species", accession="A"),
            _consensus_hit(95.0, contig_kmers=1000, species="B species", accession="B"),
        ],
        k=31,
        contig_kmers=1000,
    )
    assert weak["n_close"] == 2
    assert strong["n_close"] == 1
    assert strong["rank"] == "species"


def test_consensus_ratio_and_z_control_the_band() -> None:
    hits = [
        _consensus_hit(97.0, species="Bacillus subtilis", accession="A"),
        _consensus_hit(95.5, species="Bacillus velezensis", accession="B"),
    ]
    wide = per_contig.consensus(hits, k=31, contig_kmers=400)
    narrow = per_contig.consensus(hits, k=31, contig_kmers=400, ratio=1.0, z=0.0)
    assert wide["rank"] == "genus"
    assert narrow["rank"] == "species"


def test_consensus_without_taxonomy_falls_back_to_empty() -> None:
    assert per_contig.consensus([], k=31, contig_kmers=400) is None
    alone = per_contig.consensus(
        [_consensus_hit(100.0, taxonomy=None, accession="GCA_000000009.1")],
        k=31,
        contig_kmers=400,
    )
    assert (alone["rank"], alone["taxon"]) == (None, None)
    assert alone["n_close"] == 1
    assert alone["species"] == ["GCA_000000009.1"]
    # a close species without a lineage cannot be placed: consensus is empty
    mixed = per_contig.consensus(
        [
            _consensus_hit(99.9, species="Bacillus subtilis", accession="A"),
            _consensus_hit(99.8, taxonomy=None, accession="GCA_000000009.1"),
        ],
        k=31,
        contig_kmers=400,
    )
    assert mixed["n_close"] == 2
    assert (mixed["rank"], mixed["taxon"]) == (None, None)


def _source_hit(
    ani: float,
    lineage: str,
    source: str,
    *,
    contig_kmers: int = 400,
    accession: str | None = None,
) -> dict[str, object]:
    """One non-GTDB hit as :func:`per_contig.consensus` sees it."""
    shared = round(contig_kmers * (ani / 100.0) ** 31)
    return {
        "ani": ani,
        "shared_kmers": shared,
        "gtdb_taxonomy": None,
        "tax_lineage": lineage,
        "tax_source": source,
        "accession": accession,
    }


def test_consensus_of_prefix_less_lineages_uses_levels() -> None:
    """UHGV lineages have no rank prefixes: the consensus is a ``level N``."""
    family = "vFAM-00013;vSUBFAM-00074;vGENUS-00044;vSUBGEN-00024"
    result = per_contig.consensus(
        [
            _source_hit(99.9, f"{family};vOTU-052719", "UHGV_default", accession="A"),
            _source_hit(99.8, f"{family};vOTU-052720", "UHGV_default", accession="B"),
        ],
        k=31,
        contig_kmers=400,
    )
    assert (result["rank"], result["taxon"]) == ("level 4", "vSUBGEN-00024")
    assert result["n_close"] == 2
    assert result["species"] == ["A", "B"]


def test_prefix_less_consensus_caps_below_the_species_threshold() -> None:
    """A lone vOTU at 91 % is capped at the level above it, like genus for GTDB."""
    lineage = "vFAM-00013;vSUBFAM-00074;vGENUS-00044;vSUBGEN-00024;vOTU-052719"
    result = per_contig.consensus(
        [_source_hit(91.0, lineage, "UHGV_default", accession="A")],
        k=31,
        contig_kmers=440,
    )
    assert (result["rank"], result["taxon"]) == ("level 4", "vSUBGEN-00024")
    at_threshold = per_contig.consensus(
        [_source_hit(95.0, lineage, "UHGV_default", accession="A")],
        k=31,
        contig_kmers=440,
    )
    assert (at_threshold["rank"], at_threshold["taxon"]) == ("level 5", "vOTU-052719")


def test_consensus_of_fungal_lineages_starts_at_the_kingdom() -> None:
    result = per_contig.consensus(
        [
            _source_hit(
                99.0,
                "k__Eukaryota;p__Ascomycota;c__Sordariomycetes;o__Magnaporthales;"
                "f__Pyriculariaceae;g__Pyricularia;s__Pyricularia oryzae",
                "FungiRefSeq-latest",
                accession="GCF_000002495.2_MG8",
            )
        ],
        k=31,
        contig_kmers=400,
    )
    assert (result["rank"], result["taxon"]) == ("species", "Pyricularia oryzae")
    assert result["species"] == ["Pyricularia oryzae"]


def test_consensus_across_sources_is_mixed_sources() -> None:
    """Lineages of two sources cannot be intersected (design note B6)."""
    result = per_contig.consensus(
        [
            _source_hit(99.9, "vFAM-1;vOTU-1", "UHGV_default", accession="A"),
            _source_hit(
                99.8,
                "k__Eukaryota;p__Ascomycota;s__Pyricularia oryzae",
                "FungiRefSeq-latest",
                accession="B",
            ),
        ],
        k=31,
        contig_kmers=400,
    )
    assert (result["rank"], result["taxon"]) == (None, None)
    assert result["n_close"] == 2
    assert result["mixed_sources"] is True
    assert "mixed_sources" not in per_contig.consensus(
        [_source_hit(99.9, "vFAM-1;vOTU-1", "UHGV_default", accession="A")],
        k=31,
        contig_kmers=400,
    )


def test_contig_hits_classify_and_report_a_non_gtdb_hit() -> None:
    """A ``/query`` row resolved by a sylph-tax source gets its generic taxon."""
    rows = [
        {
            "Containment_ind": [85, 10_000],
            "Naive_ANI": 90.0,
            "gtdb_taxonomy": None,
            "tax_lineage": "vFAM-1;vOTU-9",
            "tax_source": "UHGV_default",
            "accession": None,
            "Genome_file": "votus_full.fna.gz",
        }
    ]
    hits, assignment, closest, summary = per_contig.contig_hits(
        rows, k=31, contig_kmers=85, options=per_contig.ContigOptions()
    )
    assert assignment is not None and closest is not None
    assert assignment["taxon"] == "vOTU-9"
    assert assignment["note"].startswith("UHGV_default (viruses)")
    assert hits[0]["taxon"] == "vOTU-9"
    assert hits[0]["taxonomy"] == {"level 1": "vFAM-1", "level 2": "vOTU-9"}
    assert hits[0]["taxonomy_source"] == "UHGV_default"
    assert summary is not None and (summary["rank"], summary["taxon"]) == (
        "level 2",
        "vOTU-9",
    )


# ---------------------------------------------------------------------------
# inputs and output modes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# POST /contigs
# ---------------------------------------------------------------------------


def _sketch_contig(sequence: str, name: str) -> tuple[bytes, int, float]:
    """The sample sketch of one contig, its genome-style count and its GC."""
    sketcher = pysylph.Sketcher(c=200, k=31)
    sample = sketcher.sketch_single(name, [sequence])
    kmers = len(sketcher.sketch_genome(name, [sequence], profiling=False).kmers)
    gc = round(100.0 * (sequence.count("G") + sequence.count("C")) / len(sequence), 2)
    return sample.to_bytes(), kmers, gc


def _contig_spec(name: str, sequence: str, kmers: int, gc: float) -> dict[str, object]:
    return {"name": name, "contig_kmers": kmers, "length": len(sequence), "gc": gc}


def _post_contigs(
    client: TestClient,
    payloads: list[tuple[str, bytes]],
    params: dict[str, object],
):
    return client.post(
        "/contigs",
        data={"params": json.dumps(params)},
        files=[("sketch", (f"{name}.sylsp", data)) for name, data in payloads],
    )


@pytest.fixture(scope="module")
def endpoint_data(ani_data: AniData) -> tuple[bytes, dict[str, object]]:
    """One 40 kb exact fragment of the fixture genome, as a sketch and a spec."""
    sequence = ani_data.reference[100_000:140_000]
    payload, kmers, gc = _sketch_contig(sequence, "contig1")
    return payload, _contig_spec("contig1", sequence, kmers, gc)


@pytest.fixture(scope="module")
def endpoint_app(ani_data: AniData):
    with TestClient(create_app(ServerConfig(db_path=ani_data.database))) as client:
        yield client


def test_info_advertises_the_contigs_endpoint(endpoint_app: TestClient) -> None:
    info = endpoint_app.get("/info").json()
    assert "/contigs" in info["endpoints"]
    assert info["limits"]["max_params_bytes"] == 1024 * 1024
    assert info["limits"]["max_contigs"] == 4096


def test_max_contigs_is_advertised_and_enforced(ani_data: AniData) -> None:
    """``--max-contigs`` bounds one request and /info tells clients the bound."""
    with TestClient(
        create_app(ServerConfig(db_path=ani_data.database, max_contigs=2))
    ) as client:
        assert client.get("/info").json()["limits"]["max_contigs"] == 2
        payloads = []
        specs = []
        for index in range(3):
            sequence = ani_data.reference[index * 5000 : index * 5000 + 40_000]
            payload, kmers, gc = _sketch_contig(sequence, f"c{index}")
            payloads.append((f"c{index}", payload))
            specs.append(_contig_spec(f"c{index}", sequence, kmers, gc))

        too_many = _post_contigs(client, payloads, {"contigs": specs})
        assert too_many.status_code == 400
        error = too_many.json()["error"]
        assert "too many contigs" in error
        assert "at most 2 contigs" in error
        assert "limits.max_contigs" in error

        accepted = _post_contigs(client, payloads[:2], {"contigs": specs[:2]})
        assert accepted.status_code == 200
        assert len(accepted.json()["contigs"]) == 2


def test_max_contigs_stops_parsing_before_the_rest_is_spooled(
    ani_data: AniData,
) -> None:
    """The parser stops at the (N+1)th file part instead of reading the body.

    The body is streamed lazily and far larger than the limit; if the parser
    kept going (as it did with ``max_files=limit + 1`` until the explicit
    check), every upload would be written to a temporary file. Counting the
    chunks the app pulls shows it stops right after the first refused part.
    """
    boundary = "----sylphdcontigstest"

    def part(index: int) -> bytes:
        return (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="sketch"; '
            f'filename="c{index}.sylsp"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + b"x\r\n"

    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="params"\r\n\r\n{}\r\n'
    ).encode()
    body += b"".join(part(index) for index in range(300))
    body += f"--{boundary}--\r\n".encode()

    chunk_size = 256
    total_chunks = math.ceil(len(body) / chunk_size)
    pulled = 0

    async def chunks():
        nonlocal pulled
        for start in range(0, len(body), chunk_size):
            pulled += 1
            yield body[start : start + chunk_size]

    async def post():
        app = create_app(ServerConfig(db_path=ani_data.database, max_contigs=2))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return await client.post(
                "/contigs",
                content=chunks(),
                headers={
                    "content-type": f"multipart/form-data; boundary={boundary}"
                },
            )

    response = asyncio.run(post())
    assert response.status_code == 400
    error = response.json()["error"]
    assert "too many contigs" in error
    assert "at most 2 contigs" in error
    assert "limits.max_contigs" in error
    # Two accepted parts fit in a couple of chunks; the remaining ~300 parts
    # were never read.
    assert 0 < pulled < total_chunks // 10, (pulled, total_chunks)


def test_floor_contigs_never_reach_the_profiler(
    endpoint_app: TestClient, monkeypatch
) -> None:
    """A sketch below the sample k-mer floor is answered without a query."""
    sequence = "".join(random.Random(6).choices("ACGT", k=300))
    payload, kmers, gc = _sketch_contig(sequence, "tiny")
    calls = []

    def counting(*args, **kwargs):
        calls.append(args)
        raise AssertionError("the profiler ran for a floor contig")

    monkeypatch.setattr("sylphd.server.run_contig_query", counting)
    response = _post_contigs(
        endpoint_app,
        [("tiny", payload)],
        {"contigs": [_contig_spec("tiny", sequence, kmers, gc)]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["contigs"]["tiny"]["status"] == "too_few_kmers"
    assert calls == []


def test_contig_kmers_zero_is_rejected_before_profiling(
    endpoint_app: TestClient, monkeypatch
) -> None:
    """A taste of ``contig_kmers == 0`` is a 400, never a query."""
    sequence = "A" * 1000
    payload, _, gc = _sketch_contig(sequence, "zero")
    monkeypatch.setattr(
        "sylphd.server.run_contig_query",
        lambda *args, **kwargs: pytest.fail("the profiler ran"),
    )
    response = _post_contigs(
        endpoint_app,
        [("zero", payload)],
        {"contigs": [_contig_spec("zero", sequence, 0, gc)]},
    )
    assert response.status_code == 400
    assert "contig_kmers must be between" in response.json()["error"]


def test_contigs_endpoint_response(endpoint_app, endpoint_data) -> None:
    payload, spec = endpoint_data
    response = _post_contigs(
        endpoint_app, [("contig1", payload)], {"contigs": [spec], "top": 2, "fields": []}
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "api_version",
        "table",
        "contigs",
        "summary",
        "caveats",
        "database",
        "params",
        "thresholds",
        "elapsed_ms",
    }
    assert body["api_version"] == 1
    assert body["database"] == {
        "name": ani_name(endpoint_app),
        "genomes": 3,
        "c": 200,
        "k": 31,
    }
    assert body["params"] == {
        "top": 2,
        "min_ani": 80.0,
        "consensus_ratio": 1.5,
        "consensus_z": 2.0,
        "fields": [],
    }
    assert body["thresholds"] == {"species": 95, "imprecise_below": 85}
    assert body["caveats"] == []
    assert body["elapsed_ms"] >= 0

    assert list(body["contigs"]) == ["contig1"]
    result = body["contigs"]["contig1"]
    assert result["status"] == "ok"
    assert result["length"] == spec["length"]
    assert result["contig_kmers"] == spec["contig_kmers"]
    assert result["sketch_kmers"] >= result["contig_kmers"]
    assert result["assignment"]["accession"] == "GCA_000000900.1"
    assert result["closest"]["accession"] == "GCA_000000900.1"

    table = body["table"]
    assert table["columns"] == list(per_contig.TABLE_COLUMNS)
    rows = [dict(zip(table["columns"], values)) for values in table["data"]]
    hit = rows[0]
    assert hit["contig"] == "contig1"
    assert hit["status"] == "ok"
    assert hit["rank"] == 1
    assert hit["accession"] == "GCA_000000900.1"
    assert hit["ani"] == pytest.approx(100.0, abs=0.05)
    assert hit["shared_kmers"] == hit["contig_kmers"] == spec["contig_kmers"]
    assert hit["explained_fraction"] == pytest.approx(1.0, abs=0.01)
    assert hit["taxon"] is None  # no metadata on this server
    assert "gtdb_taxonomy" not in hit  # and no metadata columns were requested

    summary = body["summary"]
    assert summary["contigs"] == 1
    assert summary["bp"] == spec["length"]
    assert summary["queried_contigs"] == 1
    assert summary["queried_bp"] == spec["length"]
    assert summary["too_few_kmers_contigs"] == 0
    assert summary["no_hit_contigs"] == 0
    assert summary["consensus_bp"] == [
        {"rank": None, "taxon": "unassigned", "bp": spec["length"], "fraction": 1.0}
    ]


def ani_name(client: TestClient) -> str:
    return client.get("/info").json()["database"]["name"]


def test_contigs_endpoint_too_few_kmers(endpoint_app: TestClient) -> None:
    """A sketch below the k-mer floor is reported, not queried."""
    sequence = "".join(random.Random(5).choices("ACGT", k=300))
    payload, kmers, gc = _sketch_contig(sequence, "tiny")
    assert kmers >= 1
    response = _post_contigs(
        endpoint_app,
        [("tiny", payload)],
        {"contigs": [_contig_spec("tiny", sequence, kmers, gc)]},
    )
    assert response.status_code == 200
    body = response.json()
    result = body["contigs"]["tiny"]
    assert result["status"] == "too_few_kmers"
    assert "too few k-mers" in result["reason"]
    assert result["consensus"] is None
    # Contig without hits appears as one row with null hit columns.
    (row_vals,) = body["table"]["data"]
    row = dict(zip(body["table"]["columns"], row_vals))
    assert row["contig"] == "tiny"
    assert row["status"] == "too_few_kmers"
    assert row["rank"] is None
    assert row["ani"] is None
    assert row["consensus_rank"] is None
    assert body["summary"]["too_few_kmers_contigs"] == 1
    assert body["summary"]["queried_contigs"] == 0


def test_slow_scoring_stays_off_the_event_loop(
    ani_data: AniData, endpoint_data, monkeypatch
) -> None:
    """Scoring runs in the worker too: /health answers during a /contigs job."""
    payload, spec = endpoint_data
    started = threading.Event()
    release = threading.Event()
    original = per_contig.contig_result

    def slow_scorer(*args, **kwargs):
        started.set()
        assert release.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr("sylphd.server.per_contig.contig_result", slow_scorer)
    app = create_app(ServerConfig(db_path=ani_data.database))
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            _post_contigs, client, [("contig1", payload)], {"contigs": [spec]}
        )
        assert started.wait(timeout=5)
        began = time.monotonic()
        response = client.get("/health")
        elapsed = time.monotonic() - began
        release.set()
        assert response.status_code == 200
        assert elapsed < 2.0
        assert future.result(timeout=30).status_code == 200


def test_duplicate_fields_are_deduplicated(
    tiny_database, tiny_metadata, genome
) -> None:
    """Repeated metadata columns are fetched once and echoed once."""
    with TestClient(
        create_app(ServerConfig(db_path=tiny_database, meta_path=tiny_metadata))
    ) as client:
        sequence = genome[:40_000]
        payload, kmers, gc = _sketch_contig(sequence, "c1")
        response = _post_contigs(
            client,
            [("c1", payload)],
            {
                "contigs": [_contig_spec("c1", sequence, kmers, gc)],
                "fields": ["gtdb_taxonomy", "gtdb_taxonomy"],
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["params"]["fields"] == ["gtdb_taxonomy"]
    row = dict(zip(body["table"]["columns"], body["table"]["data"][0]))
    assert row["gtdb_taxonomy"]


@pytest.mark.parametrize("field", ["gc", "taxon", "accession", "contig", "status"])
def test_contigs_field_collision_is_400(
    tiny_database, tiny_metadata, genome, field: str
) -> None:
    """A requested field colliding with a fixed /contigs table column is a 400."""
    with TestClient(
        create_app(ServerConfig(db_path=tiny_database, meta_path=tiny_metadata))
    ) as client:
        sequence = genome[:40_000]
        payload, kmers, gc = _sketch_contig(sequence, "c1")
        response = _post_contigs(
            client,
            [("c1", payload)],
            {
                "contigs": [_contig_spec("c1", sequence, kmers, gc)],
                "fields": [field],
            },
        )
    assert response.status_code == 400, response.text
    assert "collide" in response.json()["error"]
    assert field in response.json()["error"]


def test_contigs_endpoint_requires_multipart(endpoint_app: TestClient) -> None:
    response = endpoint_app.post("/contigs", content=b"x")
    assert response.status_code == 415
    assert "multipart" in response.json()["error"]


def test_contigs_endpoint_needs_a_sketch(endpoint_app: TestClient) -> None:
    response = endpoint_app.post(
        "/contigs", files={"params": (None, json.dumps({"contigs": []}))}
    )
    assert response.status_code == 400
    assert "no 'sketch' part" in response.json()["error"]


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"contigs": None}, "'params.contigs' must be a list"),
        (
            {"contigs": [{"name": "contig1", "length": 40000, "gc": 50.0}]},
            "contig_kmers must be an integer",
        ),
        ({"contigs": [{"name": "contig1", "contig_kmers": 0, "length": 40000, "gc": 50.0}]}, "must be between 1 and"),
        ({"contigs": [{"name": "contig1", "contig_kmers": 40_000, "length": 40000, "gc": 50.0}]}, "at most"),
        ({"contigs": [{"name": "a\tb", "contig_kmers": 100, "length": 40000, "gc": 50.0}]}, "control character"),
        ({"contigs": [{"name": "", "contig_kmers": 100, "length": 40000, "gc": 50.0}]}, "is empty"),
        ({"contigs": [{"name": "c", "contig_kmers": 100, "length": 0, "gc": 50.0}]}, "must be between 1 and"),
        ({"contigs": [{"name": "c", "contig_kmers": 100, "length": 40000, "gc": 101}]}, "gc must be between"),
        ({"contigs": [{"name": "c", "contig_kmers": 100, "length": 40000, "gc": 50.0, "x": 1}]}, "unknown key"),
        ({"contigs": [{"name": "c", "contig_kmers": "100", "length": 40000, "gc": 50.0}]}, "must be an integer"),
        ({"top": 0}, "'top' must be >= 1"),
        ({"top": 51}, "'top' must be <= 50"),
        ({"min_ani": 50}, "'min_ani' must be >= 70"),
        ({"consensus_ratio": 0.5}, "'consensus_ratio' must be >= 1"),
        ({"consensus_z": -1}, "'consensus_z' must be >= 0"),
        ({"mode": "query"}, "unknown parameter"),
        ({"fields": ["nope"]}, "no metadata database"),
        ({"tax_sources": ["nope"]}, "no sylph-tax taxonomy sources"),
    ],
)
def test_contigs_endpoint_rejects_bad_input(
    endpoint_app: TestClient, endpoint_data, overrides, needle: str
) -> None:
    payload, spec = endpoint_data
    params = {"contigs": [spec], **overrides}
    response = _post_contigs(endpoint_app, [("contig1", payload)], params)
    assert response.status_code == 400, response.text
    assert needle in response.json()["error"]


def test_contigs_endpoint_counts_must_match_the_sketches(
    endpoint_app: TestClient, endpoint_data
) -> None:
    payload, spec = endpoint_data
    response = _post_contigs(
        endpoint_app, [("contig1", payload)], {"contigs": [spec, spec]}
    )
    assert response.status_code == 400
    assert "2 entries for 1 sketches" in response.json()["error"]


def test_contigs_endpoint_rejects_duplicate_names(
    endpoint_app: TestClient, endpoint_data
) -> None:
    payload, spec = endpoint_data
    response = _post_contigs(
        endpoint_app,
        [("contig1", payload), ("contig1", payload)],
        {"contigs": [spec, spec]},
    )
    assert response.status_code == 400
    assert "repeats 'contig1'" in response.json()["error"]


def test_contigs_request_is_one_job(ani_data: AniData, endpoint_data) -> None:
    """--max-jobs 1 --max-queue 1 admits a whole request, not one job per contig."""
    payload, spec = endpoint_data
    specs = [dict(spec, name=f"contig{index}") for index in range(1, 6)]
    app = create_app(ServerConfig(db_path=ani_data.database, max_jobs=1, max_queue=1))
    with TestClient(app) as client:
        response = _post_contigs(
            client,
            [(item["name"], payload) for item in specs],
            {"contigs": specs},
        )
        assert response.status_code == 200, response.text
        assert list(response.json()["contigs"]) == [
            item["name"] for item in specs
        ]


def test_100_contigs_are_one_admitted_request(
    ani_data: AniData, endpoint_data
) -> None:
    """The public policy (--max-jobs 2 --max-queue 16) takes 100 contigs at once."""
    payload, spec = endpoint_data
    specs = [dict(spec, name=f"contig{index}") for index in range(1, 101)]
    app = create_app(
        ServerConfig(
            db_path=ani_data.database,
            max_jobs=2,
            max_queue=16,
            rate_limit=(1, 60.0),
        )
    )
    with TestClient(app) as client:
        response = _post_contigs(
            client,
            [(item["name"], payload) for item in specs],
            {"contigs": specs},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["contigs"]) == 100
        assert body["summary"]["contigs"] == 100
        # The whole request was one rate-limited POST: a second one is refused.
        assert client.get("/info").status_code == 200
        limited = _post_contigs(client, [("contig1", payload)], {"contigs": [spec]})
        assert limited.status_code == 429


def test_queue_full_refuses_a_concurrent_contigs_request(
    ani_data: AniData, endpoint_data, monkeypatch
) -> None:
    """A second request while the one job slot is busy is a 503."""
    payload, spec = endpoint_data
    second = dict(spec, name="contig2")
    started = threading.Event()
    release = threading.Event()

    def blocking_query(*args, **kwargs):
        started.set()
        release.wait(timeout=10)
        return {"sketch_kmers": 10, "rows": []}

    monkeypatch.setattr("sylphd.server.run_contig_query", blocking_query)
    app = create_app(ServerConfig(db_path=ani_data.database, max_jobs=1, max_queue=0))
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            _post_contigs,
            client,
            [("contig1", payload), ("contig2", payload)],
            {"contigs": [spec, second]},
        )
        assert started.wait(timeout=5)
        rejected = _post_contigs(client, [("contig1", payload)], {"contigs": [spec]})
        assert rejected.status_code == 503
        assert rejected.headers["retry-after"] == "1"
        assert "busy" in rejected.json()["error"]
        release.set()
        assert future.result(timeout=10).status_code == 200


# ---------------------------------------------------------------------------
# parsimony: a contaminant contig inside a reference genome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContaminantData:
    host: str
    phage: str
    database: Path


@pytest.fixture(scope="session")
def contaminant_data(tmp_path_factory) -> ContaminantData:
    """A bacterium that carries a 20 kb phage contig, plus the phage genome.

    ``GCA_000000020.1`` holds the bacterial sequence and, as one more contig,
    a 20 kb random "phage"; ``GCA_000000021.1`` is that phage on its own.  A
    contig taken from the phage is therefore *contained* in both genomes, and
    only the genome sizes can tell which one it belongs to.
    """
    directory = tmp_path_factory.mktemp("contaminant")
    host = "".join(random.Random(20261110).choices("ACGT", k=300_000))
    phage = "".join(random.Random(20261111).choices("ACGT", k=20_000))
    sketcher = pysylph.Sketcher(c=200, k=31)
    database = directory / "contaminant.syldb"
    pysylph.Database(
        [
            sketcher.sketch_genome(
                "GCA_000000020.1_genomic.fna.gz", [host, phage]
            ),
            sketcher.sketch_genome("GCA_000000021.1_genomic.fna.gz", [phage]),
        ]
    ).dump(str(database))
    return ContaminantData(host=host, phage=phage, database=database)


def _contig_row(
    shared: int,
    total: int,
    *,
    accession: str,
    taxonomy: str | None = None,
    tax_lineage: str | None = None,
    tax_source: str | None = None,
) -> dict[str, object]:
    """One hit row as the profiler returns it to :func:`per_contig.contig_hits`."""
    return {
        "Containment_ind": [shared, total],
        "Naive_ANI": 100.0 * (shared / total) ** (1 / 31),
        "gtdb_taxonomy": taxonomy,
        "tax_lineage": tax_lineage,
        "tax_source": tax_source,
        "accession": accession,
        "Genome_file": f"{accession}_genomic.fna.gz",
    }


def test_parsimony_only_reorders_hits_inside_the_noise_band() -> None:
    """A hit further than the close band keeps its evidence order."""
    rows = [
        # 100 % of the contig, a 10 Mb genome (the contaminant case)
        _contig_row(83, 10_271, accession="GCA_000001000.1"),
        # 99.96 % of the contig, the phage genome itself
        _contig_row(82, 210, accession="GCA_000001001.1"),
        # far outside the band: a genuinely worse hit with a small genome
        _contig_row(40, 100, accession="GCA_000001002.1"),
    ]
    hits, assignment, _, _ = per_contig.contig_hits(
        rows, k=31, contig_kmers=83, options=per_contig.ContigOptions(top=3)
    )
    assert [hit["accession"] for hit in hits] == [
        "GCA_000001001.1",  # parsimony: 210 reference k-mers vs 10 271 at the same ANI
        "GCA_000001000.1",
        "GCA_000001002.1",  # outside the close band: evidence order
    ]
    assert assignment["accession"] == "GCA_000001001.1"


def test_parsimony_keeps_a_non_saturated_best_hit_first() -> None:
    """97 % ANI is a real distance; a smaller genome at 95 % must stay second.

    Both hits are inside the close band, so the plain rule would have moved the
    tiny reference first.  Only hits that explain >= 95 % of the contig (i.e.
    are near-completely contained, >= ~99.83 % ANI) may be reordered.
    """
    rows = [
        # 97.0 % of the contig, a 5 000-k-mer reference
        _contig_row(78, 5_000, accession="GCA_000002000.1"),
        # 95.0 % of the contig, a 300-k-mer reference
        _contig_row(41, 300, accession="GCA_000002001.1"),
    ]
    hits, assignment, _, _ = per_contig.contig_hits(
        rows, k=31, contig_kmers=200, options=per_contig.ContigOptions(top=2)
    )
    assert [hit["accession"] for hit in hits] == [
        "GCA_000002000.1",
        "GCA_000002001.1",
    ]
    assert hits[0]["ani"] == pytest.approx(97.0, abs=0.15)
    assert hits[1]["ani"] == pytest.approx(95.0, abs=0.15)
    assert hits[1]["reference_ani"] > hits[0]["reference_ani"]
    assert hits[0]["explained_fraction"] < per_contig.PARSIMONY_MIN_EXPLAINED
    assert assignment["accession"] == "GCA_000002000.1"
    # the pair *is* inside the close band: the saturation condition is what
    # keeps the evidence order
    best_ani = 100 * (78 / 200) ** (1 / 31)
    other_ani = 100 * (41 / 200) ** (1 / 31)
    assert per_contig.is_close(
        other_ani,
        per_contig.ani_noise(41, 200, 31),
        best_ani=best_ani,
        best_sd=per_contig.ani_noise(78, 200, 31),
    )


def test_containment_only_hits_still_block_the_consensus() -> None:
    """The owner's choice: a close set spanning sources stays mixed.

    The larger containing genome is *not* dropped from the consensus, so a
    phage contig whose close set spans UHGV and GTDB has no consensus, even
    though parsimony now ranks the UHGV hit first.
    """
    rows = [
        _contig_row(
            83,
            10_271,
            accession="GCA_000001000.1",
            taxonomy="d__Bacteria;p__Bacillota;c__Bacilli;o__Bacillales;"
            "f__Bacillaceae;g__Bacillus;s__Bacillus subtilis",
        ),
        _contig_row(
            82,
            210,
            accession="",
            tax_lineage="vFAM-1;vOTU-9",
            tax_source="UHGV_default",
        ),
    ]
    hits, assignment, _, summary = per_contig.contig_hits(
        rows, k=31, contig_kmers=83, options=per_contig.ContigOptions(top=2)
    )
    assert hits[0]["taxon"] == "vOTU-9"
    assert assignment["taxon"] == "vOTU-9"
    assert summary["mixed_sources"] is True
    assert summary["n_close"] == 2
    assert (summary["rank"], summary["taxon"]) == (None, None)


# ---------------------------------------------------------------------------
# summaries of several requests
# ---------------------------------------------------------------------------


def _summary_record(
    name: str, length: int, *, consensus: dict[str, object] | None
) -> dict[str, object]:
    return {
        "name": name,
        "length": length,
        "status": "ok" if consensus is not None else "no_hit",
        "hits": [{"rank": 1}] if consensus is not None else [],
        "consensus": consensus,
    }


def test_summary_keeps_every_consensus_taxon() -> None:
    records = [
        _summary_record("a", 10_000, consensus={"rank": "genus", "taxon": "Bacillus"}),
        _summary_record("b", 20_000, consensus={"rank": "species", "taxon": "X"}),
        _summary_record("c", 30_000, consensus=None),
        _summary_record("d", 40_000, consensus={"rank": "species", "taxon": "X"}),
    ]
    merged = per_contig.summarize(records)
    assert merged["contigs"] == 4
    assert merged["queried_bp"] == 100_000
    assert merged["no_hit_contigs"] == 1
    assert [(item["rank"], item["taxon"]) for item in merged["consensus_bp"]] == [
        ("species", "X"),  # 20 000 + 40 000 bp
        (None, "unassigned"),  # 30 000 bp
        ("genus", "Bacillus"),  # 10 000 bp
    ]


def test_summary_breakdown_is_not_truncated() -> None:
    records = [
        _summary_record(f"c{index}", 1_000, consensus={"rank": "genus", "taxon": f"G{index}"})
        for index in range(7)
    ]
    summary = per_contig.summarize(records)
    assert len(summary["consensus_bp"]) == 7
    assert summary["consensus_bp"][0]["fraction"] == pytest.approx(1 / 7, abs=1e-4)
