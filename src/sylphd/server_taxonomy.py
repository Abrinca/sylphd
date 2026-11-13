"""The ``/taxonomy/*`` endpoints (only routed with ``--taxonomy-db``).

Each endpoint is one :class:`sylphd.taxdb.TaxDb` method: the JSON in the
request is the method's arguments and the JSON out is its result, so
:mod:`sylphd.ncbitax` gets the same answers from a server and from a file.
The calls are cheap (index lookups) and never wait for a job slot.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .server_errors import RequestError, answers_errors
from .taxdb import MAX_BATCH, MAX_DESCENDANTS, MAX_SEARCH, TaxDbError

#: Largest request body, in bytes (a thousand taxids or names is far less).
MAX_BODY = 1 << 20

#: Paths covered by ``--taxonomy-rate-limit``.
TAXONOMY_PATHS = frozenset(
    f"/taxonomy/{name}"
    for name in ("info", "nodes", "lineage", "names", "children", "lookup", "search")
)


async def _body(request: Request) -> dict[str, Any]:
    size = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise RequestError(f"request body over {MAX_BODY} bytes", 413)
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks) or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise RequestError("the body must be JSON") from None
    if not isinstance(body, dict):
        raise RequestError("the body must be a JSON object")
    return body


def _int_list(body: dict[str, Any], key: str) -> list[int]:
    values = body.get(key)
    if not isinstance(values, list) or not values:
        raise RequestError(f"{key!r} must be a non-empty list of taxids")
    if len(values) > MAX_BATCH:
        raise RequestError(f"at most {MAX_BATCH} {key} per request")
    if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in values):
        raise RequestError(f"{key!r} must hold positive integers")
    return values


def _optional_text(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 200:
        raise RequestError(f"{key!r} must be a short string")
    return value


def _limit(body: dict[str, Any], default: int, maximum: int) -> int:
    value = body.get("limit", default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RequestError("'limit' must be a positive integer")
    return min(value, maximum)


def _flag(body: dict[str, Any], key: str) -> bool:
    value = body.get(key, False)
    if not isinstance(value, bool):
        raise RequestError(f"{key!r} must be true or false")
    return value


async def _run(request: Request, call: Callable[[], Any]) -> JSONResponse:
    try:
        return JSONResponse(await anyio.to_thread.run_sync(call))
    except TaxDbError as error:
        raise RequestError(str(error), 503) from None


@answers_errors
async def _info(request: Request) -> JSONResponse:
    return await _run(request, request.app.state.taxdb.info)


@answers_errors
async def _nodes(request: Request) -> JSONResponse:
    taxids = _int_list(await _body(request), "taxids")
    db = request.app.state.taxdb
    return await _run(request, lambda: {"nodes": db.nodes(taxids)})


@answers_errors
async def _lineage(request: Request) -> JSONResponse:
    taxids = _int_list(await _body(request), "taxids")
    db = request.app.state.taxdb
    return await _run(request, lambda: {"lineages": db.lineage(taxids)})


@answers_errors
async def _names(request: Request) -> JSONResponse:
    taxids = _int_list(await _body(request), "taxids")
    db = request.app.state.taxdb
    return await _run(request, lambda: {"names": db.names(taxids)})


@answers_errors
async def _children(request: Request) -> JSONResponse:
    body = await _body(request)
    taxid = body.get("taxid")
    if not isinstance(taxid, int) or isinstance(taxid, bool) or taxid < 1:
        raise RequestError("'taxid' must be a positive integer")
    descendants = _flag(body, "descendants")
    rank = _optional_text(body, "rank")
    limit = _limit(body, MAX_DESCENDANTS, MAX_DESCENDANTS)
    db = request.app.state.taxdb
    return await _run(
        request,
        lambda: db.children(taxid, descendants=descendants, rank=rank, limit=limit),
    )


@answers_errors
async def _lookup(request: Request) -> JSONResponse:
    names = (await _body(request)).get("names")
    if not isinstance(names, list) or not names:
        raise RequestError("'names' must be a non-empty list of names")
    if len(names) > MAX_BATCH:
        raise RequestError(f"at most {MAX_BATCH} names per request")
    if not all(isinstance(n, str) and 0 < len(n) <= 200 for n in names):
        raise RequestError("'names' must hold non-empty strings of at most 200 characters")
    db = request.app.state.taxdb
    return await _run(request, lambda: {"results": db.lookup(names)})


@answers_errors
async def _search(request: Request) -> JSONResponse:
    body = await _body(request)
    query = body.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > 200:
        raise RequestError("'query' must be a non-empty string of at most 200 characters")
    fuzzy = _flag(body, "fuzzy")
    rank = _optional_text(body, "rank")
    limit = _limit(body, 20, MAX_SEARCH)
    db = request.app.state.taxdb
    return await _run(
        request,
        lambda: {"hits": db.search(query, fuzzy=fuzzy, rank=rank, limit=limit)},
    )


def taxonomy_routes() -> list[Route]:
    return [
        Route("/taxonomy/info", _info, methods=["GET"]),
        Route("/taxonomy/nodes", _nodes, methods=["POST"]),
        Route("/taxonomy/lineage", _lineage, methods=["POST"]),
        Route("/taxonomy/names", _names, methods=["POST"]),
        Route("/taxonomy/children", _children, methods=["POST"]),
        Route("/taxonomy/lookup", _lookup, methods=["POST"]),
        Route("/taxonomy/search", _search, methods=["POST"]),
    ]
