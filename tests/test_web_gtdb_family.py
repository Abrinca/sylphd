"""web/app.js treats GlobDB hits as GTDB taxonomy (same rule as assembly.is_gtdb_taxonomy_source)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_globdb_hits_are_gtdb_taxonomy_in_the_web_app() -> None:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    result = subprocess.run(
        ["node", str(ROOT / "tests" / "gtdb_family_check.mjs"), str(ROOT / "web" / "app.js")],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
