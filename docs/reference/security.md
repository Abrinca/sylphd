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

Malformed sketches are rejected with a `400` before any allocation can happen. The same header
check yields the `c` and `k` for the mismatch errors. The layout is tied to the pinned sylph
version, and the test suite compares the parsed header against a decoded sketch to catch drift when
sylph changes.

## Upload and resource limits

| Limit | Default | Notes |
|---|---|---|
| request body (`--max-upload-mb`) | 512 MB | rejected with `413`, also from the declared `Content-Length` |
| sketches per request | 4096 | `413` above it |
| raw-sequence `/assembly` body (`--max-sequence-mb`) | 20 MB | before decompression; sketch bodies use `--max-upload-mb` |
| decompressed `/assembly` body | 5 × `--max-sequence-mb` | bounded gunzip, `413` above it |
| concurrent jobs (`--max-jobs`) | 2 | other requests wait |
| waiting requests (`--max-queue`) | unlimited | `503` with `Retry-After: 1` when set and full |
| request rate (`--rate-limit`) | off | `429` with `Retry-After`; checked before the body is read |

Sketches uploaded to `/assembly` go through the same structural header check as those of `/profile`
(see above). Without `--allow-sequences` a server never receives sequence data at all. Raw sequences
uploaded to `/assembly` are checked (FASTA or FASTQ, only `ACGTN`) and error messages say
where a problem is, never what the sequence or its headers contain. Uploads are never logged.

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
