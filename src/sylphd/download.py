"""Download GTDB metadata files and pre-built sylph databases.

GTDB metadata is verified against the release ``MD5SUM.txt``; databases are
checked against the small catalogue below (size always, MD5 on request).
Transfers go to ``*.part`` files, resume with HTTP ``Range`` and can use
``aria2c`` to fetch from all mirrors at once.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

import httpx

#: GTDB release root, e.g. ``.../releases/release232/232.0/``.
DEFAULT_BASE_URL = "https://data.gtdb.ecogenomic.org/releases/release{release}/{release}.0/"

#: Metadata files we care about, per domain.
METADATA_FILES: dict[str, str] = {
    "bac120": "bac120_metadata_r{release}.tsv.gz",
    "ar53": "ar53_metadata_r{release}.tsv.gz",
}

MD5SUMS_FILE = "MD5SUM.txt"

#: Chunk size for streaming transfers.
CHUNK_SIZE = 1024 * 1024

#: Chunk size for checksum verification; the docs promise 8 MiB.
MD5_CHUNK_SIZE = 8 * 1024 * 1024

ProgressCallback = Callable[[str, int, int | None], None]
VerifyCallback = Callable[[int, int | None], None]


# ---------------------------------------------------------------------------
# errors and helpers
# ---------------------------------------------------------------------------


class DownloadError(RuntimeError):
    """A transfer failed on every mirror, or produced the wrong file size."""


def format_size(size: int) -> str:
    """Format a byte count for humans, e.g. ``18.7 GB``."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def release_url(release: int | str, base_url: str | None = None) -> str:
    template = base_url or DEFAULT_BASE_URL
    return template.format(release=release)


def parse_md5sums(text: str) -> dict[str, str]:
    """Parse an ``MD5SUM.txt`` file into a ``{file name: md5}`` mapping.

    Entries are usually listed with a ``./`` prefix; only the base name is
    kept as key.
    """
    checksums: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        digest, name = parts[0], parts[-1]
        if name.startswith("./"):
            name = name[2:]
        checksums[os.path.basename(name)] = digest.lower()
    return checksums


def md5_file(
    path: Path,
    *,
    progress: VerifyCallback | None = None,
    chunk_size: int = MD5_CHUNK_SIZE,
) -> str:
    """Hash ``path`` with MD5, in ``chunk_size`` chunks.

    ``progress`` is called with the number of bytes hashed so far and the file
    size. Hashing a 20 GB database reads the whole file, which takes minutes on
    slow disks.
    """
    digest = hashlib.md5()
    total = path.stat().st_size
    done = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
            done += len(chunk)
            if progress is not None:
                progress(done, total)
    return digest.hexdigest()


def _md5sum(path: Path) -> str:
    return md5_file(path)


# ---------------------------------------------------------------------------
# database catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Database:
    """A pre-built sylph database and where to get it."""

    name: str
    filename: str
    mirrors: tuple[str, ...]
    size: int | None = None
    md5: str | None = None
    genomes: int | None = None
    c: int | None = None
    description: str = ""


def _database(
    name: str,
    mirrors: tuple[str, ...],
    *,
    size: int,
    md5: str | None = None,
    genomes: int | None = None,
    c: int | None = None,
    description: str = "",
) -> Database:
    return Database(
        name=name,
        filename=name,
        mirrors=mirrors,
        size=size,
        md5=md5,
        genomes=genomes,
        c=c,
        description=description,
    )


_FAUST = "http://faust.compbio.cs.cmu.edu/sylph-stuff/"
_GCS = "https://storage.googleapis.com/sylph-stuff/"

