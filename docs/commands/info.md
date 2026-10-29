# info

```text
sylphd info [--socket PATH | --tcp HOST:PORT | --url URL] [--timeout SECONDS] [--json]
```

Prints what the server reports about itself:

- database name, path, size, genome count, `c`, `k`, `screen_c`, whether it is two-stage, and the
  SHA-256 (`-` unless the server runs with `--hash`). On a server with several databases these are
  the pooled values: `c` is the smallest `c` (the one to sketch with), `genomes` the sum, and
  `screen_c`/`two-stage` are only shown when every database agrees;
- `databases:` for a server with more than one database, one line per database with its name (the
  `--db NAME=PATH` alias, else the file name), file, genome count, `c` and `k`. A single-database
  server prints nothing extra, so the output does not change;
- the metadata database (path, GTDB release, row count, build date, number of columns) or
  `metadata: none`;
- versions of sylphd, pysylph and Python;
- limits: jobs, upload size, sketch count, and a `web:` line with the raw-sequence assembly
  (`--allow-sequences`), rate-limit and queue settings.

`--json` prints the raw `/info` payload instead. See [`GET /info`](../reference/http-api.md#get-info).
The [server address](address.md) options select the server.
