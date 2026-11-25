# Python: `sylphd.run`

Python owns the `sylphd` command, not the queries: `info`, `profile`, `query`, `assembly` and
`render` run in the native `sylphd-client`. Python's query API is therefore one helper that runs
the command line and returns the JSON document the binary got from the server:

```python
import sylphd

doc = sylphd.run("profile", "reads.fq.gz", "--socket", "/run/user/1000/sylphd.sock")
doc = sylphd.run("query", "-1", "R1.fq.gz", "-2", "R2.fq.gz",
                 "--url", "https://sylphd.abrinca.com")
doc = sylphd.run("assembly", "genome.fna.gz", "--per-contig")   # the run document
```

`run(*args)` takes the arguments of the command line:

- it adds `--json` (pass it yourself if you like) and parses stdout;
- it raises `sylphd.ClientRunError` when the binary exits non-zero, with the exit code in
  `.returncode` and the binary's complete stderr in `.stderr` (which is also the message);
- it raises `FileNotFoundError` when `sylphd-client` is not installed (the message names
  `sylphd[client]`) and `ValueError` for arguments that cannot work here (`render`, `-o`);
- on success it passes the binary's stderr (warnings, operator notes) on to `sys.stderr`.

The commands it accepts are `info`, `profile`, `query` and `assembly`. Exit codes are the command
line's: `2` for usage or input errors, `1` for server or client errors.

## The document

The result is the server's document, unchanged (see the [HTTP API](../reference/http-api.md)):

```python
doc["api_version"]                  # 1; the client checks it before sketching
doc["samples"]                      # [{"name": "reads", "kmers": ..., "reads": ..., ...}, ...]
doc["table"]["columns"]             # the ordered header
doc["table"]["data"]                # rows, one per hit
doc["caveats"]                      # note strings the server computed
doc.get("taxprof", {})              # per sample, per taxonomy (.sylphmpa text and clades)
```

`table` is pandas' `orient="split"` layout without an index, so a DataFrame is one line:

```python
import pandas as pd
frame = pd.DataFrame(**doc["table"])
```

Numbers are numbers (`null` where sylph prints `inf`, `NaN` or `NA-NA`); the renderer in the binary
is what turns them into sylph's spellings.

## Taxonomy lookups

`sylphd.ncbitax` and `sylphd.taxdb` are unchanged and need no binary: see
[NCBI taxonomy](ncbi-taxonomy.md).

## Tokens and server addresses

Everything the command line knows applies, because it is the command line: `--url`/`--socket`,
`$SYLPHD_URL`/`$SYLPHD_SOCKET`, `--token-file`/`$SYLPHD_TOKEN`, `--retries`, `--timeout`. See
[Server address](../commands/address.md).
