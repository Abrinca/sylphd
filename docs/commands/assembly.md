# assembly

```text
sylphd assembly FILE [--top N] [--min-ani ANI] [--name NAME] [--local-sketch | --no-local-sketch]
                [--json] [-o FILE]
                [--socket PATH | --tcp HOST:PORT | --url URL] [--timeout SECONDS]
```

Sketches one assembly (FASTA or FASTQ, plain or gzip) on this machine, sends the sketch to
[`POST /assembly`](../reference/http-api.md#post-assembly) and prints a compact table of its closest
database genomes. The sequence never leaves your machine and has no size limit; this works on any
server, with or without `--allow-sequences`. A server from before sketch uploads gets the raw
sequence instead (it needs `--allow-sequences`).

| Option | Default | Meaning |
|---|---|---|
| `FILE` | required | assembly in FASTA or FASTQ, plain or gzip |
| `--top N` | `10` | number of hits to print, 1-50 |
| `--min-ani ANI` | `80` | minimum ANI in percent, 70-100 |
| `--name NAME` | file base name | query name |
| `--local-sketch`, `--no-local-sketch` | automatic | `--local-sketch`: sketch locally and upload only the sketch; an error if the server cannot take sketches. `--no-local-sketch`: upload the raw sequence. By default the sketch is used whenever `GET /info` says the server takes sketches |
| `--json` | off | print the raw JSON response |
| `-o, --output FILE` | stdout | write the table here |

The table has the columns `rank`, `ani`, `relation`, `shared_rank`, `accession`, `gtdb_species`,
`ncbi_organism_name` and `ncbi_taxid`. A summary line on stderr says whether the query was assigned
to a species (`assigned to species ... (99.53 % ANI)`) or only has a closest relative
(`no species-level match (< 95 % ANI); closest relative ...`).

The ANI is the naive containment ANI (`no_adjust`), the meaningful measure for an assembly.
[Identifying an assembly](../guides/assembly.md) explains this, the 95 % species assignment and
the hit relations.
