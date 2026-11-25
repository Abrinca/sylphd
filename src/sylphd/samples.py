"""Sample names, as the server accepts them.

The server is authoritative: ``server_upload`` validates the names of uploaded sketches
with :func:`check_sample_name` and derives missing ones with :func:`sample_name_from_path`.
The native client (``sylphd-core``) generates names under the same rules.
"""

from __future__ import annotations

import os
import re
import unicodedata

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
