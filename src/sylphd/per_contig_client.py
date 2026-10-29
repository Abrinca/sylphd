"""Client side of ``sylphd assembly --per-contig``: sketch, ``POST /contigs``, report.

The command reads a fragmented assembly record by record, sketches every record
as its own sample with the server's ``c``/``k``, and measures the record's
genome-style k-mer count (:func:`sylphd.sketching.sketch_contigs`).  Those
numbers go to ``POST /contigs``, which returns one classified result per contig
(the contig-relative ANI, the ranked hits, the classification and the taxonomic
consensus; see :mod:`sylphd.per_contig`).  This module only batches the
requests, merges the answers and writes the table / JSON / stderr summary.

Batching
--------
Every contig is one server job, so a batch is bounded by the server's
``limits.max_jobs + max_queue`` and ``max_sketches``, by the advertised size of
the JSON ``params`` part (it repeats every contig name, length and k-mer
count), and by the *measured* multipart body against ``max_upload_mb``.  A
batch that is still too large is split in half, and a single sketch that cannot
fit fails before it is sent.
"""

from __future__ import annotations

import sys
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from . import per_contig
from .assembly import INTERPRETATION_THRESHOLDS
from .cli_parser import PROG
from .client import ClientError, SylphClient
from .protocol import ProfileParams, contig_json_size, contigs_json_size
from .sketching import Contig, sketch_contigs

#: Default minimum contig length, in bases.  At the usual c=200 a 20 kb contig
#: carries ~85 genome-style k-mers, which is enough for roughly +-0.5 % ANI.
DEFAULT_MIN_LENGTH = 20000

#: Defaults of the server-side rule, re-exported so the CLI and the docs name
#: one number: hits per contig, and the consensus band (1.5 and 2).
DEFAULT_TOP = per_contig.DEFAULT_TOP
DEFAULT_CONSENSUS_RATIO = per_contig.DEFAULT_CONSENSUS_RATIO
DEFAULT_CONSENSUS_Z = per_contig.DEFAULT_CONSENSUS_Z

#: How large the JSON ``params`` part of one request may grow, in bytes,
#: whatever the server allows.  The server advertises the hard limit
#: (``limits.max_params_bytes``, 1 MiB); flush well below it.
DEFAULT_PARAMS_BUDGET = 900 * 1024

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
    "genome_id",
    "gtdb_species",
    "taxon",
    "n_close",
    "consensus_rank",
    "consensus_taxon",
)


@dataclass(frozen=True)
class PerContigOptions:
    """Everything the per-contig runner needs from the command line."""

    top: int = per_contig.DEFAULT_TOP
    min_ani: float = per_contig.DEFAULT_MIN_ANI
    min_length: int = DEFAULT_MIN_LENGTH
    fields: tuple[str, ...] = ()
    consensus_ratio: float = per_contig.DEFAULT_CONSENSUS_RATIO
    consensus_z: float = per_contig.DEFAULT_CONSENSUS_Z


