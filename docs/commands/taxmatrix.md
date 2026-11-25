# taxmatrix

```text
sylphd taxmatrix [--value {relative,sequence,ani,coverage}] [--taxonomy NAME]
                 [-o FILE] FILE ...
```

Merges taxonomic profiles into one wide clade × sample TSV table — the offline equivalent of
`sylph-tax merge`. It needs no server, no `pysylph` and no extra: it works in the base install, and
can merge samples across separate profiling runs.

```bash
sylphd taxmatrix results/*.gtdb.sylphmpa -o gtdb.tsv
sylphd taxmatrix --value sequence run1/*.tax.sylphmpa run2/*.tax.sylphmpa -o seq.tsv
sylphd taxmatrix --value ani results/*.gtdb.sylphmpa
sylphd taxmatrix --taxonomy gtdb run1.json run2.json -o gtdb.tsv
```

## Inputs

The format of each file is read from its content, so `.sylphmpa` files and saved `/profile` JSON
documents can be passed freely (or mixed in one invocation):

- **`.sylphmpa` files** written by [`sylphd profile --taxprof`](profile.md) or `sylph-tax taxprof`
  (first line `#SampleID`, five standard columns; an optional sixth virus-host column from
  `sylph-tax taxprof -a` is ignored).
- **Saved `/profile` JSON documents** containing a `taxprof` object (see the
  [HTTP API](../reference/http-api.md#taxprof)). Every sample in the document is added in `samples`
  order. If a document holds only one taxonomy, it is selected automatically; if it holds more than
  one (for example `gtdb`, `ncbi` and `tax`), select one with `--taxonomy` (otherwise the command
  fails rather than guessing).

Duplicate sample names across inputs are rejected.

## Options

| Option | Default | Meaning |
|---|---|---|
| `FILE ...` | required | `.sylphmpa` files or saved `/profile` JSON documents |
| `--value {relative,sequence,ani,coverage}` | `relative` | value to tabulate, matching `sylph-tax merge --column` |
| `--taxonomy NAME` | none | taxonomy to extract from `/profile` JSON documents (`gtdb`, `ncbi`, `tax`, `tax:SOURCE`, …); required when a document holds more than one |
| `-o, --output FILE` | stdout | write the matrix TSV to `FILE` (`-` for stdout) |

## Table layout

The output has a `clade_name` column followed by one column per sample, in input order. Rows are all
clades seen in any sample (outer join), with `0.0` where a clade is absent from a sample. With two
or more samples the clades are **sorted lexicographically**, matching `sylph-tax merge`; with a
single sample they keep the profile's order.

| `--value` | `.sylphmpa` column | `sylph-tax merge --column` |
|---|---|---|
| `relative` | `relative_abundance` | `relative_abundance` |
| `sequence` | `sequence_abundance` | `sequence_abundance` |
| `ani` | `ANI (if strain-level)` | `ANI` |
| `coverage` | `Coverage (if strain-level)` | `Coverage` |

Only strain (`t__`) rows carry an ANI or coverage value, so `--value ani` and `--value coverage`
drop every clade that has no value in any sample (`NA` everywhere), and write `0.0` for a strain
missing from a sample — identical to `sylph-tax merge --column ANI|Coverage`. See
[Taxonomic profiles](../guides/taxonomic-profiles.md) for the profile format and lineage rules.

## Exit status

**0** on success; **2** on a usage error, unreadable or malformed input, an ambiguous or missing
taxonomy in a JSON document, duplicate sample names, or an unwritable output path.