#: Pre-built databases from the sylph project, see
#: https://sylph-docs.github.io/pre%E2%80%90built-databases/.
#:
#: Sizes were recorded from the servers' ``Content-Length``. MD5 sums are the
#: object checksums published by the Google Cloud mirror (``x-goog-hash``), or
#: were computed from a local copy where noted; mean "that mirror has no
#: published checksum" (multipart uploads), so ``--verify`` cannot check them.
DATABASES: dict[str, Database] = {
    database.name: database
    for database in (
        # -- GTDB ------------------------------------------------------
        _database(
            "gtdb-r232-c200-dbv2.syl2db",
            (_FAUST + "gtdb-r232-c200-dbv2.syl2db", _GCS + "gtdb-r232-c200-dbv2.syl2db"),
            size=20044737329,
            md5="89ad2dbdd532be81c72e78f5d493d78d",
            genomes=199923,
            c=200,
            description="GTDB r232 species representatives, two-stage (recommended)",
        ),
        _database(
            "gtdb-r232-c200-dbv1.syldb",
            (_FAUST + "gtdb-r232-c200-dbv1.syldb", _GCS + "gtdb-r232-c200-dbv1.syldb"),
            size=25872594195,
            md5="051e7c7debff82b367dc2073965506e5",
            genomes=199923,
            c=200,
            description="GTDB r232 species representatives, c200",
        ),
        _database(
            "gtdb-r232-c1000-dbv1.syldb",
            (_FAUST + "gtdb-r232-c1000-dbv1.syldb", _GCS + "gtdb-r232-c1000-dbv1.syldb"),
            size=5219051219,
            md5="8cc9bc3ca5eac9ab7b57527a312fd623",
            genomes=199923,
            c=1000,
            description="GTDB r232 species representatives, c1000",
        ),
        _database(
            "gtdb-r226-c200-dbv2.syl2db",
            (_FAUST + "gtdb-r226-c200-dbv2.syl2db", _GCS + "gtdb-r226-c200-dbv2.syl2db"),
            size=14272284891,
            md5="1815389774987c7ce3d713c975a8853f",
            genomes=143614,
            c=200,
            description="GTDB r226 species representatives, two-stage",
        ),
        _database(
            "gtdb-r226-c200-dbv1.syldb",
            (_FAUST + "gtdb-r226-c200-dbv1.syldb", _GCS + "gtdb-r226-c200-dbv1.syldb"),
            size=18414135368,
            md5="a4ef042a2a5b037a276a5922c150d5d3",
            genomes=143614,
            c=200,
            description="GTDB r226 species representatives, c200",
        ),
        _database(
            "gtdb-r226-c1000-dbv1.syldb",
            (_FAUST + "gtdb-r226-c1000-dbv1.syldb", _GCS + "gtdb-r226-c1000-dbv1.syldb"),
            size=3713953632,
            md5="e56ac73c24e0e77f7e72a6e25a08c6a5",
            genomes=143614,
            c=1000,
            description="GTDB r226 species representatives, c1000",
        ),
        _database(
            "gtdb-r220-c200-dbv1.syldb",
            (_FAUST + "gtdb-r220-c200-dbv1.syldb", _GCS + "gtdb-r220-c200-dbv1.syldb"),
            size=14061572967,
            genomes=113104,
            c=200,
            description="GTDB r220 species representatives, c200",
        ),
        _database(
            "gtdb-r220-c1000-dbv1.syldb",
            (_FAUST + "gtdb-r220-c1000-dbv1.syldb", _GCS + "gtdb-r220-c1000-dbv1.syldb"),
            size=2836800847,
            genomes=113104,
            c=1000,
            description="GTDB r220 species representatives, c1000",
        ),
        _database(
            "v0.3-c200-gtdb-r214.syldb",
            (_FAUST + "v0.3-c200-gtdb-r214.syldb", _GCS + "v0.3-c200-gtdb-r214.syldb"),
            size=10825539803,
            genomes=85202,
            c=200,
            description="GTDB r214 species representatives, c200",
        ),
        _database(
            "v0.3-c1000-gtdb-r214.syldb",
            (_FAUST + "v0.3-c1000-gtdb-r214.syldb", _GCS + "v0.3-c1000-gtdb-r214.syldb"),
            size=2183213283,
            genomes=85202,
            c=1000,
            description="GTDB r214 species representatives, c1000",
        ),
        # -- other prokaryotes -----------------------------------------
        _database(
            "globdb_r232_sylph_v2_c200.syl2db",
            (_FAUST + "globdb_r232_sylph_v2_c200.syl2db",),
            size=31194509116,
            genomes=346233,
            c=200,
            description="GlobDB r232, two-stage (no Google mirror)",
        ),
        _database(
            "OceanDNA-c200-v0.3.syldb",
            (_FAUST + "OceanDNA-c200-v0.3.syldb", _GCS + "OceanDNA-c200-v0.3.syldb"),
            size=827489083,
            genomes=8466,
            c=200,
            description="OceanDNA ocean MAGs",
        ),
        _database(
            "SMAG-c200-v0.3.syldb",
            (_FAUST + "SMAG-c200-v0.3.syldb", _GCS + "SMAG-c200-v0.3.syldb"),
            size=2720077433,
            genomes=21077,
            c=200,
            description="SMAG soil MAGs",
        ),
        _database(
            "uhgg_all_c200_v0.3.0.syldb",
            (_FAUST + "uhgg_all_c200_v0.3.0.syldb", _GCS + "uhgg_all_c200_v0.3.0.syldb"),
            size=27971290837,
            genomes=289232,
            c=200,
            description="UHGG v2.0.1 gut genomes, not dereplicated (not for profiling)",
        ),
        # -- viruses ---------------------------------------------------
        _database(
            "uhgv_c100_dbv1.syldb",
            (_FAUST + "uhgv_c100_dbv1.syldb", _GCS + "uhgv_c100_dbv1.syldb"),
            size=435767920,
            md5="36aa5391c50732f515a603adad9c2e19",
            genomes=171338,
            c=100,
            description="UHGV gut vOTUs, c100",
        ),
        _database(
            "uhgv_c200_dbv1.syldb",
            (_FAUST + "uhgv_c200_dbv1.syldb", _GCS + "uhgv_c200_dbv1.syldb"),
            size=227714056,
            md5="2fa195f73b7e685ac19d44f2d56dde1b",
            genomes=171338,
            c=200,
            description="UHGV gut vOTUs, c200",
        ),
        _database(
            "imgvr_c200_v0.3.0.syldb",
            (_FAUST + "imgvr_c200_v0.3.0.syldb", _GCS + "imgvr_c200_v0.3.0.syldb"),
            size=2134450877,
            genomes=2917516,
            c=200,
            description="IMG/VR4.1 high-confidence vOTU representatives",
        ),
        # -- eukaryotes ------------------------------------------------
        _database(
            "fungi-refseq-2025-10-11-c200-dbv1.syldb",
            (
                _FAUST + "fungi-refseq-2025-10-11-c200-dbv1.syldb",
                _GCS + "fungi-refseq-2025-10-11-c200-dbv1.syldb",
            ),
            size=783659265,
            md5="954d655c282c6b71df5675f113ada5ca",
            genomes=661,
            c=200,
            description="RefSeq fungi representatives (2025-10-11)",
        ),
        _database(
            "tara-eukmags-c200-v0.3.syldb",
            (_FAUST + "tara-eukmags-c200-v0.3.syldb", _GCS + "tara-eukmags-c200-v0.3.syldb"),
            size=971458802,
            genomes=713,
            c=200,
            description="TARA Oceans eukaryotic MAGs/SAGs",
        ),
    )
}

