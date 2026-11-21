# diff

```text
sylphd diff A B [--json] [--tolerance FACTOR] [--max-differences N]
```

Checks that two outputs of the same kind are **equivalent**: the same genomes or clades, every
value equal within the rounding of what was printed, and tied rows in any order. `A` is the
reference (stock `sylph` or `sylph-tax`), `B` is sylphd's. It needs no server, no pysylph and no
extra: it works in the base install.

sylphd computes at full precision. `sylph` prints rounded values, and `sylph-tax` then sums those
rounded values, so the digits differ in the last places, and rows with equal values come in
another order (stock `sylph` itself orders ties differently from run to run when it uses several
threads). Neither is a difference in the result, and byte comparison cannot tell them from one.

```bash
sylphd diff sylph/profile.tsv sylphd/profile.tsv          # per-genome TSVs
sylphd diff sylph-tax/soil.sylphmpa sylphd/soil.tax.GlobDB_r232.sylphmpa
sylphd diff sylph-tax/ sylphd/                              # directories of .sylphmpa files
```

The kind of file is read from its content: a TSV starts with `Sample_file` (`sylph profile` or
`sylph query`; sylphd's extra `accession` and metadata columns are ignored), a `.sylphmpa` starts
with `#SampleID`. `A` and `B` must be of the same kind. `sylph-tax` must have been given the
taxonomy by its catalogue name (`--taxonomy-dir DIR -t GlobDB_r232`), not a file path, so that
the `Taxonomies_used` line is the same.

## What equivalent means

| | |
|---|---|
| **Samples** | matched by name, normalised: the part after the last `/`, then `.gz`, `.sylsp`, `.paired`, `.fastq`, `.fq`, `.fasta`, `.fna` and `.fa` are removed while one ends the name (`bioreactor.paired.sylsp` is `bioreactor`). When each side has exactly one sample they are paired whatever the names. |
| **TSV rows** | the same set of genomes (`Genome_file`, plus `Contig_name` when a genome file repeats) |
| **TSV numbers** | within half a unit of the printed precision, as `sylph` rounds: `0.00005` for the two abundances (4 decimals), `0.005` for `Adjusted_ANI` and `Naive_ANI` (2), `0.0005` for the coverages, `Eff_lambda` and `Mean_cov_geq1` (3), `0.5` for `Median_cov`; `kmers_reassigned` is exact. `inf` and `NaN` must match. |
| **TSV text** | `Eff_lambda` (`HIGH`/`LOW`), the `NA-NA` intervals, `Containment_ind` and `Contig_name` must be identical |
| **sylphmpa header** | the `#SampleID` line (`Taxonomies_used` included) and the column header must be identical |
| **sylphmpa clades** | the same set of clade names |
| **sylphmpa numbers** | relative and sequence abundance within *n* × 0.00005, *n* being the number of strain (`t__`) rows at or below the clade (each strain was rounded once before `sylph-tax` summed it); ANI within 0.005, coverage within 0.0005; `NA` cells identical |
| **Order** | each file must be sorted as `sylph` sorts it: a profile TSV by `Taxonomic_abundance` descending, a query TSV by `Adjusted_ANI` descending, a `.sylphmpa` by level and, within a level, by relative abundance descending. Rows may change places between `A` and `B` only where their values tie. |

Every tolerance also carries an absolute `1e-9` for float noise.

## Output and exit status

One line per sample: `equivalent`, or the first differences with the genome or clade, the column,
both values and the tolerance. The exit status is **0** when every sample is equivalent, **1**
when one is not, and **2** for a usage error or a file that cannot be compared (unreadable,
unknown format, a TSV against a `.sylphmpa`).

| Option | Meaning |
|---|---|
| `--json` | print the report as JSON (`equivalent`, and per sample the differences) |
| `--tolerance FACTOR` | multiply every tolerance by `FACTOR` [1] |
| `--max-differences N` | differences shown per sample [10] |

A sketch without read statistics (made from contigs) under `--estimate-unknown` gives `NaN` and
`inf` in `sylph-tax`'s output; sylphd's `tax` profile writes `0.0` and `NA` there, so `diff`
reports it.
