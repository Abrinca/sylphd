# Taxonomic profiles

`sylphd profile` can fold its ANI hits into per-rank taxonomic profiles on the client. This mirrors
`sylph-tax taxprof` and `sylph-tax merge`, but needs no extra tools and no server support beyond
the metadata database.

!!! note
    The server must run with `--meta`: the profiles are built from the `gtdb_taxonomy` and
    `ncbi_taxonomy` metadata columns. The client always requests those two columns, even when
    `--fields` hides them from the main TSV. Only `profile` has these flags.

```bash
sylphd profile -r sample.fq.gz \
    --taxonomy gtdb,ncbi \
    --taxprof results/ \
    --taxmatrix results/
```

| Option | Default | Meaning |
|---|---|---|
| `--taxonomy LIST` | `gtdb,ncbi` | `gtdb`, `ncbi`, or both, comma-separated |
| `--taxprof PREFIX` | none | one `.sylphmpa` file per sample and taxonomy |
| `--taxmatrix PREFIX` | none | one clade × sample table per taxonomy |
| `--taxmatrix-value relative\|sequence` | `relative` | abundance used by `--taxmatrix` |

Both flags combine with the normal TSV or JSON output (`-o`, `--format`). The prefix is used
verbatim: end it with `/` (or give an existing directory) to write into a directory. The directory
is **not** created.

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
`d__Bacteria` is the sum of every hit of that sample. Rows are ordered rank by rank, and within a
rank by decreasing relative abundance (ties keep the order the server returned). Only `t__` rows
carry `Adjusted_ANI` and `Eff_cov` (or `True_cov`); the other rows have `NA` there.
`sylph-tax merge` can read these files directly.

## `--taxmatrix`: one table per taxonomy

`PREFIX<gtdb|ncbi>.tsv` has a `clade_name` column followed by one column per sample. The columns
are in request order, and rows are all clades seen in any sample, in first-seen order (the order
`sylph-tax merge` produces when joining its profiles in that order). A clade missing from a sample
is `0.0`.

`--taxmatrix-value relative` uses `Taxonomic_abundance`, `sequence` uses `Sequence_abundance`.

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
