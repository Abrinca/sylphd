"""NCBI taxonomy as Python objects, from a server, a socket or a file.

::

    from sylphd.ncbitax import Taxonomy

    tax = Taxonomy.connect(socket="/run/user/1000/sylphd.sock")   # a local server
    tax = Taxonomy.connect(url="https://sylphd.example.org")      # a remote one
    tax = Taxonomy.connect(db="taxdump.sqlite")                   # a file, no server

    t = tax.taxid(562)
    t.name, t.rank, t.genetic_code_id, t.mito_genetic_code_id
    t.parent, t.children(), t.lineage(), t.at_rank("genus")
    tax.by_name("Escherichia coli")
    tax.search("escherichia colli", fuzzy=True)

The three backends answer identically (see :mod:`sylphd.taxdb` for the shapes),
so code written against one runs against the others.  Importing this module
needs only the standard library, ``httpx`` is imported when a server is used,
so a client machine does not need the server's packages.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from typing import Any, Protocol, Self

from .taxdb import MAX_BATCH, TaxDb

__all__ = ["Match", "Taxon", "TaxonNotFound", "Taxonomy"]


class TaxonNotFound(KeyError):
    """The taxid or name is not in the NCBI taxonomy (deleted ids included)."""

    def __str__(self) -> str:
        return f"not in the NCBI taxonomy: {self.args[0]!r}"


class _Backend(Protocol):
    def info(self) -> dict[str, Any]: ...
    def nodes(self, taxids: Sequence[int]) -> list[dict[str, Any] | None]: ...
    def lineage(self, taxids: Sequence[int]) -> list[list[dict[str, Any]] | None]: ...
    def names(self, taxids: Sequence[int]) -> list[list[dict[str, Any]] | None]: ...
    def children(self, taxid: int, **options: Any) -> dict[str, Any]: ...
    def lookup(self, names: Sequence[str]) -> list[dict[str, Any]]: ...
    def search(self, query: str, **options: Any) -> list[dict[str, Any]]: ...


class _RemoteBackend:
    """The :class:`TaxDb` methods, answered by a server's ``/taxonomy/*``."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def info(self) -> dict[str, Any]:
        return self.client.taxonomy("info")

    def nodes(self, taxids: Sequence[int]) -> list[dict[str, Any] | None]:
        return self.client.taxonomy("nodes", {"taxids": list(taxids)})["nodes"]

    def lineage(self, taxids: Sequence[int]) -> list[list[dict[str, Any]] | None]:
        return self.client.taxonomy("lineage", {"taxids": list(taxids)})["lineages"]

    def names(self, taxids: Sequence[int]) -> list[list[dict[str, Any]] | None]:
        return self.client.taxonomy("names", {"taxids": list(taxids)})["names"]

    def children(self, taxid: int, **options: Any) -> dict[str, Any]:
        return self.client.taxonomy("children", {"taxid": taxid, **options})

    def lookup(self, names: Sequence[str]) -> list[dict[str, Any]]:
        return self.client.taxonomy("lookup", {"names": list(names)})["results"]

    def search(self, query: str, **options: Any) -> list[dict[str, Any]]:
        return self.client.taxonomy("search", {"query": query, **options})["hits"]

    def close(self) -> None:
        self.client.close()


class Match:
    """A name that matched a lookup or a search: ``taxon`` plus how it matched.

    ``name`` is the matched name (a synonym, say), ``name_class`` its class
    (``"scientific name"``, ``"synonym"``, ``"common name"``, ...), ``score``
    the similarity of a search hit (1.0 for an exact name).
    """

    __slots__ = ("taxon", "name", "name_class", "score")

    def __init__(self, taxon: Taxon, name: str, name_class: str, score: float = 1.0) -> None:
        self.taxon = taxon
        self.name = name
        self.name_class = name_class
        self.score = score

    def __repr__(self) -> str:
        return f"<Match {self.name!r} ({self.name_class}) -> {self.taxon!r} score={self.score}>"


