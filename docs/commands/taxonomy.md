# taxonomy

```text
sylphd taxonomy QUERY... [--db FILE | --socket PATH | --tcp HOST:PORT | --url URL]
                [--search [--fuzzy] [--rank RANK] [--limit N]] [--lineage]
                [--token-file PATH] [--timeout SECONDS] [--retries N]
```

Looks up NCBI taxa on a server started with `--taxonomy-db`, or in a file made by
[`taxdb build`](taxdb.md) (`--db FILE`, no server needed). Each `QUERY` is a taxid (digits) or an
exact name. It prints a header and one tab-separated line per taxon:
`taxid`, `name`, `rank`, `parent`, `genetic_code`, `mito_genetic_code`. A query with no match is
reported on stderr and the exit status is 1.

```bash
sylphd taxonomy --db taxdump.sqlite 562 "Homo sapiens"
sylphd taxonomy --url https://sylphd.abrinca.com --lineage 9606
sylphd taxonomy --db taxdump.sqlite --search --fuzzy --rank species "bacilus subtilis"
```

| Option | Meaning |
|---|---|
| `--search` | treat each `QUERY` as the start of a name |
| `--fuzzy` | with `--search`: tolerate typos (`escherichia colli`) |
| `--rank RANK` | with `--search`: only taxa of this rank (`species`, `genus`, ...) |
| `--limit N` | with `--search`: hits per query [10] |
| `--lineage` | print each taxon's lineage, root first |
| `--db FILE` | read a taxonomy file instead of asking a server |

The [server address](address.md) options and `--token-file` work as for the other commands. For
scripts, use the [Python objects](../guides/ncbi-taxonomy.md).
