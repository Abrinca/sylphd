# HTTP API

The server speaks HTTP, on a Unix socket by default, or on TCP with `--tcp`. All responses are JSON.
This page documents the wire format that `sylphd` clients and the web app use. There is no
authentication; see the [security notes](security.md).

| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | service name, version and the endpoints this server offers |
| `/health` | GET | liveness: status, version, uptime, jobs running and queued |
| `/info` | GET | database, metadata, limits and endpoints |
| `/messages` | GET | the operator's messages (`--messages`), an empty list without the option |
| `/query` | POST | ANI hits of uploaded sketches |
| `/profile` | POST | taxonomic profile of uploaded sketches |
| `/assembly` | POST | closest genomes of one assembly, sent as a sketch (always) or as raw sequence (only with `--allow-sequences`) |
| `/contigs` | POST | one classified result per contig of a fragmented assembly (contig-relative ANI, hits, consensus) |
| `/usage` | GET | aggregated usage statistics, only on a server started with `--usage-db` (otherwise `404`) |

`/health`, `/info` and `/messages` are always served. The abuse limits (`--rate-limit`, `--max-queue`) apply to
the four POST endpoints and to `GET /usage`. With `curl` over a Unix socket:

```bash
curl --unix-socket "$XDG_RUNTIME_DIR/sylphd.sock" http://localhost/info
```

## Access tokens { #access-tokens }

A request may carry `Authorization: Bearer <token>`. A token from the server's
`--privileged-token-file` skips the rate limits; anything else is treated as no token.

- Every response to a request that carried a bearer token has **`X-Token-Valid: true`** or
  **`false`**, whatever its status (also `4xx` and `429`); a request without a token gets no such
  header. The header is exposed to browsers (CORS), and `GET /info` repeats the verdict as
  `access.token_valid`. A wrong token is therefore reported, not refused.
- With `--require-token` every path except `GET /`, `/health` and `/info` answers `401`
  (`{"error": "this server requires an access token: ..."}`, `WWW-Authenticate: Bearer`) unless the
  token is valid. `/info` says `access.token_required` so a client can tell in advance.
- CORS preflights allow the `Authorization` header.

## Request id and compression

