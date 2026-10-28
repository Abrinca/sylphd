# Running a public server

By default `sylphd serve` is meant for a machine you trust: it listens on a Unix socket, has no
limits, and sends no CORS headers. Every public-facing feature is **opt-in**, so local behaviour
stays unchanged. This page lists the switches. Read the [security notes](../reference/security.md)
first: **sylphd has no authentication and no TLS by design.**

An example deployment behind a reverse proxy:

```bash
sylphd serve --db gtdb-r232-c200-dbv2.syl2db --meta gtdb-r232.sqlite \
    --tcp 127.0.0.1:8765 --allow-sequences --max-jobs 4 --max-queue 16 \
    --rate-limit 10/min --trusted-proxy 127.0.0.1 \
    --cors-origin https://abrinca.github.io \
    --terms-url https://abrinca.github.io/sylphd/terms.html
```

## Listening on TCP and reaching it with `--url`

`--tcp HOST:PORT` makes the server listen on plain HTTP. The server only speaks HTTP, so terminate
TLS in a reverse proxy and bind sylphd to `127.0.0.1`. Clients use the public address with
`--url`, which accepts `http://` and `https://` URLs with an optional path prefix. Certificates
are verified normally and the prefix is kept for every request:

```bash
sylphd query --url https://api.example.org/sylphd sample.fq.gz
export SYLPHD_URL=https://api.example.org/sylphd
```

See [Server address](../commands/address.md).

!!! info "Reverse proxy deployment"
    [`deploy/`](https://github.com/Abrinca/sylphd/tree/main/deploy) contains a
    complete, reproducible setup for this pattern: a Caddy site block that
    terminates TLS and proxies to the Unix socket, a hardened systemd unit, an
    idempotent `setup-server.sh`, and a `sylphd-update` script that installs
    release assets with checksum verification and rolls back a failed health
    check. The [deployment README](https://github.com/Abrinca/sylphd/blob/main/deploy/README.md)
    covers first-time setup, upgrades, logs and changing the domain.

## Raw sequences: `--allow-sequences`

A browser cannot sketch, so `--allow-sequences` enables [`POST /assembly`](../reference/http-api.md#post-assembly),
which takes a raw FASTA or FASTQ and sketches it on the server. Without the flag the endpoint is a
404. The [web app](https://abrinca.github.io/sylphd/) and [`sylphd assembly`](assembly.md) both use
it. Uploads are never logged: only sizes, timings and request outcomes appear in the server log.

## Limits { #limits }

| Option | Default | Applies to | Meaning |
|---|---|---|---|
| `--max-jobs N` | `2` | `/profile`, `/query`, `/assembly` | profiling jobs running at once; further requests queue |
| `--max-upload-mb N` | `512` | all `POST`s | request body limit (413 above it) |
| `--max-sequence-mb N` | `20` | `/assembly` | upload limit before decompression; gzip bodies may decompress to 5× this |
| `--rate-limit N/min` | off | `/profile`, `/query`, `/assembly` | per-client limit (`N/min` or `N/s`); over the limit: `429` with `Retry-After` |
| `--max-queue N` | unlimited | the same | requests allowed to wait for a job slot; beyond that `503` with `Retry-After: 1` |

`/health` and `/info` are always served and never limited. At most 4096 sketches fit in one
`/query` or `/profile` request; `/info` reports the cap as `max_sketches`.

## Client identity and `--trusted-proxy`

The rate limit needs to know who the client is. The identity is the **socket peer**. Behind a
reverse proxy that would be the proxy itself, so every user shares one bucket. With
`--trusted-proxy IP` (repeatable) the `X-Forwarded-For` header of that peer is believed, and the
rightmost address that is not itself a trusted proxy is used.

- When the proxy talks to the server over the default Unix socket, use `--trusted-proxy unix`.
- Without `--trusted-proxy`, the header is **ignored entirely** (a client could otherwise pick its
  own bucket). Unix-socket peers without a trusted proxy share one bucket called `unix`.

## CORS

A browser page on another origin may only call the API if the server allows its origin.
`--cors-origin` (repeatable) controls this. Preflights allow `GET` and `POST` with the
`Content-Type` request header, and `Retry-After` is exposed to JavaScript.

| Value | Effect |
|---|---|
| none (default) | no CORS headers; browsers on other origins cannot call the API |
| `'*'` | any origin; answered with `Access-Control-Allow-Origin: *` |
| `https://example.org` | that origin only; repeat the flag for several |
| `local` | `localhost`, `127.0.0.1`, `0.0.0.0` and `[::1]` on any port, for development |

`'*'` is safe for a public instance because the API uses no credentials or cookies. CORS is a
browser rule, not access control: see the [security notes](../reference/security.md#cors-is-not-access-control).

## Terms: `--terms-url`

`--terms-url URL` advertises a terms-of-use page. The URL appears as `terms_url` in `/info` and
the web app links it. Nothing is shown by default. The [terms of the Abrinca web service](../project/terms.md)
are one example.

## Using the web app against your server

The web app is a static page in `web/` that calls `GET /info` and `POST /assembly` of a server
started with `--allow-sequences`. `web/config.js` holds two addresses: `apiBase`, the public
server used by the published page (the public instance), and `devApiBase`
(`http://127.0.0.1:8765`), used only when the page itself is opened from localhost.
The published page never contacts the visitor's own machine by default, because browsers ask
for "local network access" permission when a public site does that. The `?api=` query parameter
overrides both per visit:

```bash
python -m http.server 8000 -d web
# open http://localhost:8000 (uses devApiBase), or add ?api=http://host:port
```

For local development start the server with `--cors-origin local`. The browser must be allowed by
the server's CORS origins, and an HTTPS page can only call an HTTPS API (or localhost).

When the page cannot use its server (none configured, unreachable, or one started without
`--allow-sequences`) it shows a banner above the form, and the submit button is disabled. The
banner has a **Use another server** form: enter the API URL of your own server (for example
`http://127.0.0.1:8765`) and press *Connect*; the page reloads with `?api=<url>`. The header
then shows the active URL with a *reset* link that removes `?api=`. The server must allow the
page's origin (`--cors-origin https://abrinca.github.io`, or `--cors-origin '*'`), and for a
server on localhost the browser may ask for permission to access devices on your local network.
