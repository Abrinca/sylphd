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
| `/contigs` | POST | one classified result per contig of a fragmented assembly (contig-relative ANI, hits, consensus) |

`/health` and `/info` are always served. The abuse limits (`--rate-limit`, `--max-queue`) apply to
the four POST endpoints only. With `curl` over a Unix socket:

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
  `genomes`, `c`, `k`, `screen_c`, `two_stage` and `gtdb` (`true` when a sample of up to 100
  genome names of the database are GTDB accessions of the metadata file; a fungi or UHGV server
  that has GTDB metadata only for taxonomy lookups reports `false`). Results carry no
  per-database field: a hit belongs to the database whose taxonomy it has;
- `metadata`: `file` (the base name of the SQLite file), `size`, `columns`, `rows`, `release`,
  `built`, `format_version`, `reps_only` and `sources`, or `null` without `--meta`. A format 3 file (taxonomy sources, see
  [the metadata reference](metadata.md#taxonomy-sources)) adds `taxonomy_sources` — one object per
  source with `name`, `kind` (`metadata` for the virtual GTDB source, else `sylphtax`), `origin`
  (the catalogue URL it was downloaded from, or the **base name** of a local file — never a
  local path), `md5`, `keys`, `has_host` — `taxonomy_overlaps` — the pairs of sources that share keys, each
  pair once, in setup order: `{"sources": [a, b], "keys": n, "examples": [...]}`, where `b` is
  the later-imported source and wins for those keys — and `sylphtax_rules` (the sylph-tax version
  whose rules apply). `examples` holds up to five shared keys. Both keys are absent for a format 2
  file, so its response is unchanged;
  Its `columns` also list the four `tax_*` columns, which `fields` can then select. A format 2 file
  has none of this, so its response is unchanged;
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
- `endpoints`: the endpoint paths this server serves;
- `params`: the keys each POST endpoint accepts, so a client does not have to probe with 400s:
  `{"/profile": [...], "/query": [...], "/contigs": [...], "/assembly": [...]}`. The lists are
  derived from the validators: `/query` and `/profile` list the `ProfileParams` fields plus
  `names`, `fields`, `mode` and `tax_sources`, and `/profile` adds `taxprof`; `/contigs` lists its
  `params` keys (including `contigs`); `/assembly` lists its query-string parameters.

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
| `estimate_read_counts` | boolean, `profile` only; `Sequence_abundance` then holds estimated read counts (sylph's experimental option) and implies `estimate_unknown` |
| `min_number_kmers` | integer >= 0 (default `10`) |
| `min_contain` | integer >= 0 (default `7`) |
| `screen_ani` | 0-100 (default `85`) |
| `no_adjust` | boolean, report the naive ANI |
| `names` | list with one name per sketch (defaults to the part file names, else `sample-N`) |
| `fields` | list of metadata columns; omitted means all, `[]` means none; needs a metadata database |
| `tax_sources` | list of taxonomy source names: resolve hits against these sources only (default: all of them; an empty list is the same). Unknown names, or a server without sylph-tax sources, are a 400 |
| `taxprof` | `profile` only: taxonomies to aggregate on the server, from `gtdb`, `ncbi` and `tax`. Validated against the metadata file's columns and sylph-tax sources (a 400 names what is missing). Each returned sample then carries `taxprof[taxonomy]` (see below) |
| `mode` | optional, must match the endpoint (`query` or `profile`) |

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

The response is a JSON list with one object per sample, in request order:

```json
[{"name": "sample-a", "kind": "profile", "params": {"minimum_ani": null, "seq_id": null,
   "estimate_unknown": false, "estimate_read_counts": false, "min_number_kmers": 10,
   "min_contain": 7, "screen_ani": 85.0, "no_adjust": false}, "rows": [
  {"Sample_file": "sample-a", "Genome_file": "...", "Adjusted_ANI": 99.79,
   "ANI_5-95_percentile": [99.64, 99.95], "Eff_lambda": 0.36, "Lambda_status": "OK",
   "Lambda_5-95_percentile": [0.34, 0.38], "Median_cov": 1.0, "Mean_cov_geq1": 1.187,
   "Containment_ind": [6208, 21899], "Naive_ANI": 96.02, "Contig_name": "...",
   "Taxonomic_abundance": 100.0, "Sequence_abundance": 100.0, "kmers_reassigned": 0,
   "accession": "GCA_...", "gtdb_taxonomy": "..."}]}]
```

`kind` is `query` or `profile`. `params` echoes the **effective** profiling parameters, defaults
included (the keys `/info` lists for the endpoint), so a client need not reproduce them.
`Taxonomic_abundance`, `Sequence_abundance` and `kmers_reassigned`
appear only for `profile`; with `estimate_read_counts`, `Sequence_abundance` holds the estimated
number of reads per genome (a rounded float) instead of a relative abundance. Metadata columns
follow `accession`. Rows keep sylph's column names; in JSON `Eff_cov` is never renamed to
`True_cov` (that is a TSV header), and `Lambda_status` is an extra field not present in the TSV.
With `no_adjust`, `Adjusted_ANI` is the naive containment ANI and `ANI_5-95_percentile` is `null`:
the interval still describes the disabled adjustment.

A metadata file with sylph-tax taxonomy sources (format 3) adds four columns to every row, and
they are listed in `metadata.columns` so `fields` can select them:

| Column | Meaning |
|---|---|
| `tax_source` | name of the taxonomy source whose lineage was used (the largest `ord` that has the hit's key), or `null` |
| `tax_key` | the key sylph-tax matched (verbatim: an accession, a file name, a contig id, ...), or `null` |
| `tax_lineage` | the source's lineage string, opaque (`vFAM-…;vOTU-…`, `d__…;s__…`), or `null` for a hit without any taxonomy |
| `tax_host` | the virus host of that key, from the largest `ord` whose source has one, or `null` |

The server resolves them with sylph-tax's own probe rules over the hit's `Genome_file` and
`Contig_name` (see [the metadata reference](metadata.md#resolving-a-hit)), so `query` and
`profile` rows carry them too. Their presence is additive: a format 2 file reports exactly the old
columns and rows. `/assembly` hits carry the same four fields.

### Server-side taxonomic profiles (`taxprof`) { #taxprof }

With `"taxprof": ["gtdb", "ncbi", "tax"]` the server aggregates the sample's rows once and adds a
`taxprof` object to the sample. It returns the structured clades **and** the rendered
`.sylphmpa` text, so clients neither aggregate nor format:

```json
"taxprof": {
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
```

`clades` is the cumulative lineage table (`clade_name` joins the levels with `|`; only strain rows
carry `ani`/`coverage`), ordered exactly as `sylph-tax taxprof` orders the file. `sylphmpa` is
that file, newline-terminated, byte for byte. For `tax`, `taxonomies_used` lists the source names
the profile was resolved against (`tax_sources`, in `ord` order). `gtdb`/`ncbi` come from the
metadata columns of the same name; `tax` needs a metadata file with sylph-tax sources. The
server adds the taxonomy columns the requested taxonomies need to `rows` even when `fields` hides
them. `taxprof` is `/profile` only (`/query` rejects it), and one requested taxonomy the metadata
file cannot provide is a 400.

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
{"query": {"name": "assembly", "records": 1, "length": 6013491,
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
 "hits": [{"rank": 1, "ani": 98.65, "containment": [7096, 10826],
           "containment_fraction": 0.655, "relation": "same species",
           "shared_rank": "species", "ani_species_level": true,
           "genome_file": "/db/GCA_000005845.2_genomic.fna.gz",
           "accession": "GCF_000005845.2",
           "genome_id": "GCF_000005845.2",
           "gtdb_taxonomy": "d__Bacteria;...;s__Escherichia coli",
           "taxonomy": {"domain": "Bacteria", "...": "...", "species": "Escherichia coli"},
           "taxonomy_source": "GTDB_r232",
           "ncbi_organism_name": "Escherichia coli K-12", "...": null}]}
```

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
  ...). Both keys are **absent** when the hit has no lineage, so a metadata-less response keeps its
  shape.
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
| `fields` | metadata columns added flat to every hit; omitted means all, `[]` means none; needs a metadata database |
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

The response carries one result per contig, in request order, plus a summary of the request:

```json
{"contigs": [{"name": "contig1", "length": 20000, "gc": 51.2,
              "sketch_kmers": 95, "contig_kmers": 84,
              "status": "ok", "hits": [{"rank": 1, "ani": 99.12,
                  "shared_kmers": 78, "contig_kmers": 84, "explained_fraction": 0.9286,
                  "reference_ani": 96.16, "reference_containment": [78, 8413],
                  "relation": "same species", "shared_rank": "species",
                  "ani_species_level": true, "genome_file": "...", "accession": "GCA_...",
                  "genome_id": "GCA_...", "gtdb_species": "...", "taxon": "...", "...": "..."}],
              "assignment": {"rank": "species", "taxon": "...", "accession": "...", "ani": 99.12},
              "closest": {"...": "..."},
              "consensus": {"rank": "family", "taxon": "Enterobacteriaceae",
                            "n_close": 2, "species": ["...", "..."]}}],
 "summary": {"contigs": 1, "bp": 20000, "queried_contigs": 1, "queried_bp": 20000,
             "too_few_kmers_contigs": 0, "too_few_kmers_bp": 0, "no_hit_contigs": 0,
             "consensus_bp": [{"rank": "family", "taxon": "Enterobacteriaceae",
                               "bp": 20000, "fraction": 1.0}]},
 "caveats": [], "database": {"name": "...", "genomes": 199937, "c": 200, "k": 31},
 "params": {"top": 1, "min_ani": 80.0, "consensus_ratio": 1.5, "consensus_z": 2.0, "fields": []},
 "thresholds": {"species": 95.0, "imprecise_below": 85.0},
 "elapsed_ms": 12.5}
```

- **`status`** is `ok` (hits), `no_hit` (queried, nothing above `min_ani`) or `too_few_kmers` (the
  sketch holds fewer k-mers than sylph's floor of 10; such a contig is not queried, and `reason`
  says so).
- **`ani`** is the contig-relative ANI, `100 * (shared / contig_kmers) ** (1/k)`, **not** sylph's
  `Naive_ANI`. `shared_kmers` is `Containment_ind[0]` (reference k-mers found in the contig),
  `explained_fraction` is `shared / contig_kmers` (clamped at 1), `reference_ani` is sylph's
  genome-denominator `Naive_ANI` in percent, and `reference_containment` is
  `[shared, reference k-mers]`. An exact fragment scores exactly 100 %.
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

## Errors

Errors are JSON objects `{"error": "..."}` with a 4xx or 5xx status.

| Status | When |
|---|---|
| `400` | bad parameters, invalid `params` JSON, unknown metadata columns, a sketch with the wrong `c` or `k`, a malformed sketch, more than 4096 sketches in one request (or more than the server's `max_contigs` for `/contigs`); for `/assembly` an empty body, a malformed sketch (or one with the wrong `c` or `k`), invalid `records`/`length`/`gc`, or malformed or non-`ACGTN` raw sequence; for `/contigs` a `contigs` list that does not match the sketch parts, a duplicate or unusable contig name, a `contig_kmers` that is not a positive integer or that no sequence of that `length` can have, or an out-of-range option |
| `404` | `POST /assembly` with raw sequences without `--allow-sequences` (sketches are always accepted) |
| `413` | the body exceeds `--max-upload-mb`, or for raw `/assembly` sequence the body exceeds `--max-sequence-mb` or its decompressed size exceeds five times that |
| `415` | `/query`, `/profile` and `/contigs` without a `multipart/form-data` body, or `/assembly` with an unsupported `Content-Type` |
| `429` | rate limited (`--rate-limit`); with `Retry-After` in seconds |
| `503` | the job queue is full (`--max-queue`), or the request has more samples than fit in it; nothing was run; with `Retry-After: 1` |
| `500` | profiling failed inside the server |

## Browser access

CORS is off unless `--cors-origin` is given; see
[Running a public server](../guides/public-server.md#cors). When enabled, `GET` and `POST` and the
`Content-Type` request header are allowed, and `Retry-After` is exposed to JavaScript.
