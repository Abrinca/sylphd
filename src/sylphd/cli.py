"""Python commands of ``sylphd``: ``serve``, ``setup``, ``download``.

The query commands (``profile``, ``query``, ``assembly``, ``info``, ``render``) are not
here: ``sylphd.entry`` forwards them to the native ``sylphd-client``.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sqlite3
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from .about import About, check_name
from .cli_parser import PROG, build_parser
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
from .limits import load_token_digests
from .messages import Messages
from .metadata_build import build_metadata_db, update_metadata_db
from .paths import check_socket_path, default_socket_path, parse_tcp
from .protocol import ProfileParams
from .taxonomy_sources import TaxonomySource
from .usage import Usage

if TYPE_CHECKING:
    from .profiling import DatabaseSet, DatabaseSpec
    from .server import ServerConfig

log = logging.getLogger("sylphd")

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _print_error(message: str) -> None:
    print(f"{PROG}: error: {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


def _configure_logging(level: str) -> None:
    """Make the ``sylphd`` and ``omnitax`` loggers (load times, warnings) follow ``--log-level``."""
    for name in ("sylphd", "omnitax"):
        logger = logging.getLogger(name)
        if not logger.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter(f"{PROG}: %(message)s"))
            logger.addHandler(handler)
            logger.propagate = False
        logger.setLevel(getattr(logging, level.upper(), logging.INFO))


def cmd_serve(args: argparse.Namespace) -> int:
    # The server needs starlette, uvicorn and pysylph (the server extra). Imported
    # here so that the other Python commands work without them and entry.py can
    # turn a missing package into a friendly message.
    from .profiling import DatabaseSet
    from .server import ServerConfig, create_app
    from .server_launch import serve

    if args.socket is not None and args.tcp is not None:
        _print_error("--socket and --tcp are mutually exclusive")
        return 2
    _configure_logging(args.log_level)

    # Check --name/--about first (cheap): a bad one must fail before the
    # database is loaded.
    name = check_name(args.name)
    about = About(Path(args.about) if args.about else None)
    try:
        messages = Messages(Path(args.messages) if args.messages else None)
        specs = parse_db_specs(args.db)
        usage = Usage(Path(args.usage_db)) if args.usage_db else None
        tokens = (
            load_token_digests(args.privileged_token_file)
            if args.privileged_token_file
            else ()
        )
    except ValueError as error:
        _print_error(str(error))
        return 2
    if args.require_token and not args.privileged_token_file:
        _print_error("--require-token needs --privileged-token-file")
        return 2
    config = ServerConfig(
        db_paths=specs,
        meta_path=Path(args.meta) if args.meta else None,
        max_jobs=args.max_jobs,
        max_contigs=args.max_contigs,
        max_upload_mb=args.max_upload_mb,
        compute_hash=args.hash,
        allow_sequences=args.allow_sequences,
        max_sequence_mb=args.max_sequence_mb,
        rate_limit=args.rate_limit,
        privileged_tokens=tokens,
        require_token=args.require_token,
        taxonomy_db=Path(args.taxonomy_db) if args.taxonomy_db else None,
        taxonomy_rate_limit=args.taxonomy_rate_limit,
        max_queue=args.max_queue,
        trusted_proxies=tuple(args.trusted_proxy or ()),
        cors_origins=tuple(args.cors_origin or ()),
        name=name,
        about=about,
        messages=messages,
        usage=usage,
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
    if config.meta_path is not None:
        # /info only names the file; the operator's log keeps the full path.
        log.info("metadata database: %s", config.meta_path)
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
    from .profiling import DatabaseSpec

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
    extras = [
        f"max-jobs={config.max_jobs}",
        f"max-contigs={config.max_contigs}",
        f"max-upload={config.max_upload_mb} MB",
    ]
    if config.allow_sequences:
        extras.append(f"raw-sequence-assembly={config.max_sequence_mb} MB")
    if config.max_queue is not None:
        extras.append(f"max-queue={config.max_queue}")
    if config.rate_limit is not None:
        count, window = config.rate_limit
        extras.append(f"rate-limit={count}/{int(window)}s")
    if config.taxonomy_db is not None:
        extras.append(f"taxonomy={config.taxonomy_db.name}")
    if config.require_token:
        extras.append("token-required")
    elif config.privileged_tokens:
        extras.append("privileged-tokens")
    if config.cors_origins:
        extras.append(f"cors={','.join(config.cors_origins)}")
    if config.about.path:
        extras.append(f"about={config.about.path}")
    if config.messages.path:
        extras.append(f"messages={config.messages.path}")
    if config.usage is not None:
        extras.append(f"usage-db={config.usage.path}")
    print(
        f"{PROG}: {databases.name} ({kind}, c={databases.c}, k={databases.k}, "
        f"{databases.genomes} genomes) listening on {address} "
        f"[{', '.join(extras)}]",
        file=sys.stderr,
        flush=True,
    )


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


def _cache_directory(args: argparse.Namespace) -> Path:
    """Where ``setup`` keeps downloads: ``--cache-dir`` or next to the output."""
    return Path(args.cache_dir) if args.cache_dir else Path(args.output).parent / "taxonomy"


def _resolve_taxonomy_specs(
    values: Sequence[str],
    *,
    cache_dir: Path,
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
    for spec in specs:
        if spec.path is not None:
            sources.append(
                TaxonomySource(name=spec.name, path=spec.path, origin=str(spec.path))
            )
            continue
        entry = lookup_taxonomy(spec.name)
        path = download_taxonomy(
            entry,
            cache_dir,
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
    cache_directory = _cache_directory(args)
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
                cache_dir=cache_directory,
                quiet=args.quiet,
            )
            stats = update_metadata_db(
                source,
                args.output,
                taxonomy=sources,
                force=args.force,
                progress=callback,
                taxonomy_db=args.taxonomy_db,
            )
        else:
            if not args.metadata and not taxonomy_values:
                raise ValueError("at least one of -m/--metadata and --taxonomy is required")
            sources = _resolve_taxonomy_specs(
                taxonomy_values,
                cache_dir=cache_directory,
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
                taxonomy_db=args.taxonomy_db,
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
        ncbi = source.get("ncbi")
        if ncbi:
            print(f"    NCBI lineage: {_ncbi_counts_text(ncbi)}", file=sys.stderr)
        nearest = source.get("ncbi_nearest")
        if nearest:
            print(f"    {_nearest_counts_text(nearest)}", file=sys.stderr)
    return 0


def _ncbi_counts_text(ncbi: dict[str, Any]) -> str:
    """The per-source ``setup --taxonomy-db`` counts as one line."""
    text = (
        f"{ncbi['mapped']:,} mapped, "
        f"{ncbi['no_accession']:,} without an assembly accession, "
        f"{ncbi['not_in_summary']:,} not in the taxonomy file, "
        f"{ncbi['ambiguous']:,} ambiguous"
    )
    if ncbi.get("gtdb"):
        text += f", {ncbi['gtdb']:,} with GTDB's NCBI data"
    return text


def _nearest_counts_text(nearest: dict[str, Any]) -> str:
    """The raw nearest-taxon counts, by rank, plus how many are informal."""
    keys = int(nearest.get("keys") or 0)
    assigned = int(nearest.get("assigned") or 0)
    informal = int(nearest.get("informal") or 0)
    ranks = ", ".join(
        f"{rank} {int(count):,}" for rank, count in (nearest.get("ranks") or {}).items()
    )
    text = f"nearest NCBI taxon: {assigned:,} of {keys:,} keys without an NCBI record"
    if ranks:
        text += f" ({ranks}"
        if informal:
            text += f"; {informal:,} informal, formally named above"
        text += ")"
    elif informal:
        text += f", {informal:,} informal, formally named above"
    return text


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    commands = {
        "serve": cmd_serve,
        "download": cmd_download,
        "setup": cmd_setup,
    }
    try:
        return int(commands[args.command](args))
    except KeyboardInterrupt:
        return 130
    except (
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
