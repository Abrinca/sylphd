# Taxonomic profiles

`sylphd profile` writes per-rank taxonomic profiles that the **server** aggregates. The server is
the only place the logic lives (`sylphd/taxonomy.py`): it folds its ANI hits into the clade tables
and returns both the structured clades and the rendered `.sylphmpa` text in the `/profile`
response (`params.taxprof`). The CLI writes that text and uses the clades for the matrix; it does
not re-aggregate rows. The result mirrors `sylph-tax taxprof` and `sylph-tax merge`, but needs no
extra tools and no server support beyond the metadata database.

!!! note
    The server must run with `--meta`: the profiles come from the `gtdb_taxonomy` and
    `ncbi_taxonomy` metadata columns, and `tax` from the `tax_*` columns that a metadata file with
    sylph-tax sources exposes. The server adds the columns of each requested taxonomy even when
    `--fields` hides them from the main TSV. Only `profile` has these flags.

```bash
sylphd profile -r sample.fq.gz \
    --taxonomy gtdb,ncbi,tax \
    --taxprof results/ \
    --taxmatrix results/
```

| Option | Default | Meaning |
|---|---|---|
| `--taxonomy LIST` | `gtdb,ncbi` (+ `tax`) | `gtdb`, `ncbi`, `tax`, comma-separated |
| `--taxprof PREFIX` | none | one `.sylphmpa` file per sample and taxonomy |
| `--taxmatrix PREFIX` | none | one clade × sample table per taxonomy |
| `--taxmatrix-value relative\|sequence\|ani\|coverage` | `relative` | value used by `--taxmatrix` |
| `--tax-source NAME` | all sources | with `--taxonomy tax`: restrict the merged profile to these sources (repeatable) |

Both flags combine with the normal TSV or JSON output (`-o`, `--format`). The prefix is used
verbatim, except that a prefix naming an existing directory (`results`) works like `results/`. To
write into a directory, end the prefix with `/` or give the directory's name. The directory is
**not** created.

When the server's metadata file carries [sylph-tax taxonomy sources](../reference/metadata.md#taxonomy-sources),
the default of `--taxonomy` gains `tax`, so a `--taxprof` run then writes `<sample>.gtdb.sylphmpa`,
`<sample>.ncbi.sylphmpa` **and** `<sample>.tax.sylphmpa`. Pass `--taxonomy gtdb,ncbi` for the old
set. The server must expose the four `tax_*` columns for `tax`; `--tax-source` then restricts which
sources the profile is resolved against.

## `--taxprof`: one file per sample and taxonomy

Files are named `PREFIX<sample>.<gtdb|ncbi>.sylphmpa` and follow the format of `sylph-tax taxprof`:
a `#SampleID ... Taxonomies_used:['gtdb']` comment line, then the columns `clade_name`,
`relative_abundance`, `sequence_abundance`, `ANI (if strain-level)` and `Coverage (if strain-level)`.

```text
#SampleID	sample-a	Taxonomies_used:['gtdb']
clade_name	relative_abundance	sequence_abundance	ANI (if strain-level)	Coverage (if strain-level)
d__Bacteria	100.0	100.0	NA	NA
d__Bacteria|p__Bacillota	60.0	42.5	NA	NA
d__Bacteria|p__Bacillota|c__Clostridia	60.0	42.5	NA	NA
...
d__Bacteria|p__Bacillota|c__Clostridia|...|s__Clostridium difficile	60.0	42.5	NA	NA
d__Bacteria|p__Bacillota|c__Clostridia|...|s__Clostridium difficile|t__GCA_000000001.1	60.0	42.5	99.79	1.234
```

Each row is a **cumulative lineage**: a clade's abundances are the sums of all genomes below it, so
`d__Bacteria` is the sum of every hit of that sample. Rows are ordered rank by rank (`t__` rows
last), and within a rank by decreasing relative abundance (ties keep the order the server
returned). Only `t__` rows
carry `Adjusted_ANI` and `Eff_cov` (or `True_cov`); the other rows have `NA` there.
`sylph-tax merge` can read these files directly.

## `--taxmatrix`: one table per taxonomy

`PREFIX<gtdb|ncbi>.tsv` has a `clade_name` column followed by one column per sample, in request
order. Rows are all clades seen in any sample, and a clade missing from a sample is `0.0`. With
two or more samples the rows are **sorted lexicographically** (what `sylph-tax merge` produces
when pandas joins the profiles); with a single sample they keep the `.sylphmpa` order (rank by
rank, then by decreasing abundance).

`--taxmatrix-value` selects the column, matching `sylph-tax merge --column`:

| Value | Column | `sylph-tax merge --column` |
|---|---|---|
| `relative` | `Taxonomic_abundance` | `relative_abundance` |
| `sequence` | `Sequence_abundance` | `sequence_abundance` |
| `ani` | `ANI (if strain-level)` | `ANI` |
| `coverage` | `Coverage (if strain-level)` | `Coverage` |