LIST_KEYWORD = "list"


def lookup_database(name: str) -> Database:
    """Return the catalogue entry for ``name``."""
    try:
        return DATABASES[name]
    except KeyError:
        known = ", ".join(DATABASES)
        raise ValueError(
            f"unknown database {name!r}; known names: {known} "
            "(run 'sylphd download --list-databases')"
        ) from None


def database_table() -> str:
    """Render the catalogue as an aligned text table."""
    header = ("NAME", "SIZE", "GENOMES", "C", "DESCRIPTION")
    rows = [
        (
            database.name,
            format_size(database.size) if database.size else "?",
            f"{database.genomes:,}" if database.genomes else "?",
            str(database.c) if database.c else "?",
            database.description,
        )
        for database in DATABASES.values()
    ]
    widths = [max(len(row[index]) for row in (*rows, header)) for index in range(4)]
    lines = [
        "  ".join(
            [value.ljust(width) for width, value in zip(widths, row[:4])] + [row[4]]
        ).rstrip()
        for row in (header, *rows)
    ]
    lines.insert(1, "  ".join("-" * width for width in widths))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# transfer engine
# ---------------------------------------------------------------------------


def aria2c_path() -> str | None:
    """Return the ``aria2c`` executable if it is on PATH."""
    return shutil.which("aria2c")


