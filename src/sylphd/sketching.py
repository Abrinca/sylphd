"""Turn command line inputs into `pysylph.SampleSketch` objects.

Sample naming
-------------
Each input file is one sample by default.  The sample name is the file's base
name without compression/fasta/fastq suffixes; for paired inputs ``_R1``,
``_R2``, ``.R1``, ``.R2``, ``_1`` and ``_2`` are removed.  ``--sample-name``
overrides the name when a single sample is given, and ``--sample NAME:SPEC``
names a sample explicitly.
"""

from __future__ import annotations

import collections
import itertools
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

import dnaio

from pysylph import SampleSketch, Sketcher

_COMPRESSION_SUFFIXES = (".gz", ".bz2", ".xz", ".zst", ".bgz", ".bgzf")
_READ_SUFFIXES = (
    ".fastq",
    ".fq",
    ".fasta",
    ".fa",
    ".fna",
    ".fas",
    ".fsa",
    ".sam",
    ".sylsp",
)
# same rule as web/reads.js: sample_R1, sample.R2, sample_1 and Illumina's sample_S1_L001_R1_001
_PAIR_SUFFIX_RE = re.compile(r"[_.]R?[12](_001)?$", re.IGNORECASE)

SKETCH_SUFFIX = ".sylsp"

#: Longest sample name, in characters.
MAX_SAMPLE_NAME_CHARS = 1024


class InputError(ValueError):
    """Raised for invalid input specifications."""


def check_sample_name(name: str) -> str:
    """Return ``name`` if it is a usable sample name, else raise :class:`InputError`.

    Names end up in tab-separated output, so tabs, newlines and other control
    characters are errors, not something to escape.
    """
    if len(name) > MAX_SAMPLE_NAME_CHARS:
        raise InputError(f"sample name too long (max {MAX_SAMPLE_NAME_CHARS} characters)")
    if any(unicodedata.category(char) == "Cc" for char in name):
        raise InputError(f"sample name {name!r} contains a tab, newline or other control character")
    return name


def sample_name_from_path(path: str | os.PathLike[str], *, paired: bool = False) -> str:
    """Derive a sample name from a file name.

    >>> sample_name_from_path("/data/sample-a_R1.fastq.gz", paired=True)
    'sample-a'
    >>> sample_name_from_path("reads.fq.gz")
    'reads'
    """
    base = os.path.basename(str(path))
    lowered = base.lower()
    for suffix in _COMPRESSION_SUFFIXES:
        if lowered.endswith(suffix):
            base = base[: -len(suffix)]
            lowered = lowered[: -len(suffix)]
            break
    for suffix in _READ_SUFFIXES:
        if lowered.endswith(suffix):
            base = base[: -len(suffix)]
            break
    if paired:
        base = _PAIR_SUFFIX_RE.sub("", base)
    return base or "sample"


@dataclass(frozen=True)
class SampleInput:
    """One sample: a name plus one or more inputs.

    ``inputs`` holds one tuple per input: ``(path,)`` for single-end files,
    ``(r1, r2)`` for a pair.  All inputs of a sample must have the same shape.
    """

    name: str
    inputs: tuple[tuple[str, ...], ...]
    interleaved: bool = False

    def __post_init__(self) -> None:
        check_sample_name(self.name)

    @property
    def paired(self) -> bool:
        return any(len(item) == 2 for item in self.inputs)

    @property
    def paths(self) -> list[str]:
        return [path for item in self.inputs for path in item]

    @property
    def is_sketch(self) -> bool:
        return len(self.inputs) == 1 and self.inputs[0][0].endswith(SKETCH_SUFFIX)


