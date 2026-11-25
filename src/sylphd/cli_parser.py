"""Argument parser of the ``sylphd`` command line."""

from __future__ import annotations

import argparse

from . import __version__
from .download import METADATA_FILES
from .limits import parse_rate_limit

PROG = "sylphd"


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


def _rate_limit(text: str) -> tuple[int, float]:
    try:
        return parse_rate_limit(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


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
    add_serve_parser(subparsers)
    add_download_parser(subparsers)
    add_setup_parser(subparsers)
    add_taxdb_parser(subparsers)
    add_taxonomy_parser(subparsers)
    return parser


def add_serve_parser(subparsers: argparse._SubParsersAction) -> None:
    serve_parser = subparsers.add_parser(
        "serve", help="run the server (one process, one worker)"
    )
    serve_parser.add_argument(
        "--db",
        action="append",
        required=True,
        metavar="[NAME=]PATH",
        help=(
            "a .syl2db or .syldb database to load; repeatable, and all must "
            "share k. NAME is the name /info reports, by default the file name"
        ),
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
        "--max-contigs",
        type=_positive,
        default=4096,
        metavar="N",
        help=(
            "maximum contigs in one POST /contigs request [default: 4096]; "
            "/info advertises it as limits.max_contigs and clients batch by it. "
            "A public server should use a small value such as 100, because a "
            "whole request is one job"
        ),
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
        "--allow-sequences",
        action="store_true",
        help=(
            "also accept raw FASTA/FASTQ on POST /assembly (sketches are always "
            "accepted there; without this flag the server never sees a sequence)"
        ),
    )
    serve_parser.add_argument(
        "--max-sequence-mb",
        type=_positive,
        default=20,
        metavar="N",
        help=(
            "maximum raw-sequence /assembly upload in MB, before decompression "
            "[default: 20]; sketch uploads use --max-upload-mb"
        ),
    )
    serve_parser.add_argument(
        "--rate-limit",
        type=_rate_limit,
        default=None,
        metavar="N/min",
        help="per-client POST rate limit, e.g. 10/min (off by default)",
    )
    serve_parser.add_argument(
        "--taxonomy-db",
        metavar="FILE",
        default=None,
        help=(
            "NCBI taxonomy file (from `sylphd taxdb build`): serves /taxonomy/*; "
            "a replaced file is picked up without a restart"
        ),
    )
    serve_parser.add_argument(
        "--taxonomy-rate-limit",
        type=_rate_limit,
        default=None,
        metavar="N/min",
        help="per-client rate limit of the /taxonomy/* endpoints, e.g. 120/min (off by default)",
    )
    serve_parser.add_argument(
        "--privileged-token-file",
        metavar="PATH",
        default=None,
        help=(
            "file of bearer tokens (one per line) whose holders skip --rate-limit "
            "(queue limits still apply); keep it 0600"
        ),
    )
    serve_parser.add_argument(
        "--require-token",
        action="store_true",
        help=(
            "refuse every request except / /health /info (401) without a valid token "
            "from --privileged-token-file: a private server"
        ),
    )
    serve_parser.add_argument(
        "--max-queue",
        type=_non_negative,
        default=None,
        metavar="N",
        help="maximum requests waiting for a job slot (unlimited by default)",
    )
    serve_parser.add_argument(
        "--trusted-proxy",
        action="append",
        default=[],
        metavar="IP",
        help=(
            "peer allowed to set X-Forwarded-For; repeatable, use 'unix' for a "
            "Unix socket (none by default)"
        ),
    )
    serve_parser.add_argument(
        "--cors-origin",
        action="append",
        default=[],
        metavar="ORIGIN",
        help=(
            "allowed CORS origin; repeatable (none by default). 'local' allows "
            "localhost, 127.0.0.1, 0.0.0.0 and [::1] on any port"
        ),
    )
    serve_parser.add_argument(
        "--name",
        metavar="TEXT",
        default=None,
        help="display name of this server, up to 80 characters, reported by /info",
    )
    serve_parser.add_argument(
        "--about",
        metavar="FILE",
        default=None,
        help=(
            "UTF-8 Markdown file (at most 16 KiB) describing this server, reported "
            "by /info and shown on the web page; re-read when the file changes"
        ),
    )
    serve_parser.add_argument(
        "--messages",
        metavar="FILE",
        default=None,
        help=(
            "JSON file (at most 64 KiB) with operator messages: a list of "
            '{"level": "info|warning|danger", "title": ..., "message": Markdown}; '
            "served at GET /messages, shown by the web page and, for warning and "
            "danger, printed by the client commands; re-read when the file changes"
        ),
    )
    serve_parser.add_argument(
        "--usage-db",
        metavar="FILE",
        default=None,
        help=(
            "SQLite file for opt-in usage statistics: counts of successful "
            "requests per 10-minute window, never addresses or names; public "
            "at GET /usage (off by default, nothing is recorded without it)"
        ),
    )
    serve_parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug"],
        help="uvicorn log level [default: info]",
    )