def aria2c_command(executable: str, urls: Sequence[str], partial: Path) -> list[str]:
    """Build the aria2c invocation: all mirrors, resumable, no preallocation."""
    return [
        executable,
        "-s",
        "16",
        "-x",
        "8",
        "-k",
        "5M",
        "-c",
        "--file-allocation=none",
        "-d",
        str(partial.parent),
        "-o",
        partial.name,
        *urls,
    ]


def _partial_path(destination: Path) -> Path:
    return destination.with_name(destination.name + ".part")


def _content_range_start(value: str) -> int | None:
    # "bytes 100-999/1000"
    try:
        range_part = value.split(None, 1)[1].split("/", 1)[0]
        return int(range_part.split("-", 1)[0])
    except (IndexError, ValueError):
        return None


def _download_httpx(
    url: str,
    partial: Path,
    *,
    name: str,
    expected_size: int | None,
    progress: ProgressCallback | None,
) -> None:
    """Stream ``url`` into ``partial``, resuming from what is already there.

    A server that ignores ``Range`` sends ``200`` instead of ``206``; the
    partial file is then truncated and the transfer starts over.
    """
    timeout = httpx.Timeout(None, connect=30.0)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for attempt in (0, 1):
            offset = partial.stat().st_size if partial.exists() else 0
            if expected_size is not None and offset > expected_size:
                partial.unlink()
                offset = 0
            if expected_size is not None and offset == expected_size:
                return

            headers = {"Range": f"bytes={offset}-"} if offset else {}
            with client.stream("GET", url, headers=headers) as response:
                if response.status_code == 416:
                    # The partial file does not fit the remote file; drop it
                    # and fetch from the start once.
                    partial.unlink(missing_ok=True)
                    if attempt == 0:
                        continue
                    raise DownloadError(
                        f"{name}: server rejected the resume request"
                    ) from None
                if offset and response.status_code == 206:
                    start = _content_range_start(
                        response.headers.get("Content-Range", "")
                    )
                    if start is not None and start != offset:
                        partial.unlink(missing_ok=True)
                        if attempt == 0:
                            continue
                        raise DownloadError(
                            f"{name}: server resumed at byte {start}, not {offset}"
                        )
                    mode = "ab"
                else:
                    response.raise_for_status()
                    mode = "wb"
                    offset = 0

                header = response.headers.get("Content-Length")
                remaining = int(header) if header and header.isdigit() else None
                if expected_size is not None:
                    total: int | None = expected_size
                elif remaining is not None:
                    total = offset + remaining
                else:
                    total = None

                done = offset
                with open(partial, mode) as handle:
                    for chunk in response.iter_bytes(CHUNK_SIZE):
                        handle.write(chunk)
                        done += len(chunk)
                        if progress is not None:
                            progress(name, done, total)
                return


def _download_aria2c(
    executable: str,
    urls: Sequence[str],
    partial: Path,
) -> None:
    """Run aria2c on all mirrors at once; its progress goes to the terminal."""
    command = aria2c_command(executable, urls, partial)
    try:
        result = subprocess.run(command, check=False)
    except OSError as error:
        raise DownloadError(f"could not run aria2c: {error}") from error
    if result.returncode != 0:
        raise DownloadError(f"aria2c failed with exit status {result.returncode}")


