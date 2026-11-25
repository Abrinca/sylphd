"""The agent skill may only name options that the CLI really has.

The query commands are the native ``sylphd-client`` now, so the options are read from
its ``--help``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from conftest import require_client_binary

SKILL = Path(__file__).resolve().parent.parent / "skills" / "sylphd" / "SKILL.md"
COMMANDS = ("info", "assembly", "profile")

CLIENT = require_client_binary()


def _options(command: str) -> set[str]:
    """The long options of one command, from the binary's own ``--help``."""
    done = subprocess.run(
        [str(CLIENT), command, "--help"], capture_output=True, text=True, check=True
    )
    return set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", done.stdout))


def test_front_matter() -> None:
    text = SKILL.read_text(encoding="utf-8")
    head = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert head is not None
    assert re.search(r"^name: sylphd$", head.group(1), re.MULTILINE)
    assert re.search(r"^description: \S", head.group(1), re.MULTILINE)
    assert len(text.splitlines()) <= 120


def test_every_long_option_exists() -> None:
    known = set().union(*(_options(name) for name in COMMANDS))
    named = set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", SKILL.read_text(encoding="utf-8")))
    assert named, "the skill names no options"
    named -= {"--from", "--force"}  # options of uv, not of sylphd
    named -= {"--version"}  # a top-level option, not one of a command
    assert not named - known, f"not in the CLI: {sorted(named - known)}"


def test_documented_options_belong_to_their_command() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for command, flags in {
        "assembly": ("--per-contig", "--top", "--min-ani", "--min-contig-length", "--json"),
        "profile": ("--format", "--taxonomy", "--minimum-ani", "--estimate-unknown"),
        "info": ("--json",),
    }.items():
        options = _options(command)
        for flag in flags:
            assert flag in text
            assert flag in options, (command, flag)
