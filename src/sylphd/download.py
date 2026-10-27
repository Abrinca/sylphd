"""Download GTDB metadata files, verified against the release ``MD5SUM.txt``."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Callable, Iterable

import httpx

#: GTDB release root, e.g. ``.../releases/release232/232.0/``.
DEFAULT_BASE_URL = "https://data.gtdb.ecogenomic.org/releases/release{release}/{release}.0/"

#: Metadata files we care about, per domain.
METADATA_FILES: dict[str, str] = {
    "bac120": "bac120_metadata_r{release}.tsv.gz",
    "ar53": "ar53_metadata_r{release}.tsv.gz",
}

MD5SUMS_FILE = "MD5SUM.txt"

ProgressCallback = Callable[[str, int, int | None], None]


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


def _md5sum(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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

    partial = destination.with_name(destination.name + ".part")
    digest = hashlib.md5()
    downloaded = 0
    timeout = httpx.Timeout(None, connect=30.0)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            total_header = response.headers.get("Content-Length")
            total = int(total_header) if total_header and total_header.isdigit() else None
            with open(partial, "wb") as handle:
                for chunk in response.iter_bytes(1024 * 1024):
                    handle.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
                    if progress is not None:
                        progress(destination.name, downloaded, total)
    actual = digest.hexdigest()
    if expected_md5 is not None and actual != expected_md5:
        partial.unlink(missing_ok=True)
        raise ValueError(
            f"checksum mismatch for {destination.name}: "
            f"expected {expected_md5}, got {actual}"
        )
    partial.replace(destination)
    return destination


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
