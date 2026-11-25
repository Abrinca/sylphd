"""Python commands of ``sylphd``: ``serve``, ``setup``, ``download``, ``taxdb``, ``taxonomy``.

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
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from . import parity, taxmatrix
from .about import About, check_name
from .cli_parser import PROG, build_parser
from .client import ClientError, TokenWarning, connect
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
from .ncbitax import Taxonomy
from .paths import check_socket_path, default_socket_path, parse_tcp
from .protocol import ProfileParams, ProtocolError
from .samples import InputError
from .taxdb import build_taxdb
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
    """Make the ``sylphd`` logger (load times, warnings) follow ``--log-level``."""
    logger = logging.getLogger("sylphd")
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
# taxonomy client plumbing (the query commands live in sylphd-client)
# ---------------------------------------------------------------------------


def _client_token(args: argparse.Namespace) -> str | None:
    """The privileged bearer token: ``--token-file``, else ``$SYLPHD_TOKEN``."""
    path = getattr(args, "token_file", None)
    if path is not None:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise SystemExit(f"{PROG}: error: cannot read token file {path}: {error}")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        if not lines:
            raise SystemExit(f"{PROG}: error: token file {path} is empty")
        return lines[0]
    return os.environ.get("SYLPHD_TOKEN", "").strip() or None


def _client_from_args(args: argparse.Namespace):
    options = {
        "timeout": args.timeout,
        "retries": args.retries,
        "token": _client_token(args),
    }
    if getattr(args, "url", None) is not None:
        return connect(url=args.url, **options)
    if getattr(args, "tcp", None) is not None:
        return connect(url=args.tcp, **options)
    if getattr(args, "socket", None) is not None:
        return connect(socket_path=args.socket, **options)
    env_url = os.environ.get("SYLPHD_URL")
    if env_url:
        return connect(url=env_url, **options)
    env_socket = os.environ.get("SYLPHD_SOCKET")
    if env_socket:
        return connect(socket_path=env_socket, **options)
    return connect(socket_path=default_socket_path(), **options)


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
    ncbi_cache = cache_directory / "ncbi" if args.ncbi_taxonomy else None
    download_progress = None if args.quiet else ProgressPrinter(sys.stderr)
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
                ncbi_cache=ncbi_cache,
                download_progress=download_progress,
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
                ncbi_cache=ncbi_cache,
                download_progress=download_progress,
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
    """The per-source ``setup --ncbi-taxonomy`` counts as one line."""
    text = (
        f"{ncbi['mapped']:,} mapped, "
        f"{ncbi['no_accession']:,} without an assembly accession, "
        f"{ncbi['not_in_summary']:,} not in the summaries, "
        f"{ncbi['no_lineage']:,} without a lineage"
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


def cmd_taxdb(args: argparse.Namespace) -> int:
    """``sylphd taxdb build``: write the taxonomy file."""
    log = None if args.quiet else (lambda message: print(f"{PROG}: {message}", file=sys.stderr))
    info = build_taxdb(
        args.output,
        taxdump=args.taxdump,
        force=args.force,
        fuzzy=not args.no_fuzzy,
        cache_dir=args.cache_dir,
        log=log,
        progress=None if args.quiet else ProgressPrinter(sys.stderr),
    )
    if log is not None:
        log(
            f"wrote {args.output}: {info['taxa']:,} taxa, {info['names']:,} names, "
            f"taxdump of {info['source_date']}"
        )
    return 0


_TAXON_HEADER = ("taxid", "name", "rank", "parent", "genetic_code", "mito_genetic_code")


def _taxon_line(taxon: Any) -> str:
    fields = (
        taxon.taxid,
        taxon.name,
        taxon.rank,
        taxon.parent_taxid,
        taxon.genetic_code_id,
        taxon.mito_genetic_code_id,
    )
    return "\t".join("" if value is None else str(value) for value in fields)


def cmd_taxonomy(args: argparse.Namespace) -> int:
    """``sylphd taxonomy``: look taxa up by taxid or name."""
    if args.db is not None and any(
        getattr(args, name, None) for name in ("socket", "tcp", "url")
    ):
        _print_error("--db cannot be combined with --socket, --tcp or --url")
        return 2
    tax = (
        Taxonomy.connect(db=args.db)
        if args.db is not None
        else Taxonomy.from_client(_client_from_args(args))
    )
    missing = 0
    with tax:
        print("\t".join(_TAXON_HEADER))
        for query in args.queries:
            if args.search:
                matches = tax.search(
                    query, fuzzy=args.fuzzy, rank=args.rank, limit=args.limit
                )
                taxa = [m.taxon for m in matches]
            elif query.isdigit():
                taxon = tax.get(int(query))
                taxa = [taxon] if taxon is not None else []
            else:
                taxa = tax.by_name(query)
            if not taxa:
                print(f"{PROG}: not found: {query}", file=sys.stderr)
                missing += 1
            for taxon in taxa:
                if args.lineage:
                    for step in taxon.lineage():
                        print(_taxon_line(step))
                else:
                    print(_taxon_line(taxon))
    return 1 if missing else 0


def _show_warning(message, category, filename, lineno, file=None, line=None) -> None:
    if issubclass(category, TokenWarning):
        print(f"{PROG}: warning: {message}", file=file or sys.stderr)
    else:
        _original_showwarning(message, category, filename, lineno, file, line)


_original_showwarning = warnings.showwarning


def main(argv: Sequence[str] | None = None) -> int:
    warnings.showwarning = _show_warning
    parser = build_parser()
    args = parser.parse_args(argv)
    commands = {
        "serve": cmd_serve,
        "download": cmd_download,
        "setup": cmd_setup,
        "taxdb": cmd_taxdb,
        "taxonomy": cmd_taxonomy,
        "taxmatrix": taxmatrix.run,
        "diff": parity.run,
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
