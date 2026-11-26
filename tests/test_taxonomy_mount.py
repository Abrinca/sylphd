"""omnitax's API under ``/taxonomy`` (``--taxonomy-db``): the mount, ``/info``, the token and the rate limit.

The live test is the point: an omnitax-only client (``omnitax.connect(url=".../taxonomy")``) works against a
running sylphd, with the privileged token sent as a header.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import omnitax
import pytest
from conftest import ECOLI_TWO_STAGE
from omnitax.client import ClientError
from omnitax.store import API_VERSION
from starlette.testclient import TestClient
from taxfile import (
    FUNGI_ASSEMBLIES,
    FUNGI_MERGED,
    FUNGI_NODES,
    SACCHAROMYCES,
    write_taxonomy_file,
)

from sylphd.limits import load_token_digests
from sylphd.server import ServerConfig, create_app

TOKEN = "taxonomy-token-" + "t" * 24


@pytest.fixture(scope="module")
def taxonomy_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_taxonomy_file(
        tmp_path_factory.mktemp("taxonomies") / "taxonomies.sqlite", FUNGI_NODES,
        assemblies=FUNGI_ASSEMBLIES, merged=FUNGI_MERGED,
        gtdb=[("RS_GCF_000002515.1", "d__Eukaryota;p__Ascomycota;s__Saccharomyces cerevisiae")], links=[("RS_GCF_000002515.1", 4932)],
    )


@pytest.fixture(scope="module")
def tokens(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("tokens") / "tokens"
    path.write_text(TOKEN + "\n")
    return path


def _client(taxonomy_db: Path, tokens: Path | None = None, **config) -> TestClient:
    digests = load_token_digests(tokens) if tokens else ()
    app = create_app(ServerConfig(db_path=ECOLI_TWO_STAGE, taxonomy_db=taxonomy_db, privileged_tokens=digests, **config))
    return TestClient(app)


def test_omnitax_is_mounted_under_taxonomy(taxonomy_db: Path) -> None:
    with _client(taxonomy_db) as client:
        listing = client.get("/taxonomy/taxonomies").json()
        assert [e["label"] for e in listing["taxonomies"]] == ["gtdb:232", "ncbi:2026-01-01"]
        nodes = client.post("/taxonomy/ncbi/nodes", json={"ids": ["4932", "77777"]}).json()["nodes"]
        assert nodes["4932"][3] == "Saccharomyces cerevisiae" and nodes["77777"][0] == "4932"  # a merged id answers with the current node
        assert client.get("/taxonomy/ncbi/meta").status_code == 200
        # nothing of the old surface, and sylphd's own errors are JSON too
        assert client.get("/nodes").status_code == 404
        missing = client.post("/taxonomy/nope/nodes", json={"ids": ["1"]})
        assert missing.status_code == 404 and "no taxonomy" in missing.json()["error"]


def test_info_names_what_the_catalog_lists(taxonomy_db: Path) -> None:
    with _client(taxonomy_db, taxonomy_rate_limit=(30, 60.0)) as client:
        info = client.get("/info").json()
    taxonomy = info["taxonomy"]
    assert taxonomy["enabled"] is True and taxonomy["mount"] == "/taxonomy"
    assert taxonomy["api_version"] == API_VERSION
    assert taxonomy["rate_limit"] == {"count": 30, "window_seconds": 60.0}
    ncbi = next(e for e in taxonomy["taxonomies"] if e["kind"] == "ncbi")
    assert ncbi["version"] == "2026-01-01" and ncbi["nodes"] == len(FUNGI_NODES) and ncbi["genomes"] == len(FUNGI_ASSEMBLIES) + 1  # and the GTDB link
    assert set(ncbi) == {"label", "kind", "version", "nodes", "genomes", "built", "fuzzy"}
    assert "/taxonomy/..." in info["endpoints"]


def test_info_without_a_taxonomy_file() -> None:
    with TestClient(create_app(ServerConfig(db_path=ECOLI_TWO_STAGE))) as client:
        info = client.get("/info").json()
        assert info["taxonomy"] == {"enabled": False}
        assert client.get("/taxonomy/taxonomies").status_code == 404


def test_the_taxonomy_needs_the_token_when_the_server_does(taxonomy_db: Path, tokens: Path) -> None:
    with _client(taxonomy_db, tokens, require_token=True) as client:
        refused = client.get("/taxonomy/taxonomies")
        assert refused.status_code == 401 and "requires an access token" in refused.json()["error"]
        ok = client.get("/taxonomy/taxonomies", headers={"Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200


def test_the_taxonomy_rate_limit_covers_every_endpoint_and_skips_privileged_tokens(taxonomy_db: Path, tokens: Path) -> None:
    with _client(taxonomy_db, tokens, taxonomy_rate_limit=(3, 60.0)) as client:
        for _ in range(3):
            assert client.get("/taxonomy/taxonomies").status_code == 200
        limited = client.post("/taxonomy/ncbi/nodes", json={"ids": ["4932"]})  # POST and GET share the count
        assert limited.status_code == 429 and "Retry-After" in limited.headers
        for _ in range(6):
            assert client.get("/taxonomy/taxonomies", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
        assert client.get("/health").status_code == 200  # only /taxonomy is limited


# -- a live server, an omnitax client ------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def live(taxonomy_db: Path, tokens: Path):
    port = _free_port()
    process = subprocess.Popen(
        [
            sys.executable, "-m", "sylphd", "serve", "--db", str(ECOLI_TWO_STAGE), "--tcp", f"127.0.0.1:{port}",
            "--taxonomy-db", str(taxonomy_db), "--taxonomy-rate-limit", "8/min", "--privileged-token-file", str(tokens),
            "--log-level", "warning",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"{base}/health", timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("the server did not start")
        yield f"{base}/taxonomy"
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_an_omnitax_only_client_works_against_sylphd(live: str, tokens: Path) -> None:
    headers = {"Authorization": f"Bearer {TOKEN}"}  # the privileged token lifts the limit
    with omnitax.connect(url=live, taxonomy="ncbi", headers=headers) as ncbi:
        taxon = ncbi["4932"]
        assert (taxon.name, taxon.rank) == ("Saccharomyces cerevisiae", "species")
        assert taxon.lineage_string() == SACCHAROMYCES
        assert [t.name for t in ncbi["559292"].lineage(ranks=omnitax.STANDARD_RANKS) if t][-2:] == ["Saccharomyces", "Saccharomyces cerevisiae"]
        assert ncbi.get("77777").id == "4932"  # merged
        assert ncbi.consensus(["4932", "5141"]).name == "Ascomycota"
        assert [t.id for t in ncbi.search("neurospora")] == ["5141"]
        assert ncbi.genome("GCF_000002515").taxon.id == "4932"
        with omnitax.connect(url=live, taxonomy="gtdb:232", headers=headers) as gtdb:
            assert gtdb.translate("s__Saccharomyces cerevisiae", ncbi).id == "4932"  # through the genomes
        for _ in range(20):  # far over the 8 a minute an anonymous client gets
            assert ncbi.get("4932") is not None


def test_the_same_client_without_the_token_is_limited(live: str) -> None:
    with omnitax.connect(url=live, taxonomy="ncbi") as ncbi, pytest.raises(ClientError) as limited:  # 8 a minute
        for _ in range(20):
            ncbi.get("4932")
    assert limited.value.status_code == 429
