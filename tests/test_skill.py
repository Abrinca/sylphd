"""The agent skill may only name options that the CLI really has."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from sylphd import cli

SKILL = Path(__file__).resolve().parent.parent / "skills" / "sylphd" / "SKILL.md"
COMMANDS = ("info", "assembly", "profile")


def _options(parser: argparse.ArgumentParser) -> set[str]:
    return {s for action in parser._actions for s in action.option_strings}


def _subparsers() -> dict[str, argparse.ArgumentParser]:
    parser = cli.build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("no subcommands")


def test_front_matter() -> None:
    text = SKILL.read_text(encoding="utf-8")
    head = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert head is not None
    assert re.search(r"^name: sylphd$", head.group(1), re.MULTILINE)
    assert re.search(r"^description: \S", head.group(1), re.MULTILINE)
    assert len(text.splitlines()) <= 120


def test_every_long_option_exists() -> None:
    subparsers = _subparsers()
    known = set().union(*(_options(subparsers[name]) for name in COMMANDS))
    named = set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", SKILL.read_text(encoding="utf-8")))
    assert named, "the skill names no options"
    named -= {"--from", "--force"}  # options of uv, not of sylphd
    named -= {"--version"}  # a top-level option, not one of a command
    assert not named - known, f"not in the CLI: {sorted(named - known)}"


def test_documented_options_belong_to_their_command() -> None:
    subparsers = _subparsers()
    text = SKILL.read_text(encoding="utf-8")
    for command, flags in {
        "assembly": ("--per-contig", "--top", "--min-ani", "--min-contig-length", "--json"),
        "profile": ("--format", "--taxonomy", "--minimum-ani", "--estimate-unknown"),
        "info": ("--json",),
    }.items():
        for flag in flags:
            assert flag in text
            assert flag in _options(subparsers[command]), (command, flag)
