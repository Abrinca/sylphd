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
One ``POST /contigs`` request is one server job, whatever the number of contigs
it carries (the server runs them one after the other inside that job), so a
batch is bounded by the server's advertised ``limits.max_contigs``
(``max_sketches`` on older servers), by the advertised size of the JSON
``params`` part (it repeats every contig name, length and k-mer count), and by
the *measured* multipart body against ``max_upload_mb``.  A batch that is still
too large is split in half, and a single sketch that cannot fit fails before it
is sent.
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


@dataclass(frozen=True)
class Skipped:
    """Records that were counted but never sent to ``POST /contigs``."""

    contigs: int = 0
    bp: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"contigs": self.contigs, "bp": self.bp}


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


def _format_bases(bp: int) -> str:
    """``80.0 kb`` / ``1.2 Mb``, for the skipped-record line."""
    if bp >= 1_000_000:
        return f"{bp / 1_000_000:.1f} Mb"
    if bp >= 1_000:
        return f"{bp / 1_000:.1f} kb"
    return f"{bp} bp"


class PerContigRun:
    """Records collected by :func:`run`, plus the writers for them."""

    def __init__(
        self,
        records: list[dict[str, Any]],
        *,
        summaries: list[dict[str, Any]],
        options: PerContigOptions,
        params: ProfileParams,
        database: dict[str, Any],
        skipped_short: Skipped | None = None,
        skipped_few_kmers: Skipped | None = None,
    ) -> None:
        #: The records that were sent, in input order.  Records below
        #: ``min_length`` and records with too few / zero genome-style k-mers
        #: are *not* kept (a FASTQ input can hold millions of short reads);
        #: they only survive as counters in :attr:`skipped_short` and
        #: :attr:`skipped_few_kmers`.
        self.records = records
        #: The ``summary`` of every ``POST /contigs`` response, in order.
        self.summaries = summaries
        self.options = options
        self.params = params
        self.database = database
        self.skipped_short = skipped_short or Skipped()
        self.skipped_few_kmers = skipped_few_kmers or Skipped()

    # -- documents --------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """Counts for the JSON document and the stderr summary.

        The per-request summaries are added up (the server reports every
        consensus taxon, so nothing is lost between chunks).  ``queried`` are
        the contigs that were actually sent; ``skipped_short`` (below
        ``--min-contig-length``) and ``skipped_few_kmers`` (too few sketched
        k-mers, or no genome-style k-mer) are counted separately and never
        appear as records or TSV rows.  Fractions are over queried bases only.
        Bases are aggregated by *consensus* taxon and rank, not by the top hit:
        one organism split over several near-identical GTDB species then counts
        once, at the rank the data actually supports.
        """
        merged = per_contig.merge_summaries(self.summaries)
        # Kept contigs the server itself refused for too few k-mers are in the
        # per-request summaries; the ones the client skipped never were.
        few = Skipped(
            contigs=self.skipped_few_kmers.contigs + merged["too_few_kmers_contigs"],
            bp=self.skipped_few_kmers.bp + merged["too_few_kmers_bp"],
        )
        sent = Skipped(contigs=merged["contigs"], bp=merged["bp"])
        return {
            "contigs": sent.contigs + self.skipped_short.contigs + self.skipped_few_kmers.contigs,
            "bp": sent.bp + self.skipped_short.bp + self.skipped_few_kmers.bp,
            "queried_contigs": merged["queried_contigs"],
            "queried_bp": merged["queried_bp"],
            "skipped_short": self.skipped_short.as_dict(),
            "skipped_few_kmers": few.as_dict(),
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
        short = summary["skipped_short"]
        if short["contigs"]:
            line += (
                f"; {short['contigs']} contigs ({_format_bases(short['bp'])}) "
                f"below {min_length} bp not classified"
            )
        few = summary["skipped_few_kmers"]
        if few["contigs"]:
            line += (
                f"; {few['contigs']} contigs ({_format_bases(few['bp'])}) "
                "with too few k-mers not classified"
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

    A ``/contigs`` request is one job on the server, so neither ``max_jobs``
    nor ``max_queue`` limits its size: only the per-request contig cap does.
    Use the advertised ``limits.max_contigs``; an older server without it
    falls back to ``limits.max_sketches`` (4096, and the same hard bound).
    """
    limits = info.get("limits") or {}
    size = int(limits.get("max_contigs") or limits.get("max_sketches") or 4096)
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
    skipped_short = Skipped()
    skipped_few_kmers = Skipped()
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
        if contig.sketch is None:
            # Below --min-contig-length: count it, keep no per-record object.
            skipped_short = Skipped(
                skipped_short.contigs + 1, skipped_short.bp + contig.length
            )
            continue
        if contig.sketch.n_kmers < params.min_number_kmers or not contig.contig_kmers:
            # sylph's own floor is reference-side, but a sample this small
            # cannot give a meaningful ANI either; and a tandem repeat has no
            # genome-style k-mer, so the ANI would divide by zero.  Neither is
            # sent, and neither is kept as a record.
            skipped_few_kmers = Skipped(
                skipped_few_kmers.contigs + 1, skipped_few_kmers.bp + contig.length
            )
            continue
        record = _new_record(contig)
        records.append(record)
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
        summaries=summaries,
        options=options,
        params=params,
        database=database,
        skipped_short=skipped_short,
        skipped_few_kmers=skipped_few_kmers,
    )


def _new_record(contig: Contig) -> dict[str, Any]:
    return {
        "id": contig.name,
        "index": contig.index,
        "length": contig.length,
        "gc": contig.gc,
        "status": "no_hit",
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
