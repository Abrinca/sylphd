"""web/sketching.js readSylspHeader must read the same header as sylphd.sketchcheck."""

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


NODE = r"""
globalThis.window = globalThis;
require(process.argv[1]);
const { readFileSync } = require('fs');
(async () => {
  const out = [];
  for (const file of process.argv.slice(2)) {
    out.push(await window.SylphdSketch.readSylspHeader(new File([readFileSync(file)], 'x.sylsp')));
  }
  console.log(JSON.stringify(out));
})();
"""


def test_header_matches_python() -> None:
    assert SKETCHES
    done = subprocess.run(
        ["node", "-e", NODE, str(ROOT / "web" / "sketching.js"), *map(str, SKETCHES)],
        capture_output=True, text=True, check=True,
    )
    for path, js in zip(SKETCHES, json.loads(done.stdout), strict=True):
        py = parse_sketch_header(path.read_bytes())
        assert js == {
            "kmers": py.kmer_count, "c": py.c, "k": py.k, "fileName": py.file_name,
            "sampleName": py.sample_name, "paired": py.paired,
        }, path.name