class Taxon:
    """One NCBI taxon: its fields, and its place in the tree.

    ``genetic_code_id`` is the nuclear genetic code (translation table) and
    ``mito_genetic_code_id`` the mitochondrial one; ``0`` means unspecified
    for mitochondria and ``None`` that NCBI gives none (plastid, hydrogenosome).
    ``merged_from`` is the old taxid when the one you asked for was merged
    into this one.
    """

    __slots__ = ("_tax", "_record")

    def __init__(self, tax: Taxonomy, record: dict[str, Any]) -> None:
        self._tax = tax
        self._record = record

    # -- fields -----------------------------------------------------------
    @property
    def taxid(self) -> int:
        return self._record["taxid"]

    @property
    def name(self) -> str:
        """The scientific name."""
        return self._record["name"]

    scientific_name = name

    @property
    def unique_name(self) -> str:
        """The scientific name, made unique (``Bacteria <bacteria>``) where NCBI does."""
        return self._record["unique_name"]

    @property
    def rank(self) -> str:
        return self._record["rank"]

    @property
    def parent_taxid(self) -> int:
        return self._record["parent"]

    @property
    def embl_code(self) -> str | None:
        return self._record["embl_code"]

    @property
    def division_id(self) -> int | None:
        return self._record["division_id"]

    @property
    def division_code(self) -> str | None:
        """Three-letter GenBank division of the taxon (``BCT``, ``PRI``, ...)."""
        return self._tax._division(self.division_id, "code")

    @property
    def division_name(self) -> str | None:
        return self._tax._division(self.division_id, "name")

    @property
    def genetic_code_id(self) -> int | None:
        return self._record["genetic_code_id"]

    @property
    def mito_genetic_code_id(self) -> int | None:
        return self._record["mito_genetic_code_id"]

    @property
    def plastid_genetic_code_id(self) -> int | None:
        return self._record["plastid_genetic_code_id"]

    @property
    def hydrogenosome_genetic_code_id(self) -> int | None:
        return self._record["hydrogenosome_genetic_code_id"]

    @property
    def hidden(self) -> bool:
        """NCBI's GenBank-hidden flag."""
        return self._record["hidden"]

    @property
    def hidden_subtree_root(self) -> bool:
        return self._record["hidden_subtree_root"]

    @property
    def specified_species(self) -> bool:
        return self._record["specified_species"]

    @property
    def comments(self) -> str | None:
        return self._record["comments"]

    @property
    def merged_from(self) -> int | None:
        return self._record.get("merged_from")

    def genetic_code(self, kind: str = "nuclear") -> dict[str, Any] | None:
        """The genetic code table of this taxon: ``{id, abbreviation, name, amino_acids, starts}``.

        ``kind`` is ``"nuclear"`` (the default), ``"mito"``, ``"plastid"`` or
        ``"hydrogenosome"``; ``None`` when the taxon has no such code.
        """
        field = {
            "nuclear": "genetic_code_id",
            "mito": "mito_genetic_code_id",
            "plastid": "plastid_genetic_code_id",
            "hydrogenosome": "hydrogenosome_genetic_code_id",
        }.get(kind)
        if field is None:
            raise ValueError(f"unknown kind {kind!r}: nuclear, mito, plastid or hydrogenosome")
        code = self._record[field]
        return self._tax.genetic_codes().get(code) if code else None

    # -- tree -------------------------------------------------------------
    @property
    def parent(self) -> Taxon | None:
        """The parent taxon; ``None`` for the root."""
        if self.parent_taxid == self.taxid:
            return None
        return self._tax.taxid(self.parent_taxid)

    def children(self) -> list[Taxon]:
        """The direct children."""
        return self._tax._children(self.taxid, descendants=False)

    def descendants(self, rank: str | None = None, limit: int | None = None) -> list[Taxon]:
        """All descendants (of one ``rank``, if given), at most ``limit``."""
        return self._tax._children(self.taxid, descendants=True, rank=rank, limit=limit)

    def lineage(self) -> list[Taxon]:
        """The path from the root down to this taxon (itself last)."""
        return self._tax.lineage(self.taxid)

    def at_rank(self, rank: str) -> Taxon | None:
        """The ancestor (or this taxon) of ``rank``; ``None`` if the lineage has none."""
        for taxon in reversed(self.lineage()):
            if taxon.rank == rank:
                return taxon
        return None

    tax_at_rank = at_rank

    @property
    def depth(self) -> int:
        """Number of steps down from the root."""
        return len(self.lineage()) - 1

    def names(self) -> list[dict[str, Any]]:
        """Every name: ``{name, name_class, unique_name}`` (synonyms, common names, ...)."""
        return self._tax.names(self.taxid)

    # -- plumbing ---------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return dict(self._record)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Taxon) and other.taxid == self.taxid

    def __hash__(self) -> int:
        return hash(self.taxid)

    def __int__(self) -> int:
        return self.taxid

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"<Taxon {self.taxid} {self.name} ({self.rank})>"


