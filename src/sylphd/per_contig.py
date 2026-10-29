"""Per-contig classification: the contig-relative ANI and what is built on it.

``sylphd assembly FILE --per-contig`` sends one *sample sketch* per FASTA/FASTQ
record to ``POST /contigs`` together with three numbers the client measured on
the record itself: its genome-style k-mer count (``contig_kmers``), its length
and its GC content.  This module is the server side of that endpoint: it scores
the returned hits, ranks them, classifies them and builds the taxonomic
consensus of every contig.  Nothing here depends on a client, so every client
gets the same answer from the same sketch.

Why the ANI is computed here
----------------------------
sylph's ``Naive_ANI`` (and ``Containment_ind``) divides the shared k-mers by
the **reference genome's** k-mer count.  For a whole assembly that is a
coverage-style number and works, but an exact 20 kb fragment of a 5 Mb genome
would report ~85 % ANI: the denominator does not depend on the contig at all.

Per contig the meaningful estimate is *contig-relative*::

    shared       = Containment_ind[0]        (reference k-mers found in the contig)
    n_contig     = genome-style k-mer count of the contig itself
    ani_contig   = 100 * (shared / n_contig) ** (1/k)
    explained    = shared / n_contig         (= (ani_contig/100) ** k)

``n_contig`` is measured by the client (:func:`sylphd.sketching.sketch_contigs`),
the shared numerator comes from the server's ``Containment_ind``.  An exact
fragment then scores 100.00 %, and a contig at 97 % ANI scores 97 %.
``min_ani`` is applied to this value; the query itself runs with
:data:`QUERY_PARAMS` so the server does not censor short contigs with its
genome-denominator ANI.

Chimeric contigs are not detected: k-mer counts cannot tell a chimeric contig
from a uniformly divergent one.

Ranking: parsimony inside the noise band
----------------------------------------
``shared`` saturates at ``n_contig`` whenever a reference *contains* the whole
contig, and a public assembly that carries a contaminant contig (a lambda
prophage in a bacterial genome, say) then holds every k-mer of the contig, just
like the phage genome itself.  Containment cannot tell "this contig is a region
of this genome" from "this genome happens to carry a copy of it", but genome
size can: the true source explains the contig with far fewer k-mers.  So hits
that are statistically indistinguishable on the contig side — the same close
band the consensus uses (:func:`is_close`) — **and** explain nearly the whole
contig (>= :data:`PARSIMONY_MIN_EXPLAINED`) are ranked by **fewest reference
k-mers first** (= highest ``reference_ani``), and only then by shared k-mers.
That saturation condition is what keeps the rule honest: a hit at 95 % ANI
explains only 0.95**31 = 20 % of the contig, where the ANI is a real distance
and a smaller genome is no evidence at all.  Everything else keeps the plain
evidence order.  Measured effect: a lambda
contig puts `Lambdavirus` first instead of the contaminated GTDB assemblies,
and bacterial contigs only swap rows inside their noise band.  The consensus
does **not** drop the larger containing genomes: a close set that spans UHGV and
GTDB stays ``mixed_sources``.

Two caveats are part of the output:

* ``explained`` is the fraction of the contig one reference explains.  It is
  not a purity measure: a divergent reference, a congener hit or a contig
  whose k-mers are repeated elsewhere in the reference all land well below 1.
* On a two-stage ``.syl2db`` database the first-stage screen can drop the true
  reference of a short contig before any threshold is applied; the
  probability grows with contig length and ANI.  See the command docs.

The **taxonomic consensus** answers the question the top hit alone cannot: a
near-exact contig of *E. coli* K-12 matches a swarm of ~99 % identical GTDB
genomes, and the best one changes with a k-mer or two.  Like CAT/BAT, the
consensus is the lowest common rank of every species whose divergence from the
best hit is within the ANI noise (see :func:`consensus`), so the answer
degrades from species to genus to family only as far as the evidence is
ambiguous.  Lineages are read generically (GTDB, ICTV, fungal RefSeq, UHGV
levels), but only within one taxonomy source: a close set that spans two
sources has no consensus.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .assembly import (
    SPECIES_ANI,
    classify_hits,
    comparable,
    genome_id,
    gtdb_species,
    hit_lineage,
    rank_display,
)
from .protocol import ProfileParams, ProtocolError, integer_field, number_field
from .sketching import InputError, check_sample_name

#: Default hits per contig: the best genome is what a contamination check needs.
DEFAULT_TOP = 1

#: Largest ``top`` a request may ask for, as on ``/assembly``.
MAX_TOP = 50

#: Default minimum contig ANI, in percent.
DEFAULT_MIN_ANI = 80.0

#: Accepted ``min_ani`` range, in percent (as on ``/assembly``).
MIN_ANI_RANGE = (70.0, 100.0)

#: How much of the contig a hit must explain before parsimony may reorder it.
#: ``shared`` saturates only when the reference *contains* the whole contig, so
#: the fewest-k-mers rule is only meaningful there; below this fraction the ANI
#: is a real (if noisy) distance and the evidence order stands.  At k=31,
#: ``0.95 ** (1/31) = 0.99834``, i.e. a reordered hit is at least ~99.83 % ANI,
#: so the tie-break can never move an assignment below the 95 % species
#: threshold.
PARSIMONY_MIN_EXPLAINED = 0.95

#: Consensus defaults.  A species hit joins the consensus when its divergence
#: ``d = 100 - ani`` satisfies ``d <= ratio * d_best + z * sd`` (see
#: :func:`consensus`); 1.5 tolerates a hit half again as divergent as the best
#: one, 2 covers ~95 % of the ANI sampling noise.
DEFAULT_CONSENSUS_RATIO = 1.5
DEFAULT_CONSENSUS_Z = 2.0

#: Largest contig length a request may report, in bases (1e13 is more than any
#: assembly; the bound only keeps the arithmetic in range).
MAX_CONTIG_LENGTH = 10**13

#: Upper bound of ``contig_kmers``, chosen so the ``usize`` arithmetic of the
#: server cannot overflow.
MAX_CONTIG_KMERS = 2**63 - 1

#: Values of the query side: permissive so the server never drops a contig
#: because the *reference's* denominator makes its naive ANI look bad.
QUERY_PARAMS = ProfileParams(
    no_adjust=True,
    minimum_ani=0.0,
    screen_ani=0.0,
    min_contain=3,
    min_number_kmers=10,
)

#: Keys ``params`` of ``POST /contigs`` accepts.  ``fields`` and ``tax_sources``
#: are validated by the server, which is the only place that knows the metadata
#: file.
PARAM_KEYS = frozenset(
    {"contigs", "top", "min_ani", "consensus_ratio", "consensus_z", "fields", "tax_sources"}
)

#: Keys of one ``params.contigs`` entry.
CONTIG_KEYS = frozenset({"name", "contig_kmers", "length", "gc"})


def ani_noise(shared: int, contig_kmers: int, k: int) -> float:
    """Standard deviation of the contig-relative ANI, in percentage points.

    The ``shared`` k-mers found in a contig of ``contig_kmers`` follow a
    binomial with ``p = shared / contig_kmers`` (a shared k-mer is one whose
    ``k`` bases all match), and ``ani = 100 * p ** (1/k)``.  Propagating the
    error through that ``1/k`` power gives

        sd = (100/k) * sqrt((1 - p) / (contig_kmers * p))

    which is the ``(100/k) * sqrt((1-p) / (n*p))`` of
    ``scratch/per-contig-ani/FINDINGS.md``.  An exact match (``p = 1``) has no
    sampling noise; a hit without a shared k-mer (or an empty contig) has no
    usable estimate and is returned as infinite.
    """
    if contig_kmers <= 0 or shared <= 0:
        return math.inf
    p = min(1.0, shared / contig_kmers)
    if p >= 1.0:
        return 0.0
    return (100.0 / k) * math.sqrt((1.0 - p) / (contig_kmers * p))


def is_close(
    ani: float,
    sd: float,
    *,
    best_ani: float,
    best_sd: float,
    ratio: float = DEFAULT_CONSENSUS_RATIO,
    z: float = DEFAULT_CONSENSUS_Z,
) -> bool:
    """Whether a hit's divergence is within the close band of the best hit.

    The band is ``d <= ratio * d_best + z * sqrt(sd_best**2 + sd**2)`` with
    ``d = 100 - ani``: ``ratio`` says how much more divergent than the best hit
    a reference may be, the noise term (:func:`ani_noise`) widens the allowance
    exactly as much as the two ANI estimates are uncertain.  A hit without a
    noise estimate (``sd`` infinite, i.e. no shared k-mer) is never close.
    """
    noise = math.hypot(best_sd, sd)
    if not math.isfinite(noise):
        return False
    return 100.0 - ani <= ratio * (100.0 - best_ani) + z * noise


def consensus(
    hits: Sequence[dict[str, Any]],
    *,
    k: int,
    contig_kmers: int,
    ratio: float = DEFAULT_CONSENSUS_RATIO,
    z: float = DEFAULT_CONSENSUS_Z,
) -> dict[str, Any] | None:
    """Taxonomic consensus of one contig, from its hits (any order).

    Each hit is a mapping with ``ani`` (percent, unrounded), ``shared_kmers``,
    ``gtdb_taxonomy`` and/or ``tax_lineage``/``tax_source``, ``accession`` and
    optionally ``genome_file``.  The rule, per contig:

    1. Keep the best hit of every species (by ``ani``).  A hit without a
       species name is its own taxon, keyed by its lineage or accession.
    2. A species is **close** to the best one when its divergence is within
       the band of :func:`is_close`::

           d_i <= ratio * d_best + z * sqrt(sd_best**2 + sd_i**2)

       The noise term (:func:`ani_noise`) widens the band for short contigs
       and shrinks it for long ones, so a 5 kb contig at 99 % resolves to
       genus while a 500 kb contig at 99 % resolves to species.
    3. The consensus is the lowest common rank of the lineages of the close
       set, including the best one.  With a single close species that is its
       species; two species of one genus give the genus, and so on.  Lineages
       are parsed generically (:func:`~sylphd.assembly.parse_lineage`), so a
       UHGV close set gives a ``level N`` and a fungal one a kingdom first.
    4. Only lineages of the **same source** are combined (design note B6):
       when the close set spans two taxonomy sources the consensus is empty
       and ``mixed_sources`` is true.  Hits that merely *contain* the contig in
       a far larger genome stay in the set: the consensus says which taxa the
       evidence cannot separate, not which reference is the source.
    5. A species consensus needs the best hit at or above the species
       threshold (:data:`~sylphd.assembly.SPECIES_ANI`, 95 %): below it the
       answer is truncated to the rank above the species-equivalent rank —
       genus for GTDB, one level up for a prefix-less lineage — because the
       species that happens to be the only close hit is not a species-level
       identification.

    Returns ``{"rank", "taxon", "n_close", "species"}`` (plus
    ``"mixed_sources": True`` when rule 4 applied), or ``None`` without hits.
    ``rank`` and ``taxon`` are ``None`` when a close lineage is missing (no
    metadata): there is then nothing to place the contig on, but ``n_close``
    still counts the species.
    """
    if not hits:
        return None
    # Shared k-mers are the likelihood order, and accession / genome file make
    # exact ties canonical instead of following the backend's row order.
    ordered = sorted(
        hits,
        key=lambda hit: (
            -int(hit.get("shared_kmers") or 0),
            -float(hit["ani"]),
            str(hit.get("accession") or ""),
            str(hit.get("genome_file") or ""),
        ),
    )
    candidates: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for index, hit in enumerate(ordered):
        lineage = hit_lineage(hit)
        species = lineage.species or ""
        if species:
            key: Any = ("species", lineage.family, species)
        elif lineage:
            key = ("lineage", lineage.family, lineage.ranks)
        elif hit.get("accession"):
            key = ("accession", hit["accession"])
        else:
            key = ("hit", index)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            {
                "ani": float(hit["ani"]),
                "sd": ani_noise(int(hit.get("shared_kmers") or 0), contig_kmers, k),
                "lineage": lineage,
                "names": lineage.names,
                "species": species,
                "accession": hit.get("accession"),
            }
        )

    best = candidates[0]
    close = [
        candidate
        for candidate in candidates
        if is_close(
            candidate["ani"],
            candidate["sd"],
            best_ani=best["ani"],
            best_sd=best["sd"],
            ratio=ratio,
            z=z,
        )
    ]

    rank: str | None = None
    taxon: str | None = None
    mixed_sources = False
    if all(candidate["lineage"] for candidate in close):
        if any(
            not comparable(best["lineage"], candidate["lineage"]) for candidate in close
        ):
            # Two taxonomy sources are two yardsticks: their ranks cannot be
            # intersected, so the contig gets no consensus (design note B6).
            mixed_sources = True
        else:
            rank_key: str | None = None
            for key, name in best["lineage"].ranks:
                if all(candidate["names"].get(key) == name for candidate in close):
                    rank_key, taxon = key, name
                else:
                    break
            if rank_key is not None:
                rank = rank_display(rank_key)

    species_key = best["lineage"].species_key
    if (
        rank is not None
        and species_key is not None
        and rank == rank_display(species_key)
        and best["ani"] < SPECIES_ANI
    ):
        # The species threshold is the only hard boundary the tool recognises.
        # A species that is merely the closest one (and the only close hit) is
        # not a species-level call, so stop at the rank above it.
        keys = [key for key, _ in best["lineage"].ranks]
        index = keys.index(species_key)
        rank = taxon = None
        if index > 0:
            rank = rank_display(keys[index - 1])
            taxon = best["lineage"].ranks[index - 1][1]

    summary: dict[str, Any] = {
        "rank": rank,
        "taxon": taxon,
        "n_close": len(close),
        "species": [
            name
            for name in (
                candidate["species"] or candidate["accession"] or ""
                for candidate in close
            )
            if name
        ],
    }
    if mixed_sources:
        summary["mixed_sources"] = True
    return summary


@dataclass(frozen=True)
class ContigInput:
    """One contig of a ``POST /contigs`` request, as the client measured it.

    ``contig_kmers`` is the contig's genome-style k-mer count (the denominator
    of the contig-relative ANI), ``length`` and ``gc`` are informational and
    echoed in the result.
    """

    name: str
    contig_kmers: int
    length: int
    gc: float


@dataclass(frozen=True)
class ContigOptions:
    """How the server scores and reports one request's contigs."""

    top: int = DEFAULT_TOP
    min_ani: float = DEFAULT_MIN_ANI
    consensus_ratio: float = DEFAULT_CONSENSUS_RATIO
    consensus_z: float = DEFAULT_CONSENSUS_Z
    #: Metadata columns to copy flat into every hit (resolved by the server:
    #: all of them when the request omitted ``fields``).
    fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Scored:
    """One hit of a contig, scored for the contig-relative ANI."""

    shared: int
    ani: float
    explained: float
    reference_ani: float
    #: The reference genome's k-mer count (``Containment_ind[1]``): the
    #: parsimony signal, not a denominator here.
    reference_kmers: int
    row: Mapping[str, Any]

    @property
    def accession(self) -> str:
        return str(self.row.get("accession") or "")

    @property
    def genome_file(self) -> str:
        return str(self.row.get("Genome_file") or "")


