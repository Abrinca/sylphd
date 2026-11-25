"""The wasm ``.sylsp`` header exports (what web/sketching.js calls) read the same header as sylphd.sketchcheck."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from sylphd.sketchcheck import parse_sketch_header

ROOT = Path(__file__).resolve().parents[1]
SKETCHES = sorted((ROOT / "tests" / "data").glob("*.sylsp"))


@pytest.fixture(autouse=True)
def _require_node() -> None:
    if shutil.which("node") is None:
        (pytest.fail if os.environ.get("CI") else pytest.skip)("node is not installed")


CORE_CHECK = ROOT / "rust" / "sylphd-wasm" / "tests" / "core_check.mjs"


def test_header_matches_python() -> None:
    assert SKETCHES
    done = subprocess.run(
        ["node", str(CORE_CHECK)],
        input=json.dumps({"sylsp": [str(path) for path in SKETCHES]}),
        capture_output=True, text=True, check=True,
    )
    for path, js in zip(SKETCHES, json.loads(done.stdout)["sylsp"], strict=True):
        py = parse_sketch_header(path.read_bytes())
        assert js == {
            "kmers": py.kmer_count, "c": py.c, "k": py.k, "fileName": py.file_name,
            "sampleName": py.sample_name, "paired": py.paired,
        }, path.name


def test_bad_sketches_and_compatibility_reasons() -> None:
    done = subprocess.run(
        ["node", str(CORE_CHECK)],
        input=json.dumps({"compat": [[200, 31, 200, 31], [100, 31, 200, 31], [400, 31, 200, 31], [200, 21, 200, 31]]}),
        capture_output=True, text=True, check=True,
    )
    ok, finer, sparser, other_k = json.loads(done.stdout)["compat"]
    assert ok is None and finer is None
    assert "sparser" in sparser and "c=400" in sparser
    assert "k=21" in other_k and "k=31" in other_k
