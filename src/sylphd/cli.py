"""Command line interface: ``sylphd {serve,info,profile,query,download,setup}``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .client import ClientError, connect
from .download import METADATA_FILES, ProgressPrinter, download_release
from .metadata import build_metadata_db
from .paths import check_socket_path, default_socket_path, parse_tcp
from .protocol import ProfileParams, ProtocolError, rows_to_tsv_lines
from .server import LoadedDatabase, ServerConfig, create_app, serve
from .sketching import (
    InputError,
    SampleInput,
    build_sample_inputs,
    check_sketch,
    sketch_sample,
)

PROG = "sylphd"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def _print_error(message: str) -> None:
    print(f"{PROG}: error: {message}", file=sys.stderr)


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def _non_negative(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def _fraction(text: str) -> float:
    value = float(text)
    if not 0 <= value <= 100:
        raise argparse.ArgumentTypeError("must be between 0 and 100")
    return value


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    if args.socket is not None and args.tcp is not None:
        _print_error("--socket and --tcp are mutually exclusive")
        return 2

    config = ServerConfig(
        db_path=Path(args.db),
        meta_path=Path(args.meta) if args.meta else None,
        max_jobs=args.max_jobs,
        max_upload_mb=args.max_upload_mb,
        compute_hash=args.hash,
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

    # Load the database before binding, so that bad paths fail loudly.
    database = LoadedDatabase(config.db_path, compute_hash=config.compute_hash)
    app = create_app(config, database=database)
    _log_startup(database, config, socket_path, tcp)
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


def _log_startup(
    database: LoadedDatabase,
    config: ServerConfig,
    socket_path: Path | None,
    tcp: tuple[str, int] | None,
) -> None:
    if socket_path is not None:
        address = f"unix:{socket_path}"
    else:
        assert tcp is not None
        address = f"http://{tcp[0]}:{tcp[1]}"
    kind = "two-stage" if database.two_stage else "plain"
    print(
        f"{PROG}: {database.name} ({kind}, c={database.c}, k={database.k}, "
        f"{database.genomes} genomes) listening on {address} "
        f"[max-jobs={config.max_jobs}, max-upload={config.max_upload_mb} MB]",
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
    print(f"  size: {_format_size(int(database.get('size') or 0))}")
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
    return 0


# ---------------------------------------------------------------------------
# profile / query
# ---------------------------------------------------------------------------


def cmd_contain(args: argparse.Namespace, mode: str) -> int:
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
        samples = [replace(samples[0], name=args.sample_name)]

    params = ProfileParams(
        minimum_ani=args.minimum_ani,
        seq_id=args.seq_id,
        estimate_unknown=args.estimate_unknown,
        min_number_kmers=args.min_number_kmers,
        min_contain=args.min_contain,
        screen_ani=args.screen_ani,
    )

    try:
        with _client_from_args(args) as client:
            info = client.info()
            database = info["database"]
            fields = _resolve_fields(args.fields, info.get("metadata"))
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
                    f"sketches total {_format_size(total)}, above the server's "
                    f"{_format_size(limit)} upload limit; send fewer samples per request"
                )
            if args.verbose:
                print(
                    f"{PROG}: sending {len(sketches)} sketch(es), "
                    f"{_format_size(total)}",
                    file=sys.stderr,
                )
            if mode == "profile":
                result = client.profile(sketches, params=params, fields=fields)
            else:
                result = client.query(sketches, params=params, fields=fields)
    except (ClientError, InputError, ProtocolError) as error:
        _print_error(str(error))
        return 1

    _write_result(result, mode, params, fields, args)
    return 0


def _client_from_args(args: argparse.Namespace):
    timeout = args.timeout
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
        return None if metadata is not None else []
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
                f"({_format_size(len(payload))})",
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
) -> None:
    path = getattr(args, "output", None)
    handle = None
    if path is None or path == "-":
        out = sys.stdout
    else:
        handle = open(path, "w", encoding="utf-8", newline="\n")
        out = handle
    try:
        if getattr(args, "format", "tsv") == "json":
            json.dump(samples, out, indent=None)
            out.write("\n")
        else:
            for line in rows_to_tsv_lines(samples, mode, params=params, fields=fields):
                out.write(line + "\n")
        out.flush()
    finally:
        if handle is not None:
            handle.close()


# ---------------------------------------------------------------------------
# download / setup
# ---------------------------------------------------------------------------


def cmd_download(args: argparse.Namespace) -> int:
    domains = args.only or list(METADATA_FILES)
    progress = ProgressPrinter(sys.stderr)
    try:
        paths = download_release(
            release=args.release,
            outdir=args.outdir,
            domains=domains,
            base_url=args.base_url,
            force=args.force,
            progress=progress,
        )
    except Exception as error:  # httpx errors, checksum errors, ...
        _print_error(str(error))
        return 1
    for path in paths:
        print(path)
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    printed = {"last": ""}

    def progress(seen: int, path: Path) -> None:
        line = f"{path.name}: {seen:,} rows read"
        if line != printed["last"]:
            printed["last"] = line
            print(f"\r{line}", end="", file=sys.stderr, flush=True)

    if not args.quiet:
        callback = progress
    else:
        callback = None
    try:
        stats = build_metadata_db(
            files=args.metadata,
            output=args.output,
            release=args.release,
            force=args.force,
            progress=callback,
        )
    except (OSError, ValueError) as error:
        _print_error(str(error))
        return 1
    if not args.quiet:
        print(file=sys.stderr)
    print(
        f"wrote {stats['path']} ({_format_size(int(stats['size']))}, "
        f"{stats['rows']:,} genomes)",
        file=sys.stderr,
    )
    for source in stats["sources"]:  # type: ignore[union-attr]
        print(
            f"  {source['file']}: {source['rows']:,} rows",
            file=sys.stderr,
        )
    return 0


# ---------------------------------------------------------------------------
# argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Share one in-memory sylph database between runs: clients sketch "
            "samples locally and ask the server for ANI hits or taxonomies."
        ),
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # serve -------------------------------------------------------------
    serve_parser = subparsers.add_parser(
        "serve", help="run the server (one process, one worker)"
    )
    serve_parser.add_argument(
        "--db", required=True, metavar="PATH", help=".syl2db or .syldb database to load"
    )
    serve_parser.add_argument(
        "--meta", metavar="PATH", help="SQLite metadata database built by 'sylphd setup'"
    )
    address = serve_parser.add_mutually_exclusive_group()
    address.add_argument(
        "--socket",
        metavar="PATH",
        help="Unix socket path [default: $XDG_RUNTIME_DIR/sylphd.sock]",
    )
    address.add_argument("--tcp", metavar="HOST:PORT", help="listen on TCP instead")
    serve_parser.add_argument(
        "--max-jobs",
        type=_positive,
        default=2,
        metavar="N",
        help="maximum concurrent profiling jobs [default: 2]",
    )
    serve_parser.add_argument(
        "--max-upload-mb",
        type=_positive,
        default=512,
        metavar="N",
        help="maximum request body size in MB [default: 512]",
    )
    serve_parser.add_argument(
        "--hash",
        action="store_true",
        help="compute the database sha256 at startup (slow, off by default)",
    )
    serve_parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug"],
        help="uvicorn log level [default: info]",
    )
    serve_parser.set_defaults(func=cmd_serve)

    # info --------------------------------------------------------------
    info_parser = subparsers.add_parser("info", help="print server and database info")
    _add_client_options(info_parser)
    info_parser.add_argument("--json", action="store_true", help="print the raw JSON")
    info_parser.set_defaults(func=cmd_info)

    # profile / query ---------------------------------------------------
    for mode, help_text in (
        ("profile", "taxonomic profile of one or more samples"),
        ("query", "ANI hits of one or more samples"),
    ):
        mode_parser = subparsers.add_parser(mode, help=help_text)
        _add_contain_options(mode_parser, mode)
        mode_parser.set_defaults(func=lambda args, mode=mode: cmd_contain(args, mode))

    # download ----------------------------------------------------------
    download_parser = subparsers.add_parser(
        "download", help="download GTDB metadata files"
    )
    download_parser.add_argument(
        "--release", type=int, default=232, help="GTDB release [default: 232]"
    )
    download_parser.add_argument(
        "--outdir", default=".", metavar="DIR", help="output directory [default: .]"
    )
    download_parser.add_argument(
        "--only",
        action="append",
        choices=sorted(METADATA_FILES),
        help="download only this domain (repeatable) [default: all]",
    )
    download_parser.add_argument(
        "--base-url", default=None, metavar="URL", help="mirror of the GTDB release directory"
    )
    download_parser.add_argument(
        "--force", action="store_true", help="re-download files that already exist"
    )
    download_parser.set_defaults(func=cmd_download)

    # setup -------------------------------------------------------------
    setup_parser = subparsers.add_parser(
        "setup", help="build the SQLite metadata database from GTDB TSV files"
    )
    setup_parser.add_argument(
        "-m",
        "--metadata",
        nargs="+",
        required=True,
        metavar="FILE",
        help="GTDB metadata TSV files (*.tsv.gz)",
    )
    setup_parser.add_argument(
        "-o", "--output", required=True, metavar="PATH", help="SQLite file to write"
    )
    setup_parser.add_argument(
        "--release", default=None, metavar="N", help="GTDB release to record in the file"
    )
    setup_parser.add_argument(
        "--force", action="store_true", help="overwrite the output file"
    )
    setup_parser.add_argument(
        "-q", "--quiet", action="store_true", help="no progress output"
    )
    setup_parser.set_defaults(func=cmd_setup)

    return parser


def _add_client_options(parser: argparse.ArgumentParser) -> None:
    address = parser.add_mutually_exclusive_group()
    address.add_argument(
        "--socket",
        metavar="PATH",
        help="Unix socket of the server [default: $SYLPHD_SOCKET or "
        "$XDG_RUNTIME_DIR/sylphd.sock]",
    )
    address.add_argument(
        "--tcp", metavar="HOST:PORT", help="server address [default: $SYLPHD_URL]"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="read timeout in seconds [default: no timeout]",
    )


def _add_contain_options(parser: argparse.ArgumentParser, mode: str) -> None:
    parser.add_argument(
        "files",
        nargs="*",
        metavar="FILE",
        help="one sample per FASTA/FASTQ file, or a .sylsp sketch",
    )
    parser.add_argument(
        "-r",
        "--reads",
        nargs="+",
        action="extend",
        default=[],
        metavar="FILE",
        help="single-end reads (or assemblies); each file is one sample",
    )
    parser.add_argument(
        "-1",
        "--first",
        nargs="+",
        action="extend",
        default=[],
        metavar="FILE",
        help="first mates of paired-end files",
    )
    parser.add_argument(
        "-2",
        "--second",
        nargs="+",
        action="extend",
        default=[],
        metavar="FILE",
        help="second mates of paired-end files (same count as -1)",
    )
    parser.add_argument(
        "--interleaved",
        nargs="+",
        action="extend",
        default=[],
        metavar="FILE",
        help="interleaved paired-end FASTQ files",
    )
    parser.add_argument(
        "--sample",
        action="append",
        default=[],
        metavar="NAME:SPEC",
        help=(
            "explicit sample; SPEC is a comma-separated list of files, a pair "
            "being written as 'R1+R2' (e.g. 's1:a.fq,b.fq' or 's1:a1.fq+a2.fq,"
            "b1.fq+b2.fq')"
        ),
    )
    parser.add_argument(
        "--sample-name",
        metavar="NAME",
        help="sample name to use instead of the file name (single sample only)",
    )
    parser.add_argument(
        "-o", "--output", metavar="FILE", help="write the table here [default: stdout]"
    )
    parser.add_argument(
        "--format",
        choices=["tsv", "json"],
        default="tsv",
        help="output format [default: tsv]",
    )
    parser.add_argument(
        "--fields",
        default="all",
        metavar="COLUMNS",
        help="metadata columns to add, comma-separated, or 'all'/'none' [default: all]",
    )
    parser.add_argument(
        "-t",
        "--threads",
        type=_positive,
        default=4,
        metavar="N",
        help="threads used to sketch samples locally [default: 4]",
    )
    parser.add_argument(
        "--minimum-ani",
        type=_fraction,
        default=None,
        metavar="ANI",
        help="minimum adjusted ANI, 0-100 [default: 90 query / 95 profile]",
    )
    parser.add_argument(
        "--seq-id",
        type=_fraction,
        default=None,
        metavar="PCT",
        help="read sequence identity in percent, only used with --estimate-unknown",
    )
    parser.add_argument(
        "--estimate-unknown",
        action="store_true",
        help="estimate true coverage and scale sequence abundance of unknowns",
    )
    parser.add_argument(
        "--min-number-kmers",
        type=_non_negative,
        default=10,
        metavar="N",
        help="discard genomes with fewer sampled k-mers [default: 10]",
    )
    parser.add_argument(
        "--min-contain",
        type=_non_negative,
        default=7,
        metavar="N",
        help="minimum contained k-mers for a hit [default: 7]",
    )
    parser.add_argument(
        "--screen-ani",
        type=_fraction,
        default=85.0,
        metavar="ANI",
        help="two-stage screen threshold, 0-100 [default: 85]",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="progress messages on stderr"
    )
    _add_client_options(parser)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except (ClientError, InputError, ProtocolError, OSError, ValueError, RuntimeError) as error:
        _print_error(str(error))
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