def _score(row: Mapping[str, Any], contig_kmers: int, k: int) -> _Scored:
    """Score one hit row (the shape ``/query`` returns) for this contig."""
    contained, total = row.get("Containment_ind") or (0, 0)
    shared = int(contained)
    reference_ani = float(row.get("Naive_ANI") or 0.0)
    # A repeat-rich contig can have fewer genome-style k-mers than the
    # reference k-mers found in it; clamp to 100 % instead of going over.
    explained = min(1.0, shared / contig_kmers) if contig_kmers > 0 else 0.0
    ani = 100.0 * explained ** (1.0 / k)
    return _Scored(
        shared=shared,
        ani=ani,
        explained=explained,
        reference_ani=reference_ani,
        reference_kmers=int(total),
        row=row,
    )


def _evidence_key(item: _Scored) -> tuple[Any, ...]:
    """The likelihood order: most shared k-mers first (ANI is its image)."""
    return (-item.shared, -item.ani, item.accession, item.genome_file)


def _parsimony_key(item: _Scored) -> tuple[Any, ...]:
    """Fewest reference k-mers first, then the likelihood order."""
    return (item.reference_kmers, *_evidence_key(item))


def rank_hits(
    scored: Sequence[_Scored],
    *,
    contig_kmers: int,
    k: int,
    ratio: float = DEFAULT_CONSENSUS_RATIO,
    z: float = DEFAULT_CONSENSUS_Z,
) -> list[_Scored]:
    """Order the scored hits of one contig, best first.

    The best hit is the one that explains most of the contig (highest
    ``shared``, equivalently the highest contig-relative ANI).  Hits that are
    both inside the close band of that hit (:func:`is_close`) **and** explain
    at least :data:`PARSIMONY_MIN_EXPLAINED` of the contig are statistically
    indistinguishable on the contig side *and* near-completely contained, so
    **parsimony** orders them: the reference with the fewest k-mers explains the
    contig with the smallest genome, i.e. the one with the highest
    ``reference_ani``.  Every other hit — outside the band, or in it but not
    near-saturated — keeps the plain evidence order.  See the module docstring
    for why containment needs the saturation condition.
    """
    if not scored:
        return []
    best = max(scored, key=lambda item: (item.shared, item.ani))
    best_sd = ani_noise(best.shared, contig_kmers, k)
    contained: list[_Scored] = []
    rest: list[_Scored] = []
    for item in scored:
        sd = ani_noise(item.shared, contig_kmers, k)
        if item.explained >= PARSIMONY_MIN_EXPLAINED and is_close(
            item.ani, sd, best_ani=best.ani, best_sd=best_sd, ratio=ratio, z=z
        ):
            contained.append(item)
        else:
            rest.append(item)
    contained.sort(key=_parsimony_key)
    rest.sort(key=_evidence_key)
    return contained + rest


