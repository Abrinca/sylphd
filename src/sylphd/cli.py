"""Command line interface: ``sylphd {serve,info,profile,query,download,setup}``."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sqlite3
import sys
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from . import per_contig
from .about import About, check_name
from .assembly import gtdb_species, hit_lineage, species_caveat
from .cli_parser import PROG, build_parser
from .client import ClientError, connect
from .download import (
    LIST_KEYWORD,
    METADATA_FILES,
    DownloadError,
    ProgressPrinter,
    VerifyPrinter,
    aria2c_path,
    database_table,
    download_database,
    download_release,
    download_taxonomy,
    format_size,
    lookup_taxonomy,
    parse_taxonomy_spec,
    taxonomy_table,
)
from .metadata import TaxonomySource, build_metadata_db, update_metadata_db
from .paths import check_socket_path, default_socket_path, parse_tcp
from .profiling import DatabaseSet, DatabaseSpec
from .protocol import ProfileParams, ProtocolError, rows_to_tsv_lines
from .server import ServerConfig, create_app, serve
from .sketching import (
    InputError,
    SampleInput,
    build_sample_inputs,
    check_sketch,
    sample_name_from_path,
    sketch_assembly,
    sketch_sample,
)
from .taxonomy import (
    TaxonomyError,
    default_taxonomies,
    parse_taxonomies,
    resolve_taxonomy_columns,
    write_outputs,
)

log = logging.getLogger("sylphd")

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _print_error(message: str) -> None:
    print(f"{PROG}: error: {message}", file=sys.stderr)


def _check_writable_dir(directory: Path) -> None:
    if not directory.is_dir():
        raise OSError(f"cannot write into {directory}: not a directory")
    if not os.access(directory, os.W_OK | os.X_OK):
        raise OSError(f"cannot write into {directory}: permission denied")


def _check_output_file(path: str | None) -> None:
    """Fail on an unwritable ``-o`` before the expensive work, not after it."""
    if path is None or path == "-":
        return
    if os.path.isdir(path):
        raise OSError(f"cannot write {path}: is a directory")
    _check_writable_dir(Path(path).parent)


def _check_output_prefix(prefix: str | None) -> None:
    """Same for a ``--taxprof``/``--taxmatrix`` prefix (see ``write_outputs``)."""
    if prefix is not None:
        directory = Path(prefix) if os.path.isdir(prefix) else Path(prefix + "x").parent
        _check_writable_dir(directory)


@contextmanager
def _open_output(path: str | None) -> Iterator[Any]:
    """stdout for ``None``/``-``, else the file ``path``; flushed on exit."""
    if path is None or path == "-":
        try:
            yield sys.stdout
        finally:
            sys.stdout.flush()
    else:
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            yield handle


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


def _configure_logging(level: str) -> None:
    """Make the ``sylphd`` logger (load times, warnings) follow ``--log-level``."""
    logger = logging.getLogger("sylphd")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(f"{PROG}: %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))


def cmd_serve(args: argparse.Namespace) -> int:
    if args.socket is not None and args.tcp is not None:
        _print_error("--socket and --tcp are mutually exclusive")
        return 2
    _configure_logging(args.log_level)

    # Check --name/--about first (cheap): a bad one must fail before the
    # database is loaded.
    name = check_name(args.name)
    about = About(Path(args.about) if args.about else None)
    try:
        specs = parse_db_specs(args.db)
    except ValueError as error:
        _print_error(str(error))
        return 2
    config = ServerConfig(
        db_paths=specs,
        meta_path=Path(args.meta) if args.meta else None,
        max_jobs=args.max_jobs,
        max_upload_mb=args.max_upload_mb,
        compute_hash=args.hash,
        allow_sequences=args.allow_sequences,
        max_sequence_mb=args.max_sequence_mb,
        rate_limit=args.rate_limit,
        max_queue=args.max_queue,
        trusted_proxies=tuple(args.trusted_proxy or ()),
        cors_origins=tuple(args.cors_origin or ()),
        name=name,
        about=about,
    )
    socket_path = None
    tcp = None
    if args.tcp is not None:
        try:
            tcp = parse_tcp(args.tcp)
        except ValueError as error:
            _print_error(str(error))
            return 2
    else:
        try:
            socket_path = check_socket_path(args.socket or default_socket_path())
        except ValueError as error:
            _print_error(str(error))
            return 2
        config.socket_path = socket_path

    databases = DatabaseSet.load(config.db_paths, compute_hash=config.compute_hash)
    # Warm the default profiler: its construction scans every genome when
    # there are two or more databases, which must not happen in a request.
    started = time.perf_counter()
    databases.profiler(ProfileParams())
    profiler_seconds = time.perf_counter() - started
    app = create_app(config, database=databases)
    _log_startup(databases, config, socket_path, tcp)
    log.info("profiler ready in %.2f s", profiler_seconds)
    try:
        serve(
            config,
            socket_path=socket_path,
            tcp=tcp,
            log_level=args.log_level,
            app=app,
        )
    except KeyboardInterrupt:
        pass
    return 0


def parse_db_specs(values: Sequence[str]) -> tuple[DatabaseSpec, ...]:
    """Turn the repeated ``--db`` values into specs, rejecting duplicate names."""
    specs = tuple(DatabaseSpec.parse(value) for value in values)
    if not specs:
        raise ValueError("at least one --db is required")
    seen: dict[str, Path] = {}
    for spec in specs:
        if spec.display_name in seen:
            raise ValueError(
                f"two databases are both named {spec.display_name!r} "
                f"({seen[spec.display_name]} and {spec.path}); "
                "give one an alias with --db NAME=PATH"
            )
        seen[spec.display_name] = spec.path
    return specs


def _log_startup(
    databases: DatabaseSet,
    config: ServerConfig,
    socket_path: Path | None,
    tcp: tuple[str, int] | None,
) -> None:
    if socket_path is not None:
        address = f"unix:{socket_path}"
    else:
        assert tcp is not None
        address = f"http://{tcp[0]}:{tcp[1]}"
    kind = "two-stage" if databases.two_stage else "plain"
    extras = [f"max-jobs={config.max_jobs}", f"max-upload={config.max_upload_mb} MB"]
    if config.allow_sequences:
        extras.append(f"raw-sequence-assembly={config.max_sequence_mb} MB")
    if config.max_queue is not None:
        extras.append(f"max-queue={config.max_queue}")
    if config.rate_limit is not None:
        count, window = config.rate_limit
        extras.append(f"rate-limit={count}/{int(window)}s")
    if config.cors_origins:
        extras.append(f"cors={','.join(config.cors_origins)}")
    if config.about.path:
        extras.append(f"about={config.about.path}")
    print(
        f"{PROG}: {databases.name} ({kind}, c={databases.c}, k={databases.k}, "
        f"{databases.genomes} genomes) listening on {address} "
        f"[{', '.join(extras)}]",
        file=sys.stderr,
        flush=True,
    )


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------


def cmd_info(args: argparse.Namespace) -> int:
    with _client_from_args(args) as client:
        info = client.info()
    if args.json:
        json.dump(info, sys.stdout, indent=2, sort_keys=False)
        sys.stdout.write("\n")
        return 0

    database = info.get("database", {})
    print(f"database: {database.get('name')}")
    print(f"  path: {database.get('path')}")
    print(f"  size: {format_size(int(database.get('size') or 0))}")
    print(f"  genomes: {database.get('genomes')}")
    print(
        "  c: {c}  k: {k}  screen_c: {screen_c}  two-stage: {two_stage}".format(
            c=database.get("c"),
            k=database.get("k"),
            screen_c=database.get("screen_c") if database.get("screen_c") is not None else "-",
            two_stage="yes" if database.get("two_stage") else "no",
        )
    )
    print(f"  sha256: {database.get('sha256') or '-'}")

    databases = info.get("databases") or []
    if len(databases) > 1:
        print(f"databases: {len(databases)}")
        for entry in databases:
            kind = "two-stage" if entry.get("two_stage") else "plain"
            print(
                "  {name}: {file} ({kind}, c={c}, k={k}, {genomes} genomes)".format(
                    name=entry.get("name"),
                    file=entry.get("file"),
                    kind=kind,
                    c=entry.get("c"),
                    k=entry.get("k"),
                    genomes=entry.get("genomes"),
                )
            )

    metadata = info.get("metadata")
    if metadata is None:
        print("metadata: none")
    else:
        print(
            f"metadata: {metadata.get('path')} "
            f"(release {metadata.get('release') or '?'}, "
            f"{metadata.get('rows')} rows, built {metadata.get('built') or '?'})"
        )
        print(f"  columns: {len(metadata.get('columns') or [])}")
        sources = metadata.get("taxonomy_sources") or []
        if sources:
            print(f"  taxonomy sources: {len(sources)}")
            for source in sources:
                rows = source.get("rows")
                details = [f"{rows:,} keys" if rows is not None else "key count unknown"]
                if source.get("kind") == "metadata":
                    details.append("virtual, from -m")
                if source.get("has_host"):
                    details.append("with hosts")
                if source.get("md5"):
                    details.append(f"md5 {source['md5']}")
                overlaps = source.get("overlaps") or {}
                if overlaps:
                    details.append(
                        "overlaps "
                        + ", ".join(
                            f"{name} ({int(keys):,} keys)"
                            for name, keys in overlaps.items()
                        )
                    )
                print(f"    {source.get('name')}: " + ", ".join(details))

    limits = info.get("limits", {})
    print(
        f"versions: sylphd {info.get('version')}, pysylph {info.get('pysylph_version')}, "
        f"python {info.get('python_version')}"
    )
    print(
        f"limits: max-jobs {limits.get('max_jobs')}, "
        f"max-upload {limits.get('max_upload_mb')} MB, "
        f"max-sketches {limits.get('max_sketches')}"
    )
    rate_limit = limits.get("rate_limit")
    rate = (
        "off"
        if not rate_limit
        else f"{rate_limit['count']}/{int(rate_limit['window_seconds'])}s"
    )
    sequences = (
        f"on (max {limits.get('max_sequence_mb')} MB)"
        if limits.get("allow_sequences")
        else "off"
    )
    print(
        f"web: raw-sequence assembly {sequences}, "
        f"rate-limit {rate}, "
        f"max-queue {limits.get('max_queue') if limits.get('max_queue') is not None else 'unlimited'}"
    )
    return 0


# ---------------------------------------------------------------------------
# profile / query
# ---------------------------------------------------------------------------


def cmd_contain(args: argparse.Namespace) -> int:
    mode = args.command  # "profile" or "query"
    try:
        samples = build_sample_inputs(
            files=args.files,
            reads=args.reads,
            first=args.first,
            second=args.second,
            interleaved=args.interleaved,
            specs=args.sample,
        )
    except InputError as error:
        _print_error(str(error))
        return 2

    if args.sample_name is not None:
        if len(samples) != 1:
            _print_error("--sample-name requires exactly one input sample")
            return 2
        try:
            samples = [replace(samples[0], name=args.sample_name)]
        except InputError as error:
            _print_error(str(error))
            return 2

    params = ProfileParams(
        minimum_ani=args.minimum_ani,
        seq_id=args.seq_id,
        estimate_unknown=args.estimate_unknown,
        # ``profile`` only, like the taxonomy flags.
        estimate_read_counts=getattr(args, "estimate_read_counts", False),
        min_number_kmers=args.min_number_kmers,
        min_contain=args.min_contain,
        screen_ani=args.screen_ani,
        no_adjust=args.no_adjust,
    )

    taxonomy_prefix = getattr(args, "taxprof", None)
    matrix_prefix = getattr(args, "taxmatrix", None)
    write_taxonomy = mode == "profile" and (
        taxonomy_prefix is not None or matrix_prefix is not None
    )
    taxonomies: tuple[str, ...] = ()
    tax_sources: tuple[str, ...] = ()

    # Everything that can fail without the server is checked before sketching.
    _check_output_file(args.output)
    try:
        if getattr(args, "tax_source", None) and not write_taxonomy:
            raise TaxonomyError(
                "--tax-source needs --taxprof or --taxmatrix: it restricts the "
                "sources of the aggregated 'tax' profile"
            )
        if write_taxonomy:
            # A bad --taxonomy must fail before the server is contacted; only
            # the *default* depends on what the server has.
            requested_taxonomies = (
                None if args.taxonomy is None else parse_taxonomies(args.taxonomy)
            )
            _check_output_prefix(taxonomy_prefix)
            _check_output_prefix(matrix_prefix)
        else:
            requested_taxonomies = None
        with _client_from_args(args) as client:
            info = client.info()
            database = info["database"]
            metadata = info.get("metadata")
            fields = _resolve_fields(args.fields, metadata)
            metadata_columns = list((metadata or {}).get("columns") or [])
            if write_taxonomy:
                taxonomies = (
                    requested_taxonomies
                    if requested_taxonomies is not None
                    else default_taxonomies(metadata)
                )
                tax_sources = _select_tax_sources(
                    getattr(args, "tax_source", None) or (), taxonomies, metadata
                )
                request_fields = _request_fields(fields, taxonomies, metadata)
            else:
                request_fields = fields
            if args.verbose:
                print(
                    f"{PROG}: database {database['name']} (c={database['c']}, "
                    f"k={database['k']}); sketching {len(samples)} sample(s) "
                    f"with {args.threads} thread(s)",
                    file=sys.stderr,
                )
            sketches = _sketch_all(
                samples, c=int(database["c"]), k=int(database["k"]), threads=args.threads,
                verbose=args.verbose,
            )
            limit = int(info["limits"]["max_upload_mb"]) * 1024 * 1024
            total = sum(len(payload) for _, payload in sketches)
            if total > limit:
                raise ClientError(
                    f"sketches total {format_size(total)}, above the server's "
                    f"{format_size(limit)} upload limit; send fewer samples per request"
                )
            if args.verbose:
                print(
                    f"{PROG}: sending {len(sketches)} sketch(es), "
                    f"{format_size(total)}",
                    file=sys.stderr,
                )
            if mode == "profile":
                result = client.profile(
                    sketches,
                    params=params,
                    fields=request_fields,
                    tax_sources=tax_sources or None,
                )
            else:
                result = client.query(sketches, params=params, fields=request_fields)
    except (ClientError, InputError, ProtocolError, TaxonomyError) as error:
        _print_error(str(error))
        return 1

    _write_result(result, mode, params, fields, args, metadata_columns=metadata_columns)
    if write_taxonomy:
        written = write_outputs(
            result,
            taxonomies=taxonomies,
            taxprof_prefix=taxonomy_prefix,
            taxmatrix_prefix=matrix_prefix,
            matrix_value=getattr(args, "taxmatrix_value", "relative"),
            tax_sources=tax_sources,
        )
        for path in written:
            print(f"{PROG}: wrote {path}", file=sys.stderr)
        if "tax" in taxonomies:
            # The 95 % threshold is a prokaryote convention: say so once per
            # source the merged profile actually used (design note B6).
            for note in _profile_caveats(result):
                print(f"{PROG}: warning: {note}", file=sys.stderr)
    return 0


def _profile_caveats(samples: Sequence[dict[str, Any]]) -> list[str]:
    """Distinct species-threshold caveats of a merged ``tax`` profile.

    One line per non-GTDB source, however many rows it has.  The rows carry
    ``tax_source``/``tax_lineage`` because ``--taxonomy tax`` requests them
    (``--fields`` does not matter).
    """
    notes: list[str] = []
    for sample in samples:
        for row in sample.get("rows") or ():
            note = species_caveat(hit_lineage(row))
            if note and note not in notes:
                notes.append(note)
    return notes


def _client_from_args(args: argparse.Namespace):
    timeout = args.timeout
    if getattr(args, "url", None) is not None:
        return connect(url=args.url, timeout=timeout)
    if getattr(args, "tcp", None) is not None:
        return connect(url=args.tcp, timeout=timeout)
    if getattr(args, "socket", None) is not None:
        return connect(socket_path=args.socket, timeout=timeout)
    env_url = os.environ.get("SYLPHD_URL")
    if env_url:
        return connect(url=env_url, timeout=timeout)
    env_socket = os.environ.get("SYLPHD_SOCKET")
    if env_socket:
        return connect(socket_path=env_socket, timeout=timeout)
    return connect(socket_path=default_socket_path(), timeout=timeout)


def _resolve_fields(text: str | None, metadata: dict[str, Any] | None) -> list[str] | None:
    """Turn ``--fields`` into the list of metadata columns to request."""
    available = list((metadata or {}).get("columns") or [])
    raw = (text or "all").strip()
    if raw.lower() in {"all", ""}:
        if metadata is not None:
            return list((metadata or {}).get("columns") or [])
        return []
    if raw.lower() in {"none", "-"}:
        return []
    if metadata is None:
        raise ClientError("the server has no metadata database; --fields is not available")
    requested = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = [name for name in requested if name not in available]
    if unknown:
        raise ClientError(
            "unknown metadata column(s): "
            + ", ".join(unknown)
            + f" (available: {', '.join(available)})"
        )
    return requested


def _select_tax_sources(
    values: Sequence[str],
    taxonomies: Sequence[str],
    metadata: dict[str, Any] | None,
) -> tuple[str, ...]:
    """The sources ``--tax-source`` restricts to, in the file's ``ord`` order.

    Without the flag that is every source (sylph-tax given every file).  The
    order is not the order of the flags: sylph-tax resolves a key found in
    several files by the order it was given them, which here is the ``setup``
    order of the metadata file, so that is what the header must list too.
    """
    if "tax" not in taxonomies:
        if values:
            raise TaxonomyError(
                "--tax-source only applies to '--taxonomy tax' (it restricts "
                "which sources the merged profile uses)"
            )
        return ()
    available = [
        str(source["name"])
        for source in (metadata or {}).get("taxonomy_sources") or ()
    ]
    if not values:
        return tuple(available)
    unknown = [name for name in values if name not in available]
    if unknown:
        raise TaxonomyError(
            "unknown taxonomy source(s): "
            + ", ".join(unknown)
            + f" (available: {', '.join(available) or 'none'})"
        )
    selected = set(values)
    return tuple(name for name in available if name in selected)


def _request_fields(
    fields: Sequence[str],
    taxonomies: Sequence[str],
    metadata: dict[str, Any] | None,
) -> list[str]:
    """Metadata columns to request: the displayed ones plus the taxonomies.

    ``--taxprof``/``--taxmatrix`` need ``gtdb_taxonomy``/``ncbi_taxonomy`` even
    when ``--fields`` hides them from the TSV, so ask for them explicitly.
    """
    extra = resolve_taxonomy_columns(taxonomies, metadata)
    return list(dict.fromkeys([*fields, *extra]))


def _sketch_all(
    samples: Sequence[SampleInput],
    *,
    c: int,
    k: int,
    threads: int,
    verbose: bool = False,
) -> list[tuple[str, bytes]]:
    """Sketch every sample, in parallel, keeping the input order."""

    def sketch_one(sample: SampleInput) -> tuple[str, bytes]:
        sketch = sketch_sample(sample, c=c, k=k)
        check_sketch(sketch, c=c, k=k, name=sample.name)
        payload = sketch.to_bytes()
        if verbose:
            print(
                f"{PROG}: {sample.name}: sketch c={sketch.c} k={sketch.k} "
                f"({format_size(len(payload))})",
                file=sys.stderr,
            )
        return sample.name, payload

    workers = max(1, min(threads, len(samples)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(sketch_one, samples))


def _write_result(
    samples: Sequence[dict[str, Any]],
    mode: str,
    params: ProfileParams,
    fields: Sequence[str] | None,
    args: argparse.Namespace,
    *,
    metadata_columns: Sequence[str] | None = None,
) -> None:
    with _open_output(args.output) as out:
        if args.format == "json":
            json.dump(samples, out, indent=None)
            out.write("\n")
        else:
            for line in rows_to_tsv_lines(
                samples,
                mode,
                params=params,
                fields=fields,
                metadata_columns=metadata_columns,
            ):
                out.write(line + "\n")


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


def cmd_assembly(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if args.per_contig:
        return _cmd_assembly_per_contig(args, path)
    if args.fields is not None:
        _print_error("--fields is only available with --per-contig")
        return 2
    if args.min_contig_length is not None:
        _print_error("--min-contig-length is only available with --per-contig")
        return 2
    if args.consensus_ratio is not None:
        _print_error("--consensus-ratio is only available with --per-contig")
        return 2
    if args.consensus_z is not None:
        _print_error("--consensus-z is only available with --per-contig")
        return 2

    name = args.name or sample_name_from_path(path.name)
    top = args.top if args.top is not None else 10
    _check_output_file(args.output)
    with _client_from_args(args) as client:
        if args.local_sketch:
            # The assembly is sketched here and only the sketch is uploaded: the sequence
            # never leaves this machine and has no size limit.
            info = client.info()
            database = info["database"]
            sketch, stats = sketch_assembly(
                path, name, c=int(database["c"]), k=int(database["k"])
            )
            payload = sketch.to_bytes()
            limit = int((info.get("limits") or {}).get("max_upload_mb") or 0) * 1024 * 1024
            if limit and len(payload) > limit:
                raise ClientError(
                    f"the sketch is {len(payload)} bytes, over the server's "
                    f"{limit // (1024 * 1024)} MB upload limit"
                )
            result = client.assembly_sketch(
                payload,
                top=top,
                min_ani=args.min_ani,
                name=name,
                records=stats.records,
                length=stats.length,
                gc=stats.gc,
            )
        else:
            # --no-local-sketch: raw upload, which only servers with --allow-sequences take
            try:
                payload = path.read_bytes()
            except OSError as error:
                _print_error(f"cannot read {path}: {error}")
                return 1
            content_type = (
                "application/gzip" if payload[:2] == b"\x1f\x8b" else "application/octet-stream"
            )
            result = client.assembly(
                payload,
                top=top,
                min_ani=args.min_ani,
                name=name,
                content_type=content_type,
            )
    if args.json:
        with _open_output(args.output) as output:
            json.dump(result, output, indent=None)
            output.write("\n")
    else:
        with _open_output(args.output) as output:
            output.write(
                "rank\tani\trelation\tshared_rank\taccession\ttaxon\tgtdb_species\t"
                "taxonomy_source\tncbi_organism_name\tncbi_taxid\n"
            )
            for hit in result.get("hits", ()):
                output.write(
                    "\t".join(
                        [
                            str(hit.get("rank", "")),
                            f"{float(hit.get('ani', 0.0)):.2f}",
                            str(hit.get("relation") or ""),
                            str(hit.get("shared_rank") or ""),
                            str(hit.get("accession") or ""),
                            str(_taxon(hit) or ""),
                            gtdb_species(hit.get("gtdb_taxonomy")),
                            str(hit.get("taxonomy_source") or ""),
                            str(hit.get("ncbi_organism_name") or ""),
                            "" if hit.get("ncbi_taxid") is None else str(hit["ncbi_taxid"]),
                        ]
                    )
                    + "\n"
                )
    # JSON is a machine-readable document and has always been silent on stderr.
    if not args.json and not args.quiet:
        print(_assignment_summary(result), file=sys.stderr)
        note = _species_note(result)
        if note:
            print(f"{PROG}: warning: {note}", file=sys.stderr)
    return 0


def _taxon(hit: dict[str, Any]) -> str | None:
    """The hit's last named rank from the additive ``taxonomy`` object."""
    taxonomy = hit.get("taxonomy")
    if not isinstance(taxonomy, dict) or not taxonomy:
        return None
    return str(next(reversed(taxonomy.values())))


