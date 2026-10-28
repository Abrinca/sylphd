# info

```text
sylphd info [--socket PATH | --tcp HOST:PORT | --url URL] [--timeout SECONDS] [--json]
```

Prints what the server reports about itself:

- database name, path, size, genome count, `c`, `k`, `screen_c`, whether it is two-stage, and the
  SHA-256 (`-` unless the server runs with `--hash`);
- the metadata database (path, GTDB release, row count, build date, number of columns) or
  `metadata: none`;
- versions of sylphd, pysylph and Python;
- limits: jobs, upload size, sketch count, and, if `--allow-sequences`, a rate limit or a queue cap
  is set, a `web:` line with the assembly, rate-limit and queue settings.

`--json` prints the raw `/info` payload instead. See [`GET /info`](../reference/http-api.md#get-info).
The [server address](address.md) options select the server.
