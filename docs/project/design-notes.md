# Design notes

A condensed version of the design plan the project started from.

## Why

Every process that uses pysylph deserialises the database into its own memory. For GTDB r232 `c200`
that takes about 2 s and about 2 GB of RAM per process (the screening index; the 20 GB second stage
is read lazily from the file and shared through the page cache). A server pays this once and
shares one copy across runs, parallel jobs and users.

## Decisions

| Topic | Decision |
|---|---|
| Commands | `serve`, `profile`, `query`, `info`, `download`, `setup`, and later `assembly` |
| Transport | HTTP over a Unix socket by default, `--tcp host:port` as an option. No TLS, no auth |
| Server stack | Starlette and uvicorn, exactly one worker process. Blocking pysylph calls run in a thread pool, and a semaphore caps concurrent profiling jobs (the rest queue). One `Profiler` per parameter set, cached and shared by all threads: with one database it only holds references, but with two or more its constructor scans every genome, so it must not be rebuilt per request. A `pysylph.Profiler` is read-only and safe to use concurrently |
| Sketch transfer | `SampleSketch.to_bytes()` and `from_bytes()` in the pysylph `multi-db` branch. The server enforces an upload limit and checks the sketch's `c` and `k` against the database |
| Requests | Several samples per request |
| Responses | JSON rows from the server; the CLI writes TSV (sylph-like columns plus metadata columns) |
| Database identity | File name and size, plus an optional hash (off by default), reported by `/info` |
| Fallback | None. The client requires a server |
| Service | Plain `sylphd serve`; no systemd unit shipped |
| Databases | `serve --db [NAME=]PATH`, repeatable: one pool per server process, all with the same `k`; samples are sketched at the smallest `c` |
| Tests | pytest on Python 3.14 with pysylph's small test data; GTDB only for manual runs |

## Architecture

- **Server.** Loads the databases at startup (in parallel threads) and offers `/health`, `/info`,
  `/profile`, `/query` and `/assembly`.
- **Client library and CLI.** Ask the server for `c` and `k`, sketch locally, send the sketches and
  write the rows. Inputs: single-end reads (also long reads and assemblies), paired reads,
  interleaved reads, several files per sample (combined into one sketch) and existing `.sylsp`
  files, which are sent as they are.
- **Why sketches and not reads.** Sketching is per-sample CPU work and belongs to the client.
  Sketches are small (about 1-2 MB for a 40 Mb read pool). No file paths cross the boundary.

## Pysylph fork

`sylphd` depends on the `multi-db` branch of the
[MrTomRod/pysylph](https://github.com/MrTomRod/pysylph) fork, which in turn builds on the
`expose-library-api` branch of a sylph fork. It is installed as a uv git dependency, which is why
a Rust toolchain is needed at install time.

## Metadata

The metadata database is a SQLite file, built from GTDB's TSV files by `sylphd setup`. It is keyed
by accession, with repetitive text dictionary-encoded, and it stores the taxonomy strings as one
column each. See [Metadata database](../reference/metadata.md).

## The web front end

The aim: a public page where a visitor pastes one assembly and gets its closest GTDB genomes, with
no install. The page sketches the assembly in the browser (WebAssembly) and sends only the sketch.

- **Server.** `POST /assembly` takes the sketch of one assembly (always accepted), or, with the
  optional `--allow-sequences`, raw FASTA or FASTQ, plain or gzip, which it sketches itself, with
  size caps for the raw body and its decompressed form, `top` and `min_ani` parameters, and flat
  metadata in each hit. `Profiler(no_adjust=True)` gives the naive containment ANI, which is the
  meaningful ANI for a genome. `sylphd assembly` is a client for it.
- **Public-deployment flags.** `--allow-sequences`, `--max-sequence-mb`, `--rate-limit`,
  `--max-queue`, `--trusted-proxy` and `--cors-origin`. Everything is off by default, and raw
  sequence uploads to `/assembly` are a 404 without `--allow-sequences`, so local behaviour is
  unchanged.
- **Deployment.** A reverse proxy terminates TLS on a small host.
- **Page.** The static page calls the API directly from the browser with `fetch`, so the server
  needs the CORS origin and a rate limit per client IP.

The result classification (species assignment at 95 % ANI and relations from the GTDB lineage)
replaced an earlier ANI-only label; see [Identifying an assembly](../guides/assembly.md).
