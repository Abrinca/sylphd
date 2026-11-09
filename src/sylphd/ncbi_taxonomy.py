"""NCBI lineages for the genomes of sylph-tax sources.

``sylphd setup --ncbi-taxonomy`` gives every key of a sylph-tax source that
GTDB's metadata does not cover an NCBI lineage: the assembly accession in the
key (``GCF_000002495.2`` in ``GCF_000002495.2_MG8``) maps to a taxid in NCBI's
assembly summaries, and the taxid to a ranked lineage in the taxdump's
``rankedlineage.dmp`` (with ``nodes.dmp`` supplying the node's own rank).
Nothing is matched by name.

The taxdump is downloaded on every run into a temporary directory under the
cache directory, ``rankedlineage.dmp``, ``nodes.dmp`` and ``merged.dmp`` are
extracted, and everything is deleted afterwards, so nothing NCBI stays cached
and nothing is stale.  Only the summary that can hold an accession's prefix is
read (RefSeq for ``GCF_``, GenBank for ``GCA_``), streamed and filtered, so
neither summary is stored.
"""

from __future__ import annotations

import re
import shutil
import tarfile
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import httpx

from .download import (
    CHUNK_SIZE,
    DownloadError,
    ProgressCallback,
    download_urls,
    remote_size,
)

#: NCBI's current taxonomy dump; three of its files are extracted from it.
TAXDUMP_URL = "https://ftp.ncbi.nlm.nih.gov/pub/taxonomy/new_taxdump/new_taxdump.tar.gz"

#: The taxdump files the lineage needs: ancestors, each node's own rank and
#: the taxids that were merged into another.
TAXDUMP_FILES: tuple[str, ...] = ("rankedlineage.dmp", "nodes.dmp", "merged.dmp")

#: Accession prefix -> the assembly summary that can contain it.
ASSEMBLY_SUMMARIES: dict[str, str] = {
    "GCF_": "https://ftp.ncbi.nlm.nih.gov/genomes/ASSEMBLY_REPORTS/assembly_summary_refseq.txt",
    "GCA_": "https://ftp.ncbi.nlm.nih.gov/genomes/ASSEMBLY_REPORTS/assembly_summary_genbank.txt",
}

#: ``GCF_000002515.2`` in a key such as ``GCF_000002495.2_MG8``.
ACCESSION_RE = re.compile(r"GC[AF]_\d+\.\d+", re.IGNORECASE)

#: GTDB/NCBI rank prefixes, in lineage order.
RANKS: tuple[str, ...] = ("d", "p", "c", "o", "f", "g", "s")

#: Column positions of ``rankedlineage.dmp`` (0 is the taxid, 1 the name).
_RANKED_COLUMNS = {"d": 9, "p": 7, "c": 6, "o": 5, "f": 4, "g": 3, "s": 2}

#: ``nodes.dmp`` rank names that are one of GTDB's seven ranks.
_NODE_RANKS = {
    "superkingdom": "d",
    "domain": "d",
    "phylum": "p",
    "class": "c",
    "order": "o",
    "family": "f",
    "genus": "g",
    "species": "s",
}


@dataclass(frozen=True)
class Assembly:
    """One row of an assembly summary."""

    taxid: int
    organism: str


@dataclass(frozen=True)
class NcbiGenome:
    """What an accession found in a summary resolves to.

    ``taxid`` is the current one (merged taxids are followed); ``lineage`` is
    ``None`` when the taxdump has no ranked lineage for it.
    """

    taxid: int
    organism: str
    lineage: str | None


def assembly_accession(key: str) -> str | None:
    """The assembly accession in ``key``, or ``None`` when it has none.

    >>> assembly_accession("GCF_000002495.2_MG8")
    'GCF_000002495.2'
    >>> assembly_accession("UHGV-0405173") is None
    True
    """
    match = ACCESSION_RE.search(key)
    return None if match is None else match.group(0).upper()


def _base(accession: str) -> str:
    return accession.rpartition(".")[0]


def _versions(accessions: Iterable[str]) -> dict[str, set[str]]:
    """``accession base -> requested versions``."""
    versions: dict[str, set[str]] = {}
    for accession in accessions:
        versions.setdefault(_base(accession), set()).add(accession)
    return versions


def iter_assembly_rows(lines: Iterable[str]) -> Iterator[tuple[str, int, str]]:
    """Yield ``(accession, taxid, organism)`` for the summary lines.

    The summary has ``#`` comment/header lines and tab-separated rows whose
    first, sixth and eighth columns are the assembly accession, the taxid and
    the organism name.
    """
    for line in lines:
        if line.startswith("#") or not line.strip():
            continue
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 8:
            continue
        try:
            taxid = int(parts[5])
        except ValueError:
            continue
        organism = parts[7].strip()
        if taxid > 0 and organism:
            yield parts[0].strip(), taxid, organism


