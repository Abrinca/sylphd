"""``sylphd assembly --per-contig``: one sketch and one classification per record.

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

``n_contig`` comes from :func:`sylphd.sketching.sketch_contigs`, the shared
numerator from the server's ``Containment_ind``.  An exact fragment then scores
100.00 %, and a contig at 97 % ANI scores 97 %.  ``--min-ani`` is applied to
this value, client-side; the query itself is sent with ``minimum_ani=0`` so the
server does not censor short contigs with its genome-denominator ANI.

Chimeric contigs are not detected: k-mer counts cannot tell a chimeric contig
from a uniformly divergent one.

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

import json
import math
import sys
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from .assembly import (
    INTERPRETATION_THRESHOLDS,
    SPECIES_ANI,
    classify_hits,
    comparable,
    gtdb_species,
    hit_lineage,
    rank_display,
)
from .cli_parser import PROG
from .client import ClientError, SylphClient
from .protocol import ProfileParams, params_json_size
from .sketching import Contig, sketch_contigs

#: Default hits per contig: the best genome is what a contamination check needs.
DEFAULT_TOP = 1

#: Default minimum contig length, in bases.  At the usual c=200 a 20 kb contig
#: carries ~85 genome-style k-mers, which is enough for roughly +-0.5 % ANI.
DEFAULT_MIN_LENGTH = 20000

#: Starlette refuses a non-file form part larger than 1 MiB (the JSON
#: ``params`` field repeats every sample name); flush well below that.
MAX_PARAMS_BYTES = 900 * 1024

#: Consensus defaults.  A species hit joins the consensus when its divergence
#: ``d = 100 - ani`` satisfies ``d <= ratio * d_best + z * sd`` (see
#: :func:`consensus`); 1.5 tolerates a hit half again as divergent as the best
#: one, 2 covers ~95 % of the ANI sampling noise.
DEFAULT_CONSENSUS_RATIO = 1.5
DEFAULT_CONSENSUS_Z = 2.0

#: Leading TSV columns, followed by the metadata columns selected by ``--fields``.
COLUMNS: tuple[str, ...] = (
    "contig",
    "contig_length",
    "gc",
    "status",
    "rank",
    "ani",
    "shared_kmers",
    "contig_kmers",
    "explained_fraction",
    "reference_ani",
    "relation",
    "shared_rank",
    "ani_species_level",
    "accession",
    "gtdb_species",
    "taxon",
    "n_close",
    "consensus_rank",
    "consensus_taxon",
)

#: Values of the query side: permissive so the server never drops a contig
#: because the *reference's* denominator makes its naive ANI look bad.
QUERY_PARAMS = ProfileParams(
    no_adjust=True,
    minimum_ani=0.0,
    screen_ani=0.0,
    min_contain=3,
    min_number_kmers=10,
)


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
    2. A species is **close** to the best one when its divergence
       ``d = 100 - ani`` is within ``ratio`` times the best divergence plus
       ``z`` standard deviations of the noise of the difference::

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
       and ``mixed_sources`` is true.
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
    # Same order as :func:`_contig_hits`: shared k-mers are the likelihood
    # order, and accession / genome file make exact ties canonical instead of
    # following the backend's row order.
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
    limit = ratio * (100.0 - best["ani"])
    close = [
        candidate
        for candidate in candidates
        if 100.0 - candidate["ani"]
        <= limit + z * math.hypot(best["sd"], candidate["sd"])
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
class PerContigOptions:
    """Everything the per-contig runner needs from the command line."""

    top: int = DEFAULT_TOP
    min_ani: float = 80.0
    min_length: int = DEFAULT_MIN_LENGTH
    fields: tuple[str, ...] = ()
    consensus_ratio: float = DEFAULT_CONSENSUS_RATIO
    consensus_z: float = DEFAULT_CONSENSUS_Z


@dataclass
class _Pending:
    record: dict[str, Any]
    payload: bytes


def _format_taxon(item: dict[str, Any]) -> str:
    """``Escherichia (genus) 1,150,000 bp (64.2 %)`` for the stderr summary."""
    label = str(item["taxon"])
    if item.get("rank"):
        label += f" ({item['rank']})"
    return f"{label} {item['bp']:,} bp ({100.0 * item['fraction']:.1f} %)"


class PerContigRun:
    """Records collected by :func:`run`, plus the writers for them."""

    def __init__(
        self,
        records: list[dict[str, Any]],
        *,
        options: PerContigOptions,
        params: ProfileParams,
        database: dict[str, Any],
    ) -> None:
        self.records = records
        self.options = options
        self.params = params
        self.database = database

    # -- documents --------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """Counts for the JSON document and the stderr summary.

        ``queried`` are the contigs that were actually sent; ``too_short``
        ones and those below sylph's k-mer floor are counted separately, so
        the fractions add up to the queried bases only.  Bases are aggregated
        by *consensus* taxon and rank, not by the top hit: one organism split
        over several near-identical GTDB species then counts once, at the rank
        the data actually supports.
        """
        records = self.records
        total_bp = sum(record["length"] for record in records)
        too_short = [record for record in records if record["status"] == "too_short"]
        tiny = [
            record
            for record in records
            if record["status"] == "no_hit" and "reason" in record
        ]
        queried = [
            record
            for record in records
            if record["status"] in {"ok", "no_hit"} and "reason" not in record
        ]
        queried_bp = sum(record["length"] for record in queried)
        by_consensus: dict[tuple[str | None, str], int] = {}
        for record in queried:
            summary = record.get("consensus") or {}
            taxon = summary.get("taxon")
            rank = summary.get("rank") if taxon else None
            key = (rank, taxon or "unassigned")
            by_consensus[key] = by_consensus.get(key, 0) + record["length"]
        consensus_bp = [
            {
                "rank": rank,
                "taxon": taxon,
                "bp": bp,
                "fraction": round(bp / queried_bp, 4) if queried_bp else 0.0,
            }
            for (rank, taxon), bp in sorted(
                by_consensus.items(),
                key=lambda item: (-item[1], item[0][1], item[0][0] or ""),
            )[:5]
        ]
        return {
            "contigs": len(records),
            "bp": total_bp,
            "queried_contigs": len(queried),
            "queried_bp": queried_bp,
            "skipped_contigs": len(too_short),
            "skipped_bp": sum(record["length"] for record in too_short),
            "too_few_kmers_contigs": len(tiny),
            "too_few_kmers_bp": sum(record["length"] for record in tiny),
            "no_hit_contigs": sum(1 for record in queried if not record["hits"]),
            "min_contig_length": self.options.min_length,
            "consensus_bp": consensus_bp,
        }

    def document(self, path: str | Path) -> dict[str, Any]:
        """The JSON document of ``assembly --per-contig --json``."""
        contigs = []
        for record in self.records:
            item = {
                "id": record["id"],
                "index": record["index"],
                "length": record["length"],
                "gc": record["gc"],
                "status": record["status"],
                "sketch_kmers": record["sketch_kmers"],
                "contig_kmers": record["contig_kmers"],
            }
            if "reason" in record:
                item["reason"] = record["reason"]
            item["assignment"] = record["assignment"]
            item["closest"] = record["closest"]
            item["consensus"] = record["consensus"]
            item["hits"] = record["hits"]
            contigs.append(item)
        params = self.params
        return {
            "query": {"file": str(path), "per_contig": True},
            "database": {
                "name": self.database.get("name"),
                "genomes": self.database.get("genomes"),
                "c": self.database.get("c"),
                "k": self.database.get("k"),
            },
            "params": {
                "top": self.options.top,
                "min_ani": self.options.min_ani,
                "min_contig_length": self.options.min_length,
                "consensus_ratio": self.options.consensus_ratio,
                "consensus_z": self.options.consensus_z,
                "no_adjust": params.no_adjust,
                "minimum_ani": params.minimum_ani,
                "screen_ani": params.screen_ani,
                "min_contain": params.min_contain,
                "min_number_kmers": params.min_number_kmers,
            },
            "thresholds": dict(INTERPRETATION_THRESHOLDS),
            "summary": self.summary(),
            "contigs": contigs,
        }

    # -- output -----------------------------------------------------------
    def write_tsv(self, output: TextIO) -> None:
        """Write the per-contig table (header included)."""
        header = [*COLUMNS, *self.options.fields]
        output.write("\t".join(header) + "\n")
        empty = [""] * (len(header) - 4)
        for record in self.records:
            prefix = [
                record["id"],
                str(record["length"]),
                f"{float(record['gc']):.2f}",
                record["status"],
            ]
            if not record["hits"]:
                output.write("\t".join([*prefix, *empty]) + "\n")
                continue
            consensus = record.get("consensus") or {}
            output.writelines(
                "\t".join(
                    [
                        *prefix,
                        str(hit["rank"]),
                        f"{float(hit['ani']):.2f}",
                        str(hit["shared_kmers"]),
                        str(hit["contig_kmers"]),
                        f"{float(hit['explained_fraction']):.4f}",
                        f"{float(hit['reference_ani']):.2f}",
                        str(hit.get("relation") or ""),
                        str(hit.get("shared_rank") or ""),
                        "true" if hit.get("ani_species_level") else "false",
                        str(hit.get("accession") or ""),
                        str(hit.get("gtdb_species") or ""),
                        str(hit.get("taxon") or ""),
                        str(consensus.get("n_close") or 0),
                        str(consensus.get("rank") or ""),
                        str(consensus.get("taxon") or ""),
                        *[
                            "" if hit.get(column) is None else str(hit.get(column))
                            for column in self.options.fields
                        ],
                    ]
                )
                + "\n"
                for hit in record["hits"]
            )

    def print_summary(self, stream: TextIO | None = None) -> None:
        """Two human lines on stderr (plus a caveat per non-GTDB source).

        The caveat of a source whose best hit is not GTDB (design note B6) is
        printed **once per output**, however many contigs it applies to.
        """
        if stream is None:
            stream = sys.stderr
        summary = self.summary()
        min_length = self.options.min_length
        line = (
            f"per-contig: {summary['queried_contigs']} of {summary['contigs']} contig(s), "
            f"{summary['queried_bp']:,} bp queried"
        )
        if summary["skipped_contigs"]:
            line += (
                f"; {summary['skipped_contigs']} shorter than {min_length} bp "
                f"({summary['skipped_bp']:,} bp)"
            )
        if summary["too_few_kmers_contigs"]:
            line += (
                f"; {summary['too_few_kmers_contigs']} with too few k-mers "
                f"({summary['too_few_kmers_bp']:,} bp)"
            )
        if summary["no_hit_contigs"]:
            line += f"; {summary['no_hit_contigs']} without a hit"
        print(line, file=stream)
        if summary["consensus_bp"]:
            parts = [_format_taxon(item) for item in summary["consensus_bp"]]
            print("bp by consensus: " + ", ".join(parts), file=stream)
        for note in self.caveats():
            print(f"{PROG}: warning: {note}", file=stream)

    def caveats(self) -> list[str]:
        """Distinct species-threshold caveats of this run, in first-seen order.

        The note comes from the best hit's summary (:func:`classify_hits`), so
        a GTDB-only run has none, and a run with several non-GTDB sources gets
        one line per source instead of one per contig.
        """
        notes: list[str] = []
        for record in self.records:
            summary = record.get("assignment") or record.get("closest") or {}
            note = summary.get("note")
            if note and note not in notes:
                notes.append(note)
        return notes


class _Batch:
    """One pending ``POST /query``: contigs, params and payload bytes.

    The byte account is deliberately pessimistic, so a batch is flushed well
    before it could exceed the server's upload limit: it adds a fixed overhead
    per part (boundary, part headers, content type, CRLFs) plus the file name,
    and one for the request.  :meth:`SylphClient.query_size` is still the final
    authority in :func:`_send`, which splits a chunk that turns out too large.
    """

    #: Multipart framing of one sketch part, excluding the file name.
    PART_OVERHEAD = 512
    #: Multipart framing of the request itself (boundaries, params part).
    REQUEST_OVERHEAD = 512

    def __init__(
        self,
        params: ProfileParams,
        request_fields: Sequence[str],
        upload_limit: int = 0,
    ) -> None:
        self._params = params
        self._fields = request_fields
        self._upload_limit = upload_limit
        self.items: list[_Pending] = []
        self._payload_bytes = 0
        # ``"names": []`` is two characters; every name adds its JSON encoding
        # plus the ", " separator.  The result is the exact character count of
        # the params field, so JSON escaping and Unicode are accounted for.
        self._names_json = 2
        self._base = params_json_size("query", params, [], request_fields)

    @property
    def params_bytes(self) -> int:
        return self._base - 2 + self._names_json

    @property
    def payload_bytes(self) -> int:
        return self._payload_bytes

    def projected_size(self, name: str, payload: int) -> int:
        """Upper bound of the request body after adding one more sketch."""
        names = self.params_bytes + len(json.dumps(name)) + (2 if self.items else 0)
        parts = len(self.items) + 1
        return (
            names
            + self._payload_bytes
            + payload
            + parts * (self.PART_OVERHEAD + len(name.encode("utf-8")))
            + self.REQUEST_OVERHEAD
        )

    def will_fit(self, name: str, payload: int) -> bool:
        """Whether one more sketch may join this batch, conservatively.

        Never returns ``False`` for an empty batch: a single sketch that cannot
        fit is rejected by :func:`run` (after measuring the real request), so
        flushing could not help.
        """
        if not self.items:
            return True
        extra = len(json.dumps(name)) + 2
        if self.params_bytes + extra > MAX_PARAMS_BYTES:
            return False
        return not (
            self._upload_limit
            and self.projected_size(name, payload) > self._upload_limit
        )

    def add(self, record: dict[str, Any], payload: bytes) -> None:
        self._names_json += len(json.dumps(record["id"])) + (2 if self.items else 0)
        self._payload_bytes += len(payload)
        self.items.append(_Pending(record, payload))

    def clear(self) -> None:
        self.items = []
        self._names_json = 2
        self._payload_bytes = 0


def chunk_size_for(info: dict[str, Any]) -> int:
    """How many contig sketches to send per ``POST /query`` request.

    Each sketch is one job, so a server with ``--max-queue`` refuses a request
    larger than ``max_jobs + max_queue`` as a whole (503); every server also
    caps a request at ``limits.max_sketches`` (4096).  Use the smaller bound so
    that a fragmented assembly works against a server with default limits.
    """
    limits = info.get("limits") or {}
    size = int(limits.get("max_sketches") or 4096)
    max_jobs = limits.get("max_jobs")
    max_queue = limits.get("max_queue")
    if max_jobs and max_queue is not None:
        size = min(size, max(1, int(max_jobs) + int(max_queue)))
    return max(1, size)


def classification_fields(
    fields: Sequence[str], metadata: dict[str, Any] | None
) -> list[str]:
    """Metadata to request: the displayed columns plus the lineages.

    Per-contig hits are classified exactly like ``/assembly`` hits, which needs
    a lineage even when ``--fields`` hides it from the table: GTDB's
    ``gtdb_taxonomy``, and the ``tax_lineage``/``tax_source`` a sylph-tax
    source resolved for a non-GTDB hit (design note B6).
    """
    requested = list(fields)
    available = list((metadata or {}).get("columns") or [])
    for column in ("gtdb_taxonomy", "tax_lineage", "tax_source"):
        if column in available and column not in requested:
            requested.append(column)
    return requested


def run(
    client: SylphClient,
    path: str | Path,
    options: PerContigOptions,
    *,
    info: dict[str, Any] | None = None,
) -> PerContigRun:
    """Sketch, query and classify every record of ``path``.

    ``info`` is the server's ``GET /info`` document when the caller already has
    it; otherwise it is fetched.  Raises :class:`~sylphd.sketching.InputError`
    for unreadable input and :class:`~sylphd.client.ClientError` for server
    problems, including a single sketch that cannot fit an upload limit.
    """
    info = client.info() if info is None else info
    database = info.get("database") or {}
    metadata = info.get("metadata")
    params = QUERY_PARAMS
    request_fields = classification_fields(options.fields, metadata)
    chunk_size = chunk_size_for(info)
    upload_limit = (
        int((info.get("limits") or {}).get("max_upload_mb") or 0) * 1024 * 1024
    )
    k = int(database["k"])

    records: list[dict[str, Any]] = []
    batch = _Batch(params, request_fields, upload_limit)

    def flush() -> None:
        if not batch.items:
            return
        _send(
            client,
            batch.items,
            params=params,
            request_fields=request_fields,
            upload_limit=upload_limit,
            k=k,
            options=options,
        )
        batch.clear()

    for contig in sketch_contigs(
        path,
        c=int(database["c"]),
        k=k,
        min_length=options.min_length,
    ):
        record = _new_record(contig)
        records.append(record)
        if contig.sketch is None:
            continue
        if contig.sketch.n_kmers < params.min_number_kmers:
            # sylph's own floor is reference-side, but a sample this small
            # cannot give a meaningful ANI either: report it, do not send it.
            record["status"] = "no_hit"
            record["reason"] = (
                f"too few k-mers ({contig.sketch.n_kmers} < "
                f"{params.min_number_kmers})"
            )
            continue
        payload = contig.sketch.to_bytes()
        if upload_limit and batch.projected_size(contig.name, len(payload)) > upload_limit:
            # The estimate may over-count the framing: measure the real request
            # before refusing, then fail as soon as the sketch is seen instead
            # of at the end of the batch.
            size = client.query_size(
                [(contig.name, payload)], params=params, fields=request_fields
            )
            if size > upload_limit:
                raise ClientError(
                    f"the sketch of contig {contig.name!r} needs a {size} byte "
                    f"request, over the server's "
                    f"{upload_limit // (1024 * 1024)} MB upload limit"
                )
        if not batch.will_fit(contig.name, len(payload)):
            flush()
        batch.add(record, payload)
        if len(batch.items) >= chunk_size:
            flush()
    flush()
    return PerContigRun(
        records, options=options, params=params, database=database
    )


def _new_record(contig: Contig) -> dict[str, Any]:
    return {
        "id": contig.name,
        "index": contig.index,
        "length": contig.length,
        "gc": contig.gc,
        "status": "too_short",
        "sketch_kmers": contig.sketch.n_kmers if contig.sketch is not None else None,
        "contig_kmers": contig.contig_kmers,
        "hits": [],
        "assignment": None,
        "closest": None,
        "consensus": None,
    }


def _send(
    client: SylphClient,
    items: Sequence[_Pending],
    *,
    params: ProfileParams,
    request_fields: Sequence[str],
    upload_limit: int,
    k: int,
    options: PerContigOptions,
) -> None:
    """Send one batch, splitting it when the real request body is too large."""
    queue: deque[list[_Pending]] = deque([list(items)])
    while queue:
        chunk = queue.popleft()
        samples = [(item.record["id"], item.payload) for item in chunk]
        if upload_limit:
            size = client.query_size(samples, params=params, fields=request_fields)
            if size > upload_limit:
                if len(chunk) == 1:
                    record = chunk[0].record
                    raise ClientError(
                        f"the sketch of contig {record['id']!r} needs a {size} byte "
                        f"request, over the server's "
                        f"{upload_limit // (1024 * 1024)} MB upload limit"
                    )
                middle = len(chunk) // 2
                queue.appendleft(chunk[middle:])
                queue.appendleft(chunk[:middle])
                continue
        results = client.query(samples, params=params, fields=request_fields)
        _apply(results, chunk, k=k, options=options)


def _apply(
    results: Sequence[dict[str, Any]],
    chunk: Sequence[_Pending],
    *,
    k: int,
    options: PerContigOptions,
) -> None:
    rows_by_name = {
        str(sample.get("name")): sample.get("rows") or () for sample in results
    }
    for item in chunk:
        record = item.record
        hits, assignment, closest, consensus_summary = _contig_hits(
            rows_by_name.get(record["id"], ()),
            k=k,
            contig_kmers=int(record["contig_kmers"] or 0),
            options=options,
        )
        record["hits"] = hits
        record["assignment"] = assignment
        record["closest"] = closest
        record["consensus"] = consensus_summary
        record["status"] = "ok" if hits else "no_hit"


def _contig_hits(
    rows: Sequence[dict[str, Any]],
    *,
    k: int,
    contig_kmers: int,
    options: PerContigOptions,
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    """Score, rank, classify and summarise the ``/query`` rows of one contig.

    Every returned hit is scored and kept: the consensus is computed from that
    complete set as long as the best hit passes ``--min-ani``, so raising
    ``--min-ani`` can only hide rows, never change the consensus.  A contig
    whose best hit is below ``--min-ani`` has no hits and no consensus.
    """
    scored = []
    for row in rows:
        contained, total = row.get("Containment_ind") or (0, 0)
        shared = int(contained)
        reference_ani = float(row.get("Naive_ANI") or 0.0)
        # A repeat-rich contig can have fewer genome-style k-mers than the
        # reference k-mers found in it; clamp to 100 % instead of going over.
        explained = min(1.0, shared / contig_kmers) if contig_kmers > 0 else 0.0
        ani = 100.0 * explained ** (1.0 / k)
        scored.append((shared, ani, explained, reference_ani, int(total), row))
    scored.sort(
        key=lambda item: (
            -item[0],  # shared: the likelihood order (ani is its image)
            -item[1],
            str(item[5].get("accession") or ""),  # canonical tie-breakers
            str(item[5].get("Genome_file") or ""),
        )
    )
    if not scored or scored[0][1] < options.min_ani:
        return [], None, None, None

    # The consensus sees every candidate, not just the printed ones and not
    # just those above --min-ani: the question is how far the evidence around
    # the best hit spreads, and --top/--min-ani only control the table.
    consensus_summary = consensus(
        [
            {
                "ani": ani,
                "shared_kmers": shared,
                "gtdb_taxonomy": row.get("gtdb_taxonomy"),
                "tax_lineage": row.get("tax_lineage"),
                "tax_source": row.get("tax_source"),
                "accession": row.get("accession"),
                "genome_file": row.get("Genome_file"),
            }
            for shared, ani, _, _, _, row in scored
        ],
        k=k,
        contig_kmers=contig_kmers,
        ratio=options.consensus_ratio,
        z=options.consensus_z,
    )
    displayed = [
        item for item in scored if item[1] >= options.min_ani
    ][: options.top]
    classification = [
        {
            "ani": ani,
            "accession": row.get("accession"),
            "gtdb_taxonomy": row.get("gtdb_taxonomy"),
            "tax_lineage": row.get("tax_lineage"),
            "tax_source": row.get("tax_source"),
        }
        for _, ani, _, _, _, row in displayed
    ]
    assignment, closest = classify_hits(classification)

    hits: list[dict[str, Any]] = []
    for rank, ((shared, ani, explained, reference_ani, total, row), label) in enumerate(
        zip(displayed, classification), start=1
    ):
        hit: dict[str, Any] = {
            "rank": rank,
            "ani": label["ani"],  # classify_hits rounds it for display
            "shared_kmers": shared,
            "contig_kmers": contig_kmers,
            "explained_fraction": round(explained, 4),
            "reference_ani": round(reference_ani, 2),
            "reference_containment": [shared, total],
            "relation": label["relation"],
            "shared_rank": label["shared_rank"],
            "ani_species_level": label["ani_species_level"],
            "genome_file": row.get("Genome_file"),
            "accession": row.get("accession"),
            "gtdb_species": gtdb_species(row.get("gtdb_taxonomy")),
            "taxon": hit_lineage(row).taxon,
        }
        for key in ("taxonomy", "taxonomy_source"):
            if key in label:
                hit[key] = label[key]
        for column in options.fields:
            hit[column] = row.get(column)
        hits.append(hit)
    return hits, assignment, closest, consensus_summary
