# Coding agents

The `sylphd` skill lets a coding agent with a shell (Claude Code and others) identify an assembly,
classify contigs or profile reads. It drives the same [command line](../commands/info.md) you would use
yourself: there is no separate server, MCP endpoint or API.

## Install

```sh
gh skill install Abrinca/sylphd sylphd --agent claude-code --scope user
```

This needs the GitHub CLI (`gh`). For other agents use `--agent <name>`; see
`gh skill install --help`. The skill is `skills/sylphd/SKILL.md` in the repository.

## Server and token

The skill uses the public server, https://sylphd.abrinca.com, unless you say otherwise. To use
another server, tell the agent, or set `$SYLPHD_URL` (the same as `--url`). A privileged token is
read from `--token-file PATH` or `$SYLPHD_TOKEN`, as the CLI does; the skill never prints it. See
[server address](../commands/address.md).

## What the skill does

- **Picks the mode from the input.** FASTQ is reads (`profile`, with `-1`/`-2` for a pair matched
  by `_R1`/`_R2` or `_1`/`_2`). A FASTA with one genome or a few large records is an assembly
  (`assembly`). A metagenome assembly or bin with many contigs, or a request "per contig", is
  `assembly --per-contig`. If it is unclear, the agent asks.
- **Checks the server first** with `sylphd info --json`, and shows you the operator
  [messages](public-server.md#operator-messages). `warning` and `danger` messages need your explicit
  confirmation before any analysis; `info` messages are shown once. If the server keeps
  [usage statistics](../commands/serve.md#usage-statistics), it tells you and gives the `/usage` link.
- **Writes the full output to a file** next to the input (`<name>.sylphd.tsv`, or `.json`) and
  summarises it: top hits (species, ANI, relation) for an assembly, top clades for a profile,
  contig counts and the consensus for contigs.
- **Stops on errors** (unreachable server, rejected token, HTTP 429) and reports the exact message.
  `--retries` honours `Retry-After`; the skill adds no retry loops of its own.

It uses the CLI defaults; for example `--estimate-unknown` stays off unless you ask for it.

## Privacy

The CLI sketches locally. Only the k-mer sketch is uploaded; the sequences and reads never leave
your machine.

## What it does not enforce

The message acknowledgement is a rule given to the agent. Neither the server nor the CLI enforces
it: the CLI itself never asks for confirmation, and an agent that ignores the skill can run any
command.

## Limits

Long inputs can hit an agent's shell timeout (sketching is typically under 2 minutes per GB of
reads). If that happens, run the command in a terminal; the CLI has no timeout by default. The
public server limits requests to 10 per minute.
