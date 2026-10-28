# Identifying an assembly

`sylphd assembly` finds the closest GTDB genomes to one assembly. It is the command-line twin of
the [web app](https://abrinca.github.io/sylphd/): both **sketch the assembly locally** with the
database's `c` and `k` and send only the sketch to
[`POST /assembly`](../reference/http-api.md#post-assembly), where the server runs sylph and
classifies the hits. The sequence never leaves your machine (the command line sketches with
pysylph, the web app in the browser with WebAssembly), and there is no size limit on the assembly.
A sketch of a bacterial genome is 100–500 kB.

That works on every server, whether or not it runs with `--allow-sequences`. That flag only
decides whether the server also accepts *raw* sequences, which an older client or a browser
without WebAssembly SIMD needs. A server without it never sees a sequence, which is what you
want when the assembly is confidential.

`sylphd assembly` asks `GET /info` what the server accepts and sketches locally whenever it takes
sketches. Servers from before that feature get the raw upload instead (`--allow-sequences` needed).
`--local-sketch` insists on the local sketch (an error if the server cannot take it) and
`--no-local-sketch` forces the raw upload (limited by `--max-sequence-mb`, 20 MB by default).
Both ways give the same hits.

```bash
sylphd assembly assembly.fasta
sylphd assembly assembly.fasta --top 20 --min-ani 85 --json
```

The input is one assembly in FASTA or FASTQ, plain or gzip. All records of the file form one
sample, so a multi-contig draft is fine. Only the bases `A C G T N` (in either case) are accepted; anything else is a 400.

## Output

A table on stdout with the closest genomes by decreasing ANI, and one summary line on stderr:

```text
rank  ani    relation      shared_rank  accession        gtdb_species      ncbi_organism_name     ncbi_taxid
1     99.53  same species  species      GCA_000005845.2  Escherichia coli  Escherichia coli K-12  562
2     90.12  same genus    genus        GCF_000...       Escherichia sp...  ...                    ...
```

```text
assigned to species Escherichia coli (99.53 % ANI)
no species-level match (< 95 % ANI); closest relative ... (...)
```

| Option | Default | Meaning |
|---|---|---|
| `--top N` | `10` | number of hits, 1-50 |
| `--min-ani ANI` | `80` | minimum ANI in percent, 70-100 |
| `--name NAME` | file base name | query name |
| `--local-sketch` / `--no-local-sketch` | automatic | sketch on this machine and upload only the sketch (the default when the server accepts sketches), or upload the raw sequence |
| `--json` | off | print the raw response instead of the table |
| `-o FILE` | stdout | write the table to a file |

The columns `gtdb_species`, `ncbi_organism_name` and `ncbi_taxid` are empty when the server has no
metadata database. The JSON response carries every metadata column; see the
[`POST /assembly` reference](../reference/http-api.md#post-assembly).

## Sketching in the browser { #sketching-in-the-browser }

In the [web app](https://abrinca.github.io/sylphd/#assembly) the sketching happens in your browser
(WebAssembly, the same code as for [reads](profiling-in-browser.md)). Paste FASTA or drop a file of
any size (gzip is fine); only the sketch is sent, and the result shows a *sketched locally* badge.
The 20 MB limit applies only to the raw upload that older servers and browsers without WebAssembly
SIMD fall back to. See [Running a public server](public-server.md#sketches-only-or-raw-sequences-too-allow-sequences).

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
  reaches `95` %, the query is **assigned to that hit's GTDB species**.
- Otherwise there is no species-level match. The response then has no assignment, and the
  `closest` relative (the best hit) is reported instead.
- Below **85 %** the k-mer ANI is imprecise. `thresholds.imprecise_below` (`85`) is only a hint for
  readers and never changes an assignment.

Without a metadata database the server cannot name the species. The assignment still applies at
`ani >= 95`, but its taxon is `null`.

## Hit relations

Every hit gets a `relation`, derived from the **GTDB lineage**, not from the ANI. The server
compares the lineage of each hit (`d__;p__;c__;o__;f__;g__;s__`) with a reference lineage and takes
the deepest rank they share. A rank only counts when all shallower ranks agree.

- With an assignment, the reference is the **assigned species**, and hits are labelled
  `same species`, `same genus`, `same family`, ... up to `same domain` (or `different domain`).
- Without one, the reference is the **closest relative** (the best hit). That hit is labelled
  `closest relative`, and others `same genus as closest relative`, and so on.

A hit in the same GTDB species as the assignment is `same species` whatever its ANI. Separately,
`ani_species_level` is true for every hit with `ani >= 95`. A hit that has it but a different GTDB
species name (a species boundary case) gets the lineage relation, for example `same genus`, plus
`ani_species_level: true`. The full table of relations is in the
[API reference](../reference/http-api.md#hit-relations).

Without metadata (ANI only), a hit is `same species` for `ani >= 95` and `related` otherwise.

## Limits

The upload limit is `--max-sequence-mb` (default 20, before decompression), and gzip bodies may
decompress to at most five times that. The server logs sizes, timings and outcomes, never the
sequence. See [Running a public server](public-server.md#limits).
