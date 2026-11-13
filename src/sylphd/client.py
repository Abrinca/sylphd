"""HTTP client for a running ``sylphd serve``.

The client speaks HTTP over a Unix socket (default), TCP, or an ``http://`` /
``https://`` base URL.  Sketches are made locally with the database's
``c``/``k``.  ``/assembly`` takes either such a sketch (``assembly_sketch``,
the default of ``sylphd assembly``: the sequence never leaves the machine) or
raw sequences (``assembly``, only if the server runs with ``--allow-sequences``).
``/contigs`` takes one sketch per contig of a fragmented assembly plus the
record's measurements (``contigs``) and returns the server-side per-contig
classification.
"""

from __future__ import annotations

import json
import os
import time
import warnings
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit, urlunsplit

import httpx

from .protocol import ProfileParams, contigs_json, params_json

#: ``Content-Type`` of an ``/assembly`` body that is a sketch (see the server).
SKETCH_CONTENT_TYPE = "application/vnd.sylph.sketch"

#: Statuses that mean "try again shortly" (queue full, rate limited).
RETRY_STATUSES = frozenset({429, 503})

#: Longest wait between two attempts, in seconds.
MAX_RETRY_DELAY = 30.0


class TokenWarning(UserWarning):
    """The server does not accept the access token this client sends."""


def _sketching() -> Any:
    """:mod:`sylphd.sketching`, which needs pysylph (the ``client`` extra)."""
    try:
        from . import sketching
    except ImportError as error:
        if (error.name or "").split(".")[0] not in {"pysylph", "dnaio"}:
            raise
        raise ImportError(
            f"sketching needs {error.name}: install 'sylphd[client]' (or 'sylphd[server]')"
        ) from error
    return sketching


def _sample_input(sketching: Any, reads: Any, name: str | None) -> Any:
    """A :class:`SampleInput` from a path, an ``(r1, r2)`` pair or a ready ``SampleInput``."""
    import dataclasses

    if isinstance(reads, sketching.SampleInput):
        return dataclasses.replace(reads, name=name) if name else reads
    if isinstance(reads, (str, os.PathLike)):
        path = os.fspath(reads)
        return sketching.SampleInput(
            name or sketching.sample_name_from_path(path), ((path,),)
        )
    if isinstance(reads, (tuple, list)) and len(reads) == 2:
        first, second = os.fspath(reads[0]), os.fspath(reads[1])
        return sketching.SampleInput(
            name or sketching.sample_name_from_path(first, paired=True), ((first, second),)
        )
    raise TypeError(
        "reads must be a path, an (r1, r2) pair of paths or a SampleInput, "
        f"not {type(reads).__name__}"
    )


def normalize_base_url(url: str) -> str:
    """Validate a base URL and strip its trailing slash.

    Accepts ``http://`` and ``https://`` URLs, with or without a path prefix
    (e.g. ``https://api.example.org/sylphd``).  A bare ``host:port`` gets
    ``http://`` prepended, as before.
    """
    text = url.strip()
    if not text:
        raise ValueError("empty server URL")
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise ValueError(
            f"unsupported URL scheme {parts.scheme!r}; use http:// or https://"
        )
    if not parts.hostname:
        raise ValueError(f"server URL has no host: {url!r}")
    try:
        _ = parts.port
    except ValueError as error:
        raise ValueError(f"invalid port in server URL {url!r}") from error
    if parts.query or parts.fragment:
        raise ValueError("server URL must not contain a query or fragment")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


