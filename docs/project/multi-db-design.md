# Several databases and non-GTDB taxonomy

Design note. Two coupled features: **A** one server holds several sylph databases, **B**
`sylphd setup` imports sylph-tax taxonomy files next to the GTDB metadata, so hits from viral,
fungal, MAG and GlobDB databases get a lineage. They were coupled by one question: for a hit,
*which database* and *which taxonomy* does it belong to. The first half is settled without code —
the taxonomy itself says where a hit came from (Decisions).

**Status 2026-10-29: phases 1 (A core) and 2 (B schema and `setup`) are implemented**; phases 3–4
(`resolve` and the `tax` profile, plus the extras) are not. The implementation measurements are in
`scratch/multi-db/REPORT.md` (phase 1) and `scratch/taxonomy/REPORT.md` (phase 2) and are folded
into the sections marked *measured*.

Primary use is the owner's own backend through the CLI. The public web app stays on one database;
it must keep working but gets no multi-database features here.

Checked against: pysylph `multi-db` @ `f8f45a1` (the commit in `uv.lock`), sylph @ `4b5c2be`
(`ref/`), sylph-tax @ `d14f286` (v1.9.2), and the Zenodo taxonomy files themselves. Claims I could
not verify are marked **(unverified)**.

## Decisions (2026-10-29)

The owner settled the open questions of the first draft. These decisions override anything below
that still suggests otherwise; the sections are updated to match.

- **Target deployment.** One server with GTDB r232 `db/gtdb-r232-c200-dbv2.syl2db` (199,923
  genomes), UHGV `db/uhgv_c200_dbv1.syldb` (measured in phase 1: **168,536** genomes; the
  `download` catalogue said 171,338 and is corrected) and fungi RefSeq 2025-10-11
  `db/fungi-refseq-2025-10-11-c200-dbv1.syldb` (661), all `c200`, `k31` (measured). IMG/VR is **out of scope
  for now** and may be added later. Because `sample.c` must be `<=` the `c` of *every* database, one
  `c100` database would force `c100` sketches for all samples (about twice the sketch size); the
  `c100` UHGV variant is therefore **not** used.
- **Hit origin is not needed.** The source of a hit is obvious from its taxonomy (GTDB lineage,
  UHGV `vFAM-...`, fungal RefSeq). No `database_index`, no `database` field on rows or hits, no
  fork change. Moved to "rejected / later" (A7).
- **`Profiler` construction cost** is solved in sylphd by caching `Profiler` objects per parameter
  set; no fork change. The optional pysylph `check_duplicates` switch is deferred until
  measurements show the cache is not enough.
- **`--taxonomy tax`** is bit-exact sylph-tax; `gtdb` and `ncbi` keep sylphd's normalisation.
- **`--taxmatrix`** switches to `sylph-tax merge`'s sorted clade order and gains ANI and coverage
  matrices like `sylph-tax merge --column ANI|Coverage` (B4).
- **Taxonomy sources for the target:** `GTDB_r232` (full GTDB metadata, as today), `UHGV_default`
  as the default for UHGV with `UHGV_ictv` selectable, and `FungiRefSeq-latest` (=
  `fungi_refseq_2025-10-11`). The IMG/VR import question is moot until IMG/VR returns.
- **pysylph pin.** The fork's `multi-db` branch also provides
  `Profiler(estimate_read_counts=...)` and `SampleSketch.mean_read_length` (being pushed by the
  owner). sylphd adds `profile --estimate-read-counts` in a separate small step *before* the
  multi-database phases, so the pin moves once. **Pysylph fork changes: none required** for
  multi-database support (only the pin bump above).

## Recommendations in one table

| # | Topic | Recommendation |
|---|---|---|
| A1 | Naming | `serve --db [NAME=]PATH`, repeatable. `NAME` defaults to the file name (today's value) |
| A2 | `/info` | `database` stays, as the *pooled* view (`c` = minimum `c`, `genomes` = sum); add `databases: [...]`. Single-database output is byte-identical to today |
| A3 | Compatibility | `k` equal everywhere; sample `c` <= the smallest `c` of any database; any `.syldb`/`.syl2db` mix. Fail at startup for `k`, report the effective `c` |
| A4 | Startup | Load databases in parallel threads; screening index per `.syl2db`; plain `.syldb` is fully in RAM and scanned per sample |
| A5 | query/profile | One pooled `Profiler` for both. For `query` pooled equals the union of per-database queries, so no per-database mode is needed |
| A6 | Duplicates | Leave to sylph's reassignment (earlier genome wins ties); log pysylph's warning once at startup; no server-side dedupe |
| A7 | Hit origin | **Rejected** (owner): the source is obvious from the taxonomy. No `database_index`, no `database` field on rows or hits |
| A8 | `/assembly`, web | Pooled query; hits carry no origin field (A7). Web unchanged. Single-database responses unchanged |
| A9 | Limits | No new knobs. Cache `Profiler` objects per parameter set in sylphd: with two or more databases the constructor scans every genome. Fork `check_duplicates` deferred |
| B1 | Schema | Format version 3, additive: new `taxonomy_*` tables, `metadata` view untouched. v2 files still load; old servers still read v3 files |
| B2 | `setup` | `-m` optional, new repeatable `--taxonomy SPEC` (sylph-tax name, `NAME=PATH` or `PATH`), `--update` to add to an existing file |
| B3 | Hit to lineage | Replicate sylph-tax's key probing exactly (listed below) over all imported sources; record `tax_source`, `tax_key`, `tax_lineage`, `tax_host` per hit. Per-database association later, optional |
| B4 | Profiles | New taxonomy name `tax` for `--taxonomy`: one merged, sylph-tax-exact profile. `gtdb` and `ncbi` keep their own normalisation. `--taxmatrix` sorted like `sylph-tax merge`, plus ANI and coverage |
| B5 | Virus hosts | Store the host column at setup (cheap); emit it in a later phase behind a flag |
| B6 | Assembly | Keep GTDB-only classification first; generalise the relation labels later |
| B7 | `download` | Add a taxonomy catalogue and `--database NAME --with-taxonomy`; pair each database with its sylph-tax file |

## What the reference code does

### pysylph and sylph (multi-database)

- `Profiler(db1, db2, ...)` pools databases (`ref/pysylph/src/pysylph/lib.rs`, `Profiler::run`).
  Two-stage databases are screened one by one and their survivors (each sorted by file name and
  first contig name) come first, then all genomes of the plain databases, in the order given. One
  pipeline run covers the pooled list, so genomes of all databases compete in reassignment. This is
  what `sylph profile a.syl2db b.syldb` does (`ref/sylph/src/contain.rs`, the loop over
  `two_stage_dbs`, then `genome_sketches`).
- **`query` is pooled too**, but `run_pipeline` in query mode calls `get_stats` per genome and
  never compares genomes, so the result is the union of per-database queries, sorted by ANI. Only
  `profile` (k-mer reassignment, abundances) depends on pooling. `estimate_unknown` is computed
  over the pooled hits.
- **Compatibility** (`check_database_params`, `check_compatible`; sylph: `effective_genome_c`):
  - `k` must be equal across all databases (`ValueError` at `Profiler(...)`) and equal to the
    sample's `k`.
  - The sample's `c` must be `<=` the `c` of **every genome of every database** (checked per
    call, so it costs O(genomes of plain databases) per sample). For a `.syl2db`, `c` is the dense
    `db.c`; `screen_c` is not checked and is always `>= c`. So the rule is `sample.c <=
    min(c over databases)`; databases need not share `c`. sylph does the same, and for raw reads
    sketches at `min(-c, min genome c)`.
  - `.syldb` and `.syl2db` mix freely; only `k` is constrained.