class Taxonomy:
    """The NCBI taxonomy behind a server or a file; build one with :meth:`connect`."""

    def __init__(self, backend: _Backend, *, cache: bool = True) -> None:
        self._backend = backend
        self._cache: dict[int, Taxon] | None = {} if cache else None
        self._info: dict[str, Any] | None = None

    @classmethod
    def connect(
        cls,
        *,
        socket: str | os.PathLike[str] | None = None,
        url: str | None = None,
        db: str | os.PathLike[str] | None = None,
        token: str | None = None,
        timeout: float | None = None,
        retries: int = 0,
        cache: bool = True,
    ) -> Self:
        """Connect to a server (``socket`` or ``url``) or open a taxonomy file (``db``).

        Without arguments the environment decides: ``$SYLPHD_TAXDB`` (a file),
        else ``$SYLPHD_URL``, else ``$SYLPHD_SOCKET``, else the default socket.
        A ``token`` (default ``$SYLPHD_TOKEN``) lifts a server's rate limit
        when the server knows it.  ``cache`` keeps each fetched taxon, so
        repeated lookups cost nothing.
        """
        given = [x is not None for x in (socket, url, db)]
        if sum(given) > 1:
            raise ValueError("give at most one of socket, url and db")
        if not any(given):
            if os.environ.get("SYLPHD_TAXDB"):
                db = os.environ["SYLPHD_TAXDB"]
            elif os.environ.get("SYLPHD_URL"):
                url = os.environ["SYLPHD_URL"]
            else:
                socket = os.environ.get("SYLPHD_SOCKET") or None
        if db is not None:
            return cls(TaxDb(db), cache=cache)
        from .client import connect
        from .paths import default_socket_path

        token = token or os.environ.get("SYLPHD_TOKEN", "").strip() or None
        if url is not None:
            client = connect(url=url, timeout=timeout, retries=retries, token=token)
        else:
            client = connect(
                socket_path=socket if socket is not None else default_socket_path(),
                timeout=timeout,
                retries=retries,
                token=token,
            )
        return cls(_RemoteBackend(client), cache=cache)

    @classmethod
    def from_client(cls, client: Any, *, cache: bool = True) -> Self:
        """Use an existing :class:`sylphd.client.SylphClient`."""
        return cls(_RemoteBackend(client), cache=cache)

    # -- taxa -------------------------------------------------------------
    def taxid(self, taxid: int | str) -> Taxon:
        """The taxon of ``taxid`` (a merged id gives its replacement); ``TaxonNotFound`` if none."""
        found = self.taxids([taxid])[0]
        if found is None:
            raise TaxonNotFound(taxid)
        return found

    def get(self, taxid: int | str) -> Taxon | None:
        """Like :meth:`taxid`, but ``None`` for an unknown taxid."""
        return self.taxids([taxid])[0]

    def taxids(self, taxids: Iterable[int | str]) -> list[Taxon | None]:
        """Many taxa in one go (in order, ``None`` where unknown); ids are fetched in batches."""
        wanted = [_as_taxid(t) for t in taxids]
        missing = [t for t in dict.fromkeys(wanted) if self._cache is None or t not in self._cache]
        fetched: dict[int, Taxon | None] = {}
        for start in range(0, len(missing), MAX_BATCH):
            chunk = missing[start : start + MAX_BATCH]
            for requested, record in zip(chunk, self._backend.nodes(chunk), strict=True):
                taxon = Taxon(self, record) if record is not None else None
                fetched[requested] = taxon
                if taxon is not None and self._cache is not None:
                    self._cache[requested] = taxon
        out: list[Taxon | None] = []
        for taxid in wanted:
            if self._cache is not None and taxid in self._cache:
                out.append(self._cache[taxid])
            else:
                out.append(fetched.get(taxid))
        return out

    def lineage(self, taxid: int | str) -> list[Taxon]:
        """The taxa from the root to ``taxid`` (itself last)."""
        found = self._backend.lineage([_as_taxid(taxid)])[0]
        if found is None:
            raise TaxonNotFound(taxid)
        taxa = self.taxids([step["taxid"] for step in found])
        return [t for t in taxa if t is not None]

    def names(self, taxid: int | str) -> list[dict[str, Any]]:
        """Every name of ``taxid``: ``{name, name_class, unique_name}``."""
        found = self._backend.names([_as_taxid(taxid)])[0]
        if found is None:
            raise TaxonNotFound(taxid)
        return found

    def _children(
        self,
        taxid: int,
        *,
        descendants: bool,
        rank: str | None = None,
        limit: int | None = None,
    ) -> list[Taxon]:
        options: dict[str, Any] = {"descendants": descendants}
        if rank is not None:
            options["rank"] = rank
        if limit is not None:
            options["limit"] = limit
        answer = self._backend.children(taxid, **options)
        if answer["taxa"] is None:
            raise TaxonNotFound(taxid)
        found = self.taxids([t["taxid"] for t in answer["taxa"]])
        return [t for t in found if t is not None]

    # -- names ------------------------------------------------------------
    def lookup(self, names: Iterable[str]) -> dict[str, list[Match]]:
        """Exact names (any class, case-insensitive) to taxa: ``{name: [Match, ...]}``.

        A name with no match maps to ``[]``; one name can match several taxa.
        """
        names = list(dict.fromkeys(names))
        out: dict[str, list[Match]] = {}
        for start in range(0, len(names), MAX_BATCH):
            chunk = names[start : start + MAX_BATCH]
            for result in self._backend.lookup(chunk):
                taxa = self.taxids([m["taxid"] for m in result["matches"]])
                out[result["query"]] = [
                    Match(taxon, m["name"], m["name_class"])
                    for m, taxon in zip(result["matches"], taxa, strict=True)
                    if taxon is not None
                ]
        return out

    def by_name(self, name: str) -> list[Taxon]:
        """The taxa with exactly this name (scientific name first)."""
        return [match.taxon for match in self.lookup([name])[name]]

    def search(
        self,
        query: str | int,
        *,
        fuzzy: bool = False,
        rank: str | None = None,
        limit: int = 20,
    ) -> list[Match]:
        """Names that start with ``query``, or with ``fuzzy=True`` that resemble it (typos).

        Best first, one hit per taxon, ``rank`` restricts to one rank.  Fuzzy
        search needs a taxonomy built with the fuzzy index (the default).

        A query of digits (``"56"`` or ``56``) completes the **taxid** instead: the exact
        taxid first, then the ids that start with the digits (``Match.name_class`` is
        ``"taxid"``; an old merged taxid answers with its replacement, ``"merged taxid"``).
        """
        query = str(query)
        options: dict[str, Any] = {"fuzzy": fuzzy, "limit": limit}
        if rank is not None:
            options["rank"] = rank
        hits = self._backend.search(query, **options)
        taxa = self.taxids([h["taxid"] for h in hits])
        return [
            Match(taxon, h["name"], h["name_class"], h["score"])
            for h, taxon in zip(hits, taxa, strict=True)
            if taxon is not None
        ]

    # -- tables -----------------------------------------------------------
    def info(self) -> dict[str, Any]:
        """The taxonomy's source and date, counts, divisions and genetic codes."""
        if self._info is None:
            self._info = self._backend.info()
        return self._info

    def genetic_codes(self) -> dict[int, dict[str, Any]]:
        """The genetic code tables by id (``1`` standard, ``11`` bacterial, ...)."""
        return {code["id"]: code for code in self.info()["genetic_codes"]}

    def _division(self, division_id: int | None, field: str) -> str | None:
        for division in self.info()["divisions"]:
            if division["id"] == division_id:
                return division[field]
        return None

    def cache_clear(self) -> None:
        if self._cache is not None:
            self._cache.clear()

    def close(self) -> None:
        close = getattr(self._backend, "close", None)
        if close is not None:
            close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _as_taxid(value: int | str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"not a taxid: {value!r}")
    try:
        taxid = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"not a taxid: {value!r}") from None
    if taxid < 1:
        raise ValueError(f"not a taxid: {value!r}")
    return taxid
