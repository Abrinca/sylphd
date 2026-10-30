"""Reading and validating the multipart body of ``/profile``, ``/query`` and ``/contigs``."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException
from starlette.requests import Request

from . import per_contig
from .metadata import MetadataDB
from .protocol import ProfileParams, ProtocolError
from .server_errors import RequestError, check_queue
from .sketching import InputError, check_sample_name, sample_name_from_path
from .taxonomy import TaxonomyError, parse_taxonomies, resolve_taxonomy_columns

log = logging.getLogger("uvicorn.error")


#: Maximum number of sketches accepted in one ``/profile`` or ``/query``
#: request.
MAX_SKETCHES = 4096

#: Largest non-file form field (``params``) Starlette accepts in one request.
#: ``/contigs`` repeats every contig name, length and k-mer count there, so the
#: limit is advertised in ``/info`` for clients that budget their requests.
MAX_PARAMS_BYTES = 1024 * 1024


def _too_many_contigs(limit: int) -> str:
    return (
        "too many contigs in one request; send at most "
        f"{limit} contigs per POST /contigs (one 'sketch' part each; the "
        "server's limits.max_contigs in GET /info) and batch the rest "
        "yourself, or use 'sylphd assembly --per-contig', which batches by "
        "that limit"
    )


def _is_too_many_files(message: str) -> bool:
    # Starlette stops at the first file beyond ``max_files``, so the count in
    # its message is that limit, not the number of parts the request carried.
    return message.startswith("Too many files")


def require_multipart(request: Request, hint: str) -> None:
    """Refuse a non-multipart body, or any request while the queue is full."""
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise RequestError(f"expected a multipart/form-data body with {hint}", 415)
    check_queue(request.app.state)


@asynccontextmanager
async def _read_form(
    request: Request, max_files: int, too_many: str | None = None
) -> AsyncIterator[Any]:
    """Open the multipart form, answering Starlette's parse errors.

    ``too_many`` replaces the message of Starlette's too-many-files error.  The
    form (and its temporary files) is closed on exit.
    """
    try:
        async with request.form(
            # Exactly the limit: the parser raises on the (N+1)th file part,
            # so N+1 uploads are not spooled to disk before the 400.
            max_files=max_files,
            max_fields=16,
            max_part_size=MAX_PARAMS_BYTES,
        ) as form:
            yield form
    except StarletteHTTPException as error:
        if too_many is not None and _is_too_many_files(str(error.detail)):
            raise RequestError(too_many) from None
        raise RequestError(str(error.detail), error.status_code) from None
    except MultiPartException as error:
        # Starlette re-raises this as a 400 HTTPException when the app is in
        # the scope; translating it here too keeps the message independent of
        # that. Other multipart errors (bad boundary, oversized part) keep
        # their own message.
        if too_many is not None and _is_too_many_files(error.message):
            raise RequestError(too_many) from None
        raise RequestError(error.message) from None


def _params_object(form: Any) -> dict[str, Any]:
    """The JSON object in the ``params`` field (empty when absent)."""
    raw_params = form.get("params")
    try:
        params_data: Any = {} if raw_params is None else json.loads(str(raw_params))
    except json.JSONDecodeError as error:
        raise RequestError(f"invalid JSON in 'params': {error}") from None
    if not isinstance(params_data, dict):
        raise RequestError("'params' must be a JSON object")
    return params_data


def _sketch_parts(form: Any) -> list[UploadFile]:
    sketches = form.getlist("sketch")
    if not sketches:
        raise RequestError("no 'sketch' part in the request")
    if not all(isinstance(part, UploadFile) for part in sketches):
        raise RequestError("'sketch' parts must be file uploads")
    return sketches


async def _read_payloads(sketches: Sequence[UploadFile]) -> list[bytes]:
    try:
        return [await part.read() for part in sketches]
    except Exception:
        log.exception("failed to read an uploaded sketch part")
        raise RequestError("failed to read a 'sketch' part", 500) from None


async def read_contigs(
    request: Request,
) -> tuple[
    tuple[per_contig.ContigInput, ...],
    per_contig.ContigOptions,
    Sequence[str] | None,
    list[bytes],
]:
    """Parse the ``/contigs`` form, or raise the :class:`RequestError`."""
    state = request.app.state
    limit = state.max_contigs
    async with _read_form(request, limit, _too_many_contigs(limit)) as form:
        params_data = _params_object(form)
        sketches = _sketch_parts(form)
        if len(sketches) > limit:
            raise RequestError(_too_many_contigs(limit))
        try:
            contigs, options = per_contig.parse_params(
                params_data, k=state.database.k, count=len(sketches)
            )
        except ProtocolError as error:
            raise RequestError(str(error)) from None
        fields = _validate_fields(params_data.get("fields"), state.metadata)
        tax_sources = _validate_tax_sources(
            params_data.get("tax_sources"), state.metadata
        )
        payloads = await _read_payloads(sketches)
        return (
            contigs,
            replace(options, fields=tuple(fields or ())),
            tax_sources,
            payloads,
        )


async def read_upload(
    request: Request, mode: str
) -> tuple[
    ProfileParams,
    Sequence[str] | None,
    list[str],
    list[bytes],
    Sequence[str] | None,
    Sequence[str] | None,
]:
    """Parse the form into ``(params, fields, names, payloads, tax_sources, taxprof)``.

    Raises the :class:`RequestError` when the request is invalid.
    """
    state = request.app.state
    async with _read_form(request, MAX_SKETCHES) as form:
        params_data = _params_object(form)
        try:
            params = ProfileParams.from_mapping(params_data)
        except ProtocolError as error:
            raise RequestError(str(error)) from None

        declared_mode = params_data.get("mode")
        if declared_mode is not None and declared_mode != mode:
            raise RequestError(
                f"params.mode is {declared_mode!r} but this is the /{mode} endpoint"
            )

        fields = _validate_fields(params_data.get("fields"), state.metadata)
        tax_sources = _validate_tax_sources(
            params_data.get("tax_sources"), state.metadata
        )
        taxprof = _validate_taxprof(params_data.get("taxprof"), state.metadata, mode)
        sketches = _sketch_parts(form)
        names = _resolve_names(params_data.get("names"), sketches)
        payloads = await _read_payloads(sketches)
        return params, fields, names, payloads, tax_sources, taxprof


def _validate_fields(value: Any, metadata: MetadataDB | None) -> Sequence[str] | None:
    """Validate ``params.fields``: metadata column names, in request order.

    Duplicates are removed (a repeated column must not duplicate the key of a
    hit or the work of the metadata join), and the result cannot be longer
    than the number of columns the server's metadata file has.
    """
    if value is None:
        return None if metadata is None else tuple(metadata.field_columns)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RequestError("'params.fields' must be a list of column names")
    if not value:
        return ()
    if metadata is None:
        raise RequestError(
            "this server has no metadata database; --fields is not available"
        )
    unknown = [column for column in value if column not in metadata.field_columns]
    if unknown:
        raise RequestError(f"unknown metadata column(s): {', '.join(unknown)}")
    columns = tuple(dict.fromkeys(value))
    if len(columns) > len(metadata.field_columns):
        raise RequestError(
            f"'params.fields' has {len(columns)} columns; this server's metadata "
            f"file has only {len(metadata.field_columns)}"
        )
    return columns


def _validate_tax_sources(
    value: Any, metadata: MetadataDB | None
) -> Sequence[str] | None:
    """Validate ``params.tax_sources``: source names of the metadata file.

    ``None`` means all sources (sylph-tax given every file).  A name the file
    does not have is a 400, since silently resolving against another source
    would produce a profile that looks right and is not.
    """
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RequestError("'params.tax_sources' must be a list of source names")
    if not value:  # an empty list is "no restriction", like an omitted field
        return None
    available = metadata.source_names() if metadata is not None else []
    if not available:
        raise RequestError(
            "this server has no sylph-tax taxonomy sources; "
            "'params.tax_sources' is not available"
        )
    unknown = [name for name in value if name not in available]
    if unknown:
        raise RequestError(
            "unknown taxonomy source(s): "
            + ", ".join(unknown)
            + f" (available: {', '.join(available)})"
        )
    return tuple(dict.fromkeys(value))


def _validate_taxprof(
    value: Any, metadata: MetadataDB | None, mode: str
) -> Sequence[str] | None:
    """Validate ``params.taxprof``: taxonomies the server can aggregate.

    The server is the only place that knows which metadata columns and
    sylph-tax sources its file has, so the request is checked against them
    (:func:`sylphd.taxonomy.resolve_taxonomy_columns`): a taxonomy the file
    cannot produce is a 400, not a silently empty table.
    """
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RequestError("'params.taxprof' must be a list of taxonomy names")
    if not value:
        return None
    if mode != "profile":
        raise RequestError("'params.taxprof' is only available on /profile")
    try:
        taxonomies = parse_taxonomies(value)
        summary = metadata.summary() if metadata is not None else None
        resolve_taxonomy_columns(taxonomies, summary)
    except (TaxonomyError, ProtocolError) as error:
        raise RequestError(str(error)) from None
    return taxonomies


def _resolve_names(value: Any, sketches: Sequence[Any]) -> list[str]:
    if value is None:
        names = []
        for index, part in enumerate(sketches):
            filename = getattr(part, "filename", None)
            name = sample_name_from_path(filename) if filename else ""
            names.append(name or f"sample-{index + 1}")
    elif not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RequestError("'params.names' must be a list of strings")
    elif len(value) != len(sketches):
        raise RequestError(
            f"'params.names' has {len(value)} entries for {len(sketches)} sketches"
        )
    else:
        names = value
    try:
        return [check_sample_name(name) for name in names]
    except InputError as error:
        raise RequestError(str(error)) from None