def iter_node_ranks(
    lines: Iterable[str], taxids: Iterable[int]
) -> dict[int, str]:
    """``taxid -> rank prefix`` from ``nodes.dmp``, for the wanted taxids only.

    ``nodes.dmp`` is ``tax_id | parent tax_id | rank | embl code | ...``; only
    ranks that are one of the seven GTDB ranks are kept.
    """
    wanted = set(taxids)
    ranks: dict[int, str] = {}
    for line in lines:
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 3:
            continue
        try:
            taxid = int(parts[0])
        except ValueError:
            continue
        rank = _NODE_RANKS.get(parts[2].lower())
        if rank is not None and taxid in wanted:
            ranks[taxid] = rank
    return ranks


def iter_merged(lines: Iterable[str]) -> Iterator[tuple[int, int]]:
    """Yield ``(old taxid, current taxid)`` from ``merged.dmp`` lines."""
    for line in lines:
        parts = [part.strip() for part in line.split("|")]
        try:
            yield int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            continue


def iter_ranked_lineages(
    lines: Iterable[str], ranks: Mapping[int, str], wanted: set[int]
) -> Iterator[tuple[int, str]]:
    """Yield ``(taxid, lineage)`` of the ``wanted`` taxids from ``rankedlineage.dmp``.

    The dmp is NCBI's ``tax_id | name | species | genus | family | order |
    class | phylum | kingdom | superkingdom |``, holding the *ancestors'*
    names: a node that is itself a species has an empty species column.  When
    ``ranks`` (from ``nodes.dmp``) says the node has one of the seven ranks,
    that rank takes the node's own name (column 1).  Kingdom is not one of
    them, so the lineage keeps GTDB's ``d__…;p__…;c__…;o__…;f__…;g__…;s__…``
    shape with the superkingdom (or domain) as ``d__``.  Ranks NCBI leaves out
    (no-rank intermediate nodes) stay empty.
    """
    for line in lines:
        parts = [part.strip() for part in line.split("|")]
        if len(parts) <= max(_RANKED_COLUMNS.values()):
            continue
        try:
            taxid = int(parts[0])
        except ValueError:
            continue
        if taxid not in wanted:
            continue
        names = {rank: parts[column] for rank, column in _RANKED_COLUMNS.items()}
        if (own := ranks.get(taxid)) is not None:
            names[own] = parts[1]
        if any(names.values()):
            yield taxid, ";".join(f"{rank}__{names[rank]}" for rank in RANKS)


def _read[T](path: Path, parse: Callable[[Iterator[str]], T]) -> T:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return parse(handle)


def ranked_lineages(
    ranked_path: Path, nodes_path: Path, taxids: Iterable[int]
) -> dict[int, str]:
    """The lineages of ``taxids`` from the extracted taxdump files."""
    wanted = set(taxids)
    ranks = _read(nodes_path, lambda lines: iter_node_ranks(lines, wanted))
    return _read(
        ranked_path, lambda lines: dict(iter_ranked_lineages(lines, ranks, wanted))
    )


def merged_taxids(merged_path: Path, taxids: Iterable[int]) -> dict[int, int]:
    """``taxid -> current taxid`` for the merged ones among ``taxids``."""
    wanted = set(taxids)
    return _read(
        merged_path,
        lambda lines: {old: new for old, new in iter_merged(lines) if old in wanted},
    )


def _collect(
    accession: str,
    taxid: int,
    organism: str,
    *,
    accessions: set[str],
    versions: Mapping[str, set[str]],
    exact: dict[str, Assembly],
    by_base: dict[str, Assembly],
) -> None:
    if accession in accessions:
        exact.setdefault(accession, Assembly(taxid, organism))
        return
    base = _base(accession)
    if base in versions:
        by_base.setdefault(base, Assembly(taxid, organism))


def _pick(
    exact: Mapping[str, Assembly],
    by_base: Mapping[str, Assembly],
    versions: Mapping[str, set[str]],
) -> dict[str, Assembly]:
    """Exact accessions first; a base only when the source has one version of it."""
    found = dict(exact)
    for base, wanted in versions.items():
        if len(wanted) != 1:
            continue
        accession = next(iter(wanted))
        if accession not in found and base in by_base:
            found[accession] = by_base[base]
    return found


def read_assemblies(
    paths: Iterable[Path], accessions: Iterable[str]
) -> dict[str, Assembly]:
    """Resolve ``accessions`` from local summary files (used by the tests)."""
    versions = _versions(accessions)
    wanted = {accession for wanted in versions.values() for accession in wanted}
    exact: dict[str, Assembly] = {}
    by_base: dict[str, Assembly] = {}
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for accession, taxid, organism in iter_assembly_rows(handle):
                _collect(
                    accession,
                    taxid,
                    organism,
                    accessions=wanted,
                    versions=versions,
                    exact=exact,
                    by_base=by_base,
                )
    return _pick(exact, by_base, versions)


