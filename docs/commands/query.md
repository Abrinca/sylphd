# query

```text
sylphd query [inputs] [options]
```

ANI hits of one or more samples: the equivalent of `sylph query`. The client sketches locally and
sends the sketches to the server, which returns every database genome the sample contains, with
ANI and coverage, joined with the metadata.

`query` takes the same inputs, output options, thresholds and server options as
[`profile`](profile.md), with two differences:

- the default `--minimum-ani` is **90** (`profile`: 95);
- there are no abundance columns and no `--taxonomy`, `--taxprof`, `--taxmatrix`,
  `--taxmatrix-value` or `--estimate-read-counts` options.

The output columns are:

```text
Sample_file Genome_file Adjusted_ANI Eff_cov ANI_5-95_percentile
Eff_lambda Lambda_5-95_percentile Median_cov Mean_cov_geq1
Containment_ind Naive_ANI Contig_name  accession <metadata...>
```

`<metadata...>` is the server's metadata columns; with the default `--fields all` a server whose
metadata file has sylph-tax taxonomy sources appends `tax_source`, `tax_key`, `tax_lineage` and
`tax_host` after them (the existing columns keep their order, and an explicit `--fields` list is
unchanged). See [Profiling reads](../guides/profiling-reads.md) for what they mean.

```bash
sylphd query -1 sample_R1.fq.gz -2 sample_R2.fq.gz
sylphd query --no-adjust genome.fna.gz        # a genome as a sample
```
