#!/usr/bin/env python3
"""Capture the single-database ``/info`` and ``/assembly`` goldens.

Manual, not part of the test suite: the checked-in goldens are what the
single-database server answered before multi-database support was added, and
``tests/test_multi_db.py`` compares against them (see ``tests/web_golden.py``
for the normalisation of paths, versions and timings).

Run from a checkout *without* the multi-database change when regenerating:

    uv run scripts/capture-web-golden.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import web_golden
from conftest import ECOLI_TWO_STAGE, K12_PAIRED_SKETCH
from starlette.testclient import TestClient

from sylphd.server import SKETCH_CONTENT_TYPE, ServerConfig, create_app


def capture_info(client: TestClient) -> dict[str, Any]:
    response = client.get("/info")
    assert response.status_code == 200, response.text
    document = web_golden.normalise(response.json())
    # ``databases`` did not exist before multi-database support: the golden is
    # the pooled ``database`` object and the rest of the document.
    document.pop("databases", None)
    return document


def capture_assembly(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/assembly",
        params={"top": 10, "min_ani": 80.0, "name": "golden"},
        content=K12_PAIRED_SKETCH.read_bytes(),
        headers={"Content-Type": SKETCH_CONTENT_TYPE},
    )
    assert response.status_code == 200, response.text
    return web_golden.normalise(response.json())


def main() -> int:
    web_golden.GOLDEN.mkdir(parents=True, exist_ok=True)
    with TestClient(create_app(ServerConfig(db_path=ECOLI_TWO_STAGE))) as client:
        documents = {
            "info": capture_info(client),
            "assembly": capture_assembly(client),
        }
    for name, document in documents.items():
        path = web_golden.GOLDEN / f"{name}.json"
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
