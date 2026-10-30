# info

```text
sylphd info [--socket PATH | --tcp HOST:PORT | --url URL] [--timeout SECONDS]
            [--retries N] [--json]
```

Prints what the server reports about itself:

- database name, size, genome count, `c`, `k`, `screen_c`, whether it is two-stage, and the
  SHA-256 (`-` unless the server runs with `--hash`). `/info` is public and does not expose file
  paths, so the server's directories are not printed (they are in the server log). On a server with several databases these are
  the pooled values: `c` is the smallest `c` (the one to sketch with), `genomes` the sum, and
  `screen_c`/`two-stage` are only shown when every database agrees;
- `databases:` for a server with more than one database, one line per database with its name (the
  `--db NAME=PATH` alias, else the file name), file, genome count, `c` and `k`. A single-database
  server prints nothing extra, so the output does not change;
- the metadata database (file name, GTDB release, row count, build date, number of columns) or
  `metadata: none`; taxonomy sources show their base name or the catalogue URL they came from,
  never a local path; when it has taxonomy sources (format 3), `taxonomy sources:` with one line per
  source (name, number of keys, whether it is the virtual GTDB source or an imported file, whether
  it carries virus hosts, MD5), then `taxonomy overlaps:` with one line per pair of sources that
  share keys (`First and Second: 7 shared keys; examples: ...; the source imported later wins`) —
  see [`setup`](../reference/metadata.md#overlapping-sources);
- versions of sylphd, pysylph and Python;
- limits: jobs, contigs per `/contigs` request, upload size, sketch count, and a `web:` line with
  the raw-sequence assembly
  (`--allow-sequences`), rate-limit and queue settings;
- `usage statistics: off`, or `on` with the policy and window length: the server keeps
  [usage statistics](serve.md#usage-statistics), public at `/usage`. The commands that send work
  (`profile`, `query`, `assembly`) print one line to stderr, `sylphd: note: this server keeps usage
  statistics, see <url>/usage`, once per run when it is on. `assembly --quiet` and `--json` keep
  it off stderr; nothing is asked.

`--json` prints the raw `/info` payload instead. See [`GET /info`](../reference/http-api.md#get-info).
The [server address](address.md) options select the server, and `--retries N` retries a `429` or
`503` answer.