def _species_note(result: dict[str, Any]) -> str | None:
    """The 95 % caveat of the best hit, or ``None`` for GTDB/no lineage (B6).

    ``classify_hits`` puts it on ``assignment`` when the query was assigned and
    on ``closest`` otherwise.
    """
    for key in ("assignment", "closest"):
        summary = result.get(key)
        if isinstance(summary, dict) and summary.get("note"):
            return str(summary["note"])
    return None


def _assignment_summary(result: dict[str, Any]) -> str:
    """One human line for stderr: the species assignment, or its absence."""
    assignment = result.get("assignment")
    if assignment:
        taxon = assignment.get("taxon") or assignment.get("accession") or "the best hit"
        if assignment.get("note"):
            # Not GTDB: the 95 % threshold is a heuristic there, and the taxon is
            # the last named rank of the source, which need not be a species.
            return (
                f"assigned to {taxon} at the 95 % species threshold "
                f"({float(assignment['ani']):.2f} % ANI)"
            )
        return f"assigned to species {taxon} ({float(assignment['ani']):.2f} % ANI)"
    closest = result.get("closest")
    if closest:
        taxon = closest.get("taxon") or closest.get("accession") or "the best hit"
        return (
            f"no species-level match (< {(result.get('thresholds') or {}).get('species', 95):g} % ANI); "
            f"closest relative {taxon} ({float(closest['ani']):.2f} % ANI)"
        )
    return "no hits"