class ClientError(RuntimeError):
    """Any failure talking to the server, including HTTP 4xx/5xx replies."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def default_timeout() -> httpx.Timeout:
    """No read timeout (profiling can be slow), 30 s to connect."""
    return httpx.Timeout(None, connect=30.0)


class SylphClient:
    """A small synchronous client.

    Either ``socket_path`` (Unix socket) or ``base_url`` (``http://host:port``,
    optionally with a path prefix) must be given.
    """

    def __init__(
        self,
        *,
        socket_path: str | os.PathLike[str] | None = None,
        base_url: str | None = None,
        timeout: httpx.Timeout | float | None = None,
        retries: int = 0,
        token: str | None = None,
    ) -> None:
        if (socket_path is None) == (base_url is None):
            raise ValueError("exactly one of socket_path and base_url is required")
        self.socket_path = Path(socket_path) if socket_path is not None else None
        self.base_url = (
            normalize_base_url(base_url) if base_url is not None else None
        )
        self.timeout = timeout if timeout is not None else default_timeout()
        #: How often a 429/503 reply is retried (after its ``Retry-After``).
        self.retries = max(0, retries)
        self.token = token or None
        self._info: dict[str, Any] | None = None
        #: ``False`` once a response said the token is not accepted, ``True``
        #: once one said it is, ``None`` while nothing is known (or no token).
        self.token_valid: bool | None = None
        transport = None
        if self.socket_path is not None:
            transport = httpx.HTTPTransport(uds=str(self.socket_path), retries=0)
        # The trailing slash matters: httpx keeps the path prefix only when
        # request paths are relative ("info", not "/info").
        origin = f"{self.base_url}/" if self.base_url is not None else "http://sylphd.local/"
        headers = {"user-agent": f"sylphd/{_version()}"}
        if token:
            # A privileged token skips the server's rate limit; never send it
            # in the clear to a remote host.
            if self.base_url is not None and self.base_url.startswith("http://"):
                host = urlsplit(self.base_url).hostname or ""
                if host not in ("localhost", "127.0.0.1", "::1"):
                    raise ValueError("refusing to send a token over plain http")
            headers["authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            transport=transport,
            base_url=origin,
            timeout=self.timeout,
            headers=headers,
        )

    # -- from files: sketch here, send the sketch ---------------------------
    #
    # The methods below take paths: they sketch on this machine with the server's
    # ``c`` and ``k`` (only the sketch, never the sequence, is uploaded) and call
    # the endpoints above.  They need pysylph (``pip install 'sylphd[client]'``).
    def database_info(self) -> dict[str, Any]:
        """The server's ``GET /info`` document, fetched once and kept (see :meth:`info`)."""
        if self._info is None:
            self._info = self.info()
        return self._info

    def sketch_parameters(self) -> tuple[int, int]:
        """``(c, k)`` to sketch with: the pooled values of the server's databases."""
        database = self.database_info().get("database") or {}
        try:
            return int(database["c"]), int(database["k"])
        except (KeyError, TypeError, ValueError):
            raise ClientError("the server does not report its sketch parameters (c, k)") from None

    def profile_reads(
        self,
        reads: Any,
        *,
        name: str | None = None,
        params: ProfileParams | None = None,
        fields: Sequence[str] | None = None,
        tax_sources: Sequence[str] | None = None,
        taxprof: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Taxonomic profile of one sample of reads (``POST /profile``).

        ``reads`` is a FASTQ/FASTA path (gzip fine), a ``(r1, r2)`` pair of paths for
        paired-end reads, a ``.sylsp`` sketch, or a :class:`sylphd.sketching.SampleInput`.
        The sample is named after the file unless ``name`` is given.  Returns the
        sample's result: its ``hits`` and, with ``taxprof``, the clade profiles.
        """
        return self._reads("profile", reads, name, params, fields, tax_sources, taxprof)

    def query_reads(
        self,
        reads: Any,
        *,
        name: str | None = None,
        params: ProfileParams | None = None,
        fields: Sequence[str] | None = None,
        tax_sources: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """ANI hits of one sample of reads (``POST /query``); ``reads`` as in :meth:`profile_reads`."""
        return self._reads("query", reads, name, params, fields, tax_sources, None)

    def identify_assembly(
        self,
        assembly: str | os.PathLike[str],
        *,
        name: str | None = None,
        top: int = 10,
        min_ani: float = 80.0,
    ) -> dict[str, Any]:
        """The closest genomes of one assembly (``POST /assembly``).

        ``assembly`` is a FASTA path (gzip fine), sketched here as one sample; only the
        sketch is sent, so it works on every server and the sequence never leaves this
        machine.  Returns ``query`` (name, records, length, GC), ``hits`` (best first, at
        most ``top``, ANI of at least ``min_ani``) and the server's interpretation.
        """
        sketching = _sketching()
        c, k = self.sketch_parameters()
        label = name or sketching.sample_name_from_path(assembly)
        sketch, stats = sketching.sketch_assembly(assembly, label, c=c, k=k)
        payload = sketch.to_bytes()
        self._check_upload(payload)
        return self.assembly_sketch(
            payload, top=top, min_ani=min_ani, name=label,
            records=stats.records, length=stats.length, gc=stats.gc,
        )

    def classify_contigs(
        self,
        assembly: str | os.PathLike[str],
        *,
        options: Any = None,
        **overrides: Any,
    ) -> Any:
        """Classify every contig of a fragmented assembly on its own (``POST /contigs``).

        Sketches each record, sends the sketches in batches that fit the server's contig and
        upload limits, and returns a :class:`sylphd.per_contig_client.PerContigRun`: its
        ``records`` (one dict per classified contig, input order), ``summary()``, and
        ``write_tsv()`` / ``document()`` for the same table and JSON the command line writes.
        Options are those of :class:`sylphd.per_contig_client.PerContigOptions` given as
        keywords: ``top``, ``min_ani``, ``min_length``, ``fields``, ``consensus_ratio``,
        ``consensus_z``, or a whole ``options=`` object.
        """
        _sketching()
        import dataclasses

        from . import per_contig_client

        options = dataclasses.replace(options or per_contig_client.PerContigOptions(), **overrides)
        return per_contig_client.run(self, assembly, options, info=self.database_info())

    def _reads(
        self,
        endpoint: str,
        reads: Any,
        name: str | None,
        params: ProfileParams | None,
        fields: Sequence[str] | None,
        tax_sources: Sequence[str] | None,
        taxprof: Sequence[str] | None,
    ) -> dict[str, Any]:
        sketching = _sketching()
        sample = _sample_input(sketching, reads, name)
        c, k = self.sketch_parameters()
        sketch = sketching.sketch_sample(sample, c=c, k=k)
        sketching.check_sketch(sketch, c=c, k=k, name=sample.name)
        payload = sketch.to_bytes()
        self._check_upload(payload)
        samples = [(sample.name, payload)]
        if endpoint == "profile":
            result = self.profile(
                samples, params=params, fields=fields, tax_sources=tax_sources, taxprof=taxprof
            )
        else:
            result = self.query(samples, params=params, fields=fields, tax_sources=tax_sources)
        return result[0]

    def _check_upload(self, payload: bytes) -> None:
        limit = int((self.database_info().get("limits") or {}).get("max_upload_mb") or 0) * 1024 * 1024
        if limit and len(payload) > limit:
            raise ClientError(
                f"the sketch is {len(payload)} bytes, over the server's "
                f"{limit // (1024 * 1024)} MB upload limit"
            )

    # -- plumbing ---------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return self._send(self._client.build_request(method, path, **kwargs))

    def _send(self, request: httpx.Request) -> httpx.Response:
        for attempt in range(self.retries + 1):
            try:
                response = self._client.send(request)
            except httpx.HTTPError as error:
                raise ClientError(self._connection_message(error)) from error
            if response.status_code not in RETRY_STATUSES or attempt == self.retries:
                break
            time.sleep(_retry_delay(response, attempt))
        self._note_token(response)
        if response.status_code >= 400:
            raise ClientError(_error_message(response), response.status_code)
        return response

    def _note_token(self, response: httpx.Response) -> None:
        """Warn (once) when the server says the token we send is not valid."""
        verdict = response.headers.get("x-token-valid")
        if verdict is None:
            return
        was = self.token_valid
        self.token_valid = verdict.strip().lower() == "true"
        if not self.token_valid and was is not False:
            where = self.base_url or f"the server on {self.socket_path}"
            warnings.warn(
                f"{where} does not accept this access token (mistyped, or replaced "
                "on the server?); you are treated as an anonymous client",
                TokenWarning,
                stacklevel=4,
            )

    def _connection_message(self, error: httpx.HTTPError) -> str:
        if self.socket_path is not None:
            return f"cannot talk to sylphd on {self.socket_path}: {error}"
        return f"cannot talk to sylphd at {self.base_url}: {error}"

    # -- endpoints --------------------------------------------------------
    def taxonomy(self, operation: str, payload: Mapping[str, Any] | None = None) -> Any:
        """Call a ``/taxonomy/<operation>`` endpoint: ``info`` is a GET, the rest POST JSON.

        Needs a server started with ``--taxonomy-db``; see :class:`sylphd.ncbitax.Taxonomy`
        for the friendly interface.
        """
        if operation == "info":
            return self._request("GET", "taxonomy/info").json()
        return self._request("POST", f"taxonomy/{operation}", json=dict(payload or {})).json()

    def health(self) -> dict[str, Any]:
        return self._request("GET", "health").json()

    def info(self) -> dict[str, Any]:
        return self._request("GET", "info").json()

    def profile(
        self,
        samples: Sequence[tuple[str, bytes]],
        *,
        params: ProfileParams | None = None,
        fields: Sequence[str] | None = None,
        tax_sources: Sequence[str] | None = None,
        taxprof: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Profile samples; ``taxprof`` asks the server to aggregate clades.

        Each returned sample then carries a ``taxprof`` object keyed by
        taxonomy (``gtdb``/``ncbi``/``tax``) with the structured clades and the
        rendered ``.sylphmpa`` text.
        """
        return self._post(
            "profile", samples, params, fields, tax_sources, taxprof
        )

    def query(
        self,
        samples: Sequence[tuple[str, bytes]],
        *,
        params: ProfileParams | None = None,
        fields: Sequence[str] | None = None,
        tax_sources: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self._post("query", samples, params, fields, tax_sources)

    def assembly(
        self,
        payload: bytes,
        *,
        top: int = 10,
        min_ani: float = 80.0,
        name: str | None = None,
        content_type: str = "application/octet-stream",
        extra_params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send one raw FASTA/FASTQ assembly to ``POST /assembly``.

        The server must run with ``--allow-sequences``.
        """
        return self._assembly(
            payload, top=top, min_ani=min_ani, name=name, content_type=content_type,
            extra_params=extra_params,
        )

    def assembly_sketch(
        self,
        sketch: bytes,
        *,
        top: int = 10,
        min_ani: float = 80.0,
        name: str | None = None,
        records: int | None = None,
        length: int | None = None,
        gc: float | None = None,
    ) -> dict[str, Any]:
        """Send the sketch (``.sylsp`` bytes) of one assembly to ``POST /assembly``.

        Works on every server.  ``records``, ``length`` and ``gc`` are
        informational: the server echoes them in ``query`` since it never sees
        the sequence.
        """
        extra = {
            key: value
            for key, value in (("records", records), ("length", length), ("gc", gc))
            if value is not None
        }
        return self._assembly(
            sketch, top=top, min_ani=min_ani, name=name,
            content_type=SKETCH_CONTENT_TYPE, extra_params=extra,
        )

    def _assembly(
        self,
        payload: bytes,
        *,
        top: int,
        min_ani: float,
        name: str | None,
        content_type: str,
        extra_params: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"top": top, "min_ani": min_ani}
        if name:
            params["name"] = name
        params.update(extra_params or {})
        response = self._request(
            "POST",
            "assembly",
            params=params,
            content=payload,
            headers={"content-type": content_type},
        )
        result = response.json()
        if not isinstance(result, dict) or "hits" not in result:
            raise ClientError("unexpected response from /assembly")
        return result

    def query_size(
        self,
        samples: Sequence[tuple[str, bytes]],
        *,
        params: ProfileParams | None = None,
        fields: Sequence[str] | None = None,
    ) -> int:
        """Exact size in bytes of the multipart body :meth:`query` would send.

        This is the prepared request's ``Content-Length`` (boundary, form
        headers, JSON ``params`` and file names included), so a caller can
        compare it with a server's upload limit instead of estimating.
        """
        request = self._prepare_post("query", samples, params, fields)
        length = request.headers.get("content-length")
        if length is None:  # pragma: no cover - bytes payloads always have one
            raise ClientError("cannot determine the request size")
        return int(length)

    # -- per contig -------------------------------------------------------
    def contigs(
        self,
        samples: Sequence[tuple[str, bytes]],
        *,
        contigs: Sequence[Mapping[str, Any]],
        options: Mapping[str, Any] | None = None,
        fields: Sequence[str] | None = None,
        tax_sources: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Classify the contigs of one fragmented assembly (``POST /contigs``).

        ``samples`` holds one ``(name, sketch bytes)`` per contig, ``contigs``
        one object per sketch in the same order with ``name``,
        ``contig_kmers`` (the record's genome-style k-mer count), ``length``
        and ``gc``.  ``options`` are the server-side scoring and reporting
        options (``top``, ``min_ani``, ``consensus_ratio``, ``consensus_z``).
        The server computes the contig-relative ANI, the ranking, the
        classification and the consensus; the response carries one result per
        contig plus a summary of the request.
        """
        if not samples:
            raise ValueError("no contigs to send")
        if len(samples) != len(contigs):
            raise ValueError(
                f"{len(contigs)} contig entries for {len(samples)} sketches"
            )
        response = self._send(
            self._prepare_contigs(samples, contigs, options, fields, tax_sources)
        )
        result = response.json()
        if not isinstance(result, dict) or "contigs" not in result:
            raise ClientError("unexpected response from /contigs")
        return result

    def contigs_size(
        self,
        samples: Sequence[tuple[str, bytes]],
        *,
        contigs: Sequence[Mapping[str, Any]],
        options: Mapping[str, Any] | None = None,
        fields: Sequence[str] | None = None,
        tax_sources: Sequence[str] | None = None,
    ) -> int:
        """Exact size in bytes of the multipart body :meth:`contigs` would send."""
        request = self._prepare_contigs(samples, contigs, options, fields, tax_sources)
        length = request.headers.get("content-length")
        if length is None:  # pragma: no cover - bytes payloads always have one
            raise ClientError("cannot determine the request size")
        return int(length)

    def _prepare_contigs(
        self,
        samples: Sequence[tuple[str, bytes]],
        contigs: Sequence[Mapping[str, Any]],
        options: Mapping[str, Any] | None,
        fields: Sequence[str] | None,
        tax_sources: Sequence[str] | None,
    ) -> httpx.Request:
        files: list[tuple[str, tuple[str, bytes, str]]] = [
            (
                "sketch",
                (f"{name or 'contig'}.sylsp", data, "application/octet-stream"),
            )
            for name, data in samples
        ]
        return self._client.build_request(
            "POST",
            "contigs",
            data={
                "params": contigs_json(
                    contigs, options=options, fields=fields, tax_sources=tax_sources
                )
            },
            files=files,
        )

    def _prepare_post(
        self,
        mode: str,
        samples: Sequence[tuple[str, bytes]],
        params: ProfileParams | None,
        fields: Sequence[str] | None,
        tax_sources: Sequence[str] | None = None,
        taxprof: Sequence[str] | None = None,
    ) -> httpx.Request:
        """Build the multipart request ``_post`` sends (shared with ``query_size``)."""
        names = [name for name, _ in samples]
        files: list[tuple[str, tuple[str, bytes, str]]] = [
            (
                "sketch",
                (f"{name or 'sample'}.sylsp", data, "application/octet-stream"),
            )
            for name, data in samples
        ]
        return self._client.build_request(
            "POST",
            mode,
            data={
                "params": params_json(
                    mode, params, names, fields, tax_sources, taxprof
                )
            },
            files=files,
        )

    def _post(
        self,
        mode: str,
        samples: Sequence[tuple[str, bytes]],
        params: ProfileParams | None,
        fields: Sequence[str] | None,
        tax_sources: Sequence[str] | None = None,
        taxprof: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not samples:
            raise ValueError("no samples to send")
        response = self._send(
            self._prepare_post(
                mode, samples, params, fields, tax_sources, taxprof
            )
        )
        result = response.json()
        if not isinstance(result, list):
            raise ClientError(f"unexpected response from /{mode}: not a JSON list")
        return result

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retry ``attempt + 1``: ``Retry-After``, but at
    least 1, 2, 4... s, and at most :data:`MAX_RETRY_DELAY`."""
    try:
        advised = float(response.headers.get("retry-after", 0))
    except ValueError:  # an HTTP date: not worth parsing
        advised = 0.0
    return min(MAX_RETRY_DELAY, max(advised, 2.0**attempt))


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        payload = None
    if isinstance(payload, Mapping) and "error" in payload:
        return f"server error ({response.status_code}): {payload['error']}"
    text = response.text.strip()
    return f"server error ({response.status_code}): {text or response.reason_phrase}"


def _version() -> str:
    from . import __version__

    return __version__


def connect(
    *,
    socket_path: str | os.PathLike[str] | None = None,
    url: str | None = None,
    timeout: float | None = None,
    retries: int = 0,
    token: str | None = None,
) -> SylphClient:
    """Build a client for a socket path or an HTTP(S) base URL / ``host:port``.

    The URL may carry a path prefix (``https://api.example.org/sylphd``);
    TLS certificates are verified as usual by httpx.
    """
    http_timeout = default_timeout() if timeout is None else httpx.Timeout(timeout, connect=30.0)
    if socket_path is not None:
        return SylphClient(
            socket_path=socket_path, timeout=http_timeout, retries=retries, token=token
        )
    if url is None:
        raise ValueError("either a socket path or a URL is required")
    return SylphClient(
        base_url=normalize_base_url(url),
        timeout=http_timeout,
        retries=retries,
        token=token,
    )
