# serve

`sylphd serve` loads one or more databases and answers requests until stopped. Exactly **one worker
process** runs, because sharing the in-memory indexes is the point. Blocking pysylph calls run in
worker threads, so the server stays responsive while jobs run.

```text
sylphd serve --db [NAME=]PATH [--db [NAME=]PATH ...] [--meta PATH]
             [--socket PATH | --tcp HOST:PORT]
             [--max-jobs N] [--max-upload-mb N] [--hash]
             [--allow-sequences] [--max-sequence-mb N] [--rate-limit N/min]
             [--max-queue N] [--trusted-proxy IP] [--cors-origin ORIGIN]
             [--name TEXT] [--about FILE] [--log-level LEVEL]
```

## Database and metadata

| Option | Default | Meaning |
|---|---|---|
| `--db [NAME=]PATH` | required, repeatable | a database to load at startup. `.syl2db` files are read as two-stage databases, everything else as a plain `.syldb`. `NAME` is the name `/info` reports, by default the file name |
| `--meta PATH` | none | SQLite metadata database from [`sylphd setup`](setup.md). Results are joined by accession and the metadata columns are added to each row |
| `--hash` | off | compute each database's SHA-256 at startup (slow on a 20 GB database); reported by `/info` |

One database is the normal case; several are loaded side by side and profiled as one pool, exactly
like `sylph profile db1 db2 sample`:

```bash
sylphd serve \
    --db gtdb=db/gtdb-r232-c200-dbv2.syl2db \
    --db uhgv=db/uhgv_c200_dbv1.syldb \
    --db fungi=db/fungi-refseq-2025-10-11-c200-dbv1.syldb \
    --meta meta.sqlite
```

- **`k` must be equal in every database**; a mismatch is refused at startup with the databases and
  their `k` in the message. The genomes of one plain `.syldb` must share `k` too, which is checked
  when it is loaded.
- **A plain `.syldb` may hold genomes with different `c`.** Its `c` is the smallest of them (a
  sample must be sparser than every genome), and `/info` reports that minimum.
- **`c` may differ.** Samples are sketched at the *smallest* `c` of the pool (`/info` reports it as
  `database.c`); a sketch with a larger `c` than that minimum is *sparser* (fewer k-mers) and is
  refused with a `400` that names the limiting database, for example `sketch c=250 is sparser than
  the c=200 of uhgv`. A sketch at the minimum or smaller is accepted. Differing `c` is logged as a
  warning, because a smaller `c` means bigger sketches.
- Any mix of `.syl2db` and `.syldb` works. The databases are loaded in parallel threads and the
  load time of each is logged. Precedence between duplicate genomes follows sylph: every
  `.syl2db` in `--db` order first, then every `.syldb` in `--db` order; sylph's reassignment keeps
  the earlier one. Duplicates are not removed by the server (pysylph logs a warning once).
- A `Profiler` is built once per parameter set and reused, so a request does not rescan the
  genomes; the default one is built at startup.
- `/info` reports the pooled view as `database` and one entry per database as `databases`; the
  results themselves have no per-database field.

A plain `.syldb` is held entirely in RAM *and* scanned for every sample (only `.syl2db` files are
screened), so keep small databases plain and large ones two-stage. Every database adds to the
per-sample cost: lower `--max-jobs` accordingly. On GTDB r232 `c200` plus UHGV and fungi (both
plain, 169 k genomes together) a `profile` request takes about 35 ms instead of 1.5 ms and
`/assembly` about 45 ms instead of 1 ms, while the two plain databases add just over 1 GiB of
resident memory; `--max-jobs 2` stays a sane default there. An empty database is an error.

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
| `--max-jobs N` | `2` | profiling jobs (one per sample) running at once; further jobs queue. A job costs more with several databases (plain databases are scanned per sample), so lower this on a multi-database server |
| `--max-upload-mb N` | `512` | request body limit in MB |
| `--max-sequence-mb N` | `20` | limit of a *raw-sequence* `/assembly` upload, before decompression; gzip bodies are decompressed up to 5× this. Sketch uploads are limited by `--max-upload-mb` instead |
| `--max-queue N` | unlimited | jobs (samples) allowed to wait for a job slot; a request whose samples do not all fit is refused whole with `503` and `Retry-After` |
| `--rate-limit N/min` | off | per-client limit on `/assembly`, `/profile` and `/query`, for example `10/min`; `N/s` also works. Replies are `429` with `Retry-After` |

Uploaded sketches are also checked structurally before they are decoded; see the
[security notes](../reference/security.md).

## Public-facing options

All off by default.

| Option | Default | Meaning |
|---|---|---|
| `--allow-sequences` | off | also accept *raw* FASTA/FASTQ on `POST /assembly`. Without it that endpoint takes sketches only (the web app and `sylphd assembly` sketch locally) and answers raw sequences with a 404, so **the server can never receive a sequence** |
| `--trusted-proxy IP` | none | peer whose `X-Forwarded-For` header is believed; repeatable. `unix` means a Unix-socket peer. Without it the header is ignored |
| `--cors-origin ORIGIN` | none | allowed CORS origin; repeatable. `'*'`, a specific origin, or `local` |
| `--name TEXT` | none | display name of the instance (one line, at most 80 characters), reported as `name` by `/info` and shown by the web page |
| `--about FILE` | none | UTF-8 Markdown file (at most 16 KiB) describing the instance, reported as `about` by `/info` and shown by the web page. Checked at startup (an unreadable or too large file is an error) and re-read whenever it changes, so you can edit it without restarting; see [Describing your instance](../guides/public-server.md#describing-your-instance) |

The details of the limits, client identity and CORS are in
[Running a public server](../guides/public-server.md).

## Logging

`--log-level` takes `critical`, `error`, `warning`, `info` (default) or `debug` and is passed to
uvicorn. Access lines are logged unless the level is `warning` or above. Uploads are never logged,
only sizes, timings and request outcomes.

## Endpoints

`GET /`, `GET /health`, `GET /info`, `POST /query`, `POST /profile` and `POST /assembly` (sketches
always, raw sequences only with `--allow-sequences`). See the [HTTP API](../reference/http-api.md).
