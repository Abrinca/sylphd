# Identifying an assembly

`sylphd assembly` finds the closest database genomes to one assembly — GTDB genomes on a GTDB
server, and viral, fungal or MAG genomes too when the server holds several databases with a
format 3 metadata file. It is the command-line twin of the [web app](https://abrinca.github.io/sylphd/): both **sketch the assembly locally** with the
database's `c` and `k` and send only the sketch to
[`POST /assembly`](../reference/http-api.md#post-assembly), where the server runs sylph and
classifies the hits. The sequence never leaves your machine (the command line sketches with
pysylph, the web app in the browser with WebAssembly), and the command line has no size limit on the assembly (the web app takes up to 2 GiB).
A sketch of a bacterial genome is 100–500 kB.

That works on every server, and the sequence is never sent, which is what you want when the assembly
is confidential. A server started with the optional `--allow-sequences` (the public instance is) also
takes raw FASTA, for scripts and local use: `--no-local-sketch` uploads the file as it is (limited by
`--max-sequence-mb`, 20 MB by default) and gives the same hits, but then the sequence does reach the server.

```bash
sylphd assembly assembly.fasta
sylphd assembly assembly.fasta --top 20 --min-ani 85 --json
```

The input is one assembly in FASTA or FASTQ, plain or gzip. All records of the file form one
sample, so a multi-contig draft is fine. With `--no-local-sketch` the server accepts only the bases `A C G T N` (in either case) and answers anything else with a 400.

## Output

A table on stdout with the closest genomes by decreasing ANI, and one summary line on stderr:

```text
rank  ani    relation      shared_rank  accession        taxon             gtdb_species      taxonomy_source  ncbi_organism_name     ncbi_taxid
1     99.53  same species  species      GCA_000005845.2  Escherichia coli  Escherichia coli  GTDB_r232        Escherichia coli K-12  562
2     90.12  same genus    genus        GCF_000...       Escherichia sp...  Escherichia sp...  GTDB_r232        ...                    ...
```

```text
assigned to species Escherichia coli (99.53 % ANI)
```

```text
assigned to Lambdavirus at the 95 % species threshold (99.98 % ANI)
sylphd: warning: viruses (UHGV): 95 % ANI matches the vOTU convention (MIUViG: ...)
```

```text
no species-level match (< 95 % ANI); closest relative ... (...)
```

A non-GTDB assignment is worded as "assigned to *taxon* at the 95 % species threshold", because
the taxon is the source's last named rank (an ICTV genus, a vOTU, a fungal species) and the
threshold is a heuristic there.

| Option | Default | Meaning |
|---|---|---|
| `--top N` | `10` | number of hits, 1-50 (`1` with `--per-contig`) |
| `--min-ani ANI` | `80` | minimum ANI in percent, 70-100 |
| `--name NAME` | file base name | query name (single-assembly mode only; `--per-contig` names each sample after its record id) |
| `--per-contig` | off | sketch every record as its own sample (see below) |
| `--min-contig-length BP` | `20000` | with `--per-contig`: shorter contigs are not sketched or sent and have no row; they are counted in `summary.skipped_short` |
| `--fields COLS` | `all` | with `--per-contig`: metadata columns per hit |
| `--consensus-ratio R` | `1.5` | with `--per-contig`: how much more divergent than the best hit a species may be and still join the consensus |
| `--consensus-z Z` | `2` | with `--per-contig`: allowance for ANI sampling noise, in standard deviations |
| `--local-sketch` / `--no-local-sketch` | `--local-sketch` | sketch on this machine and upload only the sketch, or upload the raw sequence (needs `--allow-sequences` on the server) |
| `--json` | off | print JSON instead of the table |
| `-o FILE` | stdout | write the output to a file |
| `--quiet` | off | no summary or caveat on stderr |

`taxon` is the finest taxon the hit's lineage names: GTDB's species for a GTDB hit, the vOTU for a
UHGV hit, the species for a fungal RefSeq hit. `taxonomy_source` says which taxonomy named it
(`GTDB_r232`, `UHGV_default`, `FungiRefSeq-latest`, ...). The columns `taxon`, `gtdb_species`,
`ncbi_organism_name` and `ncbi_taxid` are empty when the server has no metadata database, and
`gtdb_species` is also empty for a hit without an `s__` rank or from a non-GTDB source. The JSON
response carries every metadata column and the structured `taxonomy` of each hit; see the
[`POST /assembly` reference](../reference/http-api.md#post-assembly).

## Per-contig mode { #per-contig }

`--per-contig` makes one sample out of every FASTA/FASTQ record and reports the closest genomes and
a taxonomic consensus of each contig. Instead of one species call for the whole file, you get one
per contig, which is what a MAG contamination check needs: a 200 kbp contig from another species
shows that species even when the rest of the assembly is one genome. The contigs are sketched
locally and sent as a batch to
[`POST /contigs`](../reference/http-api.md#post-contigs), which does the classification on the
server; the sequence never leaves the machine. The endpoint is new, so `--per-contig` needs a
server running this version of `sylphd` (`GET /info` always lists `/contigs` here; if it is
missing, the server is older).

```bash
sylphd assembly mag.fasta --per-contig
sylphd assembly mag.fasta --per-contig --top 3 --fields none
```

**The ANI is contig-relative.** sylph's usual naive ANI divides by the *reference genome's* k-mer
count, which is a coverage-style number: an exact 20 kb fragment of a 5 Mb genome would look like
~85 % ANI. Per-contig mode instead reports `100 * (shared / contig_kmers) ** (1/k)`, with `shared`
from the server and `contig_kmers` a genome-style k-mer count of the contig computed locally, so
an exact fragment is 100 % and a contig at 97 % true ANI is 97 %. The 95 % species call and the
relations use that value; `--min-ani` filters on it too. The query is sent permissively
(`minimum_ani=0`, `screen_ani=0`, `min_contain=3`, `no_adjust`) so the server cannot drop a short
contig on the way.

The output has one row per sent contig and hit with `contig`, `contig_length`, `gc`, `status`
(`ok` or `no_hit`), `rank`, `ani`, `shared_kmers`, `contig_kmers`,
`explained_fraction`, `reference_ani`, `relation`, `shared_rank`, `ani_species_level`, `accession`,
`genome_id`, `gtdb_species`, `taxon`, `n_close`, `consensus_rank` and `consensus_taxon`, then the metadata
columns (`--fields`). `taxon` is the hit's finest named rank from whatever source resolved it.
`reference_ani` is sylph's genome-denominator value, kept for continuity;
`explained_fraction` is the fraction of the contig's k-mers the hit explains. A summary on stderr
gives the number of queried contigs and bases and the fraction of bases per consensus taxon (top
five), which is the contamination signal. Duplicate record ids get a `_2`, `_3` suffix.

**The consensus is what you should read.** The top hit is not stable: in GTDB r232 the genomes
nearest to *E. coli* K-12 belong to several near-identical placeholder species and genera, so which
one wins changes with a k-mer or two. Among near-completely contained hits (>= 95 % of the contig
explained, i.e. >= ~99.83 % ANI) within the noise band, the ranking prefers the reference with the
fewest k-mers (parsimony), which is what puts a phage genome ahead of the bacterial assemblies that
merely carry a prophage copy of it; below that containment the evidence order and the consensus
stand unchanged. Per contig the command therefore keeps the best hit per
species and takes the lowest common rank of the ones whose divergence is within `1.5x` the best
hit's plus `2` standard deviations of the ANI sampling noise (see
[the rule](../commands/assembly.md#per-contig-consensus)). A near-exact, well-covered contig
resolves to its species; an ambiguous, short or noisy one widens to genus, family or higher instead
of inventing a species. A species consensus also requires the 95 % species threshold, so a distant
contig stops at genus. The lineages are read generically: a UHGV close set resolves to `level 4`
(a vSUBGEN) where a GTDB one resolves to a genus. Lineages of **two sources are never combined** —
a close set that spans sources has no consensus (`mixed_sources: true` in JSON), because the rank
names of two taxonomies are not the same yardstick. `--consensus-ratio` and `--consensus-z` tune the
allowance; the defaults
resolve 166 of the 170 contigs of a real *Leptospira* assembly to *L. interrogans*, 3 to
`Leptospira` (genus, 91.8-94.4 % to their closest species) and 1 to `Bacteria` (3.3 kb), while a
K-12 contig comes out as `Enterobacteriaceae` (family) — exactly how far GTDB r232's *E. coli*
group is resolvable.

**GTDB names, not NCBI names.** The assignment is a GTDB species, which may be a placeholder: K-12
MG1655 is `G047199095 sp047199095` in r232, not `Escherichia coli`. Add
`--fields ncbi_organism_name` (or `all`, the default) for the NCBI name of each hit.

**Short contigs and the screen.** Contigs shorter than `--min-contig-length` (20000 bp by default)
are not queried and have no row; they are counted in the summary (`skipped_short`, and
`skipped_few_kmers` for a contig with fewer than sylph's 10 sketched k-mers or with an empty genome
sketch, such as a tandem repeat). A contig that was sent but matches nothing is `no_hit` rather
than a spurious match. At `c=200` a 20 kb contig has ~85 genome-style k-mers, which
gives roughly ±0.5 % ANI; at 10 kb (~43 k-mers) it is ~±1 % and only down to ~93 % ANI; below ~90 %
ANI a short contig has too few shared k-mers to resolve anything below family. Two-stage `.syl2db`
databases additionally drop some short contigs in their first-stage screen before any threshold
applies — measured on a real *Leptospira* assembly, 30 of the 236 queried contigs with a genuine
98-100 % match were lost to it (most below 5 kb, but also one at 22.9 kb and 99.22 % ANI), and 7
more of the 243 contigs of at least 2 kb were too small to sketch. `no_hit` therefore
means "no evidence", not "no relative": the screen may simply not have looked. Use a plain `.syldb`
or longer contigs for short-contig work, and treat the ANI of a few-kbp contig as a hint, not as a
species-level identification.

**Prophages and contaminants.** A reference genome that carries a phage (or any contaminant) as an
extra contig contains every k-mer of that phage, so containment alone cannot separate it from the
phage genome; the parsimony rule above does, because the phage genome is orders of magnitude
smaller. The other direction is deliberately not attempted: a contig that *is* a prophage region of
a genome will still match the phage genome first when both are in the database, and the consensus
says how far the evidence separates them (often `mixed_sources`, when a viral and a bacterial
source both have close hits).

Cost is per contig but small: against a 200 000-genome database, 236 short contigs took 0.24 s and
38 contigs totalling 1.87 Mb took 0.4 s, i.e. a few milliseconds per contig on the server plus
0.1-0.3 ms of local sketching. The full column and JSON reference is in
[`sylphd assembly`](../commands/assembly.md#per-contig-mode).

## Sketching in the browser { #sketching-in-the-browser }

In the [web app](https://abrinca.github.io/sylphd/#assembly) the sketching happens in your browser
(WebAssembly, the same code as for [reads](profiling-in-browser.md)). Paste FASTA or drop a file of
up to 2 GiB (gzip is fine); only the sketch is sent, and the result shows a *sketched locally* badge.
You can also upload a sketch you made earlier (`.sylsp`, for example from `sylph sketch` or
pysylph, or downloaded from the [Reads tab](profiling-in-browser.md#reupload)): it is sent as it
is, after the page has checked its `c` and `k` against the server. Records, length and GC are
then unknown and shown as "–".

A browser without WebAssembly SIMD and module workers (Chrome/Edge 91, Firefox 114, Safari 16.4 or
newer have them) cannot use the page and says so; use `sylphd assembly` there. See
[Running a public server](public-server.md#sketches-only-or-raw-sequences-too-allow-sequences).

Per-contig mode in the browser keeps at most the 100 longest contigs (largest first) and sends them
in batches of the server's `limits.max_contigs`. It reads at most 1,000,000 records per run and
refuses any single record longer than 50 Mbp, with a message saying so: those are tab-memory
guards, and `sylphd assembly FILE --per-contig` has neither limit.

## Why the ANI is not adjusted { #why-the-ani-is-not-adjusted }

sylph's default ANI is *adjusted* for sequencing coverage: it fits a coverage model to the k-mer
counts of a read sample and corrects the containment ANI accordingly. In an assembly every k-mer
occurs once, so there is no coverage to model. The correction is meaningless and can inflate the
ANI to 100.

`sylphd assembly` therefore runs sylph with `no_adjust=True` and reports the **naive containment
ANI**, which is the meaningful measure when the query is itself a genome.

A real example, assembly FAM22000 (8 contigs, 2.59 Mb) against its best GTDB hit:

| | value |
|---|---|
| Adjusted ANI | `100.00` |
| 5-95 % interval of the adjusted ANI | `112.40-114.13` (above 100 %) |
| Effective coverage | `0.014` |
| **Naive ANI** | **`98.65`** |
| Contained k-mers | `7096/10826` |

The adjusted value claims 100 % identity with an interval that cannot exist, while the naive ANI is
a plain, interpretable 98.65 %. With `no_adjust`, the confidence interval is left out entirely: it
still belongs to the disabled adjustment.

The same switch exists for read workflows as [`--no-adjust`](../commands/profile.md) on `query`
and `profile`, if you use an assembly as a sample there.

## Species assignment

The server answers with an `assignment`: does the best hit say which species the query is?

- **95 % ANI is GTDB's species circumscription** and the only hard threshold. If the best hit
  reaches `95` %, the query is **assigned to that hit's species** — for a hit from another
  taxonomy source, to the last named rank of its lineage (see
  [Outside prokaryotes](#outside-prokaryotes)).
- Otherwise there is no species-level match. The response then has no assignment, and the
  `closest` relative (the best hit) is reported instead.
- Below **85 %** the k-mer ANI is imprecise. `thresholds.imprecise_below` (`85`) is only a hint for
  readers and never changes an assignment.

Without a metadata database the server cannot name the species. The assignment still applies at
`ani >= 95`, but its taxon is `null`.

## Hit relations

Every hit gets a `relation`, derived from its **lineage**, not from the ANI. The server compares
the lineage of each hit with a reference lineage and takes the deepest rank they share. A rank only
counts when all shallower ranks agree.

- With an assignment, the reference is the **assigned species**, and hits are labelled
  `same species`, `same genus`, `same family`, ... up to `same domain` (or `different domain`).
- Without one, the reference is the **closest relative** (the best hit). That hit is labelled
  `closest relative`, and others `same genus as closest relative`, and so on.

A hit in the same species as the assignment is `same species` whatever its ANI. Separately,
`ani_species_level` is true for every hit with `ani >= 95`. A hit that has it but a different
species name (a species boundary case) gets the lineage relation, for example `same genus`, plus
`ani_species_level: true`. The full table of relations is in the
[API reference](../reference/http-api.md#hit-relations).

Without metadata (ANI only), a hit is `same species` for `ani >= 95` and `related` otherwise.

Lineages are read whatever their shape: GTDB's `d__;p__;c__;o__;f__;g__;s__`, fungal RefSeq's
`k__Eukaryota;p__...;s__...`, ICTV's `root;Monodnaviria;...` and UHGV's prefix-less
`vFAM-...;vOTU-...` (which becomes `level 1` ... `level 5`). Relations are only computed **within
one taxonomy source**: a hit from another source has no comparable ranks, so it gets
`different source` (or `different source from closest relative`) with `shared_rank: null`. GTDB
hits always compare with each other, whatever source name resolved them.

## Outside prokaryotes { #outside-prokaryotes }

The 95 % threshold is a prokaryote convention (see
[What does "same species" mean?](../species-and-ani.md#outside-prokaryotes)). On a server with
UHGV, fungal RefSeq or other non-GTDB databases the server still applies **the same 95 %** to every
source, but it says so:

- `assignment.note` (JSON) and a `sylphd: warning: ...` line on stderr (suppressed by `--quiet`,
  never printed in `--json` mode) when the best hit is not from GTDB. Viruses (UHGV) get the vOTU
  wording — MIUViG calls a vOTU at 95 % ANI over 85 % of the shorter genome, and sylph's ANI
  ignores the aligned fraction. Fungi get "no established ANI boundary, a heuristic". Any other
  source gets a generic note.
- `--per-contig` and `--taxprof`/`--taxmatrix` with `--taxonomy tax` print the same caveat **once
  per output**, not once per row.
- `thresholds.species` in JSON is still one value; a per-source threshold may come later.

## Limits

A sketch upload is limited by `--max-upload-mb` (default 512 MB; the sketch of a bacterial genome
is 100–500 kB). Only a raw upload (`--no-local-sketch`, which needs `--allow-sequences`) is limited
by `--max-sequence-mb` (default 20, before decompression), and gzip bodies may decompress to at most
five times that. The server logs sizes, timings and outcomes, never the
sequence. See [Running a public server](public-server.md#limits).
