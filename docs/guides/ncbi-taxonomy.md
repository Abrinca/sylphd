# NCBI taxonomy

A sylphd server can serve the NCBI and GTDB taxonomies with [omnitax](https://abrinca.github.io/omnitax/), at
`/taxonomy`. `sylphd.taxonomy()` (from `pip install 'sylphd[client]'`) connects to it:

```python
import sylphd

ncbi = sylphd.taxonomy("ncbi")                                    # or url="https://sylphd.abrinca.com"
ncbi["562"].name                                                  # 'Escherichia coli'
ncbi["562"].lineage_string()                                      # 'd__Bacteria;p__Pseudomonadota;...;s__Escherichia coli'
```

The server is found as for the command line ([server address](../commands/address.md): `url=`, `socket=`,
`$SYLPHD_URL`, `$SYLPHD_SOCKET`, the default socket), and the result is a plain `omnitax.Taxonomy`. The same
with the omnitax command line:

```bash
omnitax lineage --url https://sylphd.abrinca.com/taxonomy -t ncbi 562
```

A [privileged token](public-server.md#privileged-access) lifts the rate limit: `token_file=...` (or
`$SYLPHD_TOKEN`), and `--header "Authorization: Bearer ..."` for `omnitax`.

## Serving it

```bash
omnitax build -o taxonomies.sqlite ncbi+assemblies+gtdb:232 gtdb:232
sylphd serve --db ... --taxonomy-db taxonomies.sqlite
```

The same file is what [`sylphd setup --taxonomy-db`](../commands/setup.md) reads.

Everything else (search, consensus, translate, other taxonomies, building files) is in the
[omnitax documentation](https://abrinca.github.io/omnitax/).