@dataclass
class _Pending:
    record: dict[str, Any]
    spec: dict[str, Any]
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
        unsent: list[dict[str, Any]],
        summaries: list[dict[str, Any]],
        options: PerContigOptions,
        params: ProfileParams,
        database: dict[str, Any],
    ) -> None:
        self.records = records
        #: Records the server never saw (too short, too few k-mers): they are
        #: not in any per-request summary.
        self.unsent = unsent
        #: The ``summary`` of every ``POST /contigs`` response, in order.
        self.summaries = summaries
        self.options = options
        self.params = params
        self.database = database

    # -- documents --------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """Counts for the JSON document and the stderr summary.

        The per-request summaries are added up (the server reports every
        consensus taxon, so nothing is lost between chunks) and the contigs the
        server never saw are added on top.  ``queried`` are the contigs that
        were actually sent; ``too_short`` ones and those below sylph's k-mer
        floor are counted separately, so the fractions add up to the queried
        bases only.  Bases are aggregated by *consensus* taxon and rank, not by
        the top hit: one organism split over several near-identical GTDB
        species then counts once, at the rank the data actually supports.
        """
        merged = per_contig.merge_summaries([*self.summaries, per_contig.summarize(self.unsent)])
        skipped = [record for record in self.records if record["status"] == "too_short"]
        return {
            "contigs": merged["contigs"],
            "bp": merged["bp"],
            "queried_contigs": merged["queried_contigs"],
            "queried_bp": merged["queried_bp"],
            "skipped_contigs": len(skipped),
            "skipped_bp": sum(record["length"] for record in skipped),
            "too_few_kmers_contigs": merged["too_few_kmers_contigs"],
            "too_few_kmers_bp": merged["too_few_kmers_bp"],
            "no_hit_contigs": merged["no_hit_contigs"],
            "min_contig_length": self.options.min_length,
            "consensus_bp": per_contig.finalize_summary(merged)["consensus_bp"],
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
                        str(hit.get("genome_id") or ""),
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
        """Distinct species-threshold caveats of this run, in first-seen order."""
        return per_contig.caveats(self.records)


class _Batch:
    """One pending ``POST /contigs``: contigs, params and payload bytes.

    The byte account is deliberately pessimistic, so a batch is flushed well
    before it could exceed the server's upload limit: it adds a fixed overhead
    per part (boundary, part headers, content type, CRLFs) plus the file name,
    and one for the request.  :meth:`SylphClient.contigs_size` is still the
    final authority in :func:`_send`, which splits a chunk that turns out too
    large.
    """

    #: Multipart framing of one sketch part, excluding the file name.
    PART_OVERHEAD = 512
    #: Multipart framing of the request itself (boundaries, params part).
    REQUEST_OVERHEAD = 512

    def __init__(
        self,
        fields: Sequence[str],
        *,
        options: Mapping[str, Any] | None = None,
        params_budget: int = DEFAULT_PARAMS_BUDGET,
        upload_limit: int = 0,
    ) -> None:
        self._fields = fields
        self._options = dict(options or {})
        self._budget = params_budget
        self._upload_limit = upload_limit
        self.items: list[_Pending] = []
        self._payload_bytes = 0
        # ``contigs_json_size([])`` is the exact size of the params object with
        # an empty ``contigs`` list; every added entry adds its own JSON plus
        # the ", " separator.
        self._params_bytes = contigs_json_size(
            [], options=self._options, fields=fields
        )

    @property
    def params_bytes(self) -> int:
        return self._params_bytes

    @property
    def payload_bytes(self) -> int:
        return self._payload_bytes

    def projected_size(self, spec: Mapping[str, Any], payload: int) -> int:
        """Upper bound of the request body after adding one more sketch."""
        parts = len(self.items) + 1
        return (
            self._params_bytes
            + contig_json_size(spec)
            + (2 if self.items else 0)
            + self._payload_bytes
            + payload
            + parts * (self.PART_OVERHEAD + len(str(spec["name"]).encode("utf-8")))
            + self.REQUEST_OVERHEAD
        )

    def will_fit(self, spec: Mapping[str, Any], payload: int) -> bool:
        """Whether one more sketch may join this batch, conservatively.

        Never returns ``False`` for an empty batch: a single sketch that cannot
        fit is rejected by :func:`run` (after measuring the real request), so
        flushing could not help.
        """
        if not self.items:
            return True
        extra = contig_json_size(spec) + 2
        if self.params_bytes + extra > self._budget:
            return False
        return not (
            self._upload_limit
            and self.projected_size(spec, payload) > self._upload_limit
        )

    def add(self, record: dict[str, Any], spec: dict[str, Any], payload: bytes) -> None:
        self._params_bytes += contig_json_size(spec) + (2 if self.items else 0)
        self._payload_bytes += len(payload)
        self.items.append(_Pending(record, spec, payload))

    def clear(self) -> None:
        self.items = []
        self._params_bytes = contigs_json_size(
            [], options=self._options, fields=self._fields
        )
        self._payload_bytes = 0


def chunk_size_for(info: dict[str, Any]) -> int:
    """How many contig sketches to send per ``POST /contigs`` request.

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