# ---------------------------------------------------------------------------
# assembly --per-contig
# ---------------------------------------------------------------------------


def _cmd_assembly_per_contig(args: argparse.Namespace, path: Path) -> int:
    """Validate the per-contig options and hand the workflow to ``per_contig``."""
    if not args.local_sketch:
        _print_error(
            "--per-contig always sketches locally; --no-local-sketch cannot be used with it"
        )
        return 2
    if args.name is not None:
        _print_error(
            "--name cannot be combined with --per-contig: every contig is a sample "
            "named after its record id"
        )
        return 2

    min_length = (
        per_contig.DEFAULT_MIN_LENGTH
        if args.min_contig_length is None
        else args.min_contig_length
    )
    _check_output_file(args.output)
    with _client_from_args(args) as client:
        info = client.info()
        metadata = info.get("metadata")
        options = per_contig.PerContigOptions(
            top=args.top if args.top is not None else per_contig.DEFAULT_TOP,
            min_ani=args.min_ani,
            min_length=min_length,
            fields=tuple(_resolve_fields(args.fields, metadata) or ()),
            consensus_ratio=(
                per_contig.DEFAULT_CONSENSUS_RATIO
                if args.consensus_ratio is None
                else args.consensus_ratio
            ),
            consensus_z=(
                per_contig.DEFAULT_CONSENSUS_Z
                if args.consensus_z is None
                else args.consensus_z
            ),
        )
        result = per_contig.run(client, path, options, info=info)

    if args.json:
        with _open_output(args.output) as output:
            json.dump(result.document(path), output, indent=None)
            output.write("\n")
    else:
        with _open_output(args.output) as output:
            result.write_tsv(output)
    # JSON is a machine-readable document and is silent on stderr, exactly as
    # in single-assembly mode; its summary is part of the document.
    if not args.json and not args.quiet:
        result.print_summary()
    return 0