def download_urls(
    urls: Sequence[str],
    destination: Path,
    *,
    expected_size: int | None = None,
    expected_md5: str | None = None,
    use_aria2c: bool = False,
    progress: ProgressCallback | None = None,
    verify_progress: VerifyCallback | None = None,
) -> Path:
    """Download ``urls`` into ``destination``, trying mirrors in order.

    The transfer lands in ``destination.part`` and is renamed only after the
    size (and, when ``expected_md5`` is given, the MD5) matches. With
    ``use_aria2c`` all mirrors are handed to ``aria2c`` at once.
    """
    if not urls:
        raise ValueError("no download URLs given")
    partial = _partial_path(destination)
    partial.parent.mkdir(parents=True, exist_ok=True)
    name = destination.name

    if use_aria2c:
        executable = aria2c_path()
        if executable is None:
            raise DownloadError("aria2c is not on PATH")
        try:
            _download_aria2c(executable, urls, partial)
        finally:
            partial.with_name(partial.name + ".aria2").unlink(missing_ok=True)
        size = partial.stat().st_size if partial.exists() else 0
        if expected_size is not None and size != expected_size:
            partial.unlink(missing_ok=True)
            raise DownloadError(
                f"{name}: expected {expected_size} bytes, aria2c left {size}"
            )
    else:
        errors: list[str] = []
        for url in urls:
            try:
                _download_httpx(
                    url,
                    partial,
                    name=name,
                    expected_size=expected_size,
                    progress=progress,
                )
            except (httpx.HTTPError, DownloadError, OSError) as error:
                errors.append(f"{url}: {error}")
                continue
            size = partial.stat().st_size if partial.exists() else 0
            if expected_size is not None and size != expected_size:
                partial.unlink(missing_ok=True)
                errors.append(f"{url}: expected {expected_size} bytes, got {size}")
                continue
            break
        else:
            raise DownloadError(
                f"could not download {name}:" + "".join(f"\n  {e}" for e in errors)
            )

    if expected_md5 is not None:
        assert partial.exists()
        actual = md5_file(partial, progress=verify_progress)
        if actual != expected_md5:
            partial.unlink()
            raise ValueError(
                f"checksum mismatch for {name}: expected {expected_md5}, got {actual}"
            )
    partial.replace(destination)
    return destination


def download_file(
    url: str,
    destination: Path,
    *,
    expected_md5: str | None = None,
    force: bool = False,
    progress: ProgressCallback | None = None,
) -> Path:
    """Stream ``url`` to ``destination`` and optionally verify its MD5.

    Downloads go to ``<destination>.part`` and are only renamed on success.
    """
    if destination.exists() and not force:
        if expected_md5 is None or _md5sum(destination) == expected_md5:
            if progress is not None:
                progress(destination.name, destination.stat().st_size, None)
            return destination
        raise ValueError(
            f"{destination} exists but does not match the expected checksum "
            "(pass --force to re-download it)"
        )

    return download_urls(
        [url],
        destination,
        expected_md5=expected_md5,
        progress=progress,
    )


