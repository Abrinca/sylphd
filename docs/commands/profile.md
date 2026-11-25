# profile

```text
sylphd profile [inputs] [options] [--estimate-read-counts] [--taxonomy LIST]
               [--tax-source NAME] [--taxprof PREFIX]
```

This command is the native `sylphd-client`: the `sylphd` command runs the server and admin commands
in Python and forwards this one, argv unchanged (same exit codes, same stdin and stdout), and
`sylphd-client` is the same program for machines without Python. The client renders the output, so
it depends on the client version rather than the server's. `--json` saves the server's document
unchanged; [`render`](render.md) turns a saved document into the table later, without a server.

Taxonomic profile of one or more samples: the equivalent of `sylph profile`, with
`Taxonomic_abundance`, `Sequence_abundance` and `kmers_reassigned` columns, joined with the
metadata. With `--estimate-read-counts` the `Sequence_abundance` column holds the estimated number
of reads per genome instead (sylph's experimental option). The client sketches locally and sends
the sketches to the server. For the ANI hits without abundances see [`query`](query.md). The guide
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
| `--fields COLUMNS` | `all` | metadata columns to add, comma-separated, or `all` / `none`; `all` is every column the server exposes, which on a server with sylph-tax sources **appends the four `tax_*` columns** after the existing ones, and on one built with `--ncbi-taxonomy` also the eight `ncbi_nearest_*` columns (an explicit list is unchanged) |
| `-v, --verbose` | off | progress messages on stderr |

## Thresholds

| Option | Default | Meaning |
|---|---|---|
| `--minimum-ani ANI` | `95` | minimum adjusted ANI, 0-100. sylph warns that values below 95 give inaccurate profiles; for relatives at 90–94 % ANI use [`query`](query.md) (default 90) |
| `--seq-id PCT` | none | read sequence identity in percent, only used with `--estimate-unknown` |
| `--estimate-unknown` | off | estimate true coverage and scale sequence abundance of unknowns (`Eff_cov` becomes `True_cov`) |
| `--estimate-read-counts` | off | put the estimated number of reads per genome in `Sequence_abundance` instead of a relative abundance (sylph's experimental option); implies `--estimate-unknown`, so `Eff_cov` becomes `True_cov` |
| `--min-number-kmers N` | `10` | discard genomes with fewer sampled k-mers |
| `--min-contain N` | `7` | minimum contained k-mers for a hit |
| `--screen-ani ANI` | `85` | two-stage screen threshold, 0-100 |
| `--no-adjust` | off | report the naive containment ANI instead of the adjusted ANI; use for assemblies or genomes as samples |

## Taxonomic profiles

| Option | Default | Meaning |
|---|---|---|
| `--taxonomy LIST` | `gtdb,ncbi` (+ `tax`) | taxonomies to aggregate, comma-separated from `gtdb`, `ncbi`, `tax` and `tax:SOURCE`; `tax` is added to the default when the server's metadata file has sylph-tax sources |
| `--tax-source NAME` | all sources | with `--taxonomy tax`: resolve against these taxonomy sources only (repeatable, in `setup` order) |
| `--taxprof PREFIX` | none | write one sylph-tax-style `.sylphmpa` per sample and taxonomy as `PREFIX<sample>.<taxonomy>.sylphmpa` |

These need a server with `--meta`; `tax` additionally needs a metadata file with sylph-tax
sources. The aggregation happens on the server (`/profile` `taxprof`): the command writes the
returned `.sylphmpa` text directly. To merge `.sylphmpa` files (or saved `/profile` JSON
documents) into a clade × sample matrix, use [`sylphd taxmatrix`](taxmatrix.md). `tax` needs a metadata file
with sylph-tax sources, where the **default `--taxprof` writes three files per sample** (`.gtdb`, `.ncbi`, `.tax`)
instead of two — pass `--taxonomy gtdb,ncbi` for the old set. `tax` follows `sylph-tax taxprof` over all sources rule for rule (opaque lineages, `UNKNOWN`,
`NO_TAXONOMY`, level-based ordering); `tax:SOURCE` (e.g. `tax:UHGV_default`) follows
`sylph-tax taxprof -t SOURCE`, one source alone, written as `PREFIX<sample>.tax.SOURCE.sylphmpa`.
Both are equivalent to sylph-tax v1.9.2's on the same sylph run: the same clades, values equal within the TSV's rounding (sylphd works at full precision, sylph-tax sums sylph's rounded TSV), and clades with equal values may be ordered differently. Check with [`sylphd diff`](diff.md).

`gtdb` and `ncbi` keep sylphd's own normalisation. See
[Taxonomic profiles](../guides/taxonomic-profiles.md).

## Server

`--socket`, `--tcp`, `--url`, `--timeout` and `--retries`: see [Server address](address.md).
