"""Locate and run the native ``sylphd-client`` binary (shipped by the ``sylphd-bin`` wheel).

Standard library only, so it works in the base install.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import sysconfig
from typing import Any

#: Name of the binary (without ``.exe``).
BIN_NAME = "sylphd-client"

#: Environment variable that overrides the search (tests, unusual installs).
ENV_BIN = "SYLPHD_CLIENT_BIN"

#: Commands of the binary whose result is a JSON document (``--json``).
JSON_COMMANDS = frozenset({"info", "profile", "query", "assembly"})

#: Shown when the binary is missing.
INSTALL_HINT = "install it with: pip install 'sylphd[client]'  (or: uv tool install 'sylphd[client]')"


def find_client_bin() -> str | None:
    """Path of ``sylphd-client``, or ``None`` when it is not installed.

    Raises :class:`FileNotFoundError` naming the path when ``$SYLPHD_CLIENT_BIN`` is set
    but is not a file (an override that cannot work must not fall back silently).

    Search order (like ruff's ``find_ruff_bin``): ``$SYLPHD_CLIENT_BIN``, the
    ``scripts`` directory of this environment (where the wheel puts the binary),
    the user scheme's ``scripts`` directory, then ``PATH``.
    """
    override = os.environ.get(ENV_BIN)
    if override:
        if not os.path.isfile(override):
            raise FileNotFoundError(f"${ENV_BIN} is set to {override}, which is not a file")
        return override
    exe = BIN_NAME + (sysconfig.get_config_var("EXE") or "")
    directories = [sysconfig.get_path("scripts")]
    try:
        user_scheme = sysconfig.get_preferred_scheme("user")
        directories.append(sysconfig.get_path("scripts", scheme=user_scheme))
    except (KeyError, ValueError):  # pragma: no cover - exotic sysconfig
        pass
    for directory in directories:
        candidate = os.path.join(directory, exe)
        if os.path.isfile(candidate):
            return candidate
    return shutil.which(BIN_NAME)


def exec_client(binary: str, args: list[str]) -> int:
    """Hand the process over to the binary: ``execv`` on Unix, a child on Windows.

    ``execv`` keeps stdin, stdout, signals and the exit code exactly as they are. It
    only returns (as an exit code) on Windows, where there is no ``execv`` worth using.
    """
    # argv[0] is the name the user typed, so the binary's usage line reads `sylphd profile`.
    argv = ["sylphd", *args]
    if os.name == "nt":
        return subprocess.call(argv, executable=binary)
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(binary, argv)
    raise AssertionError("os.execv returned")  # pragma: no cover


class ClientRunError(RuntimeError):
    """``sylphd-client`` exited with a non-zero status (:func:`run`).

    ``returncode`` is the exit code (2 usage/input error, 1 server or client error);
    ``stderr`` is the binary's complete stderr, which is also the exception message.
    """

    def __init__(self, returncode: int, stderr: str) -> None:
        super().__init__(stderr.strip() or f"sylphd-client exited with status {returncode}")
        self.returncode = returncode
        self.stderr = stderr


def run(*args: str | os.PathLike[str], timeout: float | None = None) -> dict[str, Any]:
    """Run ``sylphd-client`` with ``--json`` and return the parsed result document.

    ``args`` are the arguments of the command line, e.g.
    ``run("profile", "reads.fq.gz", "--socket", "/run/sylphd.sock")`` (``info``,
    ``profile``, ``query`` and ``assembly``; for ``assembly --per-contig`` the document
    is the run document). The result is the server's JSON document, unchanged: for
    ``profile`` and ``query`` ``doc["table"]`` is ``{"columns": [...], "data": [...]}``,
    so ``pandas.DataFrame(**doc["table"])`` loads it.

    Raises :class:`ClientRunError` (with the binary's stderr) on a non-zero exit,
    ``FileNotFoundError`` when the binary is not installed, and ``ValueError`` for
    arguments that cannot work here (``render``, ``-o``). On success the binary's stderr
    (warnings, notes) is passed on to ``sys.stderr``.
    """
    words = [os.fspath(arg) for arg in args]
    if not words or words[0] not in JSON_COMMANDS:
        raise ValueError(
            f"sylphd.run takes one of {', '.join(sorted(JSON_COMMANDS))} as its first argument"
        )
    if any(word in ("-o", "--output") or word.startswith(("--output=", "-o")) for word in words):
        raise ValueError("sylphd.run reads the document from stdout: do not pass -o/--output")
    binary = find_client_bin()
    if binary is None:
        raise FileNotFoundError(f"{BIN_NAME} is not installed; {INSTALL_HINT}")
    command = [binary, words[0]]
    if "--json" not in words:
        command.append("--json")
    command.extend(words[1:])
    done = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        raise ClientRunError(done.returncode, done.stderr)
    if done.stderr:
        sys.stderr.write(done.stderr)
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError as error:
        raise ClientRunError(
            done.returncode, f"sylphd-client printed no JSON document: {error}"
        ) from error
