# taxdb

```text
sylphd taxdb build -o FILE [--taxdump FILE] [--cache-dir DIR] [--no-fuzzy] [--force] [-q]
```

Builds the **NCBI taxonomy file**: one indexed SQLite file with every taxon of NCBI's
`new_taxdump.tar.gz`. A server started with [`--taxonomy-db`](serve.md) answers
[`/taxonomy/*`](../reference/http-api.md#taxonomy) from it, and
[`sylphd taxonomy`](taxonomy.md) and the [Python objects](../guides/ncbi-taxonomy.md) read it
directly, without a server.

| Option | Default | Meaning |
|---|---|---|
| `-o`, `--output FILE` | required | the SQLite file to write; written next to it and renamed into place, so a running server never sees a half-written file |
| `--taxdump FILE` | download | a local `new_taxdump.tar.gz` instead of downloading the current one |
| `--cache-dir DIR` | next to `--output` | where the download is unpacked; deleted afterwards |
| `--no-fuzzy` | off | skip the fuzzy-search index (about 15 % of the file) |
| `--force` | off | replace an existing output file |
| `-q`, `--quiet` | off | no progress output |

A build takes about 30 s on a laptop (plus the 160 MB download) and the file is about 1 GB
(3 million taxa, 4.8 million names). It keeps:

- every node: rank, parent, GenBank division, the **genetic code** and the mitochondrial, plastid
  and hydrogenosome genetic codes, the hidden flags, the EMBL code and NCBI's comment;
- every name with its class (scientific name, synonym, common name, authority, ...) and the
  unique name NCBI gives to ambiguous ones (`Bacteria <bacteria>`);
- the merged and deleted taxids, so an old taxid resolves to its replacement;
- the division and genetic code tables (`1` Standard, `11` Bacterial, ... with amino acids and
  start codons).

It does not keep NCBI's `inherited` flags (the values are already filled in), citations, hosts or
type material.

## Keeping it current

NCBI updates the taxdump daily. Rebuild whenever you want to follow it:

```bash
sylphd taxdb build -o /srv/sylphd/db/taxdump.sqlite --force
```

A running server reads the new file on its next request, with no restart. The taxonomy of the
genomes inside the [metadata database](setup.md) is a snapshot taken when that file was built and
does not change with this one.
