"""Profiling work behind the HTTP endpoints: the loaded database and the jobs.

Everything here is synchronous and blocking; ``server.py`` runs it in worker
threads.  Client mistakes surface as :class:`BadRequest`.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Sequence

import pysylph

from .assembly import (
    DECOMPRESSION_FACTOR,
    classify_hits,
    gunzip_limited,
    is_gzip,
    parse_assembly,
)
from .metadata import METADATA_COLUMNS, MetadataDB, accession_from_genome_name
from .protocol import ProfileParams, result_row
from .sketchcheck import SketchFormatError, parse_sketch_header


class BadRequest(ValueError):
    """A client error detected while running a job in a worker thread."""


class LoadedDatabase:
    """A loaded sylph database plus the identity reported by ``/info``."""

    def __init__(self, path: str | os.PathLike[str], *, compute_hash: bool = False):
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"database not found: {self.path}")
        self.two_stage = self.path.name.endswith(".syl2db")
        if self.two_stage:
            self.db: Any = pysylph.TwoStageDatabase.load(str(self.path))
            self.k = int(self.db.k)
            self.c = int(self.db.c)
            self.screen_c: int | None = int(self.db.screen_c)
        else:
            self.db = pysylph.Database.load(str(self.path))
            if len(self.db) == 0:
                raise ValueError(f"database is empty: {self.path}")
            self.k = int(self.db[0].k)
            self.c = int(self.db[0].c)
            self.screen_c = None
        self.genomes = len(self.db)
        self.size = self.path.stat().st_size
        self.sha256: str | None = None
        if compute_hash:
            self.sha256 = _sha256(self.path)

    @property
    def name(self) -> str:
        return self.path.name

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "size": self.size,
            "sha256": self.sha256,
            "genomes": self.genomes,
            "c": self.c,
            "k": self.k,
            "screen_c": self.screen_c,
            "two_stage": self.two_stage,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_assembly(
    database: LoadedDatabase,
    metadata: MetadataDB | None,
    name: str,
    payload: bytes,
    top: int,
    min_ani: float,
    max_sequence_bytes: int,
) -> dict[str, Any]:
    """Parse, sketch and profile one assembly.  Runs in a worker thread."""
    data = payload
    if is_gzip(data):
        data = gunzip_limited(data, max_sequence_bytes * DECOMPRESSION_FACTOR)
    parsed = parse_assembly(data)

    sketcher = pysylph.Sketcher(c=database.c, k=database.k)
    sketch = sketcher.sketch_single(name, list(parsed.sequences))
    result = _assembly_hits(database, metadata, sketch, top, min_ani)
    result["query"] = {
        "name": name,
        "records": parsed.records,
        "length": parsed.length,
        "gc": parsed.gc,
        "sketch_kmers": sketch.n_kmers,
        "input": "sequence",
    }
    return result


def run_assembly_sketch(
    database: LoadedDatabase,
    metadata: MetadataDB | None,
    name: str,
    payload: bytes,
    top: int,
    min_ani: float,
    stats: dict[str, Any],
) -> dict[str, Any]:
    """Profile a precomputed assembly sketch.  Runs in a worker thread.

    The sketch is validated exactly like ``/profile`` uploads; everything after
    that is shared with the raw-sequence path.  The server cannot know the
    number of records, the length or the GC content of an assembly it never
    saw, so those come from the optional (informational) query parameters of
    the client, else ``null``.
    """
    sketch, header = _decode_sketch(database, name, payload)
    result = _assembly_hits(database, metadata, sketch, top, min_ani)
    result["query"] = {
        "name": name,
        "records": stats["records"],
        "length": stats["length"],
        "gc": stats["gc"],
        "sketch_kmers": header.kmer_count,
        "input": "sketch",
    }
    return result


def _decode_sketch(
    database: LoadedDatabase, name: str, payload: bytes
) -> tuple[Any, Any]:
    """Validate sketch bytes against the database and decode them.

    Checks the fixed header before decoding: sylph's deserializer allocates
    from the k-mer count prefix, and a huge count aborts the whole process.
    This also lets a c/k mismatch be rejected without deserializing anything.
    """
    try:
        header = parse_sketch_header(payload)
    except SketchFormatError as error:
        raise BadRequest(f"sample {name!r}: not a valid sample sketch ({error})") from error
    if header.k != database.k:
        raise BadRequest(
            f"sample {name!r}: sketch k={header.k} does not match the database k={database.k}"
        )
    if header.c > database.c:
        raise BadRequest(
            f"sample {name!r}: sketch c={header.c} is sparser than the database c={database.c}"
        )
    try:
        sketch = pysylph.SampleSketch.from_bytes(payload)
    except (ValueError, OSError) as error:
        raise BadRequest(f"sample {name!r}: not a valid sample sketch ({error})") from error
    return sketch, header


def _assembly_hits(
    database: LoadedDatabase,
    metadata: MetadataDB | None,
    sketch: Any,
    top: int,
    min_ani: float,
) -> dict[str, Any]:
    """Query ``sketch`` (naive ANI), join metadata and classify the hits."""
    profiler = pysylph.Profiler(
        database.db,
        no_adjust=True,
        minimum_ani=min_ani,
        screen_ani=min(min_ani, 85.0),
    )
    results = sorted(profiler.query(sketch), key=lambda result: float(result.ani), reverse=True)
    results = results[:top]

    columns = tuple(metadata.columns) if metadata is not None else METADATA_COLUMNS
    accessions = [
        accession_from_genome_name(result.genome_sketch.name) for result in results
    ]
    found = (
        metadata.lookup(accession for accession in accessions if accession)
        if metadata is not None
        else {}
    )

    hits: list[dict[str, Any]] = []
    for rank, result in enumerate(results, start=1):
        genome = result.genome_sketch
        accession = accession_from_genome_name(genome.name)
        contained, total = (int(value) for value in result.containment)
        row: dict[str, Any] = {
            "rank": rank,
            "ani": float(result.ani),
            "containment": [contained, total],
            "containment_fraction": round(contained / total, 3) if total else 0.0,
            "genome_file": genome.name,
            "accession": accession,
        }
        record = found.get(accession, {}) if accession else {}
        for column in columns:
            row[column] = record.get(column)
        hits.append(row)

    assignment, closest = classify_hits(hits)
    return {"assignment": assignment, "closest": closest, "hits": hits}


def run_sample(
    database: LoadedDatabase,
    metadata: MetadataDB | None,
    mode: str,
    params: ProfileParams,
    fields: Sequence[str] | None,
    name: str,
    payload: bytes,
    hide_ani_ci: bool = False,
) -> dict[str, Any]:
    """Deserialize, validate and profile one sketch.  Runs in a worker thread."""
    sketch, _ = _decode_sketch(database, name, payload)

    profiler = pysylph.Profiler(database.db, **params.profiler_kwargs())
    results = profiler.profile(sketch) if mode == "profile" else profiler.query(sketch)

    rows: list[dict[str, Any]] = []
    if metadata is None:
        for result in results:
            rows.append(result_row(result, name, mode=mode, hide_ani_ci=hide_ani_ci))
    else:
        accessions = [accession_from_genome_name(result.genome_sketch.name) for result in results]
        found = metadata.lookup(accession for accession in accessions if accession)
        for result, accession in zip(results, accessions):
            rows.append(
                result_row(
                    result,
                    name,
                    mode=mode,
                    metadata=found.get(accession, {}) if accession else {},
                    fields=fields,
                    hide_ani_ci=hide_ani_ci,
                )
            )
    return {"name": name, "kind": mode, "rows": rows}
