"""Argument parser of the ``sylphd`` command line."""

from __future__ import annotations

import argparse
import math

from . import __version__
from .download import METADATA_FILES
from .limits import parse_rate_limit
from .taxonomy import MATRIX_VALUES, TAXONOMIES

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


def _fraction(text: str) -> float:
    value = float(text)
    if not 0 <= value <= 100:
        raise argparse.ArgumentTypeError("must be between 0 and 100")
    return value


def _assembly_ani(text: str) -> float:
    value = float(text)
    if not 70 <= value <= 100:
        raise argparse.ArgumentTypeError("must be between 70 and 100")
    return value


def _top(text: str) -> int:
    value = int(text)
    if not 1 <= value <= 50:
        raise argparse.ArgumentTypeError("must be between 1 and 50")
    return value


def _consensus_ratio(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def _non_negative_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value < 0:
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

    # serve -------------------------------------------------------------
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
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug"],
        help="uvicorn log level [default: info]",
    )

    # info --------------------------------------------------------------
    info_parser = subparsers.add_parser("info", help="print server and database info")
    _add_client_options(info_parser)
    info_parser.add_argument("--json", action="store_true", help="print the raw JSON")

    # profile / query ---------------------------------------------------
    for mode, help_text in (
        ("profile", "taxonomic profile of one or more samples"),
        ("query", "ANI hits of one or more samples"),
    ):
        mode_parser = subparsers.add_parser(mode, help=help_text)
        _add_contain_options(mode_parser, mode)

    # assembly ----------------------------------------------------------
    assembly_parser = subparsers.add_parser(
        "assembly", help="find the closest database genomes to one assembly"
    )
    assembly_parser.add_argument(
        "file", metavar="FILE", help="assembly in FASTA or FASTQ, plain or gzip"
    )
    assembly_parser.add_argument(
        "--top",
        type=_top,
        default=None,
        metavar="N",
        help="number of hits to print [default: 10, or 1 with --per-contig]",
    )
    assembly_parser.add_argument(
        "--min-ani",
        type=_assembly_ani,
        default=80.0,
        metavar="ANI",
        help="minimum ANI in percent, 70-100 [default: 80]",
    )
    assembly_parser.add_argument(
        "--per-contig",
        action="store_true",
        help=(
            "sketch every FASTA/FASTQ record as its own sample and report the "
            "closest genomes and a taxonomic consensus of each contig "
            "(contamination checks); always sketches locally"
        ),
    )
    assembly_parser.add_argument(
        "--min-contig-length",
        type=_non_negative,
        default=None,
        metavar="BP",
        help=(
            "with --per-contig: contigs shorter than this are listed as too_short "
            "and not sketched or sent [default: 20000]"
        ),
    )
    assembly_parser.add_argument(
        "--consensus-ratio",
        type=_consensus_ratio,
        default=None,
        metavar="R",
        help=(
            "with --per-contig: a species joins the consensus when its "
            "divergence is at most R times the best hit's [default: 1.5]"
        ),
    )
    assembly_parser.add_argument(
        "--consensus-z",
        type=_non_negative_float,
        default=None,
        metavar="Z",
        help=(
            "with --per-contig: ... plus Z standard deviations of the ANI "
            "sampling noise [default: 2]"
        ),
    )
    assembly_parser.add_argument(
        "--name", metavar="NAME", help="query name [default: from the file name]"
    )
    assembly_parser.add_argument(
        "--local-sketch",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "sketch the assembly on this machine and upload only the sketch, so the "
            "sequence never leaves it and has no size limit [default]. "
            "--no-local-sketch uploads the raw sequence instead, which only servers "
            "started with --allow-sequences accept; it cannot be combined with "
            "--per-contig"
        ),
    )
    assembly_parser.add_argument(
        "--fields",
        metavar="COLS",
        default=None,
        help=(
            "with --per-contig: metadata columns to add to every hit ('all', "
            "'none' or a comma-separated list) [default: all]"
        ),
    )
    assembly_parser.add_argument(
        "--json",
        action="store_true",
        help="print JSON (the raw /assembly response, or the per-contig document)",
    )
    assembly_parser.add_argument(
        "-o", "--output", metavar="FILE", help="write the output here [default: stdout]"
    )
    assembly_parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="do not print the summary on stderr",
    )
    _add_client_options(assembly_parser)

    # download ----------------------------------------------------------
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

    # setup -------------------------------------------------------------
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
        "--no-adjust",
        action="store_true",
        help=(
            "report the naive containment ANI instead of the adjusted ANI; use "
            "for assemblies/genomes as samples"
        ),
    )
    if mode == "profile":
        parser.add_argument(
            "--estimate-read-counts",
            action="store_true",
            help=(
                "very roughly estimate the number of reads per genome in the "
                "'Sequence_abundance' column instead of a relative abundance "
                "(experimental in sylph); implies --estimate-unknown"
            ),
        )
        parser.add_argument(
            "--taxonomy",
            default=None,
            metavar="LIST",
            help=(
                "taxonomies to aggregate, comma-separated from "
                f"{', '.join(TAXONOMIES)} [default: "
                + ", ".join(TAXONOMIES[:2])
                + f", plus {TAXONOMIES[-1]} when the server has taxonomy sources]"
            ),
        )
        parser.add_argument(
            "--tax-source",
            action="append",
            default=[],
            metavar="NAME",
            help=(
                "with --taxonomy tax: restrict the merged profile to these "
                "taxonomy sources of the server's metadata file (repeatable; "
                "default: all of them)"
            ),
        )
        parser.add_argument(
            "--taxprof",
            metavar="PREFIX",
            help=(
                "write one sylph-tax-style .sylphmpa per sample and taxonomy as "
                "'PREFIX<sample>.<taxonomy>.sylphmpa'"
            ),
        )
        parser.add_argument(
            "--taxmatrix",
            metavar="PREFIX",
            help=(
                "write one clade x sample matrix per taxonomy as "
                "'PREFIX<taxonomy>.tsv'"
            ),
        )
        parser.add_argument(
            "--taxmatrix-value",
            choices=MATRIX_VALUES,
            default="relative",
            help=(
                "value in --taxmatrix: 'relative' (Taxonomic_abundance), "
                "'sequence' (Sequence_abundance), 'ani' or 'coverage' (strain "
                "rows only, like 'sylph-tax merge --column ANI|Coverage') "
                "[default: relative]"
            ),
        )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="progress messages on stderr"
    )
    _add_client_options(parser)
