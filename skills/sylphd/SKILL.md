---
name: sylphd
description: Use sylphd to identify a genome or assembly (closest reference genomes, species, ANI), classify the contigs of an assembly or bin (contamination check, per-contig consensus), or profile sequencing reads against a sylphd server. Trigger on FASTA or FASTQ input, "what species is this", "identify this genome", "check for contamination", or "profile this metagenome".
---

# sylphd

Drive the `sylphd` CLI. It sketches the sequences on this machine and uploads only the k-mer sketch to a sylphd server (the public one, https://sylphd.abrinca.com, unless the user names another). Tell the user once, before the first analysis, that the sequences never leave their machine.

Run commands as `sylphd ...`. Use only the flags listed here. Anything else: check `sylphd <command> --help` first.

## 0. Setup

Check that `sylphd --version` works. If `sylphd` is missing, ask the user before installing (the first install compiles Rust and takes a few minutes):

```sh
uv tool install "sylphd[client] @ git+https://github.com/Abrinca/sylphd"
```

If it is installed but may be out of date (its version differs from the `version` in `sylphd info --json`, or the server rejects a command or flag), offer to update, and wait for a yes:

```sh
uv tool install --force "sylphd[client] @ git+https://github.com/Abrinca/sylphd"
```

## 1. Pick the mode from the input

- FASTQ (`.fq`, `.fastq`, optionally `.gz`): reads, `profile`.
  - Paired files (matched by `_R1`/`_R2` or `_1`/`_2`): `profile -1 A -2 B`.
  - Single file: `profile -r FILE`.
- FASTA with one genome or a few large records: assembly, `assembly FILE`.
- FASTA the user wants "per contig", or a metagenome assembly or bin with many contigs: `assembly --per-contig FILE`.
- Anything else, or ambiguous: ask the user. Do not guess.

## 2. Server first, messages, usage

Select the server. The CLI's own default is a local socket, not the public server, so pass `--url https://sylphd.abrinca.com` unless one of these applies: the user gave `--url`, `--socket` or a token file, or `$SYLPHD_URL` / `$SYLPHD_SOCKET` is set (then pass nothing). Pass `--token-file PATH` only if the user gave one; `$SYLPHD_TOKEN` is read by the CLI itself. Never print, log or echo a token.

Run `sylphd info --json` with the same server options. Then:

1. Show the databases on the server.
2. Show the `messages` to the user. (Only once per session.)
3. If there are `warning` and `danger` messages: stop and ask for explicit confirmation before any analysis.
4. If `usage.enabled` is true, tell the user once that the server keeps usage statistics.

## 3. Run

Always pass `-o`. Default the output next to the input, named from the input's stem (strip `.gz`, `.fa`, `.fasta`, `.fna`, `.fq`, `.fastq`). For paired reads use the stem of the first file without `_R1`/`_1`.

```sh
sylphd assembly FILE -o STEM.sylphd.tsv                  # add --json and use STEM.sylphd.json for JSON
sylphd assembly --per-contig FILE -o STEM.sylphd.tsv
sylphd profile -r FILE -o STEM.sylphd.tsv
sylphd profile -1 A_R1.fq.gz -2 A_R2.fq.gz -o STEM.sylphd.tsv
```

Add the server options (`--url URL` / `--socket PATH`, `--token-file PATH`) as selected above. Optional, only when the user asks:

- all commands: `--timeout SECONDS`, `--retries N` (retries on HTTP 429/503, waiting for Retry-After; do not add retry loops of your own).
- `assembly`: `--top N`, `--min-ani ANI` (default 80), `--min-contig-length BP` (with `--per-contig`, default 20000), `--json`.
- `profile`: `--format tsv|json`, `--taxonomy LIST`, `--minimum-ani ANI`, `--estimate-unknown` (off by default; never turn it on silently).

Keep the CLI defaults otherwise. Several FASTA files can be given to `profile -r` in one call (the public server allows 10 requests per minute); `assembly` takes one file per call.

Notes:
- Don't lower `--minimum-ani` for `profile` below 95: sylph says that gives inaccurate results. For a relative at 90–94 % ANI, use `query` (default 90). See https://abrinca.github.io/sylphd/species-and-ani/
- Method: the `sylph` paper, https://doi.org/10.1038/s41587-024-02412-y
