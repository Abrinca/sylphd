# assembly

```text
sylphd assembly FILE [--top N] [--min-ani ANI] [--name NAME] [--local-sketch | --no-local-sketch]
                [--per-contig [--min-contig-length BP] [--fields COLS]
                              [--consensus-ratio R] [--consensus-z Z]]
                [--json] [-o FILE] [--quiet]
                [--socket PATH | --tcp HOST:PORT | --url URL] [--timeout SECONDS]
                [--retries N]
```

Sketches one assembly (FASTA or FASTQ, plain or gzip) on this machine, sends the sketch to
[`POST /assembly`](../reference/http-api.md#post-assembly) and prints a compact table of its closest
database genomes. The sequence never leaves your machine and has no size limit; this works on any
server, with or without `--allow-sequences`.

With `--per-contig` every FASTA/FASTQ record is sketched and queried as its own sample instead, and
the command reports the closest genomes and a taxonomic consensus of each contig. That is the mode
for MAG contamination checks; see [Per-contig mode](#per-contig-mode).

| Option | Default | Meaning |
|---|---|---|
| `FILE` | required | assembly in FASTA or FASTQ, plain or gzip |
| `--top N` | `10`, `1` with `--per-contig` | number of hits to print per sample, 1-50 |
| `--min-ani ANI` | `80` | minimum ANI in percent, 70-100 |
| `--name NAME` | file base name | query name (single-assembly mode) |
| `--per-contig` | off | sketch every record as its own sample |
| `--min-contig-length BP` | `20000` | with `--per-contig`: shorter contigs are not sketched or sent, only counted |
| `--fields COLS` | `all` | with `--per-contig`: metadata columns per hit (`all`, `none` or a comma-separated list) |
| `--consensus-ratio R` | `1.5` | with `--per-contig`: a species joins the consensus when its divergence is at most R times the best hit's, R >= 1 |
| `--consensus-z Z` | `2` | with `--per-contig`: ... plus Z standard deviations of the ANI sampling noise, Z >= 0 |
| `--local-sketch`, `--no-local-sketch` | `--local-sketch` | sketch locally and upload only the sketch, or (`--no-local-sketch`) upload the raw sequence, which only a server started with `--allow-sequences` accepts; `--per-contig` always sketches locally |
| `--json` | off | print JSON instead of the table |
| `-o, --output FILE` | stdout | write the output here |
| `-q, --quiet` | off | do not print the summary or caveat on stderr |

The table has the columns `rank`, `ani`, `relation`, `shared_rank`, `accession`, `genome_id`,
`taxon`, `gtdb_species`, `taxonomy_source`, `ncbi_organism_name` and `ncbi_taxid`. `taxon` is the
finest taxon the hit's lineage names (GTDB's species for a GTDB hit, a vOTU for UHGV, a species for
fungal RefSeq), `taxonomy_source` the source that named it, and `gtdb_species` stays GTDB-only.
`genome_id` is what identifies the genome when `accession` is empty: the taxonomy key a sylph-tax
source resolved, else the first contig id, else the genome file (UHGV stores every genome in
`votus_full.fna.gz`, so the file name identifies nothing). A summary
line on stderr says whether the query was assigned to a species (`assigned to species ...
(99.53 % ANI)`) or only has a closest relative (`no species-level match (< 95 % ANI); closest
relative ...`). `--quiet` suppresses that line; `--json` never prints it.

When the best hit is **not from GTDB** — a UHGV, fungal RefSeq or MAG database — a second line
follows the summary, once per run: `sylphd: warning: UHGV_default (viruses): 95 % ANI matches the
vOTU convention ...` or the fungal/generic wording, naming the actual taxonomy source (so
`UHGV_ictv` and `FungiRefSeq-latest` appear as themselves). The same text is the `note` of the JSON
`assignment`
(or `closest`), and the summary then reads `assigned to <taxon> at the 95 % species threshold`
instead of `assigned to species <taxon>`, because the taxon is the source's last named rank (an
ICTV genus, a vOTU, a fungal species). The 95 % threshold itself does not change per source; see
[Outside prokaryotes](../guides/assembly.md#outside-prokaryotes).

The ANI is the naive containment ANI (`no_adjust`), the meaningful measure for an assembly.
[Identifying an assembly](../guides/assembly.md) explains this, the 95 % species assignment and
the hit relations. Per-contig mode reports a *contig-relative* ANI instead (see
[below](#per-contig-ani)); the two numbers answer different questions.

## Per-contig mode { #per-contig-mode }

`--per-contig` reads the file record by record, sketches each record on its own with the server's
`c` and `k` (from [`GET /info`](../reference/http-api.md#get-info)) and sends the sketches to
[`POST /contigs`](../reference/http-api.md#post-contigs), which returns one classified result per
contig. Each contig is one sample, so the ANI is *not* diluted by the rest of the assembly: a
contaminating contig shows its own species, not the species of the whole file. The sequence still
never leaves your machine — only the sketches and a few numbers per record do.

```bash
sylphd assembly mag.fasta --per-contig
sylphd assembly mag.fasta --per-contig --top 3 --min-ani 90 --fields none
sylphd assembly mag.fasta --per-contig --min-contig-length 5000 --json -o contigs.json
```

### The ANI is contig-relative { #per-contig-ani }

sylph's `Naive_ANI` divides the shared k-mers by the **reference genome's** k-mer count. That is
the right coverage-style number for a whole assembly, but not for a contig: an exact 20 kb
fragment of a 5 Mb genome would report ~85 % ANI. Per-contig mode therefore computes, on the
server,

```text
ani         = 100 * (shared / contig_kmers) ** (1 / k)
explained   = shared / contig_kmers          (= (ani / 100) ** k)
```

where `shared` is `Containment_ind[0]` (the reference's k-mers found in the contig) and
`contig_kmers` is a genome-style k-mer count of the contig itself, measured locally while
streaming and sent with the sketch. An exact fragment scores exactly 100.00 %, and a contig at
97 % true ANI scores 97 %. The 95 % species assignment and every `relation` use this value.
`--min-ani` is applied to it, not to sylph's `Naive_ANI`: the query is sent with `minimum_ani=0`
and `screen_ani=0` so that sylph's genome-denominator ANI cannot censor a short contig. The other
query settings are `no_adjust`, `min_contain=3` (drops the one- or two-k-mer tail) and
`min_number_kmers=10` (which applies to the reference genome). Hits are ranked by `shared`
(equivalently by the contig ANI; `shared` is the likelihood order), not by sylph's `Naive_ANI`,
and then cut to `--top`.

The naive number is still reported as `reference_ani`, but it is a coverage-style value that
depends on the reference's size; do not read it as the contig's ANI. Inside the noise band it is
also the parsimony signal.

Two small biases remain. The denominator assumes sylph's default `--min-spacing` of 30 (pysylph's
`Sketcher` hard-codes it); a database built with another spacing shifts the ANI by roughly 0.1 %.
And `contig_kmers` is the contig's own genome sketch: on a repeat-rich contig it can exceed the
number of that contig's k-mers that survive in the *whole-genome* reference sketch, which biases
the ANI down slightly (it is clamped at 100 %). Both are far smaller than the sampling noise at
the default contig length.

### Ranking inside the noise band { #per-contig-parsimony }

`shared` saturates at `contig_kmers` whenever a reference *contains* the whole contig, and many
public assemblies carry contaminant contigs: GTDB representatives with a lambda prophage hold
every k-mer of a lambda contig, exactly like the (far smaller) phage genome itself. Containment
cannot tell "this contig is a region of this genome" from "this genome happens to carry a copy of
it", but the genome size can.

So among the hits that are statistically indistinguishable **on the contig side** — the same close
band the consensus uses (below) — *and* that explain at least 95 % of the contig
(`explained_fraction >= 0.95`), the reference with the **fewest k-mers** is listed first: it is the
most parsimonious explanation of the contig, and its `reference_ani` is the highest. The
saturation condition is what keeps the rule honest: parsimony is only meaningful when the contig is
(almost) completely contained, and at `k=31` a hit at `explained_fraction = 0.95` is already at
~99.83 % ANI, so reordering can never move a >= 95 % assignment below the species threshold. Every
other hit — outside the band, or inside it but not near-saturated — keeps the plain evidence
order. Measured on the real lambda contig of
`scratch/taxonomy/phase4a/mock-mixed.fasta` against GTDB r232 + UHGV, the viral hit
(`82/210` k-mers, `reference_ani` 97.0) now comes first instead of the six lambda-carrying GTDB
assemblies at `83/10 271` (`reference_ani` 85.6), so the contig is assigned to `Lambdavirus`
rather than to *Andreesenia angusta*.

Because only near-saturated hits are reordered, a 97 % ANI hit is never displaced by a smaller
reference at 95 %: at that divergence only ~39 % of the contig is explained, and the evidence
order stands. Measured on the mock MAG (`scratch/contigs-endpoint/`), rank-1 accessions and
assignments are unchanged from before the rule except on near-completely contained contigs. Read
the consensus, which uses every hit and is unaffected by the ordering.

### Columns and statuses

The TSV has one row per contig and hit (and one row for a contig that has none), with these leading
columns followed by the metadata columns selected by `--fields`:

```text
contig  contig_length  gc     status  rank  ani    shared_kmers  contig_kmers  explained_fraction  reference_ani  relation      shared_rank  ani_species_level  accession        genome_id        gtdb_species      taxon             n_close  consensus_rank  consensus_taxon
contig1 1954431        50.76  ok      1     99.12  6398          8413          0.7605              96.16          same species  species      true               GCA_000005845.2  GCA_000005845.2  Escherichia coli  Escherichia coli  2        family          Enterobacteriaceae
contig8 18000          61.20  no_hit
```

A record shorter than `--min-contig-length`, or one that is not sent for too few k-mers, has no
row at all: it is only counted in the summary (`summary.skipped_short` /
`summary.skipped_few_kmers` in JSON, and the stderr line).

- `ani` is the contig-relative ANI in percent, `shared_kmers` the reference k-mers found in the
  contig, `contig_kmers` the contig's own genome-style k-mer count, and `explained_fraction` the
  fraction of the contig's k-mers the hit explains (0-1). It is not a purity measure: a divergent
  reference, a congener hit or a contig whose k-mers are repeated elsewhere in the reference all
  land well below 1.
- `reference_ani` is sylph's genome-denominator `Naive_ANI` in percent.
- `ani_species_level` is `true` when the hit's ANI alone reaches 95 %; `relation` and `shared_rank`
  come from the hit's lineage (within one taxonomy source), as in single-assembly mode;
  `gtdb_species` is empty without metadata, without an `s__` rank or for a non-GTDB hit, and
  `taxon` is the hit's finest named rank from whatever source resolved it (a GTDB species, a UHGV
  vOTU, a fungal species).
- `n_close`, `consensus_rank` and `consensus_taxon` describe the whole contig (see
  [Taxonomic consensus](#per-contig-consensus)); they are the same on every row of a contig.
- `status` is `ok` or `no_hit`. Use `--fields none` for this compact table, `--fields all` (the
  default) for every metadata column, or name the columns you want. A record that was never sent
  (too short, or too few sketched/genome-style k-mers) is not a row at all; it is counted in the
  summary. (`POST /contigs` still answers `too_short`/`too_few_kmers` for a client that sends such
  a sketch, but the command no longer sends them.)

### Taxonomic consensus { #per-contig-consensus }

The best hit is not a stable answer. In GTDB r232 *E. coli* K-12 MG1655 is not in
`s__Escherichia coli` but in the placeholder genus `g__G047199095`, together with a swarm of
near-identical genomes: on a 20 kb K-12 window the two best GTDB species are 0.11 ANI points apart
and sit in *different* GTDB genera, so which one wins changes with a k-mer or two, at every contig
length. Reporting only the top hit therefore reports noise as if it were a species.

Per contig (like CAT/BAT's lowest common ancestor) the command therefore groups **every hit the
server returned** (whatever `--top` and `--min-ani` are) and reports the deepest rank the evidence
supports:

```text
d_i          = 100 - ani_i                    divergence of a species hit
close        = d_i <= R * d_best + Z * sqrt(sd_best^2 + sd_i^2)
sd           = (100/k) * sqrt((1 - p) / (n * p))    p = shared_kmers / contig_kmers, n = contig_kmers
consensus    = lowest common rank of the GTDB lineages of the close species (best included)
```

1. Only the best hit of each species counts (by unrounded ANI); `n_close` is the number of
   species left after that reduction.
2. `R * d_best` says how much more divergent than the best hit a species may be, and the noise term
   widens that allowance exactly as much as the ANI estimates themselves are uncertain: `sd` is
   ~0.05 points for a 500 kb contig and up to ~1.7 points for a 3 kb one with only a few shared
   k-mers. A short contig therefore widens to genus or family where a long one stays at species.
3. The defaults are `R = 1.5` and `Z = 2` (95 % of the sampling noise). Two examples at those
   values, on a contig of 20 kb or more: hits at 99.9 and 99.1 % (d = 0.1 and 0.9) stay one
   species, because 0.9 is far beyond `1.5 * 0.1` plus the noise; hits at 89.9 and 89.1 %
   (d = 10.1 and 10.9) are close, because `10.9 <= 1.5 * 10.1`, and the consensus becomes their
   genus. Raise `--consensus-ratio` to include more distant species (a wider, shallower consensus),
   or lower it to 1 to require a species to be almost exactly as close as the best one;
   `--consensus-z 0` drops the noise allowance.
4. A `species` consensus also requires the best hit to reach the same **95 % species threshold**
   that `assignment` uses. Below it the lowest common rank is truncated to the rank above the
   species-equivalent rank, even when the closest species is the only close hit: a 25 kb contig at
   91.75 % to *Leptospira alexanderi* comes out as `Leptospira` (genus), not that species. For a
   prefix-less lineage the species-equivalent rank is its deepest level, so a lone vOTU at 91 %
   is reported one level up (a `level 4`, vSUBGEN) instead.
5. Lineages are read generically: GTDB `d__..;s__`, fungal `k__Eukaryota;..`, ICTV
   `root;Monodnaviria;..` (``root`` is not a taxon) and UHGV's prefix-less `vFAM-..;vOTU-..` become
   `level 1` ... `level N`. `consensus_rank` is the rank's display name, so a viral consensus can
   be `level 4`.
6. Only lineages of **one source** are intersected. When the close set spans two taxonomy sources
   the consensus is empty (`consensus_rank`/`consensus_taxon` empty in the TSV) and the JSON
   consensus has `mixed_sources: true`: the ranks of two taxonomies are not the same yardstick.
7. Hits that merely *contain* the contig in a far larger reference stay in the set: the consensus
   says which taxa the evidence cannot separate, not which reference is the source. A phage contig
   whose close set spans UHGV and GTDB genomes therefore keeps no consensus (`mixed_sources`), even
   though [parsimony](#per-contig-parsimony) ranks the viral hit first.

The consensus is not a claim that the contig is a member of that taxon: it is the deepest rank the
close set agrees on. Below roughly **90-92 % ANI** there is little left to agree on — at 90 % ANI a
10 kb contig has ~1.6 expected shared k-mers and often none — so a short, distant contig can come
out at family, phylum or `Bacteria`. That is the honest answer for the evidence it has.

GTDB names are GTDB's, and some are placeholders: K-12 MG1655 is `G047199095 sp047199095` in r232,
not `Escherichia coli`, and the consensus of a K-12 contig is `Enterobacteriaceae` (family) because
the nearest GTDB genomes belong to several placeholder genera. `--fields ncbi_organism_name` adds
the NCBI name of each hit, which is the bridge back to familiar species names. Chimeric contigs are
not detected: k-mer counts cannot tell them from a uniformly divergent contig.

### Short contigs and the database screen

- `--min-contig-length` defaults to **20000 bp**. At `c=200` that is about 85 genome-style k-mers;
  the ANI error is then roughly ±0.5 % (and ±1 % at 10 kb, growing quickly below that). Short
  contigs are not sketched or sent and produce no rows; they are counted in
  `summary.skipped_short` (`{contigs, bp}`) and in the stderr summary. This keeps memory and the
  table flat on FASTQ input with millions of short reads.
- A contig whose sample sketch holds fewer k-mers than sylph's `min_number_kmers` (10) is not sent
  either, because an ANI from a handful of k-mers is noise. So is a contig whose *genome* sketch is
  empty, which happens for a tandem repeat: every k-mer occurs more than once, a genome sketch
  drops those, and the contig-relative ANI would divide by zero. Both are counted in
  `summary.skipped_few_kmers` (`{contigs, bp}`) and have no row. A contig that was sent but has no
  hit above `--min-ani` is `no_hit`.
- **`no_hit` is not proof that the database has no relative.** There are three ways to get one and
  the output cannot tell them apart: nothing passes `--min-ani`, the contig is too small for
  `min_number_kmers`, or — on a two-stage `.syl2db` database — the **first-stage screen never looked
  at the right genome**. The screen only sees the database k-mers below a coarse subsampling rate,
  so a contig that carries no such k-mer for its true reference is dropped before any threshold,
  `--min-ani` or `--min-contig-length` value can help. The probability grows as the contig gets
  shorter and less similar; a denser database does **not** fix it (the screen rate is independent of
  `c`).
- The screen rate is in `sylphd info` (field `screen_c`; `null` for a plain `.syldb`, and
  `database.screen_c` in the [`GET /info`](../reference/http-api.md#get-info) JSON):

  ```console
  $ sylphd info --socket /run/user/1000/sylphd.sock | grep screen_c
    c: 200  k: 31  screen_c: 2235  two-stage: yes
  ```

  The survival model is roughly `1 - exp(-L * (ANI/100)^k / screen_c)` (`L` in bp), but it is
  **optimistic**: measured on GTDB r232 (`screen_c=2235`) against a real 368-contig *Leptospira*
  assembly, 243 contigs are at least 2 kb; 7 of those were too small to sketch (fewer than 10
  sample k-mers, 17,545 bp), so 236 were queried and 66 are `no_hit`. Of those 66, **30** had a
  genuine 98-100 % match with at least three shared k-mers, i.e. the screen dropped a real
  relative: 4 of 17 queried at 2-3 kb, 14 of 35 at 3-5 kb, 8 of 53 at 5-10 kb, 3 of 49 at 10-20 kb
  (10.0, 10.8 and 13.6 kb) and 1 of 56 at 20-50 kb (22.9 kb, 99.22 % ANI), none at 50 kb and
  above. At the default 20 kb floor, 82 of 368 contigs were queried and 2 were `no_hit`: one
  genuinely has no k-mer in the representative (strain-specific DNA), the other is a screen loss.
  Treat `no_hit` on a `.syl2db` as "no evidence", not as "no relative", and use a plain `.syldb`
  (or `sylph db convert --screen-c`) when short contigs matter.

### Requests, classification and cost

- **Requests are chunked.** Contigs are sent as batches to `POST /contigs`, each with the
  contig's genome-style k-mer count, length and GC content. A whole request is **one server job**,
  so the chunk size is the server's `limits.max_contigs` (`--max-contigs`, 4096 by default),
  not `max_jobs + max_queue`; it is also cut when the JSON
  `params` part (which
  repeats every contig name, length and k-mer count; names may be up to 1024 characters) would
  exceed 90 % of `limits.max_params_bytes` (900 KiB at most), and when the *measured* multipart
  body would exceed `max_upload_mb`. A request that is still too large is split in half. Every
  chunk is one POST and counts once against `--rate-limit`; 100 contigs are one request, not six.
  The per-request summaries are added up, so the output is the same however the contigs were
  chunked.
- **Classification** is identical to the single-assembly path, but with the contig-relative ANI,
  and happens on the server with the same metadata: the 95 % species assignment (`assignment`),
  the closest relative, and the `relation`/`shared_rank` from the hit's lineage (generic, within
  one taxonomy source).
- **`--top`** defaults to `1` here: one row per contig is what a contamination check needs, and the
  consensus already summarises the other hits. `--top N` lists up to N hits per contig, with the
  consensus columns repeated on each row. The consensus always uses every hit the server returns,
  whatever `--top` **and `--min-ani`** are: `--min-ani` only hides rows, it cannot change the
  consensus. A contig whose best hit is below `--min-ani` is `no_hit` with a `null` consensus.
- **Cost.** All contigs of one request run one after the other inside a single job slot (a
  request holds one of `--max-jobs`), but the two-stage screen keeps the
  dense pass cheap: against a 200 000-genome `.syl2db`, 38 contigs (1.87 Mb of 5-300 kb windows)
  took 0.4 s end-to-end and 236 short contigs (4.75 Mb) 0.24 s, i.e. roughly 0.3-6 ms of server
  time per contig (more for large contigs that admit many candidate genomes) plus 0.1-0.3 ms of
  local sketching. The server returns every genome above `min_contain` and the client trims to
  `--top`, so a long contig in a dense family costs more than a short one.
- **Sample names** are the first word of each record header (`>contig1 some description` becomes
  `contig1`), validated like every other sample name. A repeated id gets a numeric suffix, in file
  order: `contig1`, `contig1_2`, `contig1_3`. A later record literally named `contig1_2` becomes
  `contig1_2_2`, so names are always unique and deterministic.

### JSON

`--json` prints one document with `query`, `database`, `params`, `thresholds`, a `summary` and a
`contigs` list. Each contig has `id`, `index` (record number), `length`, `gc`, `status`,
`sketch_kmers`, `contig_kmers`, optionally `reason`, a `consensus` object, and
`assignment`/`closest` objects of the same shape as
[`POST /assembly`](../reference/http-api.md#post-assembly), plus its `hits`. `consensus` is `null`
for a contig without hits, otherwise `{"rank", "taxon", "n_close", "species"}` (and
`"mixed_sources": true` when the close set spans two taxonomy sources), with `rank` and `taxon`
`null` when there is no metadata, the close species share no rank or the sources are mixed:

```json
"consensus": {"rank": "genus", "taxon": "Bacillus", "n_close": 2,
              "species": ["Bacillus subtilis_A", "Bacillus velezensis"]}
```

Every hit carries `rank`, `ani`, `shared_kmers`, `contig_kmers`, `explained_fraction`,
`reference_ani`, `reference_containment`, `relation`, `shared_rank`, `ani_species_level`,
`genome_file`, `genome_id`, `accession`, `gtdb_species`, `taxon` and, when it has a lineage,
`taxonomy` and `taxonomy_source`, and the metadata columns of `--fields` flat in the hit.
`params` repeats `top`, `min_ani`, `min_contig_length`, `consensus_ratio`, `consensus_z` and the
query settings sent to the server. JSON mode never writes to stderr, as in single-assembly mode:
the summary is in the document, so `--quiet` is only useful in table mode.

In table mode a summary goes to stderr: how many contigs and bases were queried, how many were
skipped for length (`N contigs (X kb) below 20000 bp not classified`) or for too few k-mers, how
many had no hit, and the fraction of the queried bases per **consensus** taxon with its rank (the
top five). The JSON `summary` carries the same counters as `skipped_short` and
`skipped_few_kmers`, each `{contigs, bp}`, so skipped records never appear in `contigs`. When a contig's best hit is not from GTDB, the
caveat of that source (viruses, fungi, generic — see
[Outside prokaryotes](../guides/assembly.md#outside-prokaryotes)) follows **once per output**,
however many contigs it applies to. Aggregating by consensus rather than by top hit
is what makes the line readable: one organism spread over several near-identical GTDB species
(and their placeholder genera) counts once, at the rank the data supports. A large minority taxon
is the contamination signal.

```text
per-contig: 27 of 38 contig(s), 1,790,000 bp queried; 11 contigs (80.0 kb) below 20000 bp not classified; 1 without a hit
bp by consensus: Enterobacteriaceae (family) 880,000 bp (49.2 %), G047199115 sp047199115 (species) 450,000 bp (25.1 %), Cutibacterium acnes (species) 165,000 bp (9.2 %), Bacteria (domain) 140,000 bp (7.8 %), Bacillus subtilis (species) 85,000 bp (4.8 %)
```

Below `--min-contig-length` the numbers get noisy fast; treat ANI from a few-kb contig as a hint,
not as a species-level identification.