def _content_length(response: httpx.Response) -> int | None:
    value = response.headers.get("Content-Length")
    return int(value) if value and value.isdigit() else None


def _lines(
    response: httpx.Response, progress: ProgressCallback | None
) -> Iterator[str]:
    """The response's lines, checking that every byte the server announced arrived."""
    name = response.url.path.rsplit("/", 1)[-1]
    total = _content_length(response)
    buffer = b""
    done = 0
    for chunk in response.iter_bytes(CHUNK_SIZE):
        done += len(chunk)
        if progress is not None:
            progress(name, done, total)
        buffer += chunk
        start = 0
        while (end := buffer.find(b"\n", start)) >= 0:
            yield buffer[start:end].decode("utf-8", "replace")
            start = end + 1
        buffer = buffer[start:]
    if buffer:
        yield buffer.decode("utf-8", "replace")
    if total is not None and done != total:
        raise DownloadError(f"{name}: expected {total} bytes, read {done}")


def summary_urls(accessions: Iterable[str]) -> tuple[str, ...]:
    """The summaries that can contain ``accessions`` (RefSeq or/and GenBank).

    >>> len(summary_urls(["GCF_000002515.2"]))
    1
    >>> len(summary_urls(["GCF_000002515.2", "GCA_000002495.2"]))
    2
    """
    prefixes = {accession[:4].upper() for accession in accessions}
    return tuple(
        url for prefix, url in ASSEMBLY_SUMMARIES.items() if prefix in prefixes
    )


def fetch_assemblies(
    accessions: Iterable[str], *, progress: ProgressCallback | None = None
) -> dict[str, Assembly]:
    """Stream the needed NCBI assembly summaries and keep the requested accessions."""
    versions = _versions(accessions)
    wanted = {accession for wanted in versions.values() for accession in wanted}
    exact: dict[str, Assembly] = {}
    by_base: dict[str, Assembly] = {}
    timeout = httpx.Timeout(None, connect=30.0)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for url in summary_urls(wanted):
            with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
                response.raise_for_status()
                for accession, taxid, organism in iter_assembly_rows(
                    _lines(response, progress)
                ):
                    _collect(
                        accession,
                        taxid,
                        organism,
                        accessions=wanted,
                        versions=versions,
                        exact=exact,
                        by_base=by_base,
                    )
    return _pick(exact, by_base, versions)


def fetch_taxdump(
    directory: Path, *, progress: ProgressCallback | None = None
) -> tuple[Path, Path, Path]:
    """Download the taxdump and extract the files of ``TAXDUMP_FILES`` into ``directory``."""
    tarball = directory / "new_taxdump.tar.gz"
    size = remote_size(TAXDUMP_URL)
    download_urls([TAXDUMP_URL], tarball, expected_size=size, progress=progress)
    try:
        with tarfile.open(tarball, "r:gz") as archive:
            for name in TAXDUMP_FILES:
                source: IO[bytes] | None = archive.extractfile(name)
                if source is None:
                    raise DownloadError(f"{tarball}: no {name}")
                with open(directory / name, "wb") as target:
                    shutil.copyfileobj(source, target)
    except (tarfile.TarError, KeyError) as error:
        raise DownloadError(f"{tarball}: {error}") from error
    finally:
        tarball.unlink(missing_ok=True)
    ranked, nodes, merged = (directory / name for name in TAXDUMP_FILES)
    return ranked, nodes, merged


def lookup(
    accessions: Iterable[str],
    cache_dir: Path,
    *,
    progress: ProgressCallback | None = None,
) -> dict[str, NcbiGenome]:
    """Resolve each accession to its taxid, organism and NCBI lineage.

    An accession NCBI's summaries do not know is absent from the result; one
    whose taxid has no ranked lineage has ``lineage=None``.  The taxdump goes
    to a temporary directory in ``cache_dir`` that is removed before returning.
    """
    accessions = list(accessions)
    if not accessions:
        return {}
    found = fetch_assemblies(accessions, progress=progress)
    if not found:
        return {}
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache_dir, prefix="taxdump-") as name:
        ranked, nodes, merged_path = fetch_taxdump(Path(name), progress=progress)
        taxids = {assembly.taxid for assembly in found.values()}
        merged = merged_taxids(merged_path, taxids)
        current = {taxid: merged.get(taxid, taxid) for taxid in taxids}
        lineages = ranked_lineages(ranked, nodes, current.values())
    return {
        accession: NcbiGenome(
            current[assembly.taxid],
            assembly.organism,
            lineages.get(current[assembly.taxid]),
        )
        for accession, assembly in found.items()
    }
