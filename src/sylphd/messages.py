"""Operator messages: ``sylphd serve --messages FILE``.

``FILE`` is UTF-8 JSON, a list of objects with exactly the keys ``level``
(``info``, ``warning`` or ``danger``), ``title`` and ``message`` (Markdown, which
the web page renders like the ``--about`` text and the command line prints as
plain text).  There are no ids or dates: a client identifies a message by its
content.  As for ``--about``, the file is checked at startup (a bad file is an
error) and re-read when its modification time or size changes; a later bad
version is logged and the last good list stays.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

#: Largest ``--messages`` file, in bytes.
MAX_MESSAGES_BYTES = 64 * 1024
#: Most messages in the file.
MAX_MESSAGES = 20
#: Longest title and message, in characters.
MAX_TITLE_CHARS = 120
MAX_MESSAGE_CHARS = 2000

LEVELS = ("info", "warning", "danger")
_KEYS = frozenset({"level", "title", "message"})

log = logging.getLogger("uvicorn.error")


class MessagesError(ValueError):
    """The ``--messages`` file is unusable."""


def parse_messages(text: str, where: str = "messages file") -> list[dict[str, str]]:
    """Validate the JSON text of a messages file; raises :class:`MessagesError`."""
    try:
        data = json.loads(text)
    except ValueError as error:
        raise MessagesError(f"{where} is not valid JSON: {error}") from error
    if not isinstance(data, list):
        raise MessagesError(f"{where} must be a JSON list of message objects")
    if len(data) > MAX_MESSAGES:
        raise MessagesError(f"{where} has {len(data)} messages (maximum {MAX_MESSAGES})")
    messages: list[dict[str, str]] = []
    for index, item in enumerate(data, 1):
        label = f"{where}, message {index}"
        if not isinstance(item, dict):
            raise MessagesError(f"{label} must be an object")
        extra = sorted(set(item) - _KEYS)
        missing = sorted(_KEYS - set(item))
        if missing:
            raise MessagesError(f"{label} lacks {', '.join(missing)}")
        if extra:
            raise MessagesError(f"{label} has unknown keys: {', '.join(extra)}")
        level = item["level"]
        if level not in LEVELS:
            raise MessagesError(
                f"{label}: level must be one of {', '.join(LEVELS)} (got {level!r})"
            )
        for key, limit in (("title", MAX_TITLE_CHARS), ("message", MAX_MESSAGE_CHARS)):
            value = item[key]
            if not isinstance(value, str):
                raise MessagesError(f"{label}: {key} must be a string")
            if not value.strip():
                raise MessagesError(f"{label}: {key} must not be empty")
            if len(value) > limit:
                raise MessagesError(
                    f"{label}: {key} is {len(value)} characters long (maximum {limit})"
                )
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in item["title"]):
            raise MessagesError(f"{label}: title must be a single line")
        messages.append(
            {
                "level": level,
                "title": item["title"].strip(),
                "message": item["message"].strip(),
            }
        )
    return messages


def read_messages(path: Path) -> list[dict[str, str]]:
    """Read and validate a messages file; raises :class:`MessagesError`."""
    try:
        size = path.stat().st_size
        if size > MAX_MESSAGES_BYTES:
            raise MessagesError(
                f"messages file {path} is {size} bytes (maximum {MAX_MESSAGES_BYTES})"
            )
        data = path.read_bytes()
    except OSError as error:
        raise MessagesError(
            f"cannot read messages file {path}: {error.strerror or error}"
        ) from error
    if len(data) > MAX_MESSAGES_BYTES:
        raise MessagesError(
            f"messages file {path} is {len(data)} bytes (maximum {MAX_MESSAGES_BYTES})"
        )
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise MessagesError(f"messages file {path} is not valid UTF-8") from error
    return parse_messages(text, f"messages file {path}")


class Messages:
    """The messages of one server, reloaded when the file changes."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._items: list[dict[str, str]] = []
        self._stamp: tuple[int, int] | None = None
        self._failed_stamp: tuple[int, int] | None = None
        if path is not None:
            self._items = read_messages(path)  # startup: errors propagate
            self._stamp = self._stat()

    def _stat(self) -> tuple[int, int] | None:
        try:
            info = os.stat(self.path)
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    @property
    def items(self) -> list[dict[str, Any]]:
        """The current messages (an empty list when unset)."""
        if self.path is None:
            return []
        stamp = self._stat()
        if stamp != self._stamp and stamp != self._failed_stamp:
            try:
                self._items = read_messages(self.path)
                self._stamp = stamp
                self._failed_stamp = None
            except MessagesError as error:
                self._failed_stamp = stamp
                log.warning("keeping the previous --messages list: %s", error)
        return [dict(item) for item in self._items]
