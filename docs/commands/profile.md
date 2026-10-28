# profile

```text
sylphd profile [inputs] [options] [--taxonomy LIST] [--taxprof PREFIX]
               [--taxmatrix PREFIX] [--taxmatrix-value {relative,sequence}]
```

Taxonomic profile of one or more samples: the equivalent of `sylph profile`, with
`Taxonomic_abundance`, `Sequence_abundance` and `kmers_reassigned` columns, joined with the
metadata. The client sketches locally and sends the sketches to the server. For the ANI hits
without abundances see [`query`](query.md). The guide
[Profiling reads](../guides/profiling-reads.md) explains inputs, names and output columns.

## Inputs

| Option | Meaning |
|---|---|
| `FILE ...` | one sample per FASTA/FASTQ file, or a `.sylsp` sketch |
| `-r, --reads FILE ...` | single-end reads (or assemblies); each file is one sample |
| `-1, --first FILE ...` | first mates of paired-end files |
| `-2, --second FILE ...` | second mates (same count as `-1`) |
| `--interleaved FILE ...` | interleaved paired-end FASTQ files |
| `--sample NAME:SPEC` | explicit sample; `SPEC` is a comma-separated list of files, a pair written `R1+R2` (e.g. `s1:a.fq,b.fq` or `s1:a1.fq+a2.fq,b1.fq+b2.fq`). Repeatable |
| `--sample-name NAME` | sample name instead of the file name (single sample only) |
| `-t, --threads N` | threads used to sketch samples locally (default `4`) |

## Output

| Option | Default | Meaning |
|---|---|---|
| `-o, --output FILE` | stdout | write the table here |
| `--format {tsv,json}` | `tsv` | output format |
| `--fields COLUMNS` | `all` | metadata columns to add, comma-separated, or `all` / `none` |
| `-v, --verbose` | off | progress messages on stderr |

## Thresholds

| Option | Default | Meaning |
|---|---|---|
| `--minimum-ani ANI` | `95` | minimum adjusted ANI, 0-100 |
| `--seq-id PCT` | none | read sequence identity in percent, only used with `--estimate-unknown` |
| `--estimate-unknown` | off | estimate true coverage and scale sequence abundance of unknowns (`Eff_cov` becomes `True_cov`) |
| `--min-number-kmers N` | `10` | discard genomes with fewer sampled k-mers |
| `--min-contain N` | `7` | minimum contained k-mers for a hit |
| `--screen-ani ANI` | `85` | two-stage screen threshold, 0-100 |
| `--no-adjust` | off | report the naive containment ANI instead of the adjusted ANI; use for assemblies or genomes as samples |

## Taxonomic profiles

| Option | Default | Meaning |
|---|---|---|
| `--taxonomy LIST` | `gtdb,ncbi` | taxonomies to aggregate, comma-separated from `gtdb`, `ncbi` |
| `--taxprof PREFIX` | none | write one sylph-tax-style `.sylphmpa` per sample and taxonomy as `PREFIX<sample>.<taxonomy>.sylphmpa` |
| `--taxmatrix PREFIX` | none | write one clade × sample matrix per taxonomy as `PREFIX<taxonomy>.tsv` |
| `--taxmatrix-value {relative,sequence}` | `relative` | abundance used in `--taxmatrix`: `relative` (`Taxonomic_abundance`) or `sequence` (`Sequence_abundance`) |

These need a server with `--meta`. See [Taxonomic profiles](../guides/taxonomic-profiles.md).

## Server

`--socket`, `--tcp`, `--url` and `--timeout`: see [Server address](address.md).
