# Security notes

`sylphd` is built to share one database on a machine or network you trust. It makes a few
deliberate choices, and this page says what they imply.

## No authentication, by design

The server has **no authentication and no TLS**. Anyone who can reach the socket or the port can
use it. That is the intended model for the default setup:

- The default transport is a **Unix socket**, created with mode `0660`. Access is controlled by the
  file permissions: keep the socket in a directory that only your group can enter, and `chgrp` it as
  needed.
- With `--tcp`, bind to `127.0.0.1` unless you really mean to expose the port.
- For a public instance, run a reverse proxy in front for TLS, and add whatever access control you
  need there. [Running a public server](../guides/public-server.md) lists the opt-in limits.

## Sketch header validation

Clients upload `SampleSketch` bytes, which the server decodes with pysylph. The decoder
**pre-allocates a hash map from a k-mer count at the start of the upload**, before it reads a single
k-mer. An upload of eight bytes that declares 100 million k-mers would make sylph reserve about
2 GB, and on failure Rust **aborts the process**. An abort cannot be caught, so one tiny request
could take the whole server down.

So the server first walks the fixed header with plain integer parsing and without touching the
k-mer entries:

1. the k-mer count prefix is checked against the real upload size;
2. then `c`, `k`, the file and sample names, the `paired` flag and the mean read length are parsed
   and validated;
3. only then is the sketch decoded.

Malformed sketches are rejected with a `400` before native allocation from unchecked length prefixes. (Validation itself allocates strings, and an accepted sketch allocates in proportion to its payload size.) The same header
check yields the `c` and `k` for the mismatch errors. The layout is tied to the pinned sylph
version, and the test suite compares the parsed header against a decoded sketch to catch drift when
sylph changes.

## Upload and resource limits

| Limit | Default | Notes |
|---|---|---|
| request body (`--max-upload-mb`) | 512 MB | rejected with `413`, also from the declared `Content-Length` |
| sketches per `/profile`, `/query` request | 4096 | `400` above it |
| contigs per `/contigs` request (`--max-contigs`) | 4096 | `400` above it; **set 100 on a public server** (see below) |
| raw-sequence `/assembly` body (`--max-sequence-mb`) | 20 MB | before decompression; sketch bodies use `--max-upload-mb` |
| decompressed `/assembly` body | 5 × `--max-sequence-mb` | bounded gunzip, `413` above it |
| concurrent jobs (`--max-jobs`) | 2 | other requests wait |
| waiting jobs (`--max-queue`) | unlimited | slots are per sample (`/query`, `/profile`), per assembly upload, or per whole `/contigs` request (its contigs run sequentially in one slot), not per HTTP request otherwise; a request whose samples do not all fit is refused whole with `503` and `Retry-After: 1`, before its body is read when the queue is already full |
| request rate (`--rate-limit`) | off | `429` with `Retry-After`; checked before the body is read |

### The `/contigs` work bound

`POST /contigs` carries one sketch per contig and a JSON object with each contig's measurements.
The whole request is **one job**: its contigs are queried and scored one after the other while
that job slot is held. The cost of a contig depends on the databases — a few milliseconds on a
two-stage `.syl2db`, but every plain `.syldb` is scanned per contig, so on GTDB + UHGV + fungi one
lambda-like contig costs about 0.6 s. A request of 4096 tiny contigs (a ~5 MB upload) would then
occupy a job slot for ~40 minutes, and with two job slots a handful of such requests is a denial
of service for everyone else. So:

- `--max-contigs N` (default 4096) bounds one request; above it the answer is a `400` that names
  the limit and points to `sylphd assembly --per-contig`, which batches by it. `/info` advertises
  the value as `limits.max_contigs`, and the CLI and the web page use it as their batch size;
- **a public instance should set `--max-contigs 100`** (the `deploy/` example policy does). That
  caps one client's request at roughly a minute of one job slot. `--max-jobs`, `--max-queue` and
  `--rate-limit` still apply on top; the contig cap is the one that bounds a single accepted
  request;
- a sketch below the sample k-mer floor (10 k-mers) or a contig with `contig_kmers == 0` cannot
  produce an ANI. It is reported as `too_few_kmers` (or rejected with a `400` for `contig_kmers`)
  **before** any query runs, so a fragmented assembly full of such records costs no profiling
  work;
- the query *and* the scoring/classification/consensus of every contig run in the same worker
  thread, so a long `/contigs` request never blocks `/health` or `/info`.

Sketches uploaded to `/assembly` go through the same structural header check as those of `/profile`
(see above). Without `--allow-sequences` a server never receives sequence data at all (the public
instance runs with it). Raw sequences uploaded to `/assembly` are processed in memory and checked (FASTA or FASTQ, only `ACGTN`) and error messages say
where a problem is, never what the sequence or its headers contain. Uploads are never logged.

## What `/info` says about the host

`GET /info` is public. It reports database and metadata **file names** and sizes, never absolute
paths, and reduces a taxonomy source's `origin` to its base name (or keeps the catalogue URL it was
downloaded from). The full paths of the databases and the metadata file stay in the server log,
which an operator reads; they are not part of the API. If you publish a server, read `/info` once
and make sure its `name`/`about` text says what you want visitors to know.

The browser per-contig path has its own bounds: it processes at most 1,000,000 records per run and
refuses any single record longer than 50 Mbp, with a message that points to
`sylphd assembly --per-contig`. That is a client-side memory guard, not a server limit: the CLI
has no such cap.

## The rate limit trusts only what you tell it to

The client identity is the socket peer. The `X-Forwarded-For` header is **ignored** unless the peer
is named in `--trusted-proxy`; otherwise a client could pick its own bucket. See
[Running a public server](../guides/public-server.md#client-identity-and-trusted-proxy).

## CORS is not access control { #cors-is-not-access-control }

CORS only tells **browsers** which web pages may read the API's responses. It does not stop `curl`,
scripts or other servers from calling the API. `--cors-origin` does not protect a server from
anyone.

`--cors-origin '*'` is acceptable for a public instance because the API uses no credentials or
cookies: there is nothing for a malicious page to steal that it could not fetch directly. It is
**not** a safe way to open an instance that is protected only by network position (for example one
reachable on a private network or on `localhost`): a web page in the user's browser could then use
the user's network access to call it. Prefer a specific origin, or none.
