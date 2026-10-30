# Server address

The client commands (`info`, `profile`, `query`, `assembly`) accept at most one of:

| Option | Meaning |
|---|---|
| `--socket PATH` | Unix socket (default: `$SYLPHD_SOCKET`, else `$XDG_RUNTIME_DIR/sylphd.sock`, else `/tmp/sylphd-$UID.sock`) |
| `--tcp HOST:PORT` | plain HTTP to a host and port |
| `--url URL` | `http://` or `https://` base URL, optionally with a path prefix |

`--url`, `--tcp` and `--socket` are mutually exclusive. All four also take `--timeout SECONDS`
and `--retries N` (below).

`--url` also accepts a bare `host:port`, as `--tcp` does, and defaults to `$SYLPHD_URL`. TLS
certificates are verified normally, so a public instance behind a reverse proxy works as

```bash
sylphd query --url https://api.example.org/sylphd reads.fq.gz
```

A path prefix is kept for every request. A URL must not contain a query string or a fragment.

## Precedence

An explicit option wins over the environment, and URLs win over sockets:

1. `--url`, then `--tcp`
2. `--socket`
3. `$SYLPHD_URL`
4. `$SYLPHD_SOCKET`
5. the default socket path

## Timeout

`--timeout SECONDS` sets the read timeout. There is none by default, because profiling can take a
while; connecting times out after 30 s.

## Retries

`--retries N` sends a request again when the server answers `429` (rate limit) or `503` (job queue
full), up to `N` times. The wait is the answer's `Retry-After` or 1, 2, 4... seconds of backoff,
whichever is longer, capped at 30 s. The default is `0`: the error is reported at once, as before.
Every other status is final, and the request is not rebuilt, so the sketches already in memory are
sent again unchanged. A connection that cannot be made at all is not retried.