# ---------------------------------------------------------------------------
# download / setup
# ---------------------------------------------------------------------------


def cmd_download(args: argparse.Namespace) -> int:
    databases: list[str] = list(args.database or [])
    taxonomies: list[str] = list(getattr(args, "taxonomy", None) or [])
    urls: list[str] = list(args.url or [])
    list_databases = args.list_databases or LIST_KEYWORD in databases
    list_taxonomies = getattr(args, "list_taxonomies", False) or LIST_KEYWORD in taxonomies
    if list_databases or list_taxonomies:
        if list_databases:
            print(database_table())
        if list_taxonomies:
            if list_databases:
                print()
            print(taxonomy_table())
        return 0
    if urls and len(databases) > 1:
        _print_error("--url can only be combined with a single --database")
        return 1

    progress = ProgressPrinter(sys.stderr)
    paths: list[Path] = []

    if databases or urls or taxonomies:
        use_aria2c = args.aria2c and aria2c_path() is not None
        jobs: list[tuple[str, tuple[str, ...]]] = []
        if databases:
            jobs = [(name, tuple(urls)) for name in databases]
        elif urls:
            for url in urls:
                name = os.path.basename(urlparse(url).path)
                if not name:
                    _print_error(f"could not derive a file name from --url {url}")
                    return 1
                jobs.append((name, (url,)))
        names = [name for name, _ in jobs]
        if len(set(names)) != len(names):
            duplicate = next(name for name in names if names.count(name) > 1)
            _print_error(f"several downloads would write the same file: {duplicate}")
            return 1
        try:
            for name, sources in jobs:
                paths.append(
                    download_database(
                        name,
                        args.outdir,
                        urls=sources,
                        verify=args.verify,
                        force=args.force,
                        use_aria2c=use_aria2c,
                        progress=None if use_aria2c else progress,
                        verify_progress=VerifyPrinter(sys.stderr),
                    )
                )
            for name in taxonomies:
                paths.append(
                    download_taxonomy(
                        name,
                        args.outdir,
                        force=args.force,
                        progress=progress,
                    )
                )
        except (DownloadError, ValueError, OSError, httpx.HTTPError) as error:
            _print_error(str(error))
            return 1
    else:
        domains = args.only or list(METADATA_FILES)
        try:
            paths = download_release(
                release=args.release,
                outdir=args.outdir,
                domains=domains,
                base_url=args.base_url,
                force=args.force,
                progress=progress,
            )
        except (DownloadError, ValueError, OSError, httpx.HTTPError) as error:
            _print_error(str(error))
            return 1

    for path in paths:
        print(path)
    return 0


