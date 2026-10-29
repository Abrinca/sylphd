"""The operator's description of a server: ``--name`` and ``--about FILE``.

``--about`` points at a UTF-8 Markdown file (at most :data:`MAX_ABOUT_BYTES`).
It is shown by the web page, which renders a small safe subset of Markdown and
never HTML.  The file is checked at startup (a bad file is an error) and
re-read whenever its modification time or size changes, so it can be edited
while the server runs; if a later read fails, the last good text stays.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

#: Largest ``--about`` file, in bytes.
MAX_ABOUT_BYTES = 16 * 1024

#: Longest ``--name``, in characters.
MAX_NAME_CHARS = 80

log = logging.getLogger("uvicorn.error")


class AboutError(ValueError):
    """The ``--about`` file or the ``--name`` text is unusable."""


def check_name(name: str | None) -> str | None:
    """Validate ``--name``: one line of at most :data:`MAX_NAME_CHARS` characters."""
    if name is None:
        return None
    text = name.strip()
    if not text:
        return None
    if len(text) > MAX_NAME_CHARS:
        raise AboutError(f"--name is {len(text)} characters long (maximum {MAX_NAME_CHARS})")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise AboutError("--name must be a single line without control characters")
    return text


def read_about(path: Path) -> str:
    """Read and validate an about file; raises :class:`AboutError`."""
    try:
        size = path.stat().st_size
        if size > MAX_ABOUT_BYTES:
            raise AboutError(
                f"about file {path} is {size} bytes (maximum {MAX_ABOUT_BYTES})"
            )
        data = path.read_bytes()
    except OSError as error:
        raise AboutError(f"cannot read about file {path}: {error.strerror or error}") from error
    if len(data) > MAX_ABOUT_BYTES:
        raise AboutError(f"about file {path} is {len(data)} bytes (maximum {MAX_ABOUT_BYTES})")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise AboutError(f"about file {path} is not valid UTF-8") from error
    return text.strip()


class About:
    """The about text of one server, reloaded when the file changes."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._text: str | None = None
        self._stamp: tuple[int, int] | None = None
        self._failed_stamp: tuple[int, int] | None = None
        if path is not None:
            self._text = read_about(path)  # startup: errors propagate
            self._stamp = self._stat()

    def _stat(self) -> tuple[int, int] | None:
        try:
            info = os.stat(self.path)
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    @property
    def text(self) -> str | None:
        """The current text, or ``None`` when unset or empty."""
        if self.path is None:
            return None
        stamp = self._stat()
        if stamp != self._stamp and stamp != self._failed_stamp:
            try:
                self._text = read_about(self.path)
                self._stamp = stamp
                self._failed_stamp = None
            except AboutError as error:
                self._failed_stamp = stamp
                log.warning("keeping the previous --about text: %s", error)
        return self._text or None