def classification_fields(fields: Sequence[str], available: Sequence[str]) -> list[str]:
    """Metadata to fetch for the hits: the displayed columns plus the lineages.

    Per-contig hits are classified exactly like ``/assembly`` hits, which needs
    a lineage even when the request hides it from the table: GTDB's
    ``gtdb_taxonomy``, and the ``tax_lineage``/``tax_source`` a sylph-tax
    source resolved for a non-GTDB hit (design note B6).
    """
    requested = list(fields)
    for column in ("gtdb_taxonomy", "tax_lineage", "tax_source"):
        if column in available and column not in requested:
            requested.append(column)
    return requested


def contig_hits(
    rows: Sequence[Mapping[str, Any]],
    *,
    k: int,
    contig_kmers: int,
    options: ContigOptions,
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    """Score, rank, classify and summarise the hits of one contig.

    ``rows`` are the hit rows of one contig's ``/query``-style result set (the
    shape :func:`sylphd.protocol.result_row` produces: ``Containment_ind``,
    ``Naive_ANI``, ``accession``, ``Genome_file`` and metadata columns).

    Every returned hit is scored and kept: the consensus is computed from that
    complete set as long as the best hit passes ``min_ani``, so raising
    ``min_ani`` can only hide rows, never change the consensus.  A contig
    whose best hit is below ``min_ani`` has no hits and no consensus.
    """
    scored = [_score(row, contig_kmers, k) for row in rows]
    if not scored or max(item.ani for item in scored) < options.min_ani:
        return [], None, None, None

    ordered = rank_hits(
        scored,
        contig_kmers=contig_kmers,
        k=k,
        ratio=options.consensus_ratio,
        z=options.consensus_z,
    )
    # The consensus sees every candidate, not just the printed ones and not
    # just those above min_ani: the question is how far the evidence around
    # the best hit spreads, and top/min_ani only control the table.
    consensus_summary = consensus(
        [
            {
                "ani": item.ani,
                "shared_kmers": item.shared,
                "gtdb_taxonomy": item.row.get("gtdb_taxonomy"),
                "tax_lineage": item.row.get("tax_lineage"),
                "tax_source": item.row.get("tax_source"),
                "accession": item.row.get("accession"),
                "genome_file": item.row.get("Genome_file"),
            }
            for item in scored
        ],
        k=k,
        contig_kmers=contig_kmers,
        ratio=options.consensus_ratio,
        z=options.consensus_z,
    )
    displayed = [item for item in ordered if item.ani >= options.min_ani][: options.top]
    classification = [
        {
            "ani": item.ani,
            "accession": item.row.get("accession"),
            "gtdb_taxonomy": item.row.get("gtdb_taxonomy"),
            "tax_lineage": item.row.get("tax_lineage"),
            "tax_source": item.row.get("tax_source"),
        }
        for item in displayed
    ]
    assignment, closest = classify_hits(classification)

    hits: list[dict[str, Any]] = []
    for rank, (item, label) in enumerate(zip(displayed, classification), start=1):
        hit: dict[str, Any] = {
            "rank": rank,
            "ani": label["ani"],  # classify_hits rounds it for display
            "shared_kmers": item.shared,
            "contig_kmers": contig_kmers,
            "explained_fraction": round(item.explained, 4),
            "reference_ani": round(item.reference_ani, 2),
            "reference_containment": [item.shared, item.reference_kmers],
            "relation": label["relation"],
            "shared_rank": label["shared_rank"],
            "ani_species_level": label["ani_species_level"],
            "genome_file": item.row.get("Genome_file"),
            "genome_id": genome_id(item.row),
            "accession": item.row.get("accession"),
            "gtdb_species": gtdb_species(item.row.get("gtdb_taxonomy")),
            "taxon": hit_lineage(item.row).taxon,
        }
        for key in ("taxonomy", "taxonomy_source"):
            if key in label:
                hit[key] = label[key]
        for column in options.fields:
            hit[column] = item.row.get(column)
        hits.append(hit)
    return hits, assignment, closest, consensus_summary


def contig_result(
    contig: ContigInput,
    *,
    sketch_kmers: int,
    rows: Sequence[Mapping[str, Any]],
    options: ContigOptions,
    k: int,
) -> dict[str, Any]:
    """The result object of one contig, as ``POST /contigs`` returns it."""
    result: dict[str, Any] = {
        "name": contig.name,
        "length": contig.length,
        "gc": contig.gc,
        "sketch_kmers": sketch_kmers,
        "contig_kmers": contig.contig_kmers,
        "status": "no_hit",
        "hits": [],
        "assignment": None,
        "closest": None,
        "consensus": None,
    }
    floor = QUERY_PARAMS.min_number_kmers
    if sketch_kmers < floor:
        # sylph's own floor is reference-side, but a sample this small cannot
        # give a meaningful ANI either: report it, do not query it.
        result["status"] = "too_few_kmers"
        result["reason"] = f"too few k-mers ({sketch_kmers} < {floor})"
        return result
    hits, assignment, closest, summary = contig_hits(
        rows, k=k, contig_kmers=contig.contig_kmers, options=options
    )
    result["hits"] = hits
    result["assignment"] = assignment
    result["closest"] = closest
    result["consensus"] = summary
    if hits:
        result["status"] = "ok"
    return result


# -- input validation -----------------------------------------------------


def parse_params(
    data: Mapping[str, Any], *, k: int, count: int
) -> tuple[tuple[ContigInput, ...], ContigOptions]:
    """Validate the ``params`` object of ``POST /contigs``.

    ``count`` is the number of ``sketch`` parts of the request and ``k`` the
    database's k: both are needed to check the request as a whole (one contig
    per sketch, names usable as sample names) and to reject a genome-style
    k-mer count no sequence of that length can have.  ``fields`` and
    ``tax_sources`` are validated by the server and are only allowed here.

    Raises :class:`~sylphd.protocol.ProtocolError` with a message that names
    the offending entry; the server turns it into a 400.
    """
    if not isinstance(data, Mapping):
        raise ProtocolError("'params' must be a JSON object")
    unknown = sorted(set(data) - PARAM_KEYS)
    if unknown:
        raise ProtocolError(f"unknown parameter(s): {', '.join(unknown)}")
    raw = data.get("contigs")
    if not isinstance(raw, list):
        raise ProtocolError(
            "'params.contigs' must be a list of objects, one per sketch part"
        )
    if len(raw) != count:
        raise ProtocolError(
            f"'params.contigs' has {len(raw)} entries for {count} sketches"
        )
    contigs = [
        _parse_contig(item, index=index, k=k) for index, item in enumerate(raw)
    ]
    seen: dict[str, int] = {}
    for index, contig in enumerate(contigs):
        if contig.name in seen:
            raise ProtocolError(
                f"params.contigs[{index}].name repeats {contig.name!r} "
                f"(first at index {seen[contig.name]})"
            )
        seen[contig.name] = index
    options = ContigOptions(
        top=integer_field(data, "top", DEFAULT_TOP, 1, MAX_TOP),
        min_ani=number_field(data, "min_ani", DEFAULT_MIN_ANI, *MIN_ANI_RANGE),
        consensus_ratio=number_field(data, "consensus_ratio", DEFAULT_CONSENSUS_RATIO, 1.0),
        consensus_z=number_field(data, "consensus_z", DEFAULT_CONSENSUS_Z, 0.0),
    )
    return tuple(contigs), options


def _parse_contig(item: Any, *, index: int, k: int) -> ContigInput:
    label = f"params.contigs[{index}]"
    if not isinstance(item, Mapping):
        raise ProtocolError(f"{label} must be an object")
    unknown = sorted(set(item) - CONTIG_KEYS)
    if unknown:
        raise ProtocolError(f"{label} has unknown key(s): {', '.join(unknown)}")

    name = item.get("name")
    if not isinstance(name, str):
        raise ProtocolError(f"{label}.name must be a string")
    try:
        name = check_sample_name(name)
    except InputError as error:
        raise ProtocolError(f"{label}.name: {error}") from None
    if not name:
        raise ProtocolError(f"{label}.name is empty")

    length = _contig_integer(item, "length", label, minimum=1, maximum=MAX_CONTIG_LENGTH)
    contig_kmers = _contig_integer(
        item, "contig_kmers", label, minimum=1, maximum=MAX_CONTIG_KMERS
    )
    limit = max(0, length - k + 1)
    if contig_kmers > limit:
        raise ProtocolError(
            f"{label}.contig_kmers is {contig_kmers} for a {length} bp contig "
            f"(at most {limit} k-mers at k={k})"
        )
    gc = item.get("gc")
    if isinstance(gc, bool) or not isinstance(gc, (int, float)):
        raise ProtocolError(f"{label}.gc must be a number")
    gc = float(gc)
    if not math.isfinite(gc) or gc < 0.0 or gc > 100.0:
        raise ProtocolError(f"{label}.gc must be between 0 and 100")
    return ContigInput(name=name, contig_kmers=contig_kmers, length=length, gc=gc)


def _contig_integer(
    item: Mapping[str, Any], key: str, label: str, *, minimum: int, maximum: int
) -> int:
    value = item.get(key)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{label}.{key} must be an integer")
    if value < minimum or value > maximum:
        raise ProtocolError(f"{label}.{key} must be between {minimum} and {maximum}")
    return value


# -- summaries ------------------------------------------------------------


def _lost_evidence(record: Mapping[str, Any]) -> bool:
    """Whether a record was never queried for lack of k-mers.

    The server reports ``too_few_kmers``; the CLI has always reported such a
    contig as ``no_hit`` with a ``reason``, and both are counted the same way.
    """
    if record.get("status") == "too_few_kmers":
        return True
    return record.get("status") == "no_hit" and "reason" in record


def _breakdown(
    bases: Mapping[tuple[str | None, str], int], queried_bp: int, top: int | None
) -> list[dict[str, Any]]:
    """``consensus_bp`` entries, most bases first, optionally cut to ``top``."""
    items = [
        {
            "rank": rank,
            "taxon": taxon,
            "bp": bp,
            "fraction": round(bp / queried_bp, 4) if queried_bp else 0.0,
        }
        for (rank, taxon), bp in sorted(
            bases.items(), key=lambda item: (-item[1], item[0][1], item[0][0] or "")
        )
    ]
    return items[:top] if top is not None else items


def summarize(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Counts and the consensus breakdown of a set of per-contig results.

    ``records`` are the objects of a ``POST /contigs`` response (or the CLI's
    merged records, which have the same fields).  Only queried contigs
    (``ok``/``no_hit`` without a ``reason``) contribute bases; ``consensus_bp``
    lists **every** consensus taxon with its share of the queried bases, so a
    caller can merge several summaries and cut afterwards
    (:func:`merge_summaries`, :func:`finalize_summary`).  ``too_short`` records
    (a client-side status: they were never sketched) are not counted in any of
    the queried/skipped buckets here.
    """
    total_bp = sum(int(record["length"]) for record in records)
    lost = [record for record in records if _lost_evidence(record)]
    queried = [
        record
        for record in records
        if record.get("status") in {"ok", "no_hit"} and not _lost_evidence(record)
    ]
    queried_bp = sum(int(record["length"]) for record in queried)
    bases: dict[tuple[str | None, str], int] = {}
    for record in queried:
        summary = record.get("consensus") or {}
        taxon = summary.get("taxon")
        rank = summary.get("rank") if taxon else None
        key = (rank, taxon or "unassigned")
        bases[key] = bases.get(key, 0) + int(record["length"])
    return {
        "contigs": len(records),
        "bp": total_bp,
        "queried_contigs": len(queried),
        "queried_bp": queried_bp,
        "too_few_kmers_contigs": len(lost),
        "too_few_kmers_bp": sum(int(record["length"]) for record in lost),
        "no_hit_contigs": sum(1 for record in queried if not record.get("hits")),
        "consensus_bp": _breakdown(bases, queried_bp, None),
    }


#: The counters :func:`merge_summaries` adds up.
_COUNTERS = (
    "contigs",
    "bp",
    "queried_contigs",
    "queried_bp",
    "too_few_kmers_contigs",
    "too_few_kmers_bp",
    "no_hit_contigs",
)


def merge_summaries(parts: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Add up per-request summaries, keeping every consensus taxon.

    Fractions are recomputed later by :func:`finalize_summary`, because they
    are over the *whole* queried base count and not over one request's.
    """
    totals: dict[str, Any] = {key: 0 for key in _COUNTERS}
    bases: dict[tuple[str | None, str], int] = {}
    for part in parts:
        for key in _COUNTERS:
            totals[key] += int(part.get(key) or 0)
        for item in part.get("consensus_bp") or ():
            key = (item.get("rank"), item.get("taxon") or "unassigned")
            bases[key] = bases.get(key, 0) + int(item["bp"])
    totals["consensus_bp"] = _breakdown(bases, int(totals["queried_bp"]), None)
    return totals


def finalize_summary(summary: Mapping[str, Any], *, top: int | None = 5) -> dict[str, Any]:
    """Recut a merged summary: fractions over its queried bases, ``top`` taxa."""
    bases = {
        (item.get("rank"), item.get("taxon") or "unassigned"): int(item["bp"])
        for item in summary.get("consensus_bp") or ()
    }
    result = {key: summary.get(key) for key in (*_COUNTERS, "consensus_bp")}
    result["consensus_bp"] = _breakdown(
        bases, int(summary.get("queried_bp") or 0), top
    )
    return result


def caveats(records: Sequence[Mapping[str, Any]]) -> list[str]:
    """Distinct species-threshold caveats of a set of results, first-seen order.

    The note comes from the best hit's summary (:func:`classify_hits`), so a
    GTDB-only run has none, and a run with several non-GTDB sources gets one
    line per source instead of one per contig.
    """
    notes: list[str] = []
    for record in records:
        summary = record.get("assignment") or record.get("closest") or {}
        note = summary.get("note")
        if note and note not in notes:
            notes.append(note)
    return notes
