"""The NCBI taxonomy file, its objects and its endpoints, on a small synthetic taxdump."""

from __future__ import annotations

import io
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

import pytest
from conftest import ECOLI_TWO_STAGE
from starlette.testclient import TestClient

from sylphd.client import SylphClient
from sylphd.ncbitax import Taxonomy, TaxonNotFound
from sylphd.server import ServerConfig, create_app
from sylphd.taxdb import TaxDb, TaxDbError, build_taxdb


def _dmp(*rows: tuple) -> bytes:
    return "".join("\t|\t".join(str(c) for c in row) + "\t|\n" for row in rows).encode()


def _node(taxid, parent, rank, *, div=0, gc=11, mgc=0, hidden=0, pgc="", comments=""):
    return (taxid, parent, rank, "", div, 1, gc, 1, mgc, 1, hidden, 0, comments, pgc, "", 0, 0, 1)


NODES = _dmp(
    _node(1, 1, "no rank", div=8, gc=1),
    _node(2, 1, "domain"),
    _node(561, 2, "genus"),
    _node(562, 561, "species", hidden=1),
    _node(550, 561, "species"),
    _node(2759, 1, "domain", div=1, gc=1, mgc=2),
    _node(9605, 2759, "genus", div=5, gc=1, mgc=2),
    _node(9606, 9605, "species", div=5, gc=1, mgc=2),
    _node(4932, 2759, "species", div=2, gc=1, mgc=3, pgc=11),
)
NAMES = _dmp(
    (1, "root", "", "scientific name"),
    (2, "Bacteria", "Bacteria <bacteria>", "scientific name"),
    (2, "eubacteria", "", "genbank common name"),
    (561, "Escherichia", "", "scientific name"),
    (562, "Escherichia coli", "", "scientific name"),
    (562, "E. coli", "", "common name"),
    (562, "Bacterium coli", "", "synonym"),
    (562, '"Bacillus coli" Migula 1895', "", "authority"),
    (550, "Escherichia albertii", "", "scientific name"),
    (2759, "Eukaryota", "", "scientific name"),
    (9605, "Homo", "Homo <genus>", "scientific name"),
    (9606, "Homo sapiens", "", "scientific name"),
    (9606, "human", "", "genbank common name"),
    (4932, "Saccharomyces cerevisiae", "", "scientific name"),
)
FILES = {
    "nodes.dmp": NODES,
    "names.dmp": NAMES,
    "merged.dmp": _dmp((12, 562)),
    "delnodes.dmp": _dmp((777,)),
    "division.dmp": _dmp((0, "BCT", "Bacteria", " "), (1, "INV", "Invertebrates", " "),
                          (2, "MAM", "Mammals", " "), (5, "PRI", "Primates", " "),
                          (8, "UNA", "Unassigned", " ")),
    "gencode.dmp": _dmp((1, "", "Standard", "F" * 64, "-" * 64),
                        (2, "", "Vertebrate Mitochondrial", "V" * 64, "M" * 64),
                        (3, "", "Yeast Mitochondrial", "Y" * 64, "M" * 64),
                        (11, "", "Bacterial, Archaeal and Plant Plastid", "B" * 64, "M" * 64)),
    "citations.dmp": b"unused\n",
}