Every response carries an **`X-Request-Id`** header, error responses included: 12 hex characters,
unique per request. The same string is written to the server's access log with the request's
outcome (see [`sylphd serve`](../commands/serve.md#logging)), so a user can quote the id of a
failed request when reporting it. With `--cors-origin` it is exposed to JavaScript.

JSON responses of **1 KB or more** are **gzip**-compressed when the request sends
`Accept-Encoding: gzip`. Smaller bodies, and clients that do not ask for it, get the plain body
with no `Content-Encoding`. A response is compressed once: a reverse proxy in front (the example
`Caddyfile` in `deploy/` runs `encode zstd gzip`) only compresses what sylphd sent uncompressed.

## `GET /health`

```json
{"status": "ok", "service": "sylphd", "version": "0.8.0", "uptime_seconds": 12.345,
 "jobs_running": 1, "jobs_queued": 0}
```

`jobs_running` is the number of jobs holding a `--max-jobs` slot right now, `jobs_queued` the
admitted jobs still waiting for one; both are `0` on an idle server. A `/contigs` request is one
job, however many contigs it carries.

## `GET /info` { #get-info }

Reports:

- `service`, `version`, `api_version` (integer, currently `1`: bumped on any change to the response
  shape; distinct from the server software `version` and from `metadata.format_version`),
  `pysylph_version`, `python_version`;
- `database`: `name`, `size`, `sha256` (`null` unless `--hash`), `genomes`, `c`, `k`,
  `screen_c` (`null` for a plain `.syldb`) and `two_stage`. **No filesystem paths** are exposed:
  `/info` is public and names files, never directories (the full paths are in the server log). A
  plain `.syldb` reports the **smallest `c` of its genomes** (genomes may differ in `c`; a sample
  must be sparser than all of them). Genomes that differ in `k` are refused at startup. This is
  the **pooled view** of a multi-database server: `name` is the database names (the
  `--db NAME=PATH` alias, else the file name) joined with `", "`, `size` and `genomes` the sums,
  `c` the **smallest** `c` (sketch samples with it), `k` the common `k`, `sha256` `null` for more
  than one database, and `screen_c`/`two_stage` only when every database agrees
  (`null`/`false` otherwise).
  A single-database server reports exactly what it always did;
- `databases`: one object per database, always present (a single entry on a single-database
  server): `name` (alias or file name), `file` (the file name, since `name` may be an alias),
  `kind` (`two-stage` for a `.syl2db`, `plain` for a `.syldb`), `size`, `sha256`,
  `genomes`, `c`, `k`, `screen_c`, `two_stage` and `gtdb` (`true` when any of a sample of up to
  100 genome names, spread evenly over the database, is a GTDB accession of the metadata file. It
  is `true` for a GTDB database and also for GlobDB, whose `GCA_`/`GCF_` names are GTDB accessions
  with or without the assembly version: it means "the genome names are GTDB accessions", not "this
  is GTDB's database". A fungi or UHGV server that has GTDB metadata only for taxonomy lookups reports `false`). Results carry no
  per-database field: a hit belongs to the database whose taxonomy it has. Each entry also has
  `licence`: `{"name": "CC BY-SA 4.0", "url": "https://creativecommons.org/licenses/by-sa/4.0/"}`
  or `null`. It comes from the catalogue of `sylphd download`, matched on the **file name** (not
  the `--db` alias), and is known for the GTDB and GlobDB files (both CC BY-SA 4.0). It is `null`
  for every other file (UHGV, RefSeq fungi, your own databases): `null` means "not known", not
  "unrestricted";
- `metadata`: `file` (the base name of the SQLite file), `size`, `columns`, `rows`, `release`,
  `built`, `format_version`, `reps_only` and `sources`, or `null` without `--meta`. It also carries `taxonomy_sources` — one object per
  source with `name`, `kind` (`metadata` for the virtual GTDB source, else `sylphtax`), `origin`
  (the catalogue URL it was downloaded from, or the **base name** of a local file — never a
  local path), `md5`, `keys`, `has_host`, and the `ncbi` counts of a `--ncbi-taxonomy` build and
  its `ncbi_nearest` counts (`keys` without an NCBI record, `assigned` nearest taxa, `informal`
  of those that are an informally named species, `ranks` per raw rank) — `taxonomy_overlaps` — the
  pairs of sources that share keys, each
  pair once, in setup order: `{"sources": [a, b], "keys": n, "examples": [...]}`, where `b` is
  the later-imported source and wins for those keys — and `sylphtax_rules` (the sylph-tax version
  whose rules apply). `examples` holds up to five shared keys. `taxonomies` lists every
  taxonomy `/profile` `taxprof` accepts with this file: `gtdb`/`ncbi` when their column exists,
  then `tax` and one `tax:<source>` per taxonomy source (see [`taxprof`](#taxprof)).
  Its `columns` also list the four `tax_*` columns, which `fields` can then select, and on a file
  built with `--ncbi-taxonomy` the eight `ncbi_nearest_*` columns: the nearest NCBI taxon and,
  where it is only informally named, the nearest ancestor NCBI names formally;
- `name` and `about`, **only when the operator set them** (the keys are absent otherwise):
  `name` is the string of `--name` (at most 80 characters, one line); `about` is
  `{"format": "markdown", "text": "..."}` with the contents of the `--about` file (UTF-8, at most
  16 KiB, surrounding whitespace removed). The server re-reads the file when it changes. `format`
  names how `text` is to be rendered and leaves room for other formats; today it is always
  `markdown`. The web page renders a safe subset (marked + DOMPurify, never HTML), see
  [Describing your instance](../guides/public-server.md#describing-your-instance). Clients must
  treat `text` as untrusted;
- `limits`: `max_jobs`, `max_upload_mb`, `max_sketches`, `max_contigs` (the largest `/contigs`
  batch, `--max-contigs`, 4096 by default; a whole `/contigs` request is one job),
  `max_params_bytes` (the largest non-file
  form field, i.e. how large the JSON `params` of one request may grow), `allow_sequences`,
  `max_sequence_mb` (`null` unless sequences are allowed), `rate_limit` (`{"count",
  "window_seconds"}` or `null`) and `max_queue` (`null` means unlimited);
- `usage`: `{"enabled": false}`, or with `--usage-db`
  `{"enabled": true, "policy": 1, "window_minutes": 10}`. `policy` is an integer that is raised
  whenever what the statistics track changes; the web page asks its visitor to agree again when it
  rises. See [`GET /usage`](#get-usage);
- `taxonomy`: `{"enabled": false}`, or with `--taxonomy-db` `{"enabled": true, "source", "source_date",
  "taxa", "fuzzy", "rate_limit"}`; see [`/taxonomy/*`](#taxonomy);
- `access`: `{"token_required": bool}` (`--require-token`), plus `"token_valid": bool` when the request
  carried `Authorization: Bearer ...`; see [Access tokens](#access-tokens);
- `endpoints`: the endpoint paths this server serves (`/usage` only when it is on; `/messages` is listed by servers that have it);
- `params`: the keys each POST endpoint accepts, so a client does not have to probe with 400s:
  `{"/profile": [...], "/query": [...], "/contigs": [...], "/assembly": [...]}`. The lists are
  derived from the validators: `/query` and `/profile` list the `ProfileParams` fields plus
  `names`, `fields`, `mode` and `tax_sources`, and `/profile` adds `taxprof`; `/contigs` lists its
  `params` keys (including `contigs`); `/assembly` lists its query-string parameters.

## `GET /messages` { #get-messages }

The operator's messages, for clients to show. The answer is `{"messages": [...]}`, an empty list
when the server was started without `--messages`. Each message has four fields:

| Field | Meaning |
|---|---|
| `level` | `info`, `warning` or `danger` |
| `title` | one line of text, at most 120 characters |
| `message` | Markdown, at most 2000 characters; treat it as untrusted like `about` (the web page renders it with the same sanitising) |
| `plain` | single-line plain text derived from `message` (Markdown links expanded as `text (url)`, emphasis stripped, whitespace collapsed), for terminals and other non-Markdown clients |

There are no ids or dates: a client recognises a message by its content (the web page remembers
a dismissed `info` or `warning` message by a hash of level, title and message, so changed text
shows again). The server re-reads the file when it changes. `/messages` is open and not
rate-limited like `/info`, and also answered when `--require-token` is on. A server older than this
endpoint answers `404`; clients must treat that as "no messages". The web page reads it when it
connects to a server, and `sylphd profile`, `query` and `assembly` print the `warning` and
`danger` messages on stderr, see [Operator messages](../guides/public-server.md#operator-messages).

## `GET /usage` { #get-usage }

Aggregated usage statistics; **`404` unless the server runs with
[`--usage-db`](../commands/serve.md#usage-statistics)**, in which case `/info` says so
(`usage.enabled`). Public, like `/info`, covered by `--rate-limit`, served with CORS like the
other endpoints, cached in memory for about a minute and not counted itself. The numbers come from
the database the server flushes about once a minute, so they trail reality by that much.

What is counted: each **successful** (`2xx`) request to `/assembly`, `/contigs`, `/profile` or
`/query`. Failed or rejected requests, `/health`, `/info` and `/usage` are not. A row is keyed by
the start of its ten-minute window (UTC), the endpoint, the **client kind** and the client
version:

| client | when | version |
|---|---|---|
| `web` | the request has an `Origin` header (a browser) | empty |
| `cli` | the `User-Agent` is `sylphd/<version>` (what `sylphd` sends) | the version if it is a plain `x.y.z`, else empty |
| `other` | anything else | empty |

Each row adds up `requests`, `contigs` (contigs classified), `samples` (sketches profiled; an
`/assembly` upload is one), `bases` (as reported by the client: the `length` of a sketch upload,
the sum of the contig lengths of `/contigs`, the parsed length of a raw upload; `0` for
`/profile` and `/query`, which carry no length) and `sketch_bytes` (the sketches received), and
keeps the maximum `peak_queue_depth` (jobs beyond `--max-jobs` when a job asked for its slot).
Each job's time in its slot and its wait for the slot also go into a fixed-bucket histogram per
row (`bucket_bounds_seconds`, plus one bucket above the last bound). A `/profile` or `/query` request has one job per
sample, `/assembly` and `/contigs` one per request. Windows with no activity have no row. Windows
older than 92 days are merged into monthly rows once a day; sums, maxima and histograms merge
exactly. **Never stored or served:** IP addresses or hashes of them, unique-visitor counts, sample,
file or contig names, results, taxa, no-hit shares, parameters, anything per user.

```json
{
  "policy": 1, "window_minutes": 10,
  "generated_at": "2026-10-30T18:04:18Z", "since": "2026-07-01T00:00Z",
  "bucket_bounds_seconds": [0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0, 3600.0],
  "totals": {"requests": 1788, "contigs": 13519, "samples": 1788, "bases": 6123000000,
             "sketch_bytes": 963412488, "peak_queue_depth": 3,
             "job_seconds": {"count": 1788, "median": 1.0, "p95": 30.0},
             "queue_wait_seconds": {"count": 1788, "median": 0.025, "p95": 5.0}},
  "by_endpoint": {"/assembly": {"requests": 726, "...": "..."}},
  "by_client": {"cli": {"requests": 900, "...": "..."}},
  "windows": {"columns": ["start", "endpoint", "client", "version", "requests", "contigs",
                          "samples", "bases", "sketch_bytes", "peak_queue_depth"],
              "rows": [["2026-10-30T17:50Z", "/assembly", "cli", "0.3.1", 2, 0, 2, 1160152, 68200, 0]]},
  "months": [{"month": "2026-07", "endpoint": "/query", "client": "cli", "version": "0.3.0",
              "requests": 31, "contigs": 0, "samples": 31, "bases": 0, "sketch_bytes": 912004,
              "peak_queue_depth": 1, "job_histogram": [0, 0, 3, 18, "..."],
              "queue_wait_histogram": [30, 1, "..."]}]
}
```

- `totals`, and each member of `by_endpoint` and `by_client`, have the same shape; `job_seconds`
  and `queue_wait_seconds` give the number of jobs and the **median** and **95th percentile**,
  read from the histograms as the **upper bound of the bucket** that holds them (so they are
  upper bounds, and `null` without jobs).
- `windows` holds the retained ten-minute rows in a compact columnar form (histograms are only in
  the totals and in `months`, to keep the response small); `months` the monthly rows, kept for
  good, with their histograms.
- `since` is the start of the oldest row, `null` when nothing was recorded yet.

## `POST /query` and `POST /profile` { #post-query-and-post-profile }

The body is `multipart/form-data`:

- one **`sketch`** file part per sample, holding the raw bytes of a `pysylph.SampleSketch` (the
  contents of a `.sylsp` file);
- an optional **`params`** field with a JSON object.

At most 4096 sketches fit in one request. Non-file form fields such as `params` may be at most
1 MiB, and at most 16 of them are accepted.

| `params` key | Meaning |
|---|---|
| `minimum_ani` | minimum adjusted ANI, 0-100; `null` or omitted uses sylph's default |
| `seq_id` | read sequence identity in percent, 0-100 |
| `estimate_unknown` | boolean; `Sequence_abundance` is then a share of all reads, so 100 minus its sum is the estimated share in no database. A sketch without read statistics (made from contigs) gets `null` for `Eff_cov` and both abundances (sylph's inf/NaN) |
| `estimate_read_counts` | boolean, `profile` only; `Sequence_abundance` then holds estimated read counts (sylph's experimental option) and implies `estimate_unknown` |
| `min_number_kmers` | integer >= 0 (default `10`) |
| `min_contain` | integer >= 0 (default `7`) |
| `screen_ani` | 0-100 (default `85`) |
| `no_adjust` | boolean, report the naive ANI |
| `names` | list with one name per sketch (defaults to the part file names, else `sample-N`) |
| `fields` | list of metadata columns; omitted means all, `[]` means none; needs a metadata database. Any requested field whose name collides with a fixed column of the response table (`accession`, `genome_id`, `Sample_file`, `Lambda_status`, ...) is a `400` |
| `tax_sources` | list of taxonomy source names: resolve hits against these sources only (default: all of them; an empty list is the same). Unknown names, or a server without sylph-tax sources, are a 400 |
| `taxprof` | `profile` only: taxonomies to aggregate on the server, from `gtdb`, `ncbi`, `tax` and `tax:<source>` (listed in `/info` `metadata.taxonomies`). Validated against the metadata file's columns and sylph-tax sources (a 400 names what is missing). The response document then carries `taxprof[sample_name][taxonomy]` (see below) |
| `mode` | optional, must match the endpoint (`query` or `profile`) |
| `samples` | optional list with one object per sketch, in request order: `{"name": "...", "reads": 1000, "bases": 150000}`. `reads` (a read pair counts once) and `bases` are what the client counted while sketching; each is a non-negative integer or `null`/omitted when unknown (a pre-made `.sylsp` has no read counts). They only come back in the response `samples`; a bad value, another key or a wrong length is a 400 |

Unknown keys are rejected with a 400. The client sketches with `database.c` and `database.k` from
`GET /info`. A sketch must match `k` exactly and must not be sparser than the database
(`c <= db.c`); otherwise the request fails with a 400. A larger `c` means a *sparser* sketch, so a
sketch with `c=250` is refused by a database with `c=200`; a sketch with `c=100` is accepted (it is
denser, and sylph samples down while profiling). On a server with several databases, `k` must
match all of them and `c` must not exceed the **smallest** `c`; the 400 names the database(s) that
cause the refusal, for example `sketch c=250 is sparser than the c=200 of uhgv`. The same 400
covers a sketch that pysylph itself refuses to profile against the database, for example one
sparser than a genome of a database whose genomes have different `c`: the response carries
pysylph's message, not a generic 500. `POST /query` and `POST /profile` run against the whole pool, i.e.
`sylph query db1 db2 ...` / `sylph profile db1 db2 ...`.

The response is a single JSON document covering all samples in request order:

```json
{"api_version": 1,
 "params": {"minimum_ani": null, "seq_id": null, "estimate_unknown": false,
            "estimate_read_counts": false, "min_number_kmers": 10,
            "min_contain": 7, "screen_ani": 85.0, "no_adjust": false},
 "samples": [{"name": "sample-a", "kmers": 25621, "mean_read_length": 150.0, "paired": false,
              "reads": 1000, "bases": 150000},
             {"name": "sample-b", "kmers": 31002, "mean_read_length": 148.7, "paired": true,
              "reads": null, "bases": null}],
 "table": {
   "columns": ["Sample_file", "Genome_file", "Taxonomic_abundance", "Sequence_abundance",
               "Adjusted_ANI", "Eff_cov", "ANI_5-95_percentile", "Eff_lambda",
               "Lambda_5-95_percentile", "Median_cov", "Mean_cov_geq1", "Containment_ind",
               "Naive_ANI", "kmers_reassigned", "Contig_name", "Lambda_status",
               "accession", "genome_id", "gtdb_taxonomy"],
   "data": [
     ["sample-a", "GCA_000005845.2_genomic.fna.gz", 100.0, 100.0,
      99.79, 1.187, [99.64, 99.95], 0.36,
      [0.34, 0.38], 1.0, 1.187, [6208, 21899],
      96.02, 0, "NC_000913.3", "OK",
      "GCA_000005845.2", "GCA_000005845.2", "d__Bacteria;..."]
   ]
 },
 "caveats": []}
```

- **`api_version`** is the integer wire-protocol version (same as in `GET /info`).
- **`params`** echoes the **effective** profiling parameters, defaults included, as provenance.
- **`samples`** has one entry per sample sent, in request order, so a sample without hits is
  distinguishable from one that was never sent: `name`; `kmers` (distinct k-mers of the uploaded
  sketch), `mean_read_length` and `paired` (from the sketch header); and `reads` and `bases`, the
  values the client sent in `params.samples` (`null` when it did not). An entry with no hits and few
  k-mers explains an empty result, e.g. one mate of a pair alone.
- **`table`** holds every hit of every sample in one standard table (`{"columns": [...], "data": [[...], ...]}`,
  pandas' `orient="split"` layout without an index; `pd.DataFrame(**doc["table"])` loads it directly).
  Every row carries its `Sample_file`. `columns` is the final ordered header: `Taxonomic_abundance`,
  `Sequence_abundance` and `kmers_reassigned` appear only for `/profile`; with `estimate_unknown` or
  `estimate_read_counts`, the actual column name is `True_cov` (not `Eff_cov`); with
  `estimate_read_counts`, `Sequence_abundance` holds the estimated number of reads per genome
  (a rounded float) instead of a relative abundance. `Lambda_status` appears as a separate column
  immediately before `accession`, which `genome_id` follows (the stable identifier of the database genome: the accession of a GTDB hit, the taxon key of a hit a non-GTDB source resolved, else the first word of `Contig_name`, else `Genome_file`), keeping `Eff_lambda` numeric (or `null` when not estimable): this
  is the only column difference between the JSON table and the TSV header. Selected metadata columns
  follow `genome_id` in request or metadata order.
- **`caveats`** is a list of distinct species-threshold warning strings for non-GTDB taxonomy
  sources present among the hits (empty for GTDB-only hits or without metadata).
- Missing or non-finite values are JSON `null` (`Eff_lambda` when `Lambda_status` is `HIGH` or
  `LOW`, missing confidence intervals, and `True_cov`/abundances of a sketch without read statistics
  under `estimate_unknown`). Interval columns (`ANI_5-95_percentile`, `Lambda_5-95_percentile`,
  `Containment_ind`) stay two-element arrays. TSV renderers format `null` as sylph does (`HIGH`/`LOW`
  from `Lambda_status`, `NA-NA`, `inf`, `NaN`) and cap `Adjusted_ANI` at 100 while JSON keeps the
  unrounded/uncapped value. With `no_adjust`, `Adjusted_ANI` is the naive containment ANI while
  `ANI_5-95_percentile` is still sylph's interval of the adjusted estimate (as in stock sylph's
  `--no-adjust`), so it can lie away from `Adjusted_ANI`; it is `null` only when sylph computes no
  interval.

With sylph-tax taxonomy sources (see [the metadata reference](metadata.md#taxonomy-sources)), every row has four more columns, and
they are listed in `metadata.columns` so `fields` can select them:

| Column | Meaning |
|---|---|
| `tax_source` | name of the taxonomy source whose lineage was used (the largest `ord` that has the hit's key), or `null` |
| `tax_key` | the key sylph-tax matched (verbatim: an accession, a file name, a contig id, ...), or `null` |
| `tax_lineage` | the source's lineage string, opaque (`vFAM-…;vOTU-…`, `d__…;s__…`), or `null` for a hit without any taxonomy |
| `tax_host` | the virus host of that key, from the largest `ord` whose source has one, or `null` |

The server resolves them with sylph-tax's own probe rules over the hit's `Genome_file` and
`Contig_name` (see [the metadata reference](metadata.md#resolving-a-hit)), so `query` and
`profile` rows carry them too. `/assembly` hits carry the same four fields.

### Server-side taxonomic profiles (`taxprof`) { #taxprof }

With `"taxprof": ["gtdb", "ncbi", "tax"]` the server aggregates each sample's hits once and adds a
top-level `taxprof` object keyed by sample name and taxonomy. It returns the structured clades
**and** the rendered `.sylphmpa` text, so clients neither aggregate nor format:

```json
"taxprof": {
  "sample-a": {
    "gtdb": {
      "columns": ["clade_name", "relative_abundance", "sequence_abundance",
                  "ANI (if strain-level)", "Coverage (if strain-level)"],
      "taxonomies_used": ["gtdb"],
      "clades": [
        {"clade_name": "d__Bacteria", "relative_abundance": 100.0,
         "sequence_abundance": 100.0, "ani": null, "coverage": null},
        {"clade_name": "d__Bacteria|...|t__GCA_000005845.2", "relative_abundance": 100.0,
         "sequence_abundance": 100.0, "ani": 99.79, "coverage": 1.234}
      ],
      "sylphmpa": "#SampleID\tsample-a\tTaxonomies_used:['gtdb']\nclade_name\t..."
    }
  }
}
```

`clades` is the cumulative lineage table (`clade_name` joins the levels with `|`; only strain rows
carry `ani`/`coverage`), ordered as `sylph-tax taxprof` orders the file. The `tax` profile is equivalent to sylph-tax v1.9.2's on the same sylph run: the same clades, values equal within the TSV's rounding (sylphd works at full precision, sylph-tax sums sylph's rounded TSV), and clades with equal values may be ordered differently. Check with [`sylphd diff`](../commands/diff.md). `sylphmpa` is
that file, newline-terminated. For `tax`, `taxonomies_used` lists the source names
the profile was resolved against (`tax_sources`, in `ord` order). `gtdb`/`ncbi` come from the
metadata columns of the same name; `tax` needs a metadata file with sylph-tax sources. The
server fetches the taxonomy columns needed for aggregation internally even when `fields` hides
them from `table`. `taxprof` is `/profile` only (`/query` rejects it), and one requested taxonomy the metadata
file cannot provide is a 400.

In `ncbi`, a hit with no NCBI lineage of its own that the metadata file gave a nearest NCBI taxon
(`ncbi_nearest_lineage`) is placed at that raw node, its strain directly below it, and the strain's
clade object carries `"nearest": {"taxid": …, "rank": …, "name": …, "clade": "g__Thiobacillus"}`.
The `ncbi_nearest_formal_*` columns say which ancestor NCBI names formally when the raw node is an
informally named species; the profile places the hit at the raw node either way.
A metadata file without nearest taxa profiles exactly as before.

`tax:<source>` (for example `tax:UHGV_default`) is one taxonomy source alone, following
`sylph-tax taxprof -t <source>`: the hits are resolved against that source only, so the hits of
other sources are `NO_TAXONOMY`, and a key that a later source overrides in `tax` keeps this
source's lineage. `taxonomies_used` is `[source]`. Its payload adds the source's `ranks`, read
from the lineage tokens (`s__Name` is rank `s`, `vOTU-052719` is rank `vOTU`) and ordered from the
root as the lineages order them, and every clade a `rank` (that of its last token; `null` for
strains, `NO_TAXONOMY` and `UNKNOWN`). Lineages may skip ranks (a vOTU without a vGENUS), so a
client picks a rank's clades by `rank`, not by depth:

```json
"tax:UHGV_default": {
  "taxonomies_used": ["UHGV_default"],
  "ranks": ["vFAM", "vSUBFAM", "vGENUS", "vSUBGEN", "vOTU"],
  "clades": [{"clade_name": "vFAM-00009|vSUBFAM-00023|vOTU-166116", "rank": "vOTU", ...}, ...],
  ...
}
```

## `POST /assembly` { #post-assembly }

Finds the closest database genomes of **one assembly**. The body is either

* a **sketch** of the assembly: the raw bytes of a `pysylph.SampleSketch` (a `.sylsp` file), with

    ```text
    Content-Type: application/vnd.sylph.sketch
    ```

    This is always available, with or without `--allow-sequences`: the server never sees a
    sequence. The web app (in the browser) and `sylphd assembly` (on the command line) send this.
    The sketch is made with the database's `c` and `k` (`/info` reports them), as one single-end
    sample whose "reads" are the assembly's records. It is validated like the sketches of
    `/profile` (header check before decoding, `k` must equal the database's, `c` must not exceed
    the database's), and bounded by the general `--max-upload-mb`, not by `--max-sequence-mb`; or

* **raw FASTA or FASTQ**, plain or gzip, which the server sketches itself. Only with
  `--allow-sequences`, otherwise a `404` whose message says so. The gzip magic bytes (`\x1f\x8b`)
  select decompression (concatenated gzip members are read in turn; a truncated stream is a `400`); the `Content-Type` header is otherwise informational, but must be one of:

    ```text
    Content-Type: text/plain | application/octet-stream | application/gzip
    ```

    (a missing `Content-Type` is accepted). All records of the body form one sample. Only the bases
    `A C G T N` are allowed.

Any other `Content-Type` is a `415`. `limits.allow_sequences` in `GET /info` says whether raw
sequences are accepted.

| Query parameter | Meaning |
|---|---|
| `top` | number of hits, 1-50 (default `10`) |
| `min_ani` | minimum ANI in percent, 70-100 (default `80`) |
| `name` | query name (default `assembly`, at most 1024 characters) |
| `records`, `length`, `gc` | sketch uploads only, optional: number of records (integer), total length in bases (integer) and GC percent (0-100) of the assembly, as counted by the client. Purely informational: they are validated and echoed in `query`, never used for the search. Ignored for raw sequences, which the server counts itself |

Both bodies get exactly the same treatment from then on: the server profiles the sketch with
`no_adjust=True` and keeps the `top` hits sorted by ANI. The same assembly gives the same hits,
assignment and closest hit whichever way it is sent. `/assembly` queries the whole pool, so a hit
list may mix databases. A hit resolved by a sylph-tax taxonomy source of a format 3 metadata file
carries `tax_lineage`/`tax_source` and is classified from that lineage: `taxonomy` and
`taxonomy_source` are added to the hit and to `assignment`/`closest` (GTDB's
`d__..;s__`, fungal `k__Eukaryota;..`, ICTV `root;..`, UHGV `vFAM-..;vOTU-..`, see
[hit relations](#hit-relations)). A hit without any lineage stays `unknown lineage`.

```json
{"api_version": 1,
 "query": {"name": "assembly", "records": 1, "length": 6013491,
           "gc": 50.76, "sketch_kmers": 200, "input": "sequence"},
 "database": {"name": "gtdb-r232-c200-dbv2.syl2db", "genomes": 199937,
              "c": 200, "k": 31},
 "params": {"top": 10, "min_ani": 80.0},
 "thresholds": {"species": 95.0, "imprecise_below": 85.0},
 "assignment": {"rank": "species", "taxon": "Escherichia coli",
                "gtdb_taxonomy": "d__Bacteria;...;s__Escherichia coli",
                "accession": "GCF_000005845.2", "ani": 98.65,
                "taxonomy": {"domain": "Bacteria", "...": "...", "species": "Escherichia coli"},
                "taxonomy_source": "GTDB_r232"},
 "closest": {"rank": "species", "taxon": "Escherichia coli",
             "gtdb_taxonomy": "d__Bacteria;...;s__Escherichia coli",
             "accession": "GCF_000005845.2", "ani": 98.65, "taxonomy": {"...": "..."},
             "taxonomy_source": "GTDB_r232"},
 "elapsed_ms": 412.7,
 "table": {
   "columns": ["rank", "ani", "relation", "shared_rank", "accession", "genome_id",
               "taxon", "gtdb_species", "taxonomy_source", "ncbi_organism_name", "ncbi_taxid",
               "containment", "containment_fraction", "ani_species_level", "genome_file",
               "taxonomy", "gtdb_taxonomy", "..."],
   "data": [
     [1, 98.65, "same species", "species", "GCF_000005845.2", "GCF_000005845.2",
      "Escherichia coli", "Escherichia coli", "GTDB_r232", "Escherichia coli K-12", 511145,
      [7096, 10826], 0.655, true, "/db/GCA_000005845.2_genomic.fna.gz",
      {"domain": "Bacteria", "...": "...", "species": "Escherichia coli"},
      "d__Bacteria;...;s__Escherichia coli", "..."]
   ]
 }}
```

- **`api_version`** is the integer wire-protocol version (same as in `GET /info`).
- **`table`** holds the hits in the standard `orient="split"` format (`{"columns": [...], "data": [[...], ...]}`).
  The leading 11 columns match the CLI's hits TSV (`rank`, `ani`, `relation`, `shared_rank`,
  `accession`, `genome_id`, `taxon`, `gtdb_species`, `taxonomy_source`, `ncbi_organism_name`,
  `ncbi_taxid`), where the server computes **`taxon`** (the last named rank of the hit's lineage, or
  `null`) and **`gtdb_species`** (the GTDB `s__` name, or `null`). Next come `containment`,
  `containment_fraction`, `ani_species_level`, `genome_file`, `taxonomy`, and every remaining
  metadata column of the server's metadata database (with `null` for missing values; a server
  without `--meta` keeps the full standard metadata column list with `null` cells so the shape is
  stable).
- **`database`** is the pooled view (see `GET /info`); the response has no `databases` key, add
  `GET /info` when a client needs the per-database list.
- **`query.input`** is `"sequence"` or `"sketch"`, how the assembly arrived. For a sketch,
  `query.records`, `length` and `gc` are the values of the optional query parameters, or `null`
  when the client did not send them (the server cannot know them); `sketch_kmers` is read from the
  sketch.
- **`ani`** is the naive containment ANI (`no_adjust=True`). There is no confidence interval: the
  coverage-adjusted ANI is meaningless for assemblies, see
  [Identifying an assembly](../guides/assembly.md#why-the-ani-is-not-adjusted).
- **`containment`** is `[contained k-mers, genome k-mers]`: how many of the hit genome's sketched
  k-mers occur in the assembly, and how many k-mers that genome's sketch holds (sylph's
  `Containment_ind`). `containment_fraction` is their ratio.
- **`assignment`** is the species assignment: `{"rank": "species", "taxon", "gtdb_taxonomy",
  "accession", "ani", "taxonomy", "taxonomy_source"}` when the best hit reaches
  `thresholds.species` (95 %), otherwise `null`. `taxon` and `gtdb_taxonomy` are `null` without
  metadata; `taxon` is the **last named rank** of the hit's lineage (GTDB's species, a UHGV vOTU, a
  fungal species). `taxonomy` is the lineage as `{rank: taxon}`, shallowest first — `{"domain":
  "Bacteria", ..., "species": "Escherichia coli"}` or `{"level 1": "vFAM-00013", ..., "level 5":
  "vOTU-052719"}` — and `taxonomy_source` the source that named it (`GTDB_r232`, `UHGV_default`,
  ...). On `assignment`/`closest` both keys are **absent** when the hit has no lineage; in `table`
  their cells are `null`.
- **`note`** (on `assignment`, or on `closest` when there is no assignment) explains what 95 % ANI
  means for a best hit that is **not** from GTDB: the vOTU convention for viruses (whose aligned
  fraction sylph's ANI ignores), a heuristic for fungi, a generic text for other sources. It is
  absent for GTDB and for a hit without a lineage.
- **`closest`** has the same shape for the best hit, or `null` without hits.
- **`genome_id`** is the identifier to show for a hit: the `accession` for a GTDB hit (or any
  hit that carries one), the `tax_key` for a hit a non-GTDB taxonomy source resolved, else the
  first token of `Contig_name`, else `genome_file`. It exists because a genome file is not always
  an identifier: UHGV stores every genome in `votus_full.fna.gz`.
- **`thresholds.imprecise_below`** (`85`) is only a hint that k-mer ANI below it is imprecise. It
  never changes an assignment.
- Uploads are never logged.

### Raw sequence with curl { #raw-sequence-with-curl }

Any language can call the endpoint without pysylph or WebAssembly: post the FASTA (or FASTQ) file as
the request body and put the parameters in the query string. Use `--data-binary`, which sends the
file unchanged; plain `-d` would strip newlines.

```bash
curl --data-binary @assembly.fasta -H 'Content-Type: text/plain' \
    'https://sylphd.abrinca.com/assembly?top=5&min_ani=85&name=my-assembly'

# a gzip file is sent as it is
curl --data-binary @assembly.fasta.gz -H 'Content-Type: application/gzip' \
    'https://sylphd.abrinca.com/assembly?top=5'
```

The response is the JSON document above. This needs a server started with `--allow-sequences`; the
public instance (`https://sylphd.abrinca.com`, the one the web page uses) has it, and `limits` in
`GET /info` tells you for any other server. The sequence then reaches the server, which sketches it
in memory; if that is not acceptable, [sketch locally](../guides/assembly.md) instead. The public
instance limits each client to 10 POST requests per minute and the body to `--max-sequence-mb` (20 MB by
default, before decompression); over the rate limit you get a `429` with `Retry-After` (see
[Errors](#errors)). See the [terms](../project/terms.md) for what counts as light use.

### Hit relations { #hit-relations }

`relation` is the human label and `shared_rank` its machine form. They come from the hit's
**lineage**, not from the ANI. Lineages are parsed generically: rank prefixes `d` domain,
`r` realm, `k` kingdom, `p` phylum, `c` class, `o` order, `f` family, `g` genus, `s` species,
`t` strain; a prefix-less lineage (UHGV's `vFAM-..;vOTU-..`, ICTV's `root;Monodnaviria;..`) is
read as `level 1` ... `level N`, with a leading `root` dropped. `shared_rank` is the deepest rank a
hit shares with the reference lineage (`species` ... `domain`, `realm`, `kingdom`, `strain`,
`level N`, or `null`); a rank only counts when all shallower ranks agree. The reference is the best
hit's lineage.

Relations are computed **within one taxonomy source only**. A hit from another source has no
comparable ranks, so it gets `different source` (or `different source from closest relative`) and
`shared_rank: null`. GTDB hits always compare with each other, whatever `tax_source` resolved them;
a hit whose lineage shares nothing with a non-GTDB reference is `different lineage`.

| Situation | `relation` of the hit |
|---|---|
| assigned, shares rank *R* with the assigned species | `same species`, `same genus`, `same family`, `same order`, `same class`, `same phylum`, `same domain`, `same level 4`, ... |
| assigned, shares nothing | `different domain` (GTDB) or `different lineage` |
| a hit of another taxonomy source | `different source` |
| not assigned, the best hit | `closest relative` (`shared_rank` is `null`) |
| not assigned, other hits, sharing rank *R* with the best hit | `same genus as closest relative`, ... |
| not assigned, sharing nothing with the best hit | `different domain from closest relative` or `different lineage from closest relative` |
| not assigned, a hit of another source | `different source from closest relative` |
| a hit without lineage next to classified hits | `unknown lineage` |
| no metadata at all (ANI only) | `same species` for `ani >= 95`, else `related` |

A hit in the same species as the assignment is `same species` whatever its ANI.
`ani_species_level` is `ani >= 95` for every hit: a hit that has it but a different species name (a
species boundary case) gets the lineage relation, for example `same genus`, plus
`ani_species_level: true`. The 95 % threshold is a prokaryote convention and applies to every
source for now; the `note` above says so where it is a heuristic, and `--per-contig` and the `tax`
profile print the same caveat once on stderr.

## `POST /contigs` { #post-contigs }

Classifies the contigs of one **fragmented assembly**: one result per contig with the
contig-relative ANI of its hits, the species assignment, and the taxonomic consensus. This is what
`sylphd assembly FILE --per-contig` uses; the ANI, the ranking, the classification and the consensus
are computed on the server, so every client gets the same answer.

The body is `multipart/form-data`:

- one **`sketch`** file part per contig: the contig sketched as a single-end sample with the
  database's `c` and `k` (exactly the bytes `POST /query` takes, a `.sylsp` file's contents);
- a **`params`** field with a JSON object:

```json
{"contigs": [{"name": "contig1", "contig_kmers": 84, "length": 20000, "gc": 51.2}],
 "top": 1, "min_ani": 80.0, "consensus_ratio": 1.5, "consensus_z": 2.0,
 "fields": [], "tax_sources": null}
```

| `params` key | Meaning |
|---|---|
| `contigs` | required, one object per `sketch` part in the same order: `name` (a valid sample name, unique in the request), `contig_kmers`, `length`, `gc` |
| `contig_kmers` | the record's **genome-style k-mer count**, a positive integer no larger than `length - k + 1`. It is the denominator of the contig-relative ANI: sketch the record with `pysylph.Sketcher(c, k).sketch_genome(...)` and count its k-mers. It is *not* the sketch's k-mer count (a sample sketch keeps duplicates and skips the minimum-spacing filter) |
| `length`, `gc` | the record's length in bases (1 to 1e13) and GC percent (0-100); informational, echoed in the result and used for the per-request summary |
| `top` | hits per contig, 1-50 (default `1`) |
| `min_ani` | minimum **contig-relative** ANI in percent, 70-100 (default `80`); a contig whose best hit is below it is `no_hit` |
| `consensus_ratio` | the close-band ratio `R` of the consensus rule, `>= 1` (default `1.5`) |
| `consensus_z` | the noise allowance `Z` of the consensus rule, `>= 0` (default `2`) |
| `fields` | metadata columns appended to `table`; omitted means all, `[]` means none; needs a metadata database. Any requested field whose name collides with a fixed column of the table (`gc`, `taxon`, `accession`, ...) is a `400` |
| `tax_sources` | like `/query`: resolve hits against these taxonomy sources only |

Unknown keys are a 400. **A whole request is one job**, whatever its number of contigs: its
contigs are queried one after the other inside that single job slot, so `--max-jobs` and
`--max-queue` bound concurrent requests (a second request beyond the capacity is refused whole
with `503`), and `--rate-limit` sees one request. The per-request bound is
`limits.max_contigs` (`--max-contigs`, 4096 by default; a public server should set 100, because a
contig on a server with plain databases costs ~0.5-0.6 s and the whole request is one job). Each
sketch is validated like a `/query`
sketch (`k` must match, `c` must not be sparser than the database's), and the whole request is
refused with a 400 if any entry is invalid (a count that does not match the sketch parts, a
`contig_kmers` no sequence of that length can have, a duplicate name, ...).

The response is a JSON document holding a flat per-hit `table`, per-contig metadata and summaries
keyed by contig name in `contigs` (in request order), and a per-request `summary`:

```json
{"api_version": 1,
 "params": {"top": 1, "min_ani": 80.0, "consensus_ratio": 1.5, "consensus_z": 2.0, "fields": []},
 "table": {
   "columns": ["contig", "contig_length", "gc", "status", "rank", "ani",
               "shared_kmers", "contig_kmers", "explained_fraction", "reference_ani",
               "relation", "shared_rank", "ani_species_level", "accession", "genome_id",
               "gtdb_species", "taxon", "n_close", "consensus_rank", "consensus_taxon"],
   "data": [
     ["contig1", 20000, 51.2, "ok", 1, 99.12,
      78, 84, 0.9286, 96.16,
      "same species", "species", true, "GCA_...", "GCA_...",
      "Escherichia coli", "Escherichia coli", 2, "family", "Enterobacteriaceae"],
     ["contig2", 15000, 48.0, "no_hit", null, null,
      null, null, null, null,
      null, null, null, null, null,
      null, null, null, null, null]
   ]
 },
 "contigs": {
   "contig1": {"length": 20000, "gc": 51.2, "sketch_kmers": 95, "contig_kmers": 84,
               "status": "ok",
               "assignment": {"rank": "species", "taxon": "...", "accession": "...", "ani": 99.12},
               "closest": {"...": "..."},
               "consensus": {"rank": "family", "taxon": "Enterobacteriaceae",
                             "n_close": 2, "species": ["...", "..."]}},
   "contig2": {"length": 15000, "gc": 48.0, "sketch_kmers": 80, "contig_kmers": 72,
               "status": "no_hit",
               "assignment": null, "closest": null, "consensus": null}
 },
 "summary": {"contigs": 2, "bp": 35000, "queried_contigs": 2, "queried_bp": 35000,
             "too_few_kmers_contigs": 0, "too_few_kmers_bp": 0, "no_hit_contigs": 1,
             "consensus_bp": [{"rank": "family", "taxon": "Enterobacteriaceae",
                               "bp": 20000, "fraction": 0.5714},
                              {"rank": null, "taxon": "unassigned",
                               "bp": 15000, "fraction": 0.4286}]},
 "caveats": [], "database": {"name": "...", "genomes": 199937, "c": 200, "k": 31},
 "thresholds": {"species": 95.0, "imprecise_below": 85.0},
 "elapsed_ms": 12.5}
```

- **`api_version`** is the integer wire-protocol version (same as in `GET /info`).
- **`table`** holds the flat per-hit table in `orient="split"` format: the 20 fixed columns above
  plus any requested `fields`, one row per hit. A contig without hits (`no_hit` or `too_few_kmers`)
  appears as one row with `null` in every hit, consensus, and metadata column.
- **`contigs`** maps each contig name (in request order) to its measurements, `status`, optional
  `reason` (for `too_few_kmers`), `assignment`, `closest`, and `consensus`.
- **`status`** is `ok` (hits), `no_hit` (queried, nothing above `min_ani`) or `too_few_kmers` (the
  sketch holds fewer k-mers than sylph's floor of 10; such a contig is not queried, and `reason`
  says so).
- **`ani`** is the contig-relative ANI, `100 * (shared / contig_kmers) ** (1/k)`, **not** sylph's
  `Naive_ANI`. `shared_kmers` is `Containment_ind[0]` (reference k-mers found in the contig),
  `explained_fraction` is `shared / contig_kmers` (clamped at 1), and `reference_ani` is sylph's
  genome-denominator `Naive_ANI` in percent. An exact fragment scores exactly 100 %.
- **Ranking.** Hits are ordered by the contig-relative ANI (equivalently by `shared`, the
  likelihood order). Hits within the consensus close band of the best hit (the same rule the
  consensus uses: `100 - ani <= R * (100 - ani_best) + Z * sqrt(sd_best^2 + sd^2)`) **and** with
  `explained_fraction >= 0.95` are statistically indistinguishable on the contig side and
  near-completely contained, so among them the reference with the fewest k-mers comes first — that
  is the highest `reference_ani`, the most parsimonious explanation of the contig. A public
  assembly that carries a contaminant contig (a lambda prophage in a bacterial genome, say)
  contains every k-mer of the contig, exactly like the much smaller true source; only the genome
  size separates them. The saturation condition keeps the rule to containment: at `k=31`,
  `explained_fraction = 0.95` is already ~99.83 % ANI, so a >= 95 % assignment can never be moved
  below the species threshold. Everything else keeps the evidence order.
- **`consensus`** is the lowest common rank of the species whose divergence is inside that band
  (the best hit included), or `null` without hits. `rank`/`taxon` are `null` when a close lineage
  is missing or when the close set spans two taxonomy sources; the latter also sets
  `mixed_sources: true`. Hits that merely *contain* the contig in a far larger genome are **not**
  dropped from the consensus, so a phage contig whose close set spans UHGV and GTDB genomes stays
  `mixed_sources`. The full rule, with `n_close` and the species list, is in
  [`sylphd assembly`](../commands/assembly.md#per-contig-consensus); `consensus` uses every hit of
  the contig, whatever `top` and `min_ani` are.
- **`assignment`/`closest`** have the shape of `POST /assembly`'s, computed from the ranked hits.
- **`summary`** covers the contigs of this request only: `contigs`/`bp` of everything sent,
  `queried_contigs`/`queried_bp` of those actually queried, the `too_few_kmers_*` counters, and
  `consensus_bp`: **every** consensus taxon with its bases and its share of the queried bases
  (not truncated, so a client can merge the summaries of several requests and cut afterwards).
- **`caveats`** lists the distinct species-threshold notes of the request (viruses, fungi, other
  sources; see [`POST /assembly`](#post-assembly)).
- `summary`, `caveats` and `database` are per request, so a client that splits a large assembly into
  several requests adds the counters and merges `consensus_bp` by `(rank, taxon)`.

## `/taxonomy/*` { #taxonomy }

Only on a server started with [`--taxonomy-db`](../commands/taxdb.md) (`/info` says
`taxonomy.enabled`). The calls are index lookups: they never wait for a job slot and are covered by
`--taxonomy-rate-limit`, not by `--rate-limit`. Bodies are JSON (at most 1 MiB), at most 1000 taxids
or names per request. A merged taxid answers with its replacement (`merged_from`); an unknown or
deleted one answers `null`. The [Python objects](../guides/ncbi-taxonomy.md) wrap these calls.

| Endpoint | Request | Answer |
|---|---|---|
| `GET /taxonomy/info` | | `source`, `source_date`, `taxa`, `names`, `fuzzy`, `divisions`, `genetic_codes` |
| `POST /taxonomy/nodes` | `{"taxids": [562, ...]}` | `{"nodes": [record or null, ...]}` in order |
| `POST /taxonomy/lineage` | `{"taxids": [...]}` | `{"lineages": [[{taxid, name, rank}, ...root first] or null]}` |
| `POST /taxonomy/names` | `{"taxids": [...]}` | `{"names": [[{name, name_class, unique_name}, ...] or null]}` |
| `POST /taxonomy/children` | `{"taxid": 561, "descendants": false, "rank": null, "limit": 100000}` | `{"taxid", "taxa": [{taxid, name, rank, parent}] or null, "truncated"}` |
| `POST /taxonomy/lookup` | `{"names": ["E. coli", ...]}` | `{"results": [{"query", "matches": [{taxid, name, name_class, scientific_name, rank}]}]}` |
| `POST /taxonomy/search` | `{"query": "escherichia c", "fuzzy": false, "rank": null, "limit": 20}` | `{"hits": [{taxid, name, name_class, scientific_name, rank, score}]}`; `limit` at most 100 |

`/taxonomy/search` is the autocomplete. A `query` of letters completes names (`name` is the matched name,
`name_class` its class, `scientific_name` the taxon's own name, `score` 1.0 for an exact match;
`fuzzy` tolerates typos). A `query` of **digits only** completes the taxid instead: the exact
taxid first, then those that start with the digits, shorter ids first (`"56"` gives 56, 561, 562,
563, ...), with `name_class` `"taxid"` and `fuzzy` ignored. An old taxid that was merged answers
with its replacement (`name_class` `"merged taxid"`, `name` the id you typed). Each hit is one taxon;
fetch the full record with `/taxonomy/nodes`.

A node record has `taxid`, `parent`, `rank`, `name`, `unique_name`, `embl_code`, `division_id`,
`genetic_code_id`, `mito_genetic_code_id`, `plastid_genetic_code_id`,
`hydrogenosome_genetic_code_id`, `hidden`, `hidden_subtree_root`, `specified_species`, `comments`
(and `merged_from` when it was merged). Bad input is a `400`, a body over 1 MiB a `413`, and over
the rate limit a `429` with `Retry-After`.

## Errors

Errors are JSON objects `{"error": "..."}` with a 4xx or 5xx status.

| Status | When |
|---|---|
| `400` | bad parameters, invalid `params` JSON, unknown metadata columns, requested `fields` that collide with a fixed column of the response table (`gc`, `taxon`, `accession`, ...), a sketch with the wrong `c` or `k`, a malformed sketch, more than 4096 sketches in one request (or more than the server's `max_contigs` for `/contigs`); for `/assembly` an empty body, a malformed sketch (or one with the wrong `c` or `k`), invalid `records`/`length`/`gc`, malformed or non-`ACGTN` raw sequence, or a `Content-Length` that is not an integer; for `/contigs` a `contigs` list that does not match the sketch parts, a duplicate or unusable contig name, a `contig_kmers` that is not a positive integer or that no sequence of that `length` can have, or an out-of-range option |
| `401` | `--require-token` and no valid token (`WWW-Authenticate: Bearer`) |
| `404` | `POST /assembly` with raw sequences without `--allow-sequences` (sketches are always accepted); `GET /usage` without `--usage-db` |
| `413` | the body exceeds `--max-upload-mb`, or for raw `/assembly` sequence the body exceeds `--max-sequence-mb` or its decompressed size exceeds five times that |
| `415` | `/query`, `/profile` and `/contigs` without a `multipart/form-data` body, or `/assembly` with an unsupported `Content-Type` |
| `429` | rate limited (`--rate-limit`, also for `GET /usage`; `--taxonomy-rate-limit` for `/taxonomy/*`); with `Retry-After` in seconds; a valid token skips it |
| `503` | the job queue is full (`--max-queue`), or the request has more samples than fit in it; nothing was run; with `Retry-After: 1` |
| `500` | profiling failed inside the server |

## Browser access

CORS is off unless `--cors-origin` is given; see
[Running a public server](../guides/public-server.md#cors). When enabled, `GET` and `POST` and the
`Content-Type` request header are allowed, and `Retry-After` and `X-Request-Id` are exposed to
JavaScript.