def _resolve_taxonomy_specs(
    values: Sequence[str],
    *,
    cache_dir: str | None,
    output: str,
    quiet: bool,
) -> list[TaxonomySource]:
    """Turn ``--taxonomy`` values into sources, downloading bare names."""
    specs = [parse_taxonomy_spec(value) for value in values]
    seen: dict[str, str] = {}
    for spec in specs:
        if spec.name in seen:
            raise ValueError(
                f"two taxonomy sources are both named {spec.name!r} "
                f"({seen[spec.name]} and {spec.text})"
            )
        seen[spec.name] = spec.text

    sources: list[TaxonomySource] = []
    directory = Path(cache_dir) if cache_dir else Path(output).parent / "taxonomy"
    for spec in specs:
        if spec.path is not None:
            sources.append(
                TaxonomySource(name=spec.name, path=spec.path, origin=str(spec.path))
            )
            continue
        entry = lookup_taxonomy(spec.name)
        path = download_taxonomy(
            entry,
            directory,
            progress=None if quiet else ProgressPrinter(sys.stderr),
        )
        sources.append(
            TaxonomySource(
                name=entry.name, path=path, origin=entry.mirrors[0], from_catalogue=True
            )
        )
    return sources


def cmd_setup(args: argparse.Namespace) -> int:
    printed = {"last": ""}

    def progress(seen: int, path: Path) -> None:
        line = f"{path.name}: {seen:,} rows read"
        if line != printed["last"]:
            printed["last"] = line
            print(f"\r{line}", end="", file=sys.stderr, flush=True)

    callback = None if args.quiet else progress
    taxonomy_values = list(getattr(args, "taxonomy", None) or [])
    update = getattr(args, "update", False)
    try:
        if update:
            if args.reps_only:
                raise ValueError("--reps-only cannot be used with --update")
            if args.release is not None:
                raise ValueError("--release cannot be used with --update")
            if args.metadata and len(args.metadata) > 1:
                raise ValueError("--update copies exactly one metadata database")
            source = args.metadata[0] if args.metadata else args.output
            sources = _resolve_taxonomy_specs(
                taxonomy_values,
                cache_dir=args.cache_dir,
                output=args.output,
                quiet=args.quiet,
            )
            stats = update_metadata_db(
                source,
                args.output,
                taxonomy=sources,
                force=args.force,
                progress=callback,
            )
        else:
            if not args.metadata and not taxonomy_values:
                raise ValueError("at least one of -m/--metadata and --taxonomy is required")
            sources = _resolve_taxonomy_specs(
                taxonomy_values,
                cache_dir=args.cache_dir,
                output=args.output,
                quiet=args.quiet,
            )
            stats = build_metadata_db(
                files=args.metadata or (),
                output=args.output,
                release=args.release,
                force=args.force,
                reps_only=args.reps_only,
                progress=callback,
                taxonomy=sources,
            )
    except (DownloadError, OSError, ValueError, httpx.HTTPError) as error:
        _print_error(str(error))
        return 1
    if not args.quiet:
        print(file=sys.stderr)
    taxonomy_sources = stats["taxonomy_sources"]  # type: ignore[union-attr]
    details = [f"{int(stats['rows']):,} genomes"]  # type: ignore[call-overload]
    if taxonomy_sources:
        details.append(f"{len(taxonomy_sources)} taxonomy source(s)")
    if args.reps_only and not update:
        details.append("species representatives only")
    print(
        f"wrote {stats['path']} ({format_size(int(stats['size']))}, "  # type: ignore[call-overload]
        f"format {stats.get('format_version', 3)}, " + ", ".join(details) + ")",
        file=sys.stderr,
    )
    for source in stats["sources"]:  # type: ignore[union-attr]
        print(
            f"  {source['file']}: {source['rows']:,} rows",
            file=sys.stderr,
        )
    for name in stats.get("skipped_taxonomy", ()):  # type: ignore[union-attr]
        print(
            f"  {name}: skipped, the metadata of this GTDB release is already "
            "the virtual source (the sylph-tax file has the same lineages)",
            file=sys.stderr,
        )
    for source in stats["taxonomy_sources"]:  # type: ignore[union-attr]
        host = ", with hosts" if source["has_host"] else ""
        print(
            f"  {source['name']}: {source['rows']:,} keys{host} ({source['kind']})",
            file=sys.stderr,
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    commands = {
        "serve": cmd_serve,
        "info": cmd_info,
        "profile": cmd_contain,
        "query": cmd_contain,
        "assembly": cmd_assembly,
        "download": cmd_download,
        "setup": cmd_setup,
    }
    try:
        return int(commands[args.command](args))
    except KeyboardInterrupt:
        return 130
    except (
        ClientError,
        InputError,
        ProtocolError,
        OSError,
        ValueError,
        RuntimeError,
        sqlite3.Error,
        csv.Error,
    ) as error:
        _print_error(str(error))
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