def download_database(
    database: str | Database,
    outdir: str | os.PathLike[str] = ".",
    *,
    urls: Sequence[str] = (),
    verify: bool = False,
    force: bool = False,
    use_aria2c: bool = False,
    progress: ProgressCallback | None = None,
    verify_progress: VerifyCallback | None = None,
) -> Path:
    """Download one pre-built database into ``outdir``.

    ``urls`` replaces the catalogue mirrors (or supplies mirrors for a
    database name that is not in the catalogue). Existing files with the
    right size are kept unless ``force`` is given; ``verify`` also checks the
    MD5 when the catalogue knows it.
    """
    if isinstance(database, Database):
        entry = database
    elif database in DATABASES:
        entry = DATABASES[database]
    elif urls:
        entry = Database(
            name=database,
            filename=os.path.basename(database),
            mirrors=tuple(urls),
        )
    else:
        entry = lookup_database(database)  # raises a helpful error
    mirrors = tuple(urls) or entry.mirrors
    if not mirrors:
        raise ValueError(f"no download URLs known for {entry.name}")
    if verify and entry.md5 is None:
        print(
            f"{entry.name}: no published MD5, checking the size only",
            file=sys.stderr,
        )

    directory = Path(outdir)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / entry.filename

    if destination.exists() and not force:
        size = destination.stat().st_size
        if entry.size is not None and size != entry.size:
            # Wrong size: the file is incomplete or from another source.
            destination.unlink()
        elif not verify or entry.md5 is None:
            return destination
        elif md5_file(destination, progress=verify_progress) == entry.md5:
            return destination
        else:
            destination.unlink()

    return download_urls(
        mirrors,
        destination,
        expected_size=entry.size,
        expected_md5=entry.md5 if verify else None,
        use_aria2c=use_aria2c,
        progress=progress,
        verify_progress=verify_progress,
    )


def download_release(
    release: int | str = 232,
    outdir: str | os.PathLike[str] = ".",
    *,
    domains: Iterable[str] = tuple(METADATA_FILES),
    base_url: str | None = None,
    force: bool = False,
    progress: ProgressCallback | None = None,
) -> list[Path]:
    """Download the GTDB metadata TSV files for ``release`` into ``outdir``."""
    domains = list(domains)
    unknown = [domain for domain in domains if domain not in METADATA_FILES]
    if unknown:
        raise ValueError(f"unknown domain(s): {', '.join(unknown)}")
    if not domains:
        raise ValueError("no domains to download")

    directory = Path(outdir)
    directory.mkdir(parents=True, exist_ok=True)
    url = release_url(release, base_url)

    timeout = httpx.Timeout(60.0, connect=30.0)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        checksum_response = client.get(url + MD5SUMS_FILE)
        checksum_response.raise_for_status()
    checksums = parse_md5sums(checksum_response.text)

    paths: list[Path] = []
    for domain in domains:
        name = METADATA_FILES[domain].format(release=release)
        expected = checksums.get(name)
        if expected is None:
            raise ValueError(f"{name} is not listed in {url}{MD5SUMS_FILE}")
        paths.append(
            download_file(
                url + name,
                directory / name,
                expected_md5=expected,
                force=force,
                progress=progress,
            )
        )
    return paths


class ProgressPrinter:
    """Callable progress reporter: rewrites one line per whole percent."""

    def __init__(self, stream: object = None) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self._last: dict[str, int] = {}

    def __call__(self, name: str, done: int, total: int | None) -> None:
        if total:
            percent = int(done * 100 / total)
            if percent == self._last.get(name) and done < total:
                return
            self._last[name] = percent
            print(
                f"\r{name}: {percent:3d}% ({done / 1e6:.1f}/{total / 1e6:.1f} MB)",
                end="\n" if done >= total else "",
                file=self.stream,
                flush=True,
            )
        else:
            print(
                f"\r{name}: {done / 1e6:.1f} MB",
                end="",
                file=self.stream,
                flush=True,
            )


class VerifyPrinter:
    """Callable checksum progress reporter on stderr, one line per percent."""

    def __init__(self, stream: object = None) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self._last = -1

    def __call__(self, done: int, total: int | None) -> None:
        if total:
            percent = int(done * 100 / total)
            if percent == self._last and done < total:
                return
            self._last = percent
        else:
            percent = 0
        print(
            f"\rmd5: {percent:3d}% ({done / 1e9:.1f}/{total / 1e9:.1f} GB)"
            if total
            else f"\rmd5: {done / 1e9:.1f} GB",
            end="\n" if total and done >= total else "",
            file=self.stream,
            flush=True,
        )
