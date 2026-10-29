# Profiling reads

`sylphd query` and `sylphd profile` mirror `sylph query` and `sylph profile`. The client reads the
database's `c` and `k` from the server, sketches your samples locally, sends the sketches, and
writes the returned rows as a TSV.

- **`query`** reports every database genome that the sample contains, with ANI and coverage.
- **`profile`** additionally resolves genomes that compete for the same k-mers and reports
  taxonomic and sequence abundances. It is also the command that builds
  [taxonomic profiles](taxonomic-profiles.md).

Both take the same inputs and options; see the [`profile`](../commands/profile.md) and
[`query`](../commands/query.md) references for every flag.

## Inputs

Each file or group of files is one **sample**. A sample is the unit the server profiles and the
name that ends up in `Sample_file`.

| Syntax | Meaning |
|---|---|
| `sylphd query reads.fq.gz` | one single-end sample (positional) |
| `sylphd query -r a.fq b.fq` | two single-end samples |
| `sylphd query -1 a_R1.fq -2 a_R2.fq` | one paired sample |
| `sylphd query -1 a_R1.fq b_R1.fq -2 a_R2.fq b_R2.fq` | two paired samples |
| `sylphd query --interleaved a.fq` | one interleaved paired sample |
| `sylphd query --sample "s1:a.fq,b.fq"` | one sample from two files |
| `sylphd query --sample "s1:a1.fq+a2.fq,b1.fq+b2.fq"` | one sample from two pairs |
| `sylphd query sample.sylsp` | an existing sketch, sent as-is |

FASTA and FASTQ are read with [dnaio](https://dnaio.readthedocs.io/), so gzip, bzip2, xz and zstd
files work. `-r` also takes assemblies and long reads. Options can be combined; samples are
processed in the order positional files, `-r`, `-1/-2`, `--interleaved`, `--sample`.

### Sample names

A sample name defaults to the file's base name without compression and read suffixes
(`.gz`, `.fastq`, `.fq`, `.fasta`, `.fa`, `.fna`, `.sylsp`, ...). For paired samples `_R1`, `_R2`,
`.R1`, `.R2`, `_1`, `_2` and Illumina's `_R1_001` are removed too, so `sample-a_R1.fastq.gz`
becomes `sample-a`.

Two samples with the same name are an error. Use `--sample-name NAME` to rename a single sample, or
`--sample NAME:SPEC` to name a sample explicitly.

### `--sample NAME:SPEC`

`SPEC` is a comma-separated list of inputs. A pair is written `R1+R2`:

```bash
sylphd profile --sample "gut1:lane1.fq.gz,lane2.fq.gz" \
               --sample "gut2:l1_R1.fq.gz+l1_R2.fq.gz,l2_R1.fq.gz+l2_R2.fq.gz"
```

All inputs of a sample are combined into **one sketch**. They must be all single-end or all paired;
mixing the two is an error.

### Existing sketches (`.sylsp`)

A file ending in `.sylsp` is sent without sketching. The server checks that the sketch matches the
database: `k` must be equal, and `c` must not be sparser than the database (`c <= db.c`). A sketch
made with another `k` is rejected, and a sketch with a larger `c` has to be re-sketched from the
reads.

### Threads

`-t/--threads` sets how many samples are sketched in parallel on the client (default `4`). Profiling
on the server is limited separately by [`--max-jobs`](../commands/serve.md).

## Thresholds

The defaults are sylph's.

| Option | Default | Meaning |
|---|---|---|
| `--minimum-ani` | `90` (`query`), `95` (`profile`) | minimum adjusted ANI, 0-100 |
| `--seq-id` | none | read sequence identity in percent, only used with `--estimate-unknown` |
| `--estimate-unknown` | off | estimate true coverage and scale sequence abundance of unknowns |
| `--min-number-kmers` | `10` | discard genomes with fewer sampled k-mers |
| `--min-contain` | `7` | minimum contained k-mers for a hit |
| `--screen-ani` | `85` | two-stage screen threshold, 0-100 |
| `--no-adjust` | off | naive containment ANI instead of the adjusted ANI; for assemblies, see [Identifying an assembly](assembly.md#why-the-ani-is-not-adjusted) |

## Output

The default output is a TSV on stdout, or `-o FILE`. `--format json` prints the server payload (a
list with one object per sample, see the [HTTP API](../reference/http-api.md#post-query-and-post-profile)).
The TSV mirrors sylph's columns, followed by `accession` and the metadata columns:

```text
query:    Sample_file Genome_file Adjusted_ANI Eff_cov ANI_5-95_percentile
          Eff_lambda Lambda_5-95_percentile Median_cov Mean_cov_geq1
          Containment_ind Naive_ANI Contig_name  accession <metadata...>
profile:  Sample_file Genome_file Taxonomic_abundance Sequence_abundance
          Adjusted_ANI Eff_cov ANI_5-95_percentile Eff_lambda
          Lambda_5-95_percentile Median_cov Mean_cov_geq1 Containment_ind
          Naive_ANI kmers_reassigned Contig_name  accession <metadata...>
```

| Column | Meaning |
|---|---|
| `Sample_file` | sample name |
| `Genome_file` | database genome file name, as stored in the database |
| `Adjusted_ANI` | coverage-adjusted ANI in percent (the naive ANI with `--no-adjust`) |
| `Eff_cov` | effective coverage; labelled `True_cov` with `--estimate-unknown`, as in sylph |
| `ANI_5-95_percentile` | confidence interval of the adjusted ANI, `low-high` |
| `Eff_lambda`, `Lambda_5-95_percentile` | sylph's effective lambda (its coverage model parameter) and its interval |
| `Median_cov`, `Mean_cov_geq1` | median coverage and mean coverage of k-mers seen at least once |
| `Containment_ind` | contained k-mers over total genome k-mers, `7096/10826` |
| `Naive_ANI` | containment ANI without the coverage adjustment |
| `Taxonomic_abundance`, `Sequence_abundance` | `profile` only: relative taxonomic and sequence abundance |
| `kmers_reassigned` | `profile` only: number of k-mers reassigned during profiling, as in sylph |
| `Contig_name` | contig name and description recorded for the genome in the database |
| `accession` | genome accession (`GCA_...`/`GCF_...`) derived from `Genome_file`; empty when there is none |

`--fields` selects the metadata columns that follow (comma-separated names, `all` by default,
`none` to omit them); see the [metadata database](../reference/metadata.md#columns) for the names.
`--fields` needs a server started with `--meta`. Unknown column names are rejected.

With `--no-adjust`, `Adjusted_ANI` equals `Naive_ANI` and `ANI_5-95_percentile` is left empty,
because that interval still describes the (disabled) adjustment.

## Using a remote server

Add `--url`, `--tcp` or `--socket` to any client command, or set `$SYLPHD_URL` or
`$SYLPHD_SOCKET`; see [Server address](../commands/address.md). Sketching stays local in every
case.

```bash
sylphd query --url https://api.example.org/sylphd reads.fq.gz
```

A public instance may rate-limit `/query` and `/profile`. The terms of the Abrinca web service do
not cover programmatic access; see [Terms](../project/terms.md) and
[Running a public server](public-server.md).
