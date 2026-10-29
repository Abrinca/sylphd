# HTTP API

The server speaks HTTP, on a Unix socket by default, or on TCP with `--tcp`. All responses are JSON.
This page documents the wire format that `sylphd` clients and the web app use. There is no
authentication; see the [security notes](security.md).

| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | service name, version and the endpoints this server offers |
| `/health` | GET | liveness: status, version, uptime |
| `/info` | GET | database, metadata, limits and endpoints |
| `/query` | POST | ANI hits of uploaded sketches |
| `/profile` | POST | taxonomic profile of uploaded sketches |
| `/assembly` | POST | closest genomes of one assembly, sent as a sketch (always) or as raw sequence (only with `--allow-sequences`) |

`/health` and `/info` are always served. The abuse limits (`--rate-limit`, `--max-queue`) apply to
the three POST endpoints only. With `curl` over a Unix socket:

```bash
curl --unix-socket "$XDG_RUNTIME_DIR/sylphd.sock" http://localhost/info
```

## `GET /health`

```json
{"status": "ok", "service": "sylphd", "version": "0.1.0", "uptime_seconds": 12.345}
```

## `GET /info` { #get-info }

Reports:

- `service`, `version`, `pysylph_version`, `python_version`;
- `database`: `name`, `path`, `size`, `sha256` (`null` unless `--hash`), `genomes`, `c`, `k`,
  `screen_c` (`null` for a plain `.syldb`) and `two_stage`;
- `metadata`: `path`, `size`, `columns`, `rows`, `release`, `built`, `format_version`, `reps_only`
  and `sources`, or `null` without `--meta`;
