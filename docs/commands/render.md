# render

```text
sylphd render [FILE]
```

Renders a saved result document as the table the command line would have printed, without a server
and without sketching anything. `FILE` is a document written by `--json` (`profile`, `query`,
`assembly`, `assembly --per-contig`), or `-`/omitted for stdin.

```bash
sylphd profile -r reads.fq.gz --json -o reads.json          # save the answer
sylphd render reads.json > reads.tsv                        # the table, later or elsewhere
sylphd assembly --per-contig mag.fna --json -o mag.json && sylphd render mag.json
```

Like the commands that produced the document, this runs in the native `sylphd-client`; the
rendering is the same code, so the table is byte-identical to the one `sylphd profile` prints
directly. The program examines the document and picks the table and the stderr lines that belong to
it:

| Document | stdout | stderr |
|---|---|---|
| `/profile`, `/query` | the TSV (sylph's columns, `accession`, the metadata columns) | the profile caveats, if any |
| `/assembly` | the hits table | the one-line assignment summary and the species note |
| the per-contig run document | the per-contig TSV | the two summary lines and the caveats |

Exit codes: `0` on success, `2` for a usage error (unreadable or unparsable file), `1` when the
document has no `api_version` or one this client does not support: a newer one says
`document API version 3 is newer than this client's (1): update sylphd`; an older one says the
document predates this sylphd.

The document format is the one in the [HTTP API](../reference/http-api.md): `table` is pandas'
`orient="split"` layout, so a document is also one line with pandas
(`pd.DataFrame(**doc["table"])`). Rendering from Python needs no document handling at all: see
[Python: `sylphd.run`](../guides/python-client.md).