- **Duplicates**: a `UserWarning` when two genomes share `(file name, first contig name)`, raised
  **in the `Profiler` constructor**, which scans every genome of every database when there are two
  or more. GenomeSketch equality includes the k-mers, so exact copies collide downstream; copies
  with different file names do not trigger the warning. For exact copies, reassignment keeps the
  first one in the pooled list (strict `>` in `winner_table`) and drops the other. I read this in
  the code; I did not run it **(unverified)**.
- **Origin of a hit**: nothing exposes it. `AniResult.genome_sketch` has `name` (= sylph's
  `Genome_file`), `description` (= `Contig_name`), `genome_size`, `c`, `k`, `min_spacing`, no
  database index. sylph's TSV has no database column either. `Hit::new` maps results to sketches by
  pointer identity, so an index could be added there.
- Memory: a `.syl2db` loaded from a path keeps only the screening index in RAM; dense sketches are
  read on demand (`TwoStageDatabase.load`). A `.syldb` is decoded fully. Both loaders release the
  GIL.

### sylph-tax

- A taxonomy file is 2 or 3 tab-separated columns, `key<TAB>lineage[<TAB>host]`, read line by line
  with `row.rstrip().split("\t")`. The lineage is an **opaque `;` string**; sylph-tax appends
  `;t__<key>`. All files given are merged into one dict; for an identical key, the later file wins.
  There is no association between a file and a database.
- Formats and row counts of the Zenodo files (downloaded and inspected):

  | File | Rows | Key | Lineage |
  |---|---|---|---|
  | `gtdb_r232_metadata` | 199,923 (= the r232 database; species representatives only) | `GCA_018646675.1` | `d__..;s__` |
  | `GlobDB_r232` | 346,233 | `GCF_003697165` (**version stripped**) | `d__..;s__` |
  | `GlobDB_r226`, `OceanDNA`, `SoilSMAG`, `TaraEukaryoticSMAG` | | file names such as `AMXMAG_0088.fa.gz`, `BL1bin.1.fa.gz` | `d__..;s__`; Tara uses `d__Eukarya` and literal `g__NA`; SoilSMAG has empty `s__` |
  | `FungiRefSeq-*` | 661 | `GCF_000002495.2` | `d__..;s__` |
  | `IMGVR_4.1` | 5,576,197 + **a header row** | `IMGVR_UViG_2504643025_000001` | `r__;k__;p__;c__;o__;f__;g__;s__`, trailing tokens often empty; a **host** column (never empty) |
  | `UHGV_default` / `UHGV_ictv` | | `UHGV-0405173` | **no rank prefixes** (`vFAM-00013;...`, `root;Monodnaviria;...`); host column |

  The IMG/VR file has 5.6 M rows but the sylph database 2.9 M genomes, so a taxonomy file is not
  tied to a database size.
- **Hit to key** (`sylph_to_taxprof.py`, `main`): two passes, no `break`, so a pass-1 match
  **overrides** a pass-0 match (checked by running it with keys that differ between the passes).
  Per pass the probes are tried in this order; the first that exists in the dict wins:

  | | pass 0 | pass 1 |
  |---|---|---|
  | `gf` | basename of `Genome_file`; if `"ASM"` occurs anywhere in the **full path**, `basename.split("_ASM")[0]`, else `basename.split("_genomic")[0]` | basename |
  | `contig` | `Contig_name.split(" ")[0].split("\|")[0]` | `Contig_name.split()[0]` |
  | `glob` | `basename.split(".")[0]` | `gf` |

  Probes: `gf`, `gf + ".gz"`, `glob`, `contig`, and only if all fail `gf.split(".fa")[0]`, then
  `gf.split(".fasta")[0]`, then `gf.split(".fna")[0]`. No match gives
  `NO_TAXONOMY;t__<gf>:<contig>` (pass-1 values). The `t__` label is the **dict key**, so for
  GlobDB r226 it is `t__AMXMAG_0088.fa.gz`.
- **Aggregation**: tokens of the lineage; an **empty token** becomes `UNKNOWN` (`o__` is *not*
  empty, so `g__`, `s__` stay as they are); cumulative `|` paths; rows ordered by **number of
  levels**, then by relative abundance descending (stable). `t__` is therefore *not* always last:
  a strain below a 3-level lineage sorts before a 4-level clade (checked). ANI and coverage of a
  strain are the last row's values.
- **Virus hosts** (`-a`): a sixth column `Virus_host (if viral)`; strain rows get the third file
  column (empty tokens become `UNKNOWN`), every other row `NA`. `--pavian` writes a different
  format; its first line is `#mpa_v3_sylphmock` glued to `#SampleID...` without a newline (code
  reading).
- **`sylph-tax merge`** outer-joins the files with pandas, which **sorts the clade names
  lexicographically** when there are two or more files (checked with pandas 3.1); one file keeps
  file order. Missing is `0`. Options include `ANI` and `Coverage`.

### sylphd today

Updated in phase 1: the first four rows describe the code *before* it.