Only `t__` rows carry an ANI and a coverage, so the `ani` and `coverage` matrices contain the
strain-level rows alone: a clade without a value in *any* sample is dropped (pandas'
`dropna(how="all")`), and a strain found in one sample but not another gets `0`, not `NA`. These
files are what `sylph-tax merge --column ANI|Coverage` writes, byte for byte.

## Lineage rules

- GTDB strings name every rank (`d__..;p__..;c__..;o__..;f__..;g__..;s__..`). NCBI strings may
  leave ranks empty.
- **Trailing empty ranks are dropped.** The lineage stops at the last named rank and the `t__`
  genome hangs directly below it (an empty `s__` puts the strain under the genus).
- **An empty rank before a named one is filled** with `{rank}__unclassified {nearest named ancestor}`,
  for example `o__unclassified Clostridia`, so every genome of a sample uses the same path for that
  rank. With no named ancestor the suffix is `root`.
- **`t__`** is the genome accession (`GCA_...`/`GCF_...`, as in the `accession` column, like
  sylph-tax's taxonomy files) or, when no accession is known, the `Genome_file` base name.
- **Rows without any usable taxonomy** (no metadata, no accession match, or a taxonomy value
  such as `none` or empty) go to a single `unclassified` clade, with the genome as `t__` below it. sylph-tax's
  default for such rows is `NO_TAXONOMY;t__<file>:<contig>`.
- **Abundances are never renormalised.** They are summed exactly as the server reports them, so
  with `--estimate-unknown` the root clade may stay below 100.

## The merged `tax` profile

`--taxonomy tax` is **exactly what `sylph-tax taxprof` writes** for the same hits and taxonomy
files, merged over every sylph-tax source of the server's metadata file. It is the profile to use
when the databases are not all GTDB: UHGV viruses, fungal RefSeq, MAGs and GlobDB have their own
lineages, and `gtdb`/`ncbi` cannot express them (their ranks are dropped).

```bash
sylphd profile -r sample.fq.gz --taxonomy tax --taxprof results/
# results/sample.tax.sylphmpa, and with --taxmatrix results/ → results/tax.tsv
```

The server resolves every hit to a source, a key and a lineage (`tax_lineage`, `tax_key`,
`tax_source` and `tax_host` columns) and aggregates the profile; the CLI writes it. Both sides
follow sylph-tax v1.9.2 rule for rule, and `tests/test_sylphtax_parity.py` compares the output
byte for byte with the real `sylph-tax taxprof` and `sylph-tax merge` (a committed golden always,
the live tool when it is installed).

Differences from `gtdb`/`ncbi`, all of them sylph-tax's behaviour:

- **Lineages are opaque.** They are split on `;` exactly as stored, and the prefixes are not
  interpreted: UHGV's `vFAM-00013;vSUBFAM-00074;…;vOTU-052719`, ICTV's `root;Monodnaviria;…` and
  IMG/VR's `r__;k__;p__;…` all work. Nothing is dropped or renamed.
- **An empty token becomes `UNKNOWN`.** A token such as `c__` is *not* empty, so the ranks after
  it are not filled in: no `unclassified X` here.
- **The last level is `t__<key>`**, the taxon key sylph-tax matched (the accession, or a file name
  for GlobDB-style sources) — not necessarily the accession column.
- **A hit without any taxonomy becomes `NO_TAXONOMY;t__<genome file>:<contig>`**, with the file
  name and contig as sylph-tax's probes report them.
- **Rows are ordered by number of levels and then by decreasing relative abundance** (stable). A
  strain below a short lineage therefore sorts *before* a deeper clade; `t__` is not always last.
- The `#SampleID` comment is
  `#SampleID<TAB><sample><TAB>Taxonomies_used:['GTDB_r232', 'UHGV_default']` — the names of the
  sources, in the order of `sylphd setup` (the order in which sylph-tax would have been given the
  files). `--tax-source NAME` (repeatable) restricts the profile to some of them, which also
  restricts the resolution: a key that exists only in an excluded source is a miss there.

**When several sources contain the same key, the source imported later wins** — the larger `ord`,
exactly as if its file had been passed later to `sylph-tax -t`. This matters when two files cover
the same genomes: a metadata file that imported both `UHGV_default` and `UHGV_ictv` resolves every
UHGV key through `UHGV_ictv`, because it was imported second. `--tax-source NAME` is how to choose
per request (e.g. `--tax-source UHGV_default`), and `sylphd info` lists the sources in that
order.

Only the five columns of `sylph-tax taxprof` are written; the virus-host column (`sylph-tax
taxprof -a`) is not emitted yet, although `tax_host` is already resolved and on the wire.

Abundances are not renormalised either, so the root row can be below 100 (with
`--estimate-unknown`).