def add_download_parser(subparsers: argparse._SubParsersAction) -> None:
    download_parser = subparsers.add_parser(
        "download",
        help="download GTDB metadata, a sylph database or a taxonomy file",
        description=(
            "Download the GTDB metadata files (default), a pre-built sylph "
            "database from the built-in catalogue (--database), a sylph-tax "
            "taxonomy file (--taxonomy), or an arbitrary file (--url). See "
            "--list-databases and --list-taxonomies for the catalogues."
        ),
    )
    download_parser.add_argument(
        "--database",
        action="append",
        metavar="NAME",
        help=(
            "pre-built database to fetch, e.g. gtdb-r232-c200-dbv2.syl2db "
            "(repeatable; 'list' prints the catalogue)"
        ),
    )
    download_parser.add_argument(
        "--url",
        action="append",
        metavar="URL",
        help=(
            "fetch --database from this source instead of the catalogue "
            "mirrors (repeatable); without --database, download this URL, "
            "named after its last path segment"
        ),
    )
    download_parser.add_argument(
        "--taxonomy",
        action="append",
        metavar="NAME",
        help=(
            "sylph-tax taxonomy file to fetch, e.g. UHGV_default or "
            "FungiRefSeq-latest (repeatable; 'list' prints the catalogue)"
        ),
    )
    download_parser.add_argument(
        "--list-databases",
        action="store_true",
        help="print the built-in database catalogue and exit",
    )
    download_parser.add_argument(
        "--list-taxonomies",
        action="store_true",
        help="print the built-in taxonomy catalogue and exit",
    )
    download_parser.add_argument(
        "--verify",
        action="store_true",
        help=(
            "also check the MD5 of a downloaded database (reads the whole "
            "file: minutes for 20 GB on slow disks)"
        ),
    )
    download_parser.add_argument(
        "--no-aria2c",
        dest="aria2c",
        action="store_false",
        default=True,
        help="never use aria2c, even if it is installed",
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
        help="download only this metadata domain (repeatable) [default: all]",
    )
    download_parser.add_argument(
        "--base-url", default=None, metavar="URL", help="mirror of the GTDB release directory"
    )
    download_parser.add_argument(
        "--force", action="store_true", help="re-download files that already exist"
    )


