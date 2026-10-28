# serve

`sylphd serve` loads one database and answers requests until stopped. Exactly **one worker
process** runs, because sharing the in-memory index is the point. Blocking pysylph calls run in
worker threads, so the server stays responsive while jobs run.

```text
sylphd serve --db PATH [--meta PATH] [--socket PATH | --tcp HOST:PORT]
             [--max-jobs N] [--max-upload-mb N] [--hash]
             [--allow-sequences] [--max-sequence-mb N] [--rate-limit N/min]
             [--max-queue N] [--trusted-proxy IP] [--cors-origin ORIGIN]
             [--terms-url URL] [--log-level LEVEL]
```

## Database and metadata

| Option | Default | Meaning |
|---|---|---|
| `--db PATH` | required | database to load at startup. `.syl2db` files are read as two-stage databases, everything else as a plain `.syldb` |
| `--meta PATH` | none | SQLite metadata database from [`sylphd setup`](setup.md). Results are joined by accession and the metadata columns are added to each row |
| `--hash` | off | compute the database SHA-256 at startup (slow on a 20 GB database); reported by `/info` |

One database per server process. An empty database is an error.

## Address

| Option | Default | Meaning |
|---|---|---|
| `--socket PATH` | `$XDG_RUNTIME_DIR/sylphd.sock`, else `/tmp/sylphd-$UID.sock` | Unix socket path |
| `--tcp HOST:PORT` | none | listen on plain HTTP instead (`--socket` and `--tcp` are exclusive). `[::1]:9000` and `:9000` (= `127.0.0.1`) work |

The server removes a stale socket file at startup and creates the socket with mode `0660`: its
owner and group can connect. Change the group with `chgrp` if needed. Socket paths longer than 107
bytes are rejected. The socket file is removed on shutdown, including on `SIGTERM`.

## Jobs and limits

| Option | Default | Meaning |
|---|---|---|
| `--max-jobs N` | `2` | profiling jobs running at once; further requests queue |
| `--max-upload-mb N` | `512` | request body limit in MB |
| `--max-sequence-mb N` | `20` | `/assembly` upload limit before decompression; gzip bodies are decompressed up to 5× this |
| `--max-queue N` | unlimited | requests allowed to wait for a job slot; beyond that the server answers `503` with `Retry-After` |
| `--rate-limit N/min` | off | per-client limit on `/assembly`, `/profile` and `/query`, for example `10/min`; `N/s` also works. Replies are `429` with `Retry-After` |

Uploaded sketches are also checked structurally before they are decoded; see the
[security notes](../reference/security.md).

## Public-facing options

All off by default.

| Option | Default | Meaning |
|---|---|---|
| `--allow-sequences` | off | enable `POST /assembly` (raw FASTA/FASTQ instead of a sketch). Without it the endpoint is a 404 |
| `--trusted-proxy IP` | none | peer whose `X-Forwarded-For` header is believed; repeatable. `unix` means a Unix-socket peer. Without it the header is ignored |
| `--cors-origin ORIGIN` | none | allowed CORS origin; repeatable. `'*'`, a specific origin, or `local` |
| `--terms-url URL` | none | terms-of-use page, reported as `terms_url` by `/info` |

The details of the limits, client identity and CORS are in
[Running a public server](../guides/public-server.md).

## Logging

`--log-level` takes `critical`, `error`, `warning`, `info` (default) or `debug` and is passed to
uvicorn. Access lines are logged unless the level is `warning` or above. Uploads are never logged,
only sizes, timings and request outcomes.

## Endpoints

`GET /`, `GET /health`, `GET /info`, `POST /query`, `POST /profile`, and with `--allow-sequences`
`POST /assembly`. See the [HTTP API](../reference/http-api.md).