| Where | Assumption |
|---|---|
| `server.py: ServerConfig.db_path`, `create_app` | exactly one database (**now** `ServerConfig.db_paths: tuple[DatabaseSpec, ...]`, `db_path` kept as the single-database spelling) |
| `profiling.py: LoadedDatabase` | `c`, `k` from `db[0]` for a plain database; one `Profiler` per request (`run_sample`, `_assembly_hits`) (**now** `DatabaseSet` with one pooled `Profiler` per parameter set) |
| `profiling.py: _decode_sketch` | sketch `c` compared with that one database (**now** `DatabaseSet.check_sketch`, naming the limiting database) |
| `server.py: _info` | `database` is one object (**now** the pooled view plus `databases`); the web app reads `database.{name,genomes,c,k}` and `metadata.release` (`web/app.js`, `loadInfo`, `sketchLocally`) |
| `metadata.py: accession_from_genome_name` | only `GC[AF]_\d+(\.\d+)?`; **every hit from IMG/VR, UHGV, OceanDNA, SoilSMAG, Tara and GlobDB r226 gets no accession and no metadata** |
| `taxonomy.py: parse_lineage`, `TAXONOMY_COLUMNS` | ranks `d,p,c,o,f,g,s` only; other tokens are dropped; empty ranks filled with `unclassified X`; `t__` forced last |
| `assembly.py: classify_hits` | GTDB lineage only; no lineage on the best hit means ANI-only labels, on another hit `unknown lineage` |
| `taxonomy.py: clade_matrix_lines`, `docs/guides/taxonomic-profiles.md` | claim first-seen order equals `sylph-tax merge`; it does not (sorted) |

## A. Several databases per server

### A1. Naming

Options: (a) repeat `--db`, name = file name; (b) repeat `--db`, optional alias; (c) a TOML file of
databases.

**Recommendation: (b).** `sylphd serve --db gtdb=db/gtdb-r232-c200-dbv2.syl2db
--db uhgv=db/uhgv_c200_dbv1.syldb --db fungi=db/fungi-refseq-2025-10-11-c200-dbv1.syldb`.
The alias is split off at the first `=` only if the left part matches `[A-Za-z0-9_.-]+`, so paths
containing `=` still work. Default name is the file name, so `database.name` of a single-database
server does not change. Duplicate names are an error. A config file (c) is more than a handful of
flags need; revisit if `serve` grows per-database options.

`ServerConfig.db_path` becomes `db_paths: tuple[DatabaseSpec, ...]`, keeping `db_path` as a
read-only property so existing tests and callers work.

### A2. `/info`

```json
{"database": {"name": "gtdb-r232-c200-dbv2.syl2db, uhgv_c200_dbv1.syldb,
                        fungi-refseq-2025-10-11-c200-dbv1.syldb", "genomes": 369120,
              "c": 200, "k": 31, "screen_c": null, "two_stage": false, "sha256": null},
 "databases": [{"name": "gtdb", "file": "gtdb-r232-c200-dbv2.syl2db", "path": "...",
                "size": "...", "sha256": null, "genomes": 199923, "c": 200, "k": 31,
                "screen_c": "...", "two_stage": true},
               {"name": "uhgv", "file": "uhgv_c200_dbv1.syldb", "...": "...",
                "genomes": 168536, "c": 200, "k": 31, "screen_c": null, "two_stage": false},
               {"name": "fungi", "file": "fungi-refseq-2025-10-11-c200-dbv1.syldb", "...": "...",
                "genomes": 661, "c": 200, "k": 31, "screen_c": null, "two_stage": false}]}
```

- `database` is the **pooled view** and is what existing clients read: `c` is the *minimum* `c`
  (the value to sketch with), `k` the common `k`, `genomes` the sum, `screen_c` and `two_stage`
  only when every database agrees. For one database it equals today's object exactly.
- `databases` is new, always present (one entry for a single database), with `file` added because
  `name` can be an alias.
- Clients that need `c` and `k` (`cli.py`, `web/app.js`, per-contig `assembly`) work unchanged.
  The web page labels the database `GTDB r<release>` from `metadata.release` when it is set; on a
  multi-database server that is misleading, so switch it to `databases` later (cosmetic).

### A3. Compatibility checks

| Check | Where | Rule |
|---|---|---|
| `k` of databases | server startup | all equal, else refuse to start (naming the databases and their `k`) |
| `c` of databases | server startup | may differ; effective sample `c` = minimum; log a warning when they differ, because sketches grow (`c=100` is about twice the size of `c=200`, so revisit `--max-upload-mb`) |
| uploaded sketch `k` | `_decode_sketch` | `== k`, as today |
| uploaded sketch `c` | `_decode_sketch` | `<= minimum c`; name the limiting database in the 400 |
| `.syldb` / `.syl2db` | nothing | any mix |
| genomes of one plain `.syldb` | server startup | may differ in `c` (the database's `c` is their minimum); a mixed `k` is refused, naming the database |

Detail: genomes of one plain database may differ in `c`, and pysylph checks them all per call
(`ValueError`, a 400 since phase -1). To advertise the *effective* `c`, phase 1 walks the genomes
of every plain `.syldb` once at load: `c` becomes their minimum and a mixed `k` fails at startup
with the database named (before, `c` came from `db[0]` only, so a mixed-c file advertised more
than every genome allows and every request failed, naming a genome). Measured: **+44 ms for
UHGV's 168,536 genomes** (0.26 µs per genome, one Python `GenomeSketch` wrapper each, no k-mer
decoding), 0 ms for fungi's 661; `.syl2db` files are not scanned (their `c` is uniform). This is
cheap enough to keep; without it the server would have to assume a uniform `c`/`k`.

The target set is GTDB r232 `c200` + UHGV `c200` + fungi RefSeq `c200`, all `k31`,
so the effective `c` is 200 and no client sees a change. UHGV also ships a `c100` variant; it is
**not used**, because `sample.c <= min c` would force `c100` sketches for every sample (about
twice the sketch size of `c200`). GTDB `c1000` + a `c200` database would sample at 200, which is
fine (denser sample than genome). The target trio has `k=31` and `c=200` in every database
(measured in phase 1, by loading the files); no other prebuilt database was opened, so `k` beyond
the trio is still unchecked.

A plain database sketched with `--disable-profiling` makes sylph refuse `profile`; pysylph has no
such check and `GenomeSketch` exposes no accessor for it, so sylphd cannot detect it
**(unverified whether any prebuilt database is affected)**.

### A4. Memory and startup

