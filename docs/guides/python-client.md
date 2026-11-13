# Python client

`sylphd.client` talks to a server from Python; install `sylphd[client]` (it sketches locally with
pysylph, see [Install](../install.md)). Connect to a Unix socket or a URL:

```python
from sylphd.client import connect

client = connect(socket_path="/run/user/1000/sylphd.sock")        # a local server
client = connect(url="https://sylphd.abrinca.com", token="...")    # a remote one (token optional)
```

## From files: sketch here, send the sketch

These methods take paths. They read the server's `c` and `k` once, sketch on your machine and upload
only the sketch (never the sequence), so they work on every server, whatever its `--allow-sequences`.

| Method | Input | Server call | Returns |
|---|---|---|---|
| `client.profile_reads(reads)` | a FASTQ/FASTA path (gzip fine), a `(r1, r2)` pair, a `.sylsp` sketch or a `SampleInput` | `POST /profile` | the sample's result: `name`, `rows` (the hits), `params`, and with `taxprof=` the clade profiles |
| `client.query_reads(reads)` | the same | `POST /query` | the sample's ANI hits |
| `client.identify_assembly(assembly)` | a FASTA path | `POST /assembly` | the closest genomes of the assembly: `query` (name, records, length, GC), `hits` (best first, at most `top`, ANI of at least `min_ani`) and the interpretation |
| `client.classify_contigs(assembly)` | a FASTA path | `POST /contigs` | a `PerContigRun`: each contig classified on its own, plus the consensus |

```python
sample = client.profile_reads(("sample_R1.fq.gz", "sample_R2.fq.gz"), taxprof=["gtdb"])
sample["rows"]                         # per-genome rows, like the TSV of `sylph profile`
sample["taxprof"]["gtdb"]["sylphmpa"]  # the .sylphmpa text (and ["clades"] as data)
best = client.identify_assembly("genome.fna.gz", top=3)["hits"][0]

run = client.classify_contigs("assembly.fna", min_length=2000)
run.records            # one dict per classified contig, input order
run.summary()          # counts, skipped contigs, bp by consensus taxon
run.write_tsv(open("contigs.tsv", "w"))
```

- `profile_` and `query_` exist for reads only, where sylph has both modes: `profile` computes abundances
  of the genomes in a mixture (and, with `taxprof=`, the `.sylphmpa` profile; `query_reads` has no
  `taxprof`), `query` gives the plain ANI hits. A single genome has no abundance, so assemblies and
  contigs have one operation each, named for what it does: `identify_assembly` and `classify_contigs`.
- A sample is named after its file (`sample_R1.fq.gz` and `_R2` become `sample`); give `name=` to choose.
- `profile_reads` and `query_reads` take `params=`, `fields=` and `tax_sources=` like the lower-level
  `client.profile()` and `client.query()`.
- `classify_contigs` takes the options of the command line as keywords (`top`, `min_ani`, `min_length`,
  `fields`, `consensus_ratio`, `consensus_z`), or a whole `PerContigOptions` as `options=`. It splits the
  contigs into requests that fit the server's contig and upload limits.
- A sketch over the server's upload limit is refused before it is sent.
- `client.sketch_parameters()` gives the `(c, k)` the server wants and `client.database_info()` its
  `/info`, fetched once.

## From sketches or bytes

Lower-level calls take sketch bytes: `client.profile([(name, sketch.to_bytes())])`, `client.query(...)`,
`client.assembly_sketch(sketch_bytes, ...)`, `client.assembly(raw_fasta)` (only with `--allow-sequences`)
and `client.contigs(...)`. The sketching functions behind the methods above are in `sylphd.sketching`:
`sketch_sample`, `sketch_assembly` and `sketch_contigs`.

## A token that is not accepted

With a `token=` the client warns once (`TokenWarning`) when the server answers `X-Token-Valid: false`,
and `client.token_valid` says what the last response said. See
[Privileged access](public-server.md#privileged-access).

For NCBI taxonomy lookups see [NCBI taxonomy](ncbi-taxonomy.md).
