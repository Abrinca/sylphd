# NCBI taxonomy

sylphd can serve the whole NCBI taxonomy next to the genome database, and a small Python API gives
you taxon objects from a server, a Unix socket or a plain file. It replaces tools such as ete4's
`NCBITaxa` or `get-tax-info` for lookups.

## Install

Reading taxonomy needs no Rust and no pysylph: install `sylphd[taxonomy]` (only `httpx`) on a machine
that just looks taxa up, `sylphd[client]` where you also profile, and `sylphd[server]` on the machine that
runs `sylphd serve` and builds the file. See [Install](../install.md).

## Build the file

```bash
sylphd taxdb build -o taxdump.sqlite            # downloads NCBI's new_taxdump.tar.gz
sylphd serve --db ... --taxonomy-db taxdump.sqlite --taxonomy-rate-limit 120/min
```

See [`taxdb`](../commands/taxdb.md). The server then answers [`/taxonomy/*`](../reference/http-api.md#taxonomy);
`/info` reports `taxonomy.enabled`, the taxdump's date and the number of taxa.

## Use it from Python

```python
from sylphd.ncbitax import Taxonomy

tax = Taxonomy.connect(socket="/run/user/1000/sylphd.sock")   # a local server
tax = Taxonomy.connect(url="https://sylphd.abrinca.com")      # a remote one
tax = Taxonomy.connect(db="taxdump.sqlite")                   # a file, no server
tax = Taxonomy.connect()   # $SYLPHD_TAXDB, else $SYLPHD_URL, else $SYLPHD_SOCKET, else the default socket

t = tax.taxid(562)
t.name, t.rank, t.parent_taxid            # 'Escherichia coli', 'species', 561
t.genetic_code_id                         # 11
t.mito_genetic_code_id                    # 0 (unspecified)
t.genetic_code()                          # {'id': 11, 'name': 'Bacterial, Archaeal and Plant Plastid', 'amino_acids': ..., 'starts': ...}
t.parent, t.children(), t.lineage()       # Taxon objects, lineage from the root down
t.at_rank("genus")                        # <Taxon 561 Escherichia (genus)>
t.descendants("species", limit=1000)
t.names()                                 # synonyms, common names, authorities, ...
t.division_code                           # 'BCT'

tax.by_name("E. coli")                    # exact name of any class, case-insensitive
tax.lookup(["human", "mouse"])            # many names at once: {name: [Match, ...]}
tax.taxids([562, 9606, 12])               # many taxids in one request; None where unknown
tax.search("escherichia c")               # names starting with ...
tax.search("escherichia colli", fuzzy=True, rank="species")   # tolerates typos
```

- A taxid that NCBI merged gives its replacement, with `.merged_from` set to the id you asked for.
  A deleted or unknown taxid is `None` from `tax.get()` and `tax.taxids()` and raises
  `TaxonNotFound` (a `KeyError`) from `tax.taxid()`.
- Taxa are fetched in batches and cached, so walking a tree costs few requests. `Taxonomy.connect(cache=False)`
  turns the cache off.
- The three backends answer identically: code written against a file runs unchanged against a server.
- A `token=` (or `$SYLPHD_TOKEN`) is sent as a bearer token, which lifts the server's rate limit if the
  server knows it ([privileged access](public-server.md#privileged-access)).
- `sylphd.ncbitax` and `sylphd.taxdb` import only the standard library; `sylphd[taxonomy]` is enough to
  read a file or talk to a server (`httpx` is imported only for a server). Building the file
  (`sylphd taxdb build`) and the command line need `sylphd[client]` or `sylphd[server]`.

## Genetic codes

`genetic_code_id` and the mitochondrial, plastid and hydrogenosome ids come straight from NCBI's
`nodes.dmp`, already filled in for every taxon from its ancestors. `tax.genetic_codes()` holds the
tables (`1` Standard, `2` Vertebrate Mitochondrial, `11` Bacterial, ...) with amino acids and start
codons. A mitochondrial code of `0` means NCBI gives none.

## Fuzzy search

Each query word is corrected against the vocabulary of all names, then the names that contain the
corrected words are ranked by their similarity to the whole query, best first, one hit per taxon
(`score` 0 to 1). It answers in tens of milliseconds. The file must be built with the fuzzy index
(the default; `--no-fuzzy` leaves it out).
