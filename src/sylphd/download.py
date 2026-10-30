"""Download GTDB metadata, sylph databases and sylph-tax taxonomy files.

GTDB metadata is verified against the release ``MD5SUM.txt``; databases are
checked against the catalogue in :mod:`sylphd.catalog` (size always, MD5 on request).
Taxonomy files come from the sylph-tax catalogue (Zenodo records and one
fileshare host) and are always verified against the sizes and MD5 sums that
the catalogue records.  Transfers go to ``*.part`` files, resume with HTTP
``Range`` and can use ``aria2c`` to fetch from all mirrors at once.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from .catalog import DATABASES, TAXONOMIES, ZENODO_API, Database, Taxonomy

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
        name = name.removeprefix("./")
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
# catalogue lookups (the entries are in catalog.py)
# ---------------------------------------------------------------------------

LIST_KEYWORD = "list"


def lookup_taxonomy(name: str) -> Taxonomy:
    """Return the catalogue entry for the sylph-tax taxonomy ``name``."""
    try:
        return TAXONOMIES[name]
    except KeyError:
        known = ", ".join(TAXONOMIES)
        raise ValueError(
            f"unknown taxonomy {name!r}; known names: {known} "
            "(run 'sylphd download --list-taxonomies')"
        ) from None


def paired_databases(name: str) -> list[str]:
    """Return the catalogue databases that use the taxonomy ``name``."""
    return [
        database.name
        for database in DATABASES.values()
        if database.taxonomy == name or name in database.taxonomy_alternatives
    ]


def taxonomy_table() -> str:
    """Render the taxonomy catalogue as an aligned text table."""
    header = ("NAME", "SIZE", "ROWS", "DATABASE", "DESCRIPTION")
    rows = []
    for taxonomy in TAXONOMIES.values():
        paired = paired_databases(taxonomy.name)
        if len(paired) > 2:
            paired_text = f"{paired[0]} (+{len(paired) - 1})"
        else:
            paired_text = ", ".join(paired)
        rows.append(
            (
                taxonomy.name,
                format_size(taxonomy.size) if taxonomy.size else "?",
                f"{taxonomy.rows:,}" if taxonomy.rows else "?",
                paired_text or "-",
                taxonomy.description,
            )
        )
    widths = [max(len(row[index]) for row in (*rows, header)) for index in range(4)]
    lines = [
        "  ".join(
            [value.ljust(width) for width, value in zip(widths, row[:4])] + [row[4]]
        ).rstrip()
        for row in (header, *rows)
    ]
    lines.insert(1, "  ".join("-" * width for width in widths))
    return "\n".join(lines)


#: ``NAME=PATH`` name part: the same characters as a database alias.
_SPEC_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
_COMPRESSION_SUFFIXES = (".gz", ".bz2", ".xz", ".zst", ".zstd", ".bgz", ".bgzf")


@dataclass(frozen=True)
class TaxonomySpec:
    """A parsed ``--taxonomy`` value: a catalogue name or a local file.

    ``path`` is ``None`` for a bare sylph-tax name (the file is downloaded).
    """

    name: str
    text: str
    path: Path | None = None

    @property
    def download(self) -> bool:
        return self.path is None


def taxonomy_source_name(path: Path) -> str:
    """Default source name for a local taxonomy file: its stem, without ``.gz``."""
    name = path.name
    lowered = name.lower()
    for suffix in _COMPRESSION_SUFFIXES:
        if lowered.endswith(suffix):
            name = name[: -len(suffix)]
            break
    stem = Path(name).stem
    return stem or path.name


def parse_taxonomy_spec(text: str) -> TaxonomySpec:
    """Parse one ``--taxonomy`` value: ``NAME``, ``NAME=PATH`` or ``PATH``.

    A bare value is a sylph-tax name when the catalogue knows it and an
    existing local file otherwise; a value with a path separator (or ``.``
    prefix) is always a path.  A local file must exist, so a typo fails at
    setup time rather than after the GTDB files were read.
    """
    value = text.strip()
    if not value:
        raise ValueError("empty taxonomy specification")
    name, separator, rest = value.partition("=")
    if separator and rest and _SPEC_NAME_RE.fullmatch(name):
        path = Path(rest)
        if not path.is_file():
            raise FileNotFoundError(f"{value}: no such file: {rest}")
        return TaxonomySpec(name=name, text=value, path=path)
    candidate = Path(value)
    looks_like_path = (
        "/" in value or os.sep in value or value.startswith(".") or candidate.is_file()
    )
    if looks_like_path:
        if not candidate.is_file():
            raise FileNotFoundError(f"{value}: no such file")
        return TaxonomySpec(name=taxonomy_source_name(candidate), text=value, path=candidate)
    if value in TAXONOMIES:
        return TaxonomySpec(name=value, text=value)
    known = ", ".join(TAXONOMIES)
    raise ValueError(
        f"unknown taxonomy {value!r}; give a sylph-tax name ({known}), "
        "NAME=PATH or a path to a taxonomy file"
    )


def zenodo_file_info(
    record: int, filename: str, *, timeout: float = 60.0
) -> dict[str, object]:
    """Ask the Zenodo API for the size and MD5 of one file of a record.

    Used to fill in catalogue entries whose size/MD5 were not recorded.  The
    API returns ``{"files": [{"key": ..., "size": ..., "checksum": "md5:..."}]}``;
    anything else (no ``files`` list, an entry without a usable size or MD5) is
    a ``DownloadError`` rather than a silently unverified download.
    """
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(ZENODO_API.format(record=record))
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise DownloadError(
            f"Zenodo API response for record {record} is not a JSON object"
        )
    files = payload.get("files")
    if not isinstance(files, list):
        raise DownloadError(
            f"Zenodo API response for record {record} has no 'files' list"
        )
    for entry in files:
        if not isinstance(entry, dict):
            raise DownloadError(
                f"Zenodo API response for record {record} has a malformed file entry"
            )
        if entry.get("key") != filename:
            continue
        size = entry.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise DownloadError(
                f"Zenodo record {record} reports no usable size for {filename}"
            )
        checksum = entry.get("checksum")
        if not isinstance(checksum, str) or not checksum.startswith("md5:"):
            raise DownloadError(f"Zenodo record {record} reports no MD5 for {filename}")
        md5 = checksum.removeprefix("md5:")
        if len(md5) != 32 or not all(c in "0123456789abcdef" for c in md5):
            raise DownloadError(
                f"Zenodo record {record} reports a malformed MD5 for {filename}"
            )
        return {"size": size, "md5": md5}
    raise DownloadError(
        f"Zenodo record {record} does not publish {filename} "
        f"(files: {', '.join(str(entry.get('key')) for entry in files)})"
    )


def _taxonomy_expectation(entry: Taxonomy) -> tuple[int | None, str | None]:
    """Return the ``(size, md5)`` to verify a taxonomy download with.

    Zenodo entries are hydrated from the catalogue or, where it has gaps, from
    the Zenodo API.  An MD5 is mandatory: it is what makes a taxonomy file the
    published one, so an entry without one fails instead of being accepted on
    its size alone.  ``size_only`` is reserved for GlobDB r232, whose publisher
    publishes no checksum.
    """
    size, md5 = entry.size, entry.md5
    if entry.zenodo is not None and (size is None or md5 is None):
        try:
            info = zenodo_file_info(entry.zenodo, entry.filename)
        except (httpx.HTTPError, DownloadError) as error:
            print(
                f"{entry.name}: could not read the Zenodo API ({error}); "
                "using what the catalogue records",
                file=sys.stderr,
            )
        else:
            api_size, api_md5 = info.get("size"), info.get("md5")
            if size is None and isinstance(api_size, int):
                size = api_size
            if md5 is None and isinstance(api_md5, str):
                md5 = api_md5
    if md5 is not None:
        return (int(size) if size is not None else None), md5
    if entry.size_only:
        if size is None:
            raise DownloadError(
                f"{entry.name}: neither a size nor an MD5 is known for "
                f"{entry.filename}; refusing to download a file that cannot be checked"
            )
        return int(size), None
    raise DownloadError(
        f"{entry.name}: no MD5 is known for {entry.filename} (the catalogue has "
        "none and the Zenodo API did not provide one); refusing to accept a file "
        "that cannot be verified"
    )


def download_taxonomy(
    taxonomy: str | Taxonomy,
    outdir: str | os.PathLike[str] = ".",
    *,
    force: bool = False,
    progress: ProgressCallback | None = None,
    verify_progress: VerifyCallback | None = None,
) -> Path:
    """Download one sylph-tax taxonomy file into ``outdir``.

    The size and the MD5 of the published file are always checked (see
    ``_taxonomy_expectation``); an existing file is reused only when it
    verifies, and a file that cannot be verified is never accepted.  Downloads
    are at most 27 MB, so verification is not opt-in as it is for databases.
    """
    entry = taxonomy if isinstance(taxonomy, Taxonomy) else lookup_taxonomy(taxonomy)
    size, md5 = _taxonomy_expectation(entry)

    directory = Path(outdir)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / entry.filename

    if destination.exists() and not force:
        same_size = size is None or destination.stat().st_size == size
        if same_size and (
            md5 is None or md5_file(destination, progress=verify_progress) == md5
        ):
            return destination
        destination.unlink()

    return download_urls(
        entry.mirrors,
        destination,
        expected_size=size,
        expected_md5=md5,
        progress=progress,
        verify_progress=verify_progress,
    )


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

    A resume that does not get a ``206`` with a ``Content-Range`` starting at
    the offset (a server that ignores ``Range`` sends ``200``) starts over
    from zero.
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
                    if start != offset:
                        partial.unlink(missing_ok=True)
                        if attempt == 0:
                            continue
                        raise DownloadError(
                            f"{name}: server did not resume at byte {offset}"
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


def _checksum_mismatch(
    partial: Path, expected_md5: str | None, progress: VerifyCallback | None
) -> str | None:
    """Return an error message (and drop ``partial``) if its MD5 is wrong."""
    if expected_md5 is None:
        return None
    actual = md5_file(partial, progress=progress)
    if actual == expected_md5:
        return None
    partial.unlink()
    return f"checksum mismatch: expected {expected_md5}, got {actual}"


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
    control = partial.with_name(partial.name + ".aria2")
    partial.parent.mkdir(parents=True, exist_ok=True)
    name = destination.name

    if use_aria2c:
        executable = aria2c_path()
        if executable is None:
            raise DownloadError("aria2c is not on PATH")
        # aria2c deletes its control file itself on success; on failure or
        # interrupt it must stay, because the partial file may have holes.
        _download_aria2c(executable, urls, partial)
        size = partial.stat().st_size if partial.exists() else 0
        if expected_size is not None and size != expected_size:
            partial.unlink(missing_ok=True)
            control.unlink(missing_ok=True)
            raise DownloadError(
                f"{name}: expected {expected_size} bytes, aria2c left {size}"
            )
        mismatch = _checksum_mismatch(partial, expected_md5, verify_progress)
        if mismatch is not None:
            raise DownloadError(f"{name}: {mismatch}")
    else:
        if control.exists():
            # An unfinished aria2c transfer can have holes: never resume it by length.
            partial.unlink(missing_ok=True)
            control.unlink()
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
            mismatch = _checksum_mismatch(partial, expected_md5, verify_progress)
            if mismatch is not None:
                errors.append(f"{url}: {mismatch}")
                continue
            break
        else:
            raise DownloadError(
                f"could not download {name}:" + "".join(f"\n  {e}" for e in errors)
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
        elif not verify or entry.md5 is None or md5_file(destination, progress=verify_progress) == entry.md5:
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