def params_budget(info: dict[str, Any]) -> int:
    """How large the JSON ``params`` part of one request may grow, in bytes.

    The server advertises the hard form-part limit (``limits.max_params_bytes``,
    1 MiB).  Keep a margin: the JSON repeats every contig name (up to 1024
    characters), and a rejected request would lose the whole batch.  Never more
    than :data:`DEFAULT_PARAMS_BUDGET`.
    """
    limits = info.get("limits") or {}
    limit = int(limits.get("max_params_bytes") or 1024 * 1024)
    return min(DEFAULT_PARAMS_BUDGET, max(1024, limit * 9 // 10))


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
    params = per_contig.QUERY_PARAMS
    chunk_size = chunk_size_for(info)
    upload_limit = (
        int((info.get("limits") or {}).get("max_upload_mb") or 0) * 1024 * 1024
    )
    k = int(database["k"])

    records: list[dict[str, Any]] = []
    unsent: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    #: The server-side options: what to score against and what to report.  The
    #: client cannot evaluate them (the ANI is contig-relative and needs the
    #: server's k-mer rows), so they travel with every request.
    server_options = {
        "top": options.top,
        "min_ani": options.min_ani,
        "consensus_ratio": options.consensus_ratio,
        "consensus_z": options.consensus_z,
    }
    batch = _Batch(
        options.fields,
        options=server_options,
        params_budget=params_budget(info),
        upload_limit=upload_limit,
    )

    def flush() -> None:
        if not batch.items:
            return
        _send(
            client,
            batch.items,
            options=server_options,
            fields=options.fields,
            upload_limit=upload_limit,
            summaries=summaries,
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
            unsent.append(record)
            continue
        if contig.sketch.n_kmers < params.min_number_kmers:
            # sylph's own floor is reference-side, but a sample this small
            # cannot give a meaningful ANI either: report it, do not send it.
            record["status"] = "no_hit"
            record["reason"] = (
                f"too few k-mers ({contig.sketch.n_kmers} < "
                f"{params.min_number_kmers})"
            )
            unsent.append(record)
            continue
        if not contig.contig_kmers:
            # The server requires a positive contig_kmers (it is the ANI's
            # denominator): a tandem repeat has none, since a genome sketch
            # drops every k-mer that occurs more than once.
            record["status"] = "no_hit"
            record["reason"] = (
                "no genome-style k-mers (every k-mer of the contig is repeated)"
            )
            unsent.append(record)
            continue
        spec = {
            "name": contig.name,
            "contig_kmers": contig.contig_kmers,
            "length": contig.length,
            "gc": contig.gc,
        }
        payload = contig.sketch.to_bytes()
        if upload_limit and batch.projected_size(spec, len(payload)) > upload_limit:
            # The estimate may over-count the framing: measure the real request
            # before refusing, then fail as soon as the sketch is seen instead
            # of at the end of the batch.
            size = client.contigs_size(
                [(contig.name, payload)],
                contigs=[spec],
                options=server_options,
                fields=options.fields,
            )
            if size > upload_limit:
                raise ClientError(
                    f"the sketch of contig {contig.name!r} needs a {size} byte "
                    f"request, over the server's "
                    f"{upload_limit // (1024 * 1024)} MB upload limit"
                )
        if not batch.will_fit(spec, len(payload)):
            flush()
        batch.add(record, spec, payload)
        if len(batch.items) >= chunk_size:
            flush()
    flush()
    return PerContigRun(
        records,
        unsent=unsent,
        summaries=summaries,
        options=options,
        params=params,
        database=database,
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
    options: Mapping[str, Any],
    fields: Sequence[str],
    upload_limit: int,
    summaries: list[dict[str, Any]],
) -> None:
    """Send one batch, splitting it when the real request body is too large."""
    queue: deque[list[_Pending]] = deque([list(items)])
    while queue:
        chunk = queue.popleft()
        samples = [(item.record["id"], item.payload) for item in chunk]
        specs = [item.spec for item in chunk]
        if upload_limit:
            size = client.contigs_size(
                samples, contigs=specs, options=options, fields=fields
            )
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
        response = client.contigs(
            samples, contigs=specs, options=options, fields=fields
        )
        _apply(response, chunk, summaries=summaries)


def _apply(
    response: Mapping[str, Any],
    chunk: Sequence[_Pending],
    *,
    summaries: list[dict[str, Any]],
) -> None:
    """Merge one ``/contigs`` response into the records of the batch."""
    results = response.get("contigs")
    if not isinstance(results, list) or len(results) != len(chunk):
        raise ClientError("unexpected response from /contigs: contig count mismatch")
    for item, result in zip(chunk, results):
        record = item.record
        if not isinstance(result, Mapping) or str(result.get("name")) != record["id"]:
            raise ClientError(
                "unexpected response from /contigs: contigs are out of order"
            )
        record["hits"] = list(result.get("hits") or ())
        record["assignment"] = result.get("assignment")
        record["closest"] = result.get("closest")
        record["consensus"] = result.get("consensus")
        if result.get("contig_kmers") is not None:
            record["contig_kmers"] = result["contig_kmers"]
        status = result.get("status")
        if status == "ok":
            record["status"] = "ok"
        elif status == "too_few_kmers":
            # The CLI reports a contig it cannot score as no_hit with a reason,
            # whether it skipped the request itself or the server noticed.
            record["status"] = "no_hit"
            record["reason"] = str(
                result.get("reason") or "too few k-mers to score the contig"
            )
        else:
            record["status"] = "no_hit"
    summary = response.get("summary")
    if isinstance(summary, Mapping):
        summaries.append(dict(summary))
