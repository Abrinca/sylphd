# NCBI taxonomy

sylphd serves taxonomies next to the genome database, with [omnitax](https://abrinca.github.io/omnitax/): NCBI
(taxids, lineages, names, fuzzy search, genetic codes), GTDB and the genomes that link them. sylphd has no
taxonomy commands or Python classes of its own; the server mounts omnitax's API at `/taxonomy`, and omnitax is
the client.

## Build the file

The file is built with omnitax, on the machine that runs `sylphd serve` (`pip install omnitax`, or it came
with `sylphd[server]`):

```bash
omnitax build -o taxonomies.sqlite ncbi+assemblies+gtdb:232 gtdb:232
sylphd serve --db ... --taxonomy-db taxonomies.sqlite --taxonomy-rate-limit 120/min
```

- `ncbi+assemblies+gtdb:232` is NCBI's `new_taxdump` with its genomes: NCBI's assembly summaries (about 1 GB,
  streamed) and the NCBI taxid of every genome of GTDB 232 (two metadata files of about 100 MB). Plain `ncbi`
  is enough to serve lookups; the genomes are what [`sylphd setup --taxonomy-db`](../commands/setup.md)
  needs.
- `gtdb:232` is GTDB's own taxonomy (`s__Escherichia coli`, ...), which `setup` translates into NCBI. Use the
  release of your metadata file.
- Rebuild with the same command to refresh: a replaced file is picked up by the running server without a
  restart.

See omnitax's [`build`](https://abrinca.github.io/omnitax/commands/build/) for the other sources (SILVA,
Greengenes, Open Tree, your own table), specific NCBI dates and local files.

## Use it

The server answers at `/taxonomy` (the [HTTP API](../reference/http-api.md#taxonomy)), so omnitax is given the
URL with that path:

```bash
omnitax --url https://sylphd.abrinca.com/taxonomy -t ncbi lookup 562 "Homo sapiens"
omnitax --url https://sylphd.abrinca.com/taxonomy -t ncbi lineage 562
omnitax --url https://sylphd.abrinca.com/taxonomy list
```

```python
import omnitax

tax = omnitax.connect(url="https://sylphd.abrinca.com/taxonomy", taxonomy="ncbi")
tax["562"].lineage_string()          # 'd__Bacteria;p__Pseudomonadota;...;s__Escherichia coli'
tax.consensus({"562": 6, "564": 4}, exclude=tax.uninformative)
tax.search("escherichia c")
```

`/info` says whether a server has a taxonomy file, and which taxonomies, versions and build dates it holds
(`taxonomy.taxonomies`); `omnitax list` shows the same with their genomes and provenance.

### With a privileged token

A server may limit the taxonomy requests of every client (`--taxonomy-rate-limit`), or ask for a token for
everything (`--require-token`). A [privileged token](public-server.md#privileged-access) lifts the limit; send it
as a header:

```bash
omnitax --url https://sylphd.abrinca.com/taxonomy --header "Authorization: Bearer $(cat ~/.config/sylphd/token)" \
        -t ncbi lineage 562
```

```python
token = (Path.home() / ".config/sylphd/token").read_text().strip()
tax = omnitax.connect(url="https://sylphd.abrinca.com/taxonomy", taxonomy="ncbi", headers={"Authorization": f"Bearer {token}"})
```

A client that gets a 429 waits for `Retry-After` when it is given `--retries N` / `retries=N`.