- `name` and `about`, **only when the operator set them** (the keys are absent otherwise):
  `name` is the string of `--name` (at most 80 characters, one line); `about` is
  `{"format": "markdown", "text": "..."}` with the contents of the `--about` file (UTF-8, at most
  16 KiB, surrounding whitespace removed). The server re-reads the file when it changes. `format`
  names how `text` is to be rendered and leaves room for other formats; today it is always
  `markdown`. The web page renders a safe subset (marked + DOMPurify, never HTML), see
  [Describing your instance](../guides/public-server.md#describing-your-instance). Clients must
  treat `text` as untrusted;
- `limits`: `max_jobs`, `max_upload_mb`, `max_sketches`, `allow_sequences`, `max_sequence_mb`
  (`null` unless sequences are allowed), `rate_limit` (`{"count", "window_seconds"}` or `null`) and
  `max_queue` (`null` means unlimited);
- `endpoints`: the endpoint paths this server serves.

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
| `estimate_unknown` | boolean |
| `min_number_kmers` | integer >= 0 (default `10`) |
| `min_contain` | integer >= 0 (default `7`) |
| `screen_ani` | 0-100 (default `85`) |
| `no_adjust` | boolean, report the naive ANI |
| `names` | list with one name per sketch (defaults to the part file names, else `sample-N`) |
| `fields` | list of metadata columns; omitted means all, `[]` means none; needs a metadata database |
| `mode` | optional, must match the endpoint (`query` or `profile`) |

Unknown keys are rejected with a 400. The client sketches with the database's `c` and `k`. A
sketch must match `k` exactly and must not be sparser than the database (`c <= db.c`); otherwise
the request fails with a 400.

The response is a JSON list with one object per sample, in request order:

```json
[{"name": "sample-a", "kind": "profile", "rows": [
  {"Sample_file": "sample-a", "Genome_file": "...", "Adjusted_ANI": 99.79,
   "ANI_5-95_percentile": [99.64, 99.95], "Eff_lambda": 0.36, "Lambda_status": "OK",
   "Lambda_5-95_percentile": [0.34, 0.38], "Median_cov": 1.0, "Mean_cov_geq1": 1.187,
   "Containment_ind": [6208, 21899], "Naive_ANI": 96.02, "Contig_name": "...",
   "Taxonomic_abundance": 100.0, "Sequence_abundance": 100.0, "kmers_reassigned": 0,
   "accession": "GCA_...", "gtdb_taxonomy": "..."}]}]
```

`kind` is `query` or `profile`. `Taxonomic_abundance`, `Sequence_abundance` and `kmers_reassigned`
appear only for `profile`. Metadata columns follow `accession`. Rows keep sylph's column names; in
JSON `Eff_cov` is never renamed to `True_cov` (that is a TSV header), and `Lambda_status` is an
extra field not present in the TSV. With `no_adjust`, `Adjusted_ANI` is the naive containment ANI
and `ANI_5-95_percentile` is `null`: the interval still describes the disabled adjustment.

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
    `/profile` (header check before decoding, `k` must equal the database's, `c` must not be
    sparser), and bounded by the general `--max-upload-mb`, not by `--max-sequence-mb`; or

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
assignment and closest hit whichever way it is sent.

```json
{"query": {"name": "assembly", "records": 1, "length": 6013491,
           "gc": 50.76, "sketch_kmers": 200, "input": "sequence"},
 "database": {"name": "gtdb-r232-c200-dbv2.syl2db", "genomes": 199937,
              "c": 200, "k": 31},
 "params": {"top": 10, "min_ani": 80.0},
 "thresholds": {"species": 95.0, "imprecise_below": 85.0},
 "assignment": {"rank": "species", "taxon": "Escherichia coli",
                "gtdb_taxonomy": "d__Bacteria;...;s__Escherichia coli",
                "accession": "GCF_000005845.2", "ani": 98.65},
 "closest": {"rank": "species", "taxon": "Escherichia coli",
             "gtdb_taxonomy": "d__Bacteria;...;s__Escherichia coli",
             "accession": "GCF_000005845.2", "ani": 98.65},
 "elapsed_ms": 412.7,
 "hits": [{"rank": 1, "ani": 98.65, "containment": [7096, 10826],
           "containment_fraction": 0.655, "relation": "same species",
           "shared_rank": "species", "ani_species_level": true,
           "genome_file": "/db/GCA_000005845.2_genomic.fna.gz",
           "accession": "GCF_000005845.2",
           "gtdb_taxonomy": "d__Bacteria;...;s__Escherichia coli",
           "ncbi_organism_name": "Escherichia coli K-12", "...": null}]}
```

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
  "accession", "ani"}` when the best hit reaches `thresholds.species` (95 %), otherwise `null`.
  `taxon` and `gtdb_taxonomy` are `null` without metadata.
- **`closest`** has the same shape for the best hit, or `null` without hits.
- **`thresholds.imprecise_below`** (`85`) is only a hint that k-mer ANI below it is imprecise. It
  never changes an assignment.
- **Metadata:** every metadata column of the server's metadata database is added flat to each hit,
  or `null` when the accession has no metadata. A server without `--meta` returns all columns as
  `null`, so the shape is stable.
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

`relation` is the human label and `shared_rank` its machine form. They come from the GTDB lineage
(`d__;p__;c__;o__;f__;g__;s__`), not from the ANI. `shared_rank` is the deepest rank a hit shares
with the reference lineage (`species`, `genus`, `family`, `order`, `class`, `phylum`, `domain` or
`null`); a rank only counts when all shallower ranks agree. The reference is the best hit's lineage.

| Situation | `relation` of the hit |
|---|---|
| assigned, shares rank *R* with the assigned species | `same species`, `same genus`, `same family`, `same order`, `same class`, `same phylum`, `same domain` |
| assigned, shares nothing | `different domain` |
| not assigned, the best hit | `closest relative` (`shared_rank` is `null`) |
| not assigned, other hits, sharing rank *R* with the best hit | `same genus as closest relative`, ... |
| not assigned, sharing nothing with the best hit | `different domain from closest relative` |
| a hit without lineage next to classified hits | `unknown lineage` |
| no metadata at all (ANI only) | `same species` for `ani >= 95`, else `related` |

A hit in the same GTDB species as the assignment is `same species` whatever its ANI.
`ani_species_level` is `ani >= 95` for every hit: a hit that has it but a different GTDB species
name (a species boundary case) gets the lineage relation, for example `same genus`, plus
`ani_species_level: true`.

## Errors

Errors are JSON objects `{"error": "..."}` with a 4xx or 5xx status.

| Status | When |
|---|---|
| `400` | bad parameters, invalid `params` JSON, unknown metadata columns, a sketch with the wrong `c` or `k`, a malformed sketch, more than 4096 sketches in one request; for `/assembly` an empty body, a malformed sketch (or one with the wrong `c` or `k`), invalid `records`/`length`/`gc`, or malformed or non-`ACGTN` raw sequence |
| `404` | `POST /assembly` with raw sequences without `--allow-sequences` (sketches are always accepted) |
| `413` | the body exceeds `--max-upload-mb`, or for raw `/assembly` sequence the body exceeds `--max-sequence-mb` or its decompressed size exceeds five times that |
| `415` | `/query` and `/profile` without a `multipart/form-data` body, or `/assembly` with an unsupported `Content-Type` |
| `429` | rate limited (`--rate-limit`); with `Retry-After` in seconds |
| `503` | the job queue is full (`--max-queue`), or the request has more samples than fit in it; nothing was run; with `Retry-After: 1` |
| `500` | profiling failed inside the server |

## Browser access

CORS is off unless `--cors-origin` is given; see
[Running a public server](../guides/public-server.md#cors). When enabled, `GET` and `POST` and the
`Content-Type` request header are allowed, and `Retry-After` is exposed to JavaScript.