def add_setup_parser(subparsers: argparse._SubParsersAction) -> None:
    setup_parser = subparsers.add_parser(
        "setup",
        help="build the SQLite metadata database from GTDB TSV and sylph-tax files",
    )
    setup_parser.add_argument(
        "-m",
        "--metadata",
        nargs="+",
        metavar="FILE",
        help=(
            "GTDB metadata TSV files (*.tsv.gz); with --update, the existing "
            "metadata SQLite file to copy (default: -o itself)"
        ),
    )
    setup_parser.add_argument(
        "--taxonomy",
        action="append",
        default=[],
        metavar="SPEC",
        help=(
            "taxonomy source, repeatable: a sylph-tax name (UHGV_default, "
            "FungiRefSeq-latest, ...; downloaded to --cache-dir), NAME=PATH or "
            "PATH to a local key<TAB>lineage[<TAB>host] file"
        ),
    )
    setup_parser.add_argument(
        "--update",
        action="store_true",
        help=(
            "add the --taxonomy sources to an existing metadata file instead of "
            "building one (also migrates a format 2 file to 3); atomic"
        ),
    )
    setup_parser.add_argument(
        "--cache-dir",
        metavar="DIR",
        help="where downloaded taxonomy files are kept [default: <output dir>/taxonomy]",
    )
    setup_parser.add_argument(
        "--ncbi-taxonomy",
        action="store_true",
        help=(
            "give the sylph-tax keys that GTDB does not cover an NCBI lineage, "
            "and those with no NCBI assembly (GlobDB MAGs) a nearest NCBI taxon "
            "(streams the assembly summaries and downloads NCBI's taxdump each run; "
            "nothing is kept in --cache-dir afterwards)"
        ),
    )
    setup_parser.add_argument(
        "-o", "--output", required=True, metavar="PATH", help="SQLite file to write"
    )
    setup_parser.add_argument(
        "--release", default=None, metavar="N", help="GTDB release to record in the file"
    )
    setup_parser.add_argument(
        "--reps-only",
        action="store_true",
        help="store only species representatives (gtdb_representative true)",
    )
    setup_parser.add_argument(
        "--force", action="store_true", help="overwrite the output file"
    )
    setup_parser.add_argument(
        "-q", "--quiet", action="store_true", help="no progress output"
    )


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
    address.add_argument(
        "--url",
        metavar="URL",
        help=(
            "server base URL, http:// or https://, optionally with a path "
            "prefix (e.g. https://api.example.org/sylphd) "
            "[default: $SYLPHD_URL]"
        ),
    )
    parser.add_argument(
        "--token-file",
        metavar="PATH",
        default=None,
        help=(
            "file holding a privileged bearer token, sent as Authorization "
            "[default: $SYLPHD_TOKEN, the token itself]"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="read timeout in seconds [default: no timeout]",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=0,
        metavar="N",
        help="retry up to N times when the server answers 429 or 503, waiting "
        "for its Retry-After (at most 30 s) [default: 0]",
    )


def add_taxdb_parser(subparsers: argparse._SubParsersAction) -> None:
    taxdb_parser = subparsers.add_parser(
        "taxdb", help="build the NCBI taxonomy file (see `sylphd serve --taxonomy-db`)"
    )
    actions = taxdb_parser.add_subparsers(dest="taxdb_command", required=True)
    build = actions.add_parser(
        "build",
        help="build the taxonomy SQLite file from NCBI's new_taxdump.tar.gz",
    )
    build.add_argument(
        "-o", "--output", required=True, metavar="FILE", help="SQLite file to write (atomic)"
    )
    build.add_argument(
        "--taxdump",
        metavar="FILE",
        help="a local new_taxdump.tar.gz [default: download the current one]",
    )
    build.add_argument(
        "--cache-dir",
        metavar="DIR",
        help="where the download is unpacked, deleted afterwards [default: next to --output]",
    )
    build.add_argument(
        "--no-fuzzy",
        action="store_true",
        help="skip the fuzzy-search index (saves about 15%% of the file)",
    )
    build.add_argument("--force", action="store_true", help="replace the output file")
    build.add_argument("-q", "--quiet", action="store_true", help="no progress output")


def add_taxonomy_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "taxonomy",
        help="look up NCBI taxa by taxid or name, on a server or in a taxonomy file",
        description=(
            "Each QUERY is a taxid (digits) or a name. Prints one tab-separated line per "
            "taxon: taxid, name, rank, parent, genetic code, mitochondrial genetic code."
        ),
    )
    parser.add_argument("queries", nargs="+", metavar="QUERY", help="taxid or exact name")
    parser.add_argument(
        "--db", metavar="FILE", help="read this taxonomy file instead of asking a server"
    )
    parser.add_argument(
        "--search",
        action="store_true",
        help="treat each QUERY as the start of a name (with --fuzzy: a similar name), or of a taxid when it is digits",
    )
    parser.add_argument("--fuzzy", action="store_true", help="with --search: tolerate typos")
    parser.add_argument("--rank", metavar="RANK", help="with --search: only this rank")
    parser.add_argument(
        "--limit", type=int, default=10, metavar="N", help="with --search: hits per query [10]"
    )
    parser.add_argument(
        "--lineage", action="store_true", help="print the lineage (root first) instead"
    )
    _add_client_options(parser)