- Each `.syl2db` costs its screening index in RAM (about 2 GB for GTDB r232 `c200`, from the
  design notes; GlobDB's is **(unverified)**) plus page cache for the dense stage.
- A `.syldb` is fully in RAM (UHGV 227 MB, fungi 784 MB; GTDB r232 as a `.syldb` would be 25.9 GB)
  **and is scanned for every sample** (`get_stats` over all genomes; only `.syl2db` is screened).
  In the target set that is 168,536 viral plus 661 fungal genomes per sample, about 17× fewer than
  the 2.9 M of IMG/VR (which is out of scope). sylph advises against `.syl2db` for small genomes,
  so a plain `.syldb` is the right form for UHGV and fungi.
- **Measured on the target trio (phase 1)**: RSS after the load is 3.22 GiB with the two plain
  databases against 2.21 GiB for GTDB alone, an increase of 1.01 GiB (their files are 227 MB +
  784 MB = 217 MiB + 747 MiB); the peak is 5.45 GiB, reached while loading (GTDB alone: 4.43 GiB),
  and requests add about 20 MB. The plain scan costs **about 30 ms per sample**: a `profile`
  request takes 33–34 ms instead of 1.3–1.8 ms, paired 51–58 ms instead of 21.6 ms, `/assembly`
  40–49 ms instead of 1 ms. Absolute cost, not relative, is what matters here, and 35 ms per
  sample keeps the server interactive.
- Load databases concurrently (`concurrent.futures` threads around `LoadedDatabase`), since both
  loaders release the GIL. Log name, genomes, `c`, `k` and load time of each. **Measured**: GTDB
  2.2–2.5 s, UHGV 0.4–0.5 s, fungi 0.5–2.6 s, wall 2.3–2.6 s for the trio (the slowest database,
  not the sum).
- Compute `--hash` per database only on request, as now.

### A5. `query` versus `profile`

Options: (a) one pooled `Profiler` for both; (b) pooled for `profile`, one `Profiler` per database
for `query`; (c) per-database `profile`.

**Recommendation: (a).** For `query` (a) and (b) return the same rows, (a) is one call and one
screen. Pooled `profile` is what `sylph profile a b` gives, so the CLI is comparable. (c) is
wrong: abundances would not sum to 100 and the same sample would be double counted.
Optional later: `params.databases: ["gtdb", "uhgv"]` to run against a subset (a `Profiler` of the
subset; unknown name is a 400). Abundances then renormalise over the subset; say so in the docs.

### A6. Duplicate genomes

Same accession in two databases (GTDB and GlobDB overlap, **unverified how much**). The target
trio is disjoint (prokaryotes, viruses, fungi), so this is a non-issue there. In `profile` the
earlier genome in the pooled list wins ties and the copy disappears; in `query` both rows appear,
distinguishable only by taxonomy, since rows carry no origin field (A7). Precedence is: every
`.syl2db` in `--db` order, then every `.syldb` in `--db` order, whatever the interleaving on the
command line. Do not dedupe in the server.
Install a `warnings.catch_warnings(record=True)` around the first `Profiler` and log the count.

### A7. Which database did a hit come from — rejected

**Rejected by the owner:** the source of a hit is obvious from its taxonomy (GTDB lineage, UHGV
`vFAM-...`/`root;Monodnaviria;...`, fungal RefSeq), so rows and `/assembly` hits get **no**
`database` field and pysylph gets **no** `database_index`. Rejected alternatives, recorded so they
are not re-argued:

- (a) pysylph returns it: `AniResult.database_index` via a pointer-to-index map in
  `run_pipeline`/`Hit::new` (about 30 lines). Cheapest of the three, but unnecessary. If origin is
  ever needed (e.g. per-database taxonomy association, B3 option 2), this is the way back.
- (b) map `(Genome_file, Contig_name)` to a database at startup: needs every genome name, but
  `TwoStageDatabase[i]` decodes the dense sketch from the file (reading all 20 GB), `Database[i]`
  builds Python objects for millions of genomes, and exact `(name, contig)` collisions are exactly
  the duplicates in A6.
- (c) infer from `c` or name patterns: not reliable.

### A8. `/assembly` and the web app

- `/assembly` and per-contig `assembly` query the pooled set, sort by ANI and keep `top`, so a hit
  list may mix databases. `/assembly` already adds every metadata column flat to each hit.
- With only GTDB metadata, a non-GTDB hit has no `gtdb_taxonomy`; `classify_hits` then labels it
  `unknown lineage`, or, if it is the *best* hit, drops to ANI-only labels for all hits
  (`assembly.py`). Acceptable for the first phases; B6 generalises it, and B3 supplies
  `tax_lineage` for those hits in the meantime.
- The response's `database` object is the pooled view (A2); add `databases` only. Hits get no
  origin field (A7); the taxonomy columns say what a hit is.
- Web: one database per public server stays the supported setup. Guard rail: a test that a
  single-database `/info` and `/assembly` response is byte-identical to a golden file.

### A9. Limits and admission

- `--max-jobs`, `--max-queue`, the rate limiter and `JobSlots` count samples, not databases, and
  need no change. A multi-database sample costs more (plain scans, bigger screens), so operators
  lower `--max-jobs`; document it.
- `Profiler` construction is **not cheap** with two or more databases: `check_database_params`
  hashes `(file name, first contig)` of every genome on every construction, and the design notes'
  "new `Profiler` per request (cheap)" no longer holds. **Measured on the target trio**: 93 ms for
  369,120 genomes (0.25 µs per genome, the same rate as the 6-genome toy; about 0.75 s for 3 M
  genomes). One database is free (no duplicate scan). The scan holds the GIL.
  **Solution (owner, no fork change):** keep a small LRU of `Profiler`s in sylphd keyed by
  `(database subset, params)` (the parameters are few; `/assembly` varies only `min_ani`), warm the
  default one at startup, and reuse it across requests. The optional pysylph
  `check_duplicates=False` switch stays unimplemented until measurements show the cache is not
  enough; then it is a separate, additive fork change. **Phase 1**: implemented as an `OrderedDict`
  of 8 entries keyed by `ProfileParams` (a frozen dataclass, so hashable), guarded by a lock that
  only protects the dictionary; the default entry is warmed at startup (0.09 s) and the first
  `/assembly` request after startup pays its own 93 ms construction (measured: 152 ms instead of
  40–49 ms).
- `max_upload_mb` guidance in the public-server guide assumes `c=200`; mention the `c=100` case
  (not used by the target set, see A3).

## B. Taxonomy beyond GTDB

### B1. Data model and schema v3

Today: one `metadata` view over `metadata_data` (accession-keyed, GTDB + NCBI columns).
sylph-tax sources are `key -> lineage [-> host]` with arbitrary keys, so they do not fit the
accession-keyed table.

Options: (a) a separate table of entries for sylph-tax sources, `metadata` untouched; (b) migrate
everything, GTDB included, into one entry table with `metadata` as annotation; (c) one SQLite file
per source and `--meta` repeatable.

**Recommendation: (a).** Additive, so no API or web changes.

```sql
taxonomy_source (id INTEGER PRIMARY KEY, ord INTEGER NOT NULL, name TEXT NOT NULL,
                 kind TEXT NOT NULL,            -- 'metadata' (virtual, see below) | 'sylphtax'
                 origin TEXT, md5 TEXT, rows INTEGER, has_host INTEGER, built TEXT)
taxonomy_entry  (source_id INTEGER NOT NULL, key TEXT NOT NULL, lineage INTEGER, host INTEGER,
                 PRIMARY KEY (source_id, key)) WITHOUT ROWID
dict_tax_lineage (id INTEGER PRIMARY KEY, value TEXT NOT NULL)
dict_tax_host    (id INTEGER PRIMARY KEY, value TEXT NOT NULL)
```

- Lineage and host strings are dictionary-encoded like the existing columns (IMG/VR: 6,195
  distinct lineages and 15,767 distinct hosts for 5.6 M rows).
- Measured: IMG/VR 4.1 loaded into exactly this layout is **228 MiB** (8 KiB pages), built in 8 s;
  a 100-key lookup takes about 0.35 ms. That is the upper bound; IMG/VR is out of scope now
  (Decisions). The target sources are small: UHGV and fungi total about 172 k keys, so the v3 file
  stays dominated by GTDB metadata (198 MiB). Keys are the bulk.
- **GTDB stays where it is.** Full GTDB metadata imported with `-m` is registered as a *virtual*
  source (`kind='metadata'`, name `GTDB_r<release>`) whose entries are `metadata_data.accession`
  and `gtdb_taxonomy`, so lineages are not stored twice. Importing the sylph-tax catalogue file of
  that *same* release (`GTDB_r232` with r232 metadata) *and* `-m` is redundant: log it and skip the
  former. Only that combination is redundant — a catalogue file for a different release
  (`GTDB_r214` next to r232) is a different taxonomy and is imported, and so is any local file
  (`NAME=PATH`, `PATH`) under a name of its own: a local file is never taken for sylph-tax's own
  file. (The sylph-tax GTDB files are representatives-only: 199,923 rows for r232.)
- `ord` is the order given to `setup`; for an identical key in two sources the larger `ord` wins,
  as the later file wins in sylph-tax.
- `info` gets `format_version = "3"`, `taxonomy_sources` (JSON list with name, rows, md5, origin)
  and the sylph-tax version whose rules were implemented (`sylphtax_rules = "1.9.2"`).

**Migration.** v3 is a superset of v2:

- Reader: `MetadataDB` accepts versions 2 and 3; v2 means no `taxonomy_*` tables and behaves as
  today. It already only requires the `metadata` view and `info`.
- Old servers reading a v3 file work (they ignore the new tables).
- Upgrading a v2 file: `sylphd setup --update old.sqlite --taxonomy UHGV_default -o new.sqlite`
  copies, adds tables and bumps the version, without the GTDB TSV files. Otherwise rebuild: the
  full setup takes minutes.
- Virtual columns exposed by the `metadata` summary only when at least one source exists:
  `tax_source`, `tax_key`, `tax_lineage`, `tax_host` (B3). They behave like metadata columns for
  `--fields`; the wire protocol needs no new concept.

### B2. `sylphd setup` and sources

```text
sylphd setup [-m FILE...] [--taxonomy SPEC...] -o PATH [--release N] [--reps-only]
             [--update] [--cache-dir DIR] [--force] [-q]
SPEC := NAME | NAME=PATH | PATH        NAME is a sylph-tax name, e.g. UHGV_default, FungiRefSeq-latest
```

Target invocation: `sylphd setup -m <gtdb metadata> --taxonomy UHGV_default --taxonomy
FungiRefSeq-latest -o ...`; the GTDB source is the virtual `GTDB_r232` created by `-m` (B1).

- At least one of `-m` and `--taxonomy`. A bare sylph-tax name is downloaded to `--cache-dir`
  (default `<output dir>/taxonomy/`) with the existing engine in `download.py` (`download_urls`:
  mirrors, `.part`, resume, size and MD5). A local `PATH` is read as it is (gzip detected by magic
  as in `open_text`), with `NAME` defaulting to the file stem.
- Parsing mirrors sylph-tax: `line.rstrip().split("\t")`; key `spl[0]` verbatim (an empty key is
  accepted, as sylph-tax accepts it); lineage `spl[1]` right-stripped; optional host `spl[2]`; a
  header row is just an entry that never matches (IMG/VR). A blank line or a line without a tab is
  an error naming the file and the line — sylph-tax raises `IndexError` there, and a silent skip
  would hide a truncated file. A trailing tab is not an empty host column: `rstrip()` runs before
  the split, so it means "no host". Keys are **case sensitive and not normalised**; only the GTDB
  virtual source keeps `normalize_accession`. Within one file a repeated key keeps the last lineage
  and only replaces its host when the later row has a third column; across files see B3.
- Sources and their order are listed by `sylphd info` (metadata line), so the owner can see what a
  server will resolve against.
- `--reps-only` applies to `-m` only.

### B3. Mapping a hit to a taxonomy

Options:

1. **sylph-tax heuristics over all sources** (merged key space, probing as in the table above).
   Zero configuration; identical to sylph-tax by construction; ambiguity possible between sources.
2. **Per-database association** (`--db uhgv=PATH?tax=UHGV_default`, or a table in the SQLite): each
   hit looks up only the sources assigned to its database. Unambiguous; would need hit origin
   (A7, rejected) and configuration, and diverges from sylph-tax when the owner mislabels.
3. Per-source key rules (regex per source). More machinery than the heuristics already encode.

**Recommendation: 1 first, 2 as an optional restriction later.** Option 1 is also what makes the
parity test meaningful. For the target trio the key spaces do not overlap (GTDB `GCA_...`,
UHGV `UHGV-...`, fungi `GCF_...`), so ambiguity is theoretical. Where sources of the same kind
meet, keys resolve by the probe order above (an exact key before `glob`), and for identical keys
by `ord`.

Implementation: `MetadataDB.resolve(hits)` takes `(Genome_file, Contig_name)` per hit, builds the
probe keys of both passes (a pure function, `sylphtax_probes`, no database access), fetches all of
them with batched `IN` queries over `taxonomy_entry` and the virtual GTDB source, and picks per
hit with the exact precedence above (pass 1 overrides pass 0). It returns `tax_source`,
`tax_key`, `tax_lineage`, `tax_host`; `None` everywhere when nothing matches (the `tax` profile
then writes sylph-tax's `NO_TAXONOMY;t__<gf>:<contig>`, see B4). It replaces the
`accession_from_genome_name` join in `profiling.py: run_sample` and `_assembly_hits`; the existing
`accession` column and the `metadata` view join (NCBI/quality columns) keep their meaning. Cost:
one extra query per sample, as `lookup` today.

Probing is on `Genome_file` and `Contig_name` as pysylph reports them (`GenomeSketch.name`,
`.description`), i.e. what sylph writes. In the test database `Genome_file` is
`test_files/e.coli-K12.fasta.gz`, so file names are whatever the database builder passed; the
probes exist for that reason. How the prebuilt databases name genomes is inferred from sylph-tax's
rules only **(unverified: no prebuilt database was opened)**.

**Hosts across sources (phase 3 must replicate this exactly).** sylph-tax does not keep hosts
per file: `sylph_to_taxprof.py` creates `genome_to_taxonomy` and `genome_to_additional_data` once,
*before* the loop over metadata files, and fills both from every file in the order given. So for a
key that occurs in several files the final state is:

- **lineage**: from the *last* file that contains the key (assigned on every row);
- **host**: from the last row *that had a third column*, which may be a different, earlier file —
  a later file's row without a host column leaves the earlier host in place, while its lineage
  still wins.

The database stores one row per `(source_id, key)`, which is the honest representation and what
`info`/per-source dumps need, so `resolve` must combine them the same way: for each matched key
take the lineage from the largest `ord` whose source has the key, and the host from the largest
`ord` whose source has that key *and* a host. (For the target trio the key spaces do not overlap,
so this only matters for GlobDB/UHGV-style multi-source setups; it is cheap to get right.)

### B4. Mixed-taxonomy profiles

**Findings that shape the design**

- sylph-tax treats lineages as opaque tokens; sylphd's `parse_lineage` understands only
  `d,p,c,o,f,g,s`, so UHGV (`vFAM-...`), ICTV (`root;Monodnaviria;...`) and the viral
  `r__`/`k__` ranks would be dropped.
- For empty ranks sylphd differs from sylph-tax *by design* (`unclassified X`, trailing ranks
  dropped, `t__` forced last, `unclassified` instead of `NO_TAXONOMY`). With GTDB lineages (always
  complete, 7 ranks) the outputs coincide; with mixed sources they do not (level-based ordering,
  `UNKNOWN`).
- `--taxmatrix` documents first-seen order as sylph-tax's; sylph-tax sorts lexicographically
  (checked), so `--taxmatrix` is not a drop-in today. This is a bug in current docs and code,
  independent of this feature, and is fixed in phase -1.
- `sylph-tax merge` (`merge_sylph_taxprof.py`) is
  `sylph-tax merge -o OUT --column {relative_abundance,sequence_abundance,ANI,Coverage} FILE...`
  (`--column` required, default output `merged_data.tsv`). It reads only the requested column and
  the sample name from the `#SampleID` line, so ANI and coverage are merged exactly like
  abundances.

**Recommendation.** Keep `gtdb` and `ncbi` (`--taxonomy`) as they are. Add **`tax`**:

- `--taxonomy gtdb,ncbi,tax`; `tax` means *one merged profile* over all sources of the metadata
  file, like `sylph-tax taxprof -t A B C`; file `PREFIX<sample>.tax.sylphmpa`, matrix
  `PREFIXtax.tsv`. `--tax-source NAME` (repeatable) restricts the sources; default is all. The
  default of `--taxonomy` stays `gtdb,ncbi`, plus `tax` when the server has sylph-tax sources.
- `tax` is **exact sylph-tax**: opaque tokens, empty token to `UNKNOWN`, `NO_TAXONOMY`, ordering
  by number of levels then abundance (stable), `t__<tax_key>`, header
  `Taxonomies_used:['GTDB_r232', 'UHGV_default']` (source names, list repr), full float
  precision as `taxonomy.py: _cell` already does.
- The client aggregates the `tax_lineage` column; the server does no aggregation. The client
  requests `tax_source,tax_key,tax_lineage` through the existing `_request_fields` and
  `resolve_taxonomy_columns` machinery.
- `--taxmatrix` for `tax` (and, by the bug fix, for the others) follows **`sylph-tax merge`**
  (`merge_sylph_taxprof.py`, `d14f286`) row for row, extended to all four of its columns:
  - Sample names are the profile's sample label (what sylph-tax reads from the `#SampleID` line),
    already what `clade_matrix_lines` writes; not the output file name.
  - Clade names are sorted lexicographically when there are two or more samples (pandas `join`);
    one sample keeps the profile's order. Rows are the outer union; a missing value is `0`.
  - `--taxmatrix-value` grows `relative|sequence|ani|coverage`, matching
    `sylph-tax merge --column relative_abundance|sequence_abundance|ANI|Coverage`. `ani` reads
    sylphd's `ANI (if strain-level)` (strain rows only), `coverage` reads
    `Coverage (if strain-level)`.
  - For `ani`/`coverage`, a clade with no value in *any* sample is dropped, as pandas
    `dropna(how="all")` does (non-strain clades therefore do not appear); a clade with a value in
    one sample and none in another gets `0`, not `NA` (`_cell`'s `NA` stays for `.sylphmpa`).
  - Header `clade_name<TAB><sample>...` and tab separators, as `to_csv(sep="\t")` writes them.
  The existing `CladeAbundance.ani`/`.coverage` fields already carry the values; only
  `clade_matrix_lines` needs to sort and to handle the two extra columns.
- `web/taxonomy.js` is a port of `taxonomy.py`; leave it GTDB/NCBI-only. `tests/test_web_taxonomy_js.py`
  keeps guarding the port; do not extend `tax` to JavaScript.

### B5. Virus-host annotation

In scope, but last. In the target trio only UHGV carries hosts (`UHGV_default`/`UHGV_ictv`; GTDB
metadata and the fungal file have no host column), and B5 is exactly what makes the default UHGV
source useful. Store the host column from the start (it costs a dictionary and one integer
per row), expose it as `tax_host`, and add `--taxprof-virus-hosts` later: sixth column
`Virus_host (if viral)`, strain rows get the host (empty tokens `UNKNOWN`), all other rows `NA`,
as in sylph-tax. `--pavian` is out of scope (a different tool's format, with its first-line
quirk).

### B6. Assembly classification for non-GTDB hits

`classify_hits` and the web page's lineage colouring need a GTDB lineage and treat 95 % ANI as
species. For a non-GTDB best hit today: ANI-only labels, `taxon` null.

Phases: (1) leave as is, document. (2) Parse `tax_lineage` generically in `classify_hits`: rank
names from the prefix (`d domain, r realm, k kingdom, p phylum, c class, o order, f family,
g genus, s species`), `level N` for prefix-less sources; `assignment.taxon` = last named rank; keep
`gtdb_taxonomy` for GTDB hits and add `taxonomy` and `taxonomy_source` to `assignment`, `closest`
and hits (additive JSON). (3) **Decided: 95 % ANI for every source**, no per-source threshold for
now. Add hints and warnings instead:

- When the best hit is not from GTDB, `assignment` carries a `note` (JSON) and the CLI prints a
  stderr warning (unless `--quiet`): viruses (UHGV): 95 % ANI matches the vOTU convention (MIUViG:
  95 % ANI over 85 % of the shorter genome), but sylph's ANI ignores aligned fraction; fungi: there
  is no established ANI species boundary, 95 % is a heuristic and species calls may be wrong.
- Per-contig mode and taxonomic profiles add the same caveat once per output (stderr), not per row.
- Docs (`guides/assembly.md`, `species-and-ani.md`) get a short "outside prokaryotes" section.
- `thresholds.species` stays a single value in JSON; making it per source remains possible later
  without breaking the format.
The web app keeps working in all phases because its JSON is only ever extended.

### B7. `sylphd download`

Add a taxonomy catalogue next to `DATABASES`, reusing the engine. The target trio is
`gtdb-r232-c200-dbv2.syl2db` + `GTDB_r232` (via `-m`, no download), `uhgv_c200_dbv1.syldb` +
`UHGV_default`, `fungi-refseq-2025-10-11-c200-dbv1.syldb` + `FungiRefSeq-latest`; the rest of the
table is for later, and IMG/VR is out of scope (its row stays as documentation of the pairing):

| Database (catalogue) | Taxonomy (sylph-tax name) | Zenodo record |
|---|---|---|
| `gtdb-r232-*` | `GTDB_r232` | 19646381 |
| `gtdb-r226-*` | `GTDB_r226` | 15314244 |
| `gtdb-r220-*` | `GTDB_r220` | 14320496 |
| `v0.3-*-gtdb-r214` | `GTDB_r214` | 14320496 |
| `globdb_r232_*` | `GlobDB_r232` | `fileshare.lisc.univie.ac.at`, not Zenodo, no MD5 |
| `OceanDNA-*` | `OceanDNA` | 14320496 |
| `SMAG-*` | `SoilSMAG` | 14320496 |
| `imgvr_*` | `IMGVR_4.1` | 14320496 |
| `uhgv_*` | `UHGV_default` (default), `UHGV_ictv` selectable | 15670321 |
| `fungi-refseq-2025-10-11-*` | `FungiRefSeq-latest` (file `fungi_refseq_2025-10-11_metadata.tsv.gz`) | 17330476 |
| `tara-eukmags-*` | `TaraEukaryoticSMAG` | 14320496 |
| `uhgg_all_*` | none | |

(Pairings are by name and date; I did not check each database's genome names against each file
**(unverified)**, including the target pairings. The real check is phase 3's resolution test on a
sample of hits.) Zenodo's API (`/api/records/<id>`) gives size and MD5 per file, which fits the
`Database` entries (`size`, `md5`); file sizes range from 12 KB (fungi 2024) to 27 MB (IMG/VR).

New: `Database.taxonomy: str | None`; `sylphd download --taxonomy NAME` (repeatable),
`--list-taxonomies`, and `--with-taxonomy` for `--database` (fetch the paired file as well).
The existing `--release` GTDB-metadata download stays; `setup --taxonomy NAME` calls the same
function when the file is not in the cache. Remember the catalogue also needs to carry the
sylph-tax version the pairing was taken from.

## Phasing

Each step ships alone, with tests (pytest; `tests/data` plus the pysylph test files).

### Phase -1/0 — small independent fixes found during design

Small PRs, each shippable alone and none needing the multi-database work; they may land just
before step 0:

- **`--taxmatrix` order and values.** Sort clade names for two or more samples (as `sylph-tax
  merge` does), add the `ani` and `coverage` values of `sylph-tax merge --column ANI|Coverage`
  (B4), and correct `docs/guides/taxonomic-profiles.md`, which claims first-seen order is the same.
- **`Profiler` `ValueError` → 400.** A bad `k`/`c`, including a database genome denser than the
  sample, becomes an HTTP 400 in `_handle` instead of a 500 — it is a bad request, not a server
  fault.
- **Non-`GC[AF]` accessions get no metadata** (`accession_from_genome_name`); solved by B3 in
  phase 3, only documented until then.
- **Parity harness** against sylph-tax for the current GTDB path (below), which later phases
  extend.

### Phases 0–4

0. **Estimate read counts.** `profile --estimate-read-counts` backed by the fork's
   `Profiler(estimate_read_counts=...)` and `SampleSketch.mean_read_length`; bump the pysylph pin
   in `uv.lock` and document the option. Independent of A and B, and it is the only step that
   moves the pin, so the multi-database phases build on a fixed fork.
1. **A core.** `DatabaseSet` (replaces the single `LoadedDatabase` in `create_app`), repeatable
   `--db`, aliases, parallel load, `k` check, pooled `c`, `Profiler` LRU, `/info.databases`,
   `sylphd info` listing. Tests: pooled result equals `Profiler(*dbs)` run directly, with an
   ecoli `.syldb` + `.syl2db` + clodf13 mix (copy `clodf13.syldb` from pysylph) shaped like the
   target trio; `k` mismatch refuses to start; sketch `c` above the minimum is a 400 naming the
   database; **single-database `/info` and `/assembly` JSON equal a golden file**; a second
   request reuses the cached `Profiler`. Then a manual run on the real trio, recording load time,
   RSS and per-sample plain-scan time. **Done 2026-10-29**: load 2.3–2.6 s for the trio (GTDB
   2.2–2.5 s, UHGV 0.4–0.5 s, fungi 0.5–2.6 s, in parallel), RSS 3.22 GiB after the load and
   5.45 GiB peak (GTDB alone: 2.21/4.43 GiB), `Profiler` construction 93 ms, plain-database scan
   ~30 ms per sample. Numbers and method in `scratch/multi-db/REPORT.md`.
2. **B schema and `setup`.** Format v3, `taxonomy_*` tables, `--taxonomy SPEC` (`UHGV_default`,
   `FungiRefSeq-latest` for the target), `--update`, reader for v2/v3, `download
   --taxonomy`/`--list-taxonomies` (httpx mocked as in `test_download.py`). Tests: v2 file loads
   and serves; v3 built from small synthetic files (header row, gzip, 3 columns, duplicate keys
   across sources and across the 20,000-row batch split); the frozen v2 reader
   (`tests/compat/metadata_v2_reader.py`, commit `e72b807`) opens a v3 file. **Done 2026-10-29**:
   real-data run in `scratch/taxonomy/REPORT.md` (v3 file +25.8 MiB, `--update` 1.4–1.9 s).
3. **B resolution and the `tax` profile.** `sylphtax_probes`, `MetadataDB.resolve`, `tax_*`
   columns, `--taxonomy tax`, `--tax-source`, exact-sylph-tax aggregation (hosts resolved by `ord`
   independently of lineages, see B3). Tests: the parity plan,
   including the `--taxmatrix` output that phase -1 already fixed.
4. **Extras.** `--taxprof-virus-hosts`, per-database association (`?tax=`; needs A7 to come back),
   generic lineage in `classify_hits`, `download --with-taxonomy`, web label from `databases`.

A and B are independent until step 3: a one-database server with `--taxonomy UHGV_default` is
useful by itself, and step 1 is useful without any taxonomy work. A hit-origin step no longer
exists (A7 rejected).

## Parity-test plan against real sylph-tax

sylph-tax is pure Python and pandas; pin the commit (`d14f286`) and run it with
`uv run --with pandas` from `ref/sylph-tax`.

1. **Resolution goldens.** A script (`scripts/regen-sylphtax-golden`, manual, not CI) builds a
   synthetic taxonomy file and a sylph-style TSV covering each probe: `_ASM` in the path, a
   `_genomic` name, the `.gz` key, version-stripped key (`GCF_003697165`), contig id with `|`, a
   `.fa`/`.fasta`/`.fna` split, a pass-0 versus pass-1 conflict (I verified pass 1 wins), a miss
   (`NO_TAXONOMY`), a header row, three-column hosts with empty tokens. It runs real
   `sylph_to_taxprof.main` and commits the `.sylphmpa` outputs under `tests/data/sylphtax/`.
   CI compares `sylphd` output to them byte for byte, and a `skipif(no sylph_tax)` test re-runs
   sylph-tax live to catch drift.
2. **Aggregation parity.** Parse the TSV that `sylphd profile` writes (values as printed, so the
   same rounding sylph-tax sees), feed it to real sylph-tax, and compare with sylphd's own
   `--taxprof` built from the unrounded JSON: exact text on the TSV-derived path, and
   `abs <= 1e-3` per abundance on the end-to-end path (the TSV rounds abundances to four
   decimals).
3. **Merge parity.** Two or three profiles through `sylph-tax merge --column
   relative_abundance|sequence_abundance|ANI|Coverage` versus `--taxmatrix-value
   relative|sequence|ani|coverage`: clade order (sorted), zero fill, rows dropped when no sample
   has an ANI/coverage value, one-sample case (file order).
4. **Multi-database parity** (manual on the owner's real trio, plus a toy version in CI): `sylph
   profile gtdb.syl2db uhgv.syldb fungi.syldb sample.sylsp` versus the pooled server, rows equal
   after the TSV rounding; then both through sylph-tax versus `--taxonomy tax`. The toy CI version
   uses the ecoli databases (the pysylph tests already assert pooled == merged for the library).
5. **Real taxonomy files** (manual, slow): the target files (`UHGV_default`,
   `FungiRefSeq-latest`, `GTDB_r232` metadata) import; stored row counts equal the input rows minus
   repeated keys; a sample of keys resolves identically through `sylphtax_probes` and sylph-tax.
   IMG/VR only if it returns to scope.

## Open questions for the owner

None. The last one (species threshold outside prokaryotes) is settled: 95 % ANI for every
source (owner decision, 2026-10-29), with hints and warnings; see B6.

## Risks

- **Heuristic drift.** The probing rules are sylph-tax's, not a specification; a sylph-tax release
  can change them (v1.7.0 and v1.9.2 both did). Pin the version in `info`, test against goldens,
  and keep the rules in one pure function.
- **Upstream files.** Zenodo records and the GlobDB host are external; the GlobDB r232 file has no
  published MD5. Catalogue entries need size and MD5 captured at release time, like databases.
- **Cost surprises.** `Profiler` construction (A9) and per-sample plain-database scans (A4).
  Measured on the real trio in phase 1: construction 93 ms for 369 k genomes (cached, and the
  default is warmed at startup), and the plain databases add ~30 ms per sample (33 ms instead of
  1.3 ms for `profile`, 40–49 ms instead of 1 ms for `/assembly`), so the two cost surprises are
  bounded. `c=100` sketches are avoided by the c200 choice.
- **Duplicate genomes** across databases behave deterministically but not intuitively (database
  kind decides precedence). Documented, not hidden; the target trio has no overlaps.
- **Ambiguous keys.** Different sources with colliding keys resolve by probe order and `ord`. The
  target key spaces are disjoint, so the optional per-database association stays unbuilt.
- **Web assumptions.** The page reads `database.*` and `metadata.release` and GTDB lineages. The
  golden single-database test and additive-only JSON keep it working; a public multi-database
  server would show non-GTDB hits as `unknown lineage` until B6.
- **pysylph coupling.** Only the `estimate_read_counts` pin (step 0) changes the fork; `uv.lock`
  has to follow and the wheels in releases have to be rebuilt. Caching `Profiler`s keeps the
  multi-database work fork-free, at the cost of one small piece of state shared across requests
  (must be thread-safe and invalidated only on shutdown).
- **Rank semantics.** 95 % ANI as "same species" and the rank names are prokaryote conventions;
  applying them to viruses and eukaryotes is a labelling choice, not a measurement.

## Verification notes

Verified by reading: pysylph `lib.rs` (`Profiler`, `check_compatible`, `check_database_params`,
`run_pipeline`), sylph `contain.rs` (multi-database loop, `effective_genome_c`, `get_seq_sketch`,
`winner_table`), `twostage_db.rs` (lazy load), sylph-tax `sylph_to_taxprof.py`,
`merge_sylph_taxprof.py` (CLI `--column` choices, `#SampleID` naming, `dropna`/`fillna`,
`to_csv` output), `metadata_files.py`, and the sylphd modules named in the tables.
Verified by running: sylph-tax `main` on a synthetic file (pass-1 override, level ordering, host
column, `UNKNOWN`), sylph-tax merge (sorted rows), the Zenodo files (formats and row counts), an
IMG/VR load into the proposed layout (228 MiB), pysylph `Profiler` construction on a toy pair
(duplicate warning at construction). Database sizes and genome counts above are the catalogue
entries in `download.py`, cross-checked against the local `db/` files.

Verified by running in phase 1 (2026-10-29, machine with 16 cores and 30 GiB RAM): the target trio
loads (`k=31`, `c=200`, 199,923 + 168,536 + 661 genomes; the UHGV file has 168,536 genomes and its
MD5 matches the catalogue entry, whose genome count was wrong and is corrected), load time 2.3–2.6 s wall for the trio and 2.2 s
for GTDB alone, RSS 3.22 GiB after the load and 5.45 GiB peak for the trio (2.21/4.43 GiB for GTDB
alone), `Profiler` construction 93 ms for 369 k genomes, the plain-database scan ~30 ms per sample
(33 ms versus 1.3 ms for `profile`; 40–49 ms versus 1 ms for `/assembly`), a shared `Profiler`
used from 8 threads with identical results, and no duplicate-genome warning for the trio.

Still not verified: how the prebuilt databases name their genomes (beyond "the pooled results carry
no origin", which is by design) and their `c` per genome (only `uhgv_c200_dbv1.syldb` was checked:
all 168,536 genomes have `c=200`), profiling k-mers (a `.syldb` built with `--disable-profiling`),
duplicate behaviour of pooled reassignment at runtime, GlobDB's screening-index size, and the
database-to-taxonomy pairings in B7.

The Decisions section is the owner's; where it contradicts an analysis below, the decision wins.