@pytest.fixture(scope="module")
def tarball(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("dump") / "new_taxdump.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, data in FILES.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


@pytest.fixture(scope="module")
def taxdb_path(tarball, tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("db") / "taxdump.sqlite"
    build_taxdb(path, taxdump=tarball)
    return path


@pytest.fixture(scope="module")
def db(taxdb_path) -> TaxDb:
    return TaxDb(taxdb_path)


class TestBuild:
    def test_info(self, db: TaxDb) -> None:
        info = db.info()
        assert info["taxa"] == 9 and info["names"] == 14
        assert info["merged"] == 1 and info["deleted"] == 1 and info["fuzzy"] is True
        assert info["genetic_codes"][-1] == {
            "id": 11,
            "abbreviation": None,
            "name": "Bacterial, Archaeal and Plant Plastid",
            "amino_acids": "B" * 64,
            "starts": "M" * 64,
        }
        assert {d["code"] for d in info["divisions"]} >= {"BCT", "PRI"}

    def test_refuses_to_overwrite_and_leaves_no_partial_file(self, tarball, tmp_path) -> None:
        out = tmp_path / "t.sqlite"
        build_taxdb(out, taxdump=tarball)
        with pytest.raises(TaxDbError, match="exists"):
            build_taxdb(out, taxdump=tarball)
        build_taxdb(out, taxdump=tarball, force=True, fuzzy=False)
        assert TaxDb(out).info()["fuzzy"] is False
        assert [p.name for p in tmp_path.iterdir()] == ["t.sqlite"]

    def test_bad_tarball(self, tmp_path) -> None:
        bad = tmp_path / "bad.tar.gz"
        bad.write_bytes(b"not a tarball")
        with pytest.raises(TaxDbError):
            build_taxdb(tmp_path / "x.sqlite", taxdump=bad)
        assert not (tmp_path / "x.sqlite").exists()

    def test_not_a_taxonomy_file(self, tmp_path) -> None:
        other = tmp_path / "other.sqlite"
        other.write_bytes(b"")
        with pytest.raises(TaxDbError):
            TaxDb(other)
        with pytest.raises(TaxDbError, match="not found"):
            TaxDb(tmp_path / "missing.sqlite")


class TestQueries:
    def test_nodes_carry_every_field(self, db: TaxDb) -> None:
        node = db.nodes([562])[0]
        assert node["name"] == "Escherichia coli" and node["rank"] == "species"
        assert (node["genetic_code_id"], node["mito_genetic_code_id"]) == (11, 0)
        assert node["hidden"] is True and node["parent"] == 561
        yeast = db.nodes([4932])[0]
        assert (yeast["genetic_code_id"], yeast["mito_genetic_code_id"]) == (1, 3)
        assert yeast["plastid_genetic_code_id"] == 11 and yeast["division_id"] == 2
        assert db.nodes([2])[0]["unique_name"] == "Bacteria <bacteria>"

    def test_merged_unknown_and_deleted(self, db: TaxDb) -> None:
        merged, unknown, deleted = db.nodes([12, 424242, 777])
        assert merged["taxid"] == 562 and merged["merged_from"] == 12
        assert unknown is None and deleted is None

    def test_lineage_runs_root_first(self, db: TaxDb) -> None:
        names = [step["name"] for step in db.lineage([562])[0]]
        assert names == ["root", "Bacteria", "Escherichia", "Escherichia coli"]
        assert db.lineage([1])[0] == [{"taxid": 1, "name": "root", "rank": "no rank"}]
        assert db.lineage([424242]) == [None]

    def test_children_and_descendants(self, db: TaxDb) -> None:
        direct = db.children(561)
        assert [t["taxid"] for t in direct["taxa"]] == [550, 562] and not direct["truncated"]
        everything = db.children(1, descendants=True)
        assert len(everything["taxa"]) == 8  # the root is not its own descendant
        species = db.children(1, descendants=True, rank="species")
        assert {t["taxid"] for t in species["taxa"]} == {550, 562, 9606, 4932}
        assert db.children(1, descendants=True, limit=3)["truncated"] is True
        assert db.children(424242)["taxa"] is None

    def test_names(self, db: TaxDb) -> None:
        classes = {n["name_class"] for n in db.names([562])[0]}
        assert classes == {"scientific name", "common name", "synonym", "authority"}
        assert db.names([424242]) == [None]

    def test_lookup_is_exact_and_case_insensitive(self, db: TaxDb) -> None:
        results = db.lookup(["escherichia COLI", "E. coli", "Bacteria <bacteria>", "Escherich"])
        assert [m["taxid"] for m in results[0]["matches"]] == [562]
        assert results[1]["matches"][0]["name_class"] == "common name"
        assert results[1]["matches"][0]["scientific_name"] == "Escherichia coli"
        assert [m["taxid"] for m in results[2]["matches"]] == [2]
        assert results[3]["matches"] == []

    def test_prefix_search(self, db: TaxDb) -> None:
        hits = db.search("escherichia")
        assert [h["taxid"] for h in hits] == [561, 562, 550]  # the exact name, then the shortest
        assert db.search("escherichia", rank="species")[0]["taxid"] == 562
        assert db.search("") == []

    def test_fuzzy_search_tolerates_typos(self, db: TaxDb) -> None:
        assert db.search("escherichia colli", fuzzy=True)[0]["taxid"] == 562
        assert db.search("saccharomyces cerevisae", fuzzy=True)[0]["taxid"] == 4932
        assert db.search("homo sapien", fuzzy=True, rank="species")[0]["taxid"] == 9606
        assert db.search("bacterum coli", fuzzy=True)[0]["name_class"] == "synonym"
        assert db.search("qqqqqq zzzzzz", fuzzy=True) == []
        # typos in two words, one of them short; the words in another order
        assert db.search("eschrichia cli", fuzzy=True)[0]["taxid"] == 562
        assert db.search("coli escherichia", fuzzy=True)[0]["taxid"] == 562

    def test_fuzzy_needs_its_index(self, tarball, tmp_path) -> None:
        out = tmp_path / "plain.sqlite"
        build_taxdb(out, taxdump=tarball, fuzzy=False)
        with pytest.raises(TaxDbError, match="fuzzy"):
            TaxDb(out).search("escherichia", fuzzy=True)

    def test_a_replaced_file_is_picked_up(self, tarball, tmp_path) -> None:
        out = tmp_path / "live.sqlite"
        build_taxdb(out, taxdump=tarball, fuzzy=False)
        live = TaxDb(out)
        assert live.nodes([562])[0]["name"] == "Escherichia coli"
        build_taxdb(out, taxdump=tarball, force=True, fuzzy=True)
        assert live.search("escherichia colli", fuzzy=True)[0]["taxid"] == 562


class TestObjects:
    @pytest.fixture
    def tax(self, taxdb_path) -> Taxonomy:
        return Taxonomy.connect(db=taxdb_path)

    def test_taxon_fields(self, tax: Taxonomy) -> None:
        taxon = tax.taxid(562)
        assert (taxon.name, taxon.rank, taxon.parent_taxid) == ("Escherichia coli", "species", 561)
        assert (taxon.genetic_code_id, taxon.mito_genetic_code_id) == (11, 0)
        assert taxon.genetic_code()["name"].startswith("Bacterial")
        assert taxon.genetic_code("mito") is None  # 0 = unspecified
        assert tax.taxid(4932).genetic_code("mito")["name"] == "Yeast Mitochondrial"
        assert (taxon.division_code, taxon.division_name) == ("BCT", "Bacteria")
        assert repr(taxon) == "<Taxon 562 Escherichia coli (species)>" and int(taxon) == 562
        with pytest.raises(ValueError):
            taxon.genetic_code("nonsense")

    def test_tree(self, tax: Taxonomy) -> None:
        taxon = tax.taxid(562)
        assert taxon.parent.name == "Escherichia" and tax.taxid(1).parent is None
        assert taxon.at_rank("genus").taxid == 561 and taxon.at_rank("phylum") is None
        assert taxon.tax_at_rank("species") == taxon and taxon.depth == 3
        assert [t.taxid for t in taxon.lineage()] == [1, 2, 561, 562]
        assert [t.taxid for t in tax.taxid(561).children()] == [550, 562]
        assert {t.taxid for t in tax.taxid(1).descendants("species")} == {550, 562, 9606, 4932}

    def test_merged_unknown_and_errors(self, tax: Taxonomy) -> None:
        old = tax.taxid(12)
        assert old.taxid == 562 and old.merged_from == 12
        assert tax.taxid(562).merged_from is None  # the new id is not marked
        assert tax.get(424242) is None
        with pytest.raises(TaxonNotFound):
            tax.taxid(424242)
        with pytest.raises(ValueError):
            tax.taxid("abc")
        assert tax.taxid("562") == tax.taxid(562)

    def test_names_and_search(self, tax: Taxonomy) -> None:
        assert [t.taxid for t in tax.by_name("e. coli")] == [562]
        lookup = tax.lookup(["human", "nobody"])
        assert lookup["human"][0].taxon.taxid == 9606 and lookup["nobody"] == []
        hit = tax.search("escherichia colli", fuzzy=True)[0]
        assert hit.taxon.taxid == 562 and 0.9 < hit.score <= 1
        assert tax.search("eschericia", fuzzy=True, rank="genus")[0].taxon.taxid == 561
        assert {n["name_class"] for n in tax.taxid(562).names()} >= {"synonym"}

    def test_batch_fetch_and_cache(self, taxdb_path) -> None:
        calls = []

        class Counting(TaxDb):
            def nodes(self, taxids):
                calls.append(list(taxids))
                return super().nodes(taxids)

        tax = Taxonomy(Counting(taxdb_path))
        assert [t.taxid for t in tax.taxids([562, 561, 562, 424242][:3])] == [562, 561, 562]
        tax.taxid(562)
        assert calls == [[562, 561]]


@pytest.fixture(scope="module")
def client(taxdb_path):
    app = create_app(ServerConfig(db_path=ECOLI_TWO_STAGE, taxonomy_db=taxdb_path))
    with TestClient(app) as client:
        yield client


class TestEndpoints:
    def test_info_lists_the_endpoints(self, client) -> None:
        info = client.get("/info").json()
        assert info["taxonomy"]["enabled"] is True and info["taxonomy"]["taxa"] == 9
        assert "/taxonomy/search" in info["endpoints"]
        assert client.get("/taxonomy/info").json()["genetic_codes"][0]["id"] == 1

    def test_calls(self, client) -> None:
        nodes = client.post("/taxonomy/nodes", json={"taxids": [562, 12, 424242]}).json()["nodes"]
        assert nodes[0]["genetic_code_id"] == 11 and nodes[1]["merged_from"] == 12 and nodes[2] is None
        lineage = client.post("/taxonomy/lineage", json={"taxids": [562]}).json()["lineages"][0]
        assert [s["taxid"] for s in lineage] == [1, 2, 561, 562]
        children = client.post("/taxonomy/children", json={"taxid": 561}).json()
        assert [t["taxid"] for t in children["taxa"]] == [550, 562]
        lookup = client.post("/taxonomy/lookup", json={"names": ["E. coli"]}).json()["results"]
        assert lookup[0]["matches"][0]["taxid"] == 562
        hits = client.post(
            "/taxonomy/search", json={"query": "escherichia colli", "fuzzy": True, "limit": 2}
        ).json()["hits"]
        assert hits[0]["taxid"] == 562 and len(hits) <= 2
        names = client.post("/taxonomy/names", json={"taxids": [562]}).json()["names"][0]
        assert len(names) == 4

    @pytest.mark.parametrize(
        ("path", "body", "status"),
        [
            ("nodes", {}, 400),
            ("nodes", {"taxids": []}, 400),
            ("nodes", {"taxids": ["x"]}, 400),
            ("nodes", {"taxids": [True]}, 400),
            ("nodes", {"taxids": list(range(1, 1002))}, 400),
            ("children", {"taxid": 0}, 400),
            ("children", {"taxid": 1, "descendants": "yes"}, 400),
            ("lookup", {"names": [""]}, 400),
            ("lookup", {"names": "E. coli"}, 400),
            ("search", {"query": " "}, 400),
            ("search", {"query": "a", "limit": 0}, 400),
        ],
    )
    def test_bad_requests(self, client, path, body, status) -> None:
        assert client.post(f"/taxonomy/{path}", json=body).status_code == status

    def test_body_must_be_json_and_small(self, client) -> None:
        assert client.post("/taxonomy/nodes", content=b"{nope").status_code == 400
        assert client.post("/taxonomy/nodes", content=b"[1]").status_code == 400
        assert client.post("/taxonomy/nodes", content=b" " * (1 << 21)).status_code == 413

    def test_limit_is_capped(self, client) -> None:
        hits = client.post("/taxonomy/search", json={"query": "e", "limit": 10_000}).json()["hits"]
        assert len(hits) <= 100

    def test_off_without_the_flag(self) -> None:
        with TestClient(create_app(ServerConfig(db_path=ECOLI_TWO_STAGE))) as client:
            assert client.get("/info").json()["taxonomy"] == {"enabled": False}
            assert client.post("/taxonomy/nodes", json={"taxids": [1]}).status_code == 404

    def test_rate_limit_is_separate_and_a_token_lifts_it(self, taxdb_path, tmp_path) -> None:
        from sylphd.limits import load_token_digests

        token = tmp_path / "token"
        token.write_text("tax-token-" + "x" * 20)
        app = create_app(
            ServerConfig(
                db_path=ECOLI_TWO_STAGE,
                taxonomy_db=taxdb_path,
                taxonomy_rate_limit=(2, 60.0),
                privileged_tokens=load_token_digests(token),
            )
        )
        with TestClient(app) as client:
            body = {"taxids": [562]}
            codes = [client.post("/taxonomy/nodes", json=body).status_code for _ in range(3)]
            assert codes == [200, 200, 429]
            auth = {"authorization": "Bearer " + token.read_text()}
            assert all(
                client.post("/taxonomy/nodes", json=body, headers=auth).status_code == 200
                for _ in range(5)
            )
            assert client.get("/health").status_code == 200  # other endpoints untouched

    def test_remote_backend_gives_the_same_answers(self, client, taxdb_path) -> None:
        remote_client = SylphClient(base_url="http://testserver")
        remote_client._client = client  # a TestClient is an httpx.Client
        remote = Taxonomy.from_client(remote_client)
        local = Taxonomy.connect(db=taxdb_path)
        assert remote.taxid(12).to_dict() == local.taxid(12).to_dict()
        assert [t.taxid for t in remote.taxid(1).descendants("species")] == [
            t.taxid for t in local.taxid(1).descendants("species")
        ]
        assert remote.taxid(562).genetic_code()["id"] == 11
        assert [m.taxon.taxid for m in remote.search("escherichia colli", fuzzy=True)][0] == 562
        assert remote.by_name("human")[0].taxid == 9606
        with pytest.raises(TaxonNotFound):
            remote.taxid(424242)


def test_the_client_side_needs_no_server_packages() -> None:
    """A machine that only reads taxonomy needs neither pysylph nor starlette."""
    code = textwrap.dedent(
        """
        import sys
        for name in ("pysylph", "starlette", "uvicorn", "dnaio", "multipart", "python_multipart"):
            sys.modules[name] = None
        from sylphd.ncbitax import Taxonomy
        import sylphd.client
        print("ok")
        """
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert result.stdout.strip() == "ok", result.stderr
