# assembly

```text
sylphd assembly FILE [--top N] [--min-ani ANI] [--name NAME] [--json] [-o FILE]
                [--socket PATH | --tcp HOST:PORT | --url URL] [--timeout SECONDS]
```

Sends one assembly (FASTA or FASTQ, plain or gzip) to [`POST /assembly`](../reference/http-api.md#post-assembly)
and prints a compact table of its closest database genomes. The server must run with
`--allow-sequences`.

| Option | Default | Meaning |
|---|---|---|
| `FILE` | required | assembly in FASTA or FASTQ, plain or gzip |
| `--top N` | `10` | number of hits to print, 1-50 |
| `--min-ani ANI` | `80` | minimum ANI in percent, 70-100 |
| `--name NAME` | file base name | query name |
| `--json` | off | print the raw JSON response |
| `-o, --output FILE` | stdout | write the table here |

The table has the columns `rank`, `ani`, `relation`, `shared_rank`, `accession`, `gtdb_species`,
`ncbi_organism_name` and `ncbi_taxid`. A summary line on stderr says whether the query was assigned
to a species (`assigned to species ... (99.53 % ANI)`) or only has a closest relative
(`no species-level match (< 95 % ANI); closest relative ...`).

The ANI is the naive containment ANI (`no_adjust`), the meaningful measure for an assembly.
[Identifying an assembly](../guides/assembly.md) explains this, the 95 % species assignment and
the hit relations.