def _split_paths(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def parse_multi_file_spec(spec: str, *, default_name: str | None = None) -> SampleInput:
    """Parse ``NAME:file1,file2`` or ``NAME:r1+r2,r1b+r2b``.

    ``+`` separates the two mates of a pair, ``,`` separates inputs of the same
    sample.  A spec without ``:`` uses ``default_name`` (or the first file's
    name) as the sample name.
    """
    if ":" in spec:
        name, _, rest = spec.partition(":")
        name = name.strip()
        if not name:
            raise InputError(f"empty sample name in {spec!r}")
    else:
        rest = spec
        name = default_name or ""

    items: list[tuple[str, ...]] = []
    for item in _split_paths(rest):
        mates = [mate.strip() for mate in item.split("+") if mate.strip()]
        if not mates:
            continue
        if len(mates) > 2:
            raise InputError(
                f"{spec!r}: at most two files per input (R1+R2), got {len(mates)}"
            )
        items.append(tuple(mates))
    if not items:
        raise InputError(f"no input files in {spec!r}")

    paired = {len(item) == 2 for item in items}
    if len(paired) > 1:
        raise InputError(
            f"{spec!r}: mixing single-end and paired inputs in one sample is not supported"
        )
    if not name:
        name = sample_name_from_path(items[0][0], paired=len(items[0]) == 2)
    return SampleInput(name=name, inputs=tuple(items))


def _resolve_pair_paths(first: Sequence[str], second: Sequence[str]) -> list[tuple[str, str]]:
    if not first and not second:
        return []
    if len(first) != len(second):
        raise InputError(
            f"--first/--second lists differ in length ({len(first)} != {len(second)})"
        )
    if not first:
        raise InputError("--first/--second given without files")
    return list(zip(first, second))


def build_sample_inputs(
    *,
    files: Sequence[str] = (),
    reads: Sequence[str] = (),
    first: Sequence[str] = (),
    second: Sequence[str] = (),
    interleaved: Sequence[str] = (),
    specs: Sequence[str] = (),
) -> list[SampleInput]:
    """Build the ordered list of samples from the command line inputs."""
    samples: list[SampleInput] = []
    for path in files:
        samples.append(SampleInput(sample_name_from_path(path), ((path,),)))
    for path in reads:
        samples.append(SampleInput(sample_name_from_path(path), ((path,),)))
    for r1, r2 in _resolve_pair_paths(first, second):
        samples.append(
            SampleInput(
                sample_name_from_path(r1, paired=True),
                ((r1, r2),),
            )
        )
    for path in interleaved:
        samples.append(
            SampleInput(
                sample_name_from_path(path, paired=True),
                ((path,),),
                interleaved=True,
            )
        )
    for spec in specs:
        samples.append(parse_multi_file_spec(spec))
    if not samples:
        raise InputError("no input samples given")

    names: dict[str, int] = {}
    for sample in samples:
        names[sample.name] = names.get(sample.name, 0) + 1
    duplicates = sorted(name for name, count in names.items() if count > 1)
    if duplicates:
        raise InputError(
            f"duplicate sample name(s): {', '.join(duplicates)} "
            "(use --sample NAME:FILES or --sample-name)"
        )
    return samples


def iter_sequences(path: str) -> Iterator[str]:
    """Yield the sequences of a FASTA/FASTQ file (gzip handled transparently)."""
    with dnaio.open(path) as reader:
        for record in reader:
            yield record.sequence


def _paired_records(sample: SampleInput) -> Iterator[tuple[str, str]]:
    """Yield ``(r1_sequence, r2_sequence)`` pairs from paired or interleaved input.

    ``dnaio`` validates read names and counts for each lane, so mismatched
    mates, odd interleaved counts, and lane-count mismatches fail loudly
    instead of silently dropping reads.
    """
    if sample.interleaved:
        if any(len(item) != 1 for item in sample.inputs):
            raise InputError(
                f"sample {sample.name!r}: interleaved input must be a single file per input"
            )
        for item in sample.inputs:
            path = item[0]
            with dnaio.open(path, interleaved=True) as reader:
                for r1, r2 in reader:
                    yield r1.sequence, r2.sequence
        return
    if any(len(item) != 2 for item in sample.inputs):
        raise InputError(
            f"sample {sample.name!r}: paired input must have exactly two files per input"
        )
    for r1_path, r2_path in sample.inputs:
        with dnaio.open(r1_path, file2=r2_path) as reader:
            for r1, r2 in reader:
                yield r1.sequence, r2.sequence


def _combined(sides: Iterable[Iterable[str]]) -> Iterator[str]:
    return itertools.chain.from_iterable(sides)


def load_sketch(path: str) -> SampleSketch:
    """Load a pre-sketched ``.sylsp`` file."""
    try:
        return SampleSketch.load(path)
    except (ValueError, OSError) as error:
        raise InputError(f"cannot load sketch {path}: {error}") from error


def _split_pairs(
    pairs: Iterator[tuple[str, str]],
) -> tuple[Iterator[str], Iterator[str]]:
    """Split one stream of mate pairs into two lazy streams for ``sketch_paired``.

    ``sketch_paired`` zips its arguments in lockstep (R1 first, then R2), so the
    R1 side drives the shared ``dnaio`` reader and parks the mate in a one-item
    buffer. Memory stays constant.

    Errors raised by ``dnaio`` (mismatched IDs or counts) surface on the R1
    side. Rust's ``zip`` drops an R1 error if R2 is exhausted at the same step,
    so the R2 side re-raises it.
    """
    pending: collections.deque[str] = collections.deque()
    failure: list[BaseException] = []

    def first() -> Iterator[str]:
        try:
            for seq1, seq2 in pairs:
                pending.append(seq2)
                yield seq1
        except BaseException as error:
            failure.append(error)
            raise

    def second() -> Iterator[str]:
        while True:
            if pending:
                yield pending.popleft()
            elif failure:
                raise failure[0]
            else:
                return

    return first(), second()


def sketch_sample(sample: SampleInput, *, c: int, k: int) -> SampleSketch:
    """Sketch one sample with the server's ``c`` and ``k``."""
    if sample.is_sketch:
        return load_sketch(sample.paths[0])

    sketcher = Sketcher(c=c, k=k)
    try:
        if sample.interleaved or sample.paired:
            seq1, seq2 = _split_pairs(_paired_records(sample))
            return sketcher.sketch_paired(sample.name, seq1, seq2)
        reads = _combined(iter_sequences(item[0]) for item in sample.inputs)
        return sketcher.sketch_single(sample.name, reads)
    except (
        OSError,
        EOFError,
        ValueError,
        dnaio.FileFormatError,
        dnaio.UnknownFileFormat,
    ) as error:
        raise InputError(f"cannot sketch sample {sample.name!r}: {error}") from error


def check_sketch(sketch: SampleSketch, *, c: int, k: int, name: str = "") -> None:
    """Check that a sketch can be profiled against a database with ``c``/``k``."""
    prefix = f"{name}: " if name else ""
    if sketch.k != k:
        raise InputError(
            f"{prefix}sketch was built with k={sketch.k}, but the database uses k={k}"
        )
    if sketch.c > c:
        raise InputError(
            f"{prefix}sketch was built with c={sketch.c}, which is sparser than the "
            f"database c={c}; re-sketch it at c<={c} or send the reads instead"
        )


@dataclass(frozen=True)
class AssemblyStats:
    """Facts about a sketched assembly that a sketch does not carry."""

    records: int
    length: int
    gc: float


def sketch_assembly(
    path: str | os.PathLike[str], name: str, *, c: int, k: int
) -> tuple[SampleSketch, AssemblyStats]:
    """Sketch one assembly (FASTA/FASTQ, gzip fine) like the server does for ``/assembly``.

    Every record is one "read" of a single-end sample.  Also returns the record
    count, total length and GC percent, counted while the file streams by.
    """
    records = length = gc = 0

    def sequences() -> Iterator[str]:
        nonlocal records, length, gc
        with dnaio.open(str(path)) as reader:
            for record in reader:
                sequence = record.sequence.upper()
                if not sequence:
                    raise InputError(f"{path}: sequence #{records + 1} is empty")
                records += 1
                length += len(sequence)
                gc += sequence.count("G") + sequence.count("C")
                yield sequence

    try:
        sketch = Sketcher(c=c, k=k).sketch_single(name, sequences())
    except (
        OSError,
        EOFError,
        ValueError,
        dnaio.FileFormatError,
        dnaio.UnknownFileFormat,
    ) as error:
        raise InputError(f"cannot sketch {path}: {error}") from error
    if not records or not length:
        raise InputError(f"{path} contains no sequences")
    return sketch, AssemblyStats(records, length, round(100.0 * gc / length, 2))


@dataclass(frozen=True)
class Contig:
    """One record of a fragmented assembly, considered on its own.

    ``sketch`` is the sample-style sketch sent to the server and is ``None``
    for a record shorter than the per-contig minimum: such a contig is not
    sketched and not sent, but still reported.  ``contig_kmers`` is the number
    of k-mers the contig would contribute to a *genome* sketch of itself
    (``None`` when not sketched), the denominator of the contig-relative ANI.
    """

    index: int
    name: str
    length: int
    gc: float
    sketch: SampleSketch | None = None
    contig_kmers: int | None = None


def unique_contig_name(raw: str, counts: dict[str, int], used: set[str], index: int) -> str:
    """Return a sample name for one contig, suffixing repeated ids.

    The first ``contig`` keeps its name, the second becomes ``contig_2``, the
    third ``contig_3``, and a later record that is *literally* named
    ``contig_2`` becomes ``contig_2_2``.  Names are thus unique and depend
    only on record order.

    >>> counts: dict[str, int] = {}
    >>> used: set[str] = set()
    >>> [unique_contig_name("c", counts, used, i) for i in range(1, 4)]
    ['c', 'c_2', 'c_3']
    """
    base = raw or f"contig-{index}"
    count = counts.get(base, 0) + 1
    name = base if count == 1 else f"{base}_{count}"
    while name in used:
        count += 1
        name = f"{base}_{count}"
    counts[base] = count
    used.add(name)
    return name


def sketch_contigs(
    path: str | os.PathLike[str], *, c: int, k: int, min_length: int
) -> Iterator[Contig]:
    """Yield one :class:`Contig` per record, each sketched as its own sample.

    The sample name is the first word of the record header (``dnaio``'s
    ``id``), validated by :func:`check_sample_name`; repeated ids of *sketched*
    records are suffixed by :func:`unique_contig_name`.  Records shorter than
    ``min_length`` are yielded with no name bookkeeping at all (their ``name``
    is the raw record id or ``contig-<index>``): they are never sent, so they
    need neither uniqueness nor an entry in the growing ``counts``/``used``
    sets.  That keeps memory flat for FASTQ input with millions of short reads,
    where only a handful of records may ever be sketched.  The same applies to
    per-record objects: this generator yields one :class:`Contig` per record
    (the caller decides what to keep), but the name-tracking state is bounded
    by the number of *sent* records.

    The file is streamed and only one record is sketched at a time, so memory
    is bounded by the largest record plus the bookkeeping the caller keeps
    (the names already used, and any records/rows it collects).
    """
    sketcher = Sketcher(c=c, k=k)
    counts: dict[str, int] = {}
    used: set[str] = set()
    seen = 0
    try:
        with dnaio.open(str(path)) as reader:
            for index, record in enumerate(reader, start=1):
                seen += 1
                sequence = record.sequence.upper()
                length = len(sequence)
                gc = (
                    round(100.0 * (sequence.count("G") + sequence.count("C")) / length, 2)
                    if length
                    else 0.0
                )
                # Validate the raw id for every record (a broken header is an
                # input error whatever its length), but allocate a unique name
                # only for the records that are actually sketched and sent.
                if not record.id or not record.id.strip():
                    name = f"contig-{index}"
                else:
                    name = record.id
                try:
                    check_sample_name(name)
                except InputError as error:
                    raise InputError(f"record #{index}: {error}") from error
                sketch = contig_kmers = None
                if length and length >= min_length:
                    name = unique_contig_name(record.id, counts, used, index)
                    sketch = sketcher.sketch_single(name, [sequence])
                    # The genome-style count is the exact denominator of the
                    # contig-relative ANI: the sample sketch keeps every
                    # selected k-mer, while a genome sketch drops duplicates
                    # and k-mers closer than min_spacing.  Counting the
                    # contig's own genome sketch makes an exact fragment score
                    # exactly 100 %.
                    contig_kmers = len(
                        sketcher.sketch_genome(name, [sequence], profiling=False).kmers
                    )
                yield Contig(
                    index=index,
                    name=name,
                    length=length,
                    gc=gc,
                    sketch=sketch,
                    contig_kmers=contig_kmers,
                )
    except InputError:
        raise
    except (
        OSError,
        EOFError,
        ValueError,
        dnaio.FileFormatError,
        dnaio.UnknownFileFormat,
    ) as error:
        raise InputError(f"cannot sketch {path}: {error}") from error
    if not seen:
        raise InputError(f"{path} contains no sequences")
