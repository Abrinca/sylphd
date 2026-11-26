# setup

```text
sylphd setup [-m FILE...] [--taxonomy SPEC...] -o PATH [--release N] [--reps-only]
             [--update] [--cache-dir DIR] [--taxonomy-db FILE] [--force] [-q]

SPEC := NAME | NAME=PATH | PATH
```

Streams GTDB metadata TSV files (gzip or plain) into a SQLite file that `sylphd serve --meta` joins
to every result, and imports [sylph-tax](https://github.com/bluenote-1577/sylph-tax) taxonomy files
next to it. The imported sources are stored now so that a later release can resolve hits from
non-GTDB databases (UHGV viruses, fungi, MAGs, ...) to a lineage and a host; `setup` itself only
stores and reports them. The layout and columns are in the
[metadata database](../reference/metadata.md) reference.

| Option | Default | Meaning |
|---|---|---|
| `-m, --metadata FILE ...` | none | GTDB metadata TSV files (`*.tsv.gz`) |
| `--taxonomy SPEC` | none | taxonomy source, repeatable: a sylph-tax name, `NAME=PATH` or `PATH` |
| `-o, --output PATH` | required | SQLite file to write |
| `--update` | off | add the sources to an existing file instead of building one |
| `--cache-dir DIR` | `<output dir>/taxonomy` | where downloaded taxonomy files are kept |
| `--taxonomy-db FILE` | off | an [omnitax](../guides/ncbi-taxonomy.md) file (NCBI with its genomes, and GTDB): gives the sylph-tax genomes that GTDB does not cover an NCBI lineage, and those without an assembly a nearest NCBI taxon |
| `--release N` | none | GTDB release to record in the file (and the name of the virtual source) |
| `--reps-only` | off | store only species representatives (`gtdb_representative` true), `-m` only |
| `--force` | off | replace the output file (only once the new one is built; a failure keeps the old one) |
| `-q, --quiet` | off | no progress output |

At least one of `-m` and `--taxonomy` is required. `SPEC` is:

- a **sylph-tax name**: `UHGV_default`, `UHGV_ictv`, `FungiRefSeq-latest`, `GTDB_r232`,
  `GlobDB_r232`, ... (`sylphd download --list-taxonomies` prints all of them with sizes, row counts
  and the database each one goes with). The file is downloaded into `--cache-dir`, resuming
  partial transfers and verifying the size (and MD5 when the publisher has one) before it is used;
- **`NAME=PATH`**: a local file under a chosen source name, read as it is (gzip is detected by
  content). Useful to override a name or to import your own file;
- **`PATH`**: a local file; the source name defaults to the file stem
  (`uhgv_default_metadata.tsv.gz` → `uhgv_default_metadata`).

```bash
# GTDB metadata plus UHGV and fungal lineages for the target trio
sylphd setup -m data/bac120_metadata_r232.tsv.gz data/ar53_metadata_r232.tsv.gz \
             --taxonomy UHGV_default --taxonomy FungiRefSeq-latest \
             -o data/meta.sqlite --release 232

# the GlobDB configuration of the public server: GlobDB's own genomes resolve to their GTDB rows,
# and the ones with no NCBI record get a nearest NCBI taxon (see deploy/README.md)
sylphd setup -m data/bac120_metadata_r232.tsv.gz data/ar53_metadata_r232.tsv.gz \
             --taxonomy GlobDB_r232 --taxonomy UHGV_default --taxonomy FungiRefSeq-latest \
             --taxonomy-db data/taxonomies.sqlite -o data/meta-globdb.sqlite --release 232

# a local file under a chosen name
sylphd setup -m data/*.tsv.gz --taxonomy UHGV=./my_uhgv.tsv.gz -o data/meta.sqlite

# add sources to an existing file ; atomic
sylphd setup --update --taxonomy UHGV_ictv -o data/meta.sqlite
```

All ~900k GTDB genomes are stored by default (about 200 MB). `--reps-only` stores about 200k
(about 80 MB); lookups of other accessions then find no metadata. When two input files contain the
same accession, the later row wins.

The GTDB metadata becomes the **virtual** source `GTDB_r<release>`: its lineages are not stored a
second time, so importing the sylph-tax catalogue file of the *same* release (`GTDB_r232` with
`--release 232`) is redundant and that source is skipped with a message. A catalogue file of a
different release (`GTDB_r214`) and a local file of any other name are imported normally; a local
file named exactly like the virtual source is refused, because importing it would replace it.
Sources are stored in the order given; for a key found in two sources the later one wins (as the
later file wins in sylph-tax). Re-importing a source name replaces it.

**Import one taxonomy per database.** Overlapping sources are accepted, never silently: after each
import `setup` counts the keys the source shares with the sources already in the file — including
the virtual GTDB source, whose keys are the metadata accessions — and, for every pair that shares
keys, prints a warning naming both sources, the number of shared keys, the first five keys and the
rule that applies: *the source imported later wins for these keys; import only one of them, or
rebuild without one*. The import still succeeds (a warning, not an error); the counts are stored in
the file and shown by [`sylphd info`](info.md) and `GET /info`. This is the same collision that
`profile --tax-source NAME` can work around per request.

`--update` copies the existing file (the `-o` path, or `-m` when you want a separate output) and
adds the sources; it needs no GTDB TSV files. The copy is written next to the output and replaces
it only on success, so an interrupted or failed update leaves the old file untouched.
`--release` and `--reps-only` cannot be used with `--update`.

## NCBI lineages

GTDB's metadata has no row for a genome of another source (a `FungiRefSeq-latest` key
`GCF_000002495.2_MG8`, say), so its NCBI taxonomy view, NCBI Taxonomy links and `ncbi_*` columns
would be empty. `--taxonomy-db` fills them, for a build and for `--update`. It names a taxonomy file
that [omnitax](../guides/ncbi-taxonomy.md) built, with NCBI and its genomes and the GTDB release of your
metadata:

```bash
omnitax build -o data/taxonomies.sqlite ncbi+assemblies+gtdb:232 gtdb:232
sylphd setup -m data/bac120_metadata_r232.tsv.gz \
             --taxonomy FungiRefSeq-latest --taxonomy-db data/taxonomies.sqlite \
             -o data/meta.sqlite --release 232
```

`setup` downloads nothing for this: the taxdump and the assembly summaries are in the file, which is built
once and also serves [`/taxonomy`](../reference/http-api.md#taxonomy). The release picks the GTDB taxonomy in
the file (`gtdb:232`; the newest one when `--release` is not given), and the genomes are looked up through
GTDB's own NCBI taxids (`ncbi+gtdb:232`) and then NCBI's assembly summaries (`ncbi+assemblies`), whichever of the two
the file has.

For every key of every sylph-tax source whose assembly accession has no GTDB metadata row (a GTDB
row is never replaced), the assembly accession in the key (`GC[AF]_\d+(?:\.\d+)?`) is looked up as omnitax looks
genomes up: the same accession, else its version, else the only stored version of its number (GlobDB's
unversioned `GCF_003697165` finds the one version the file has; two versions are ambiguous and skipped), and
`GCA_` and `GCF_` accessions of one number are different genomes. The genome's taxon is the taxid and its lineage
the lineage (merged taxids are followed by omnitax) — never from an organism name. `ncbi_taxonomy` keeps GTDB's
`d__…;p__…;c__…;o__…;f__…;g__…;s__…` shape, with NCBI's superkingdom as `d__`; NCBI's
kingdom is not one of the seven ranks and is left out. The rows are stored like GTDB metadata rows
(accession key, GTDB columns `NULL`), under the accession the file holds the genome by (or the key's own when
it has a version), so every reader finds them without a change; GTDB rows keep
their own NCBI data. Every `--taxonomy-db` run deletes and rebuilds these rows, so running
`--update --taxonomy-db` again is idempotent. `--update` without the flag keeps them, deletes the
ones no source key refers to any more and drops the counts of the sources it replaced. The counts per source
are `gtdb` (covered by GTDB's metadata), `mapped`, `no_accession`, `not_in_summary` (the file has no such genome)
and `ambiguous`.

A key with a GTDB-Tk lineage but no assembly in NCBI at all — a GlobDB MAG (`MOTU40_002958`) — still
gets a place in the NCBI view: the **nearest NCBI taxon** of its deepest GTDB clade that has one. Each clade is
translated into NCBI through the genomes the two taxonomies share (omnitax's `translate`): every GTDB leaf
below the clade votes once, with the NCBI nodes of its genomes (the leaf's vote is split between them, so a
species of many genomes does not outvote one of few), under NCBI's *unclassified sequences*, *other
sequences* and *environmental samples* nothing votes, and the deepest NCBI node that **more than 90 %** of the
votes have as ancestor-or-self wins, lifted to a standard rank (a strict LCA would be dragged up by one
misclassified genome; a bare majority put a genus's MAGs at a species the data barely supports: on a
benchmark of known genomes 0.5 gave 68 % precision, 0.9 gives 91 %). That node is reported as it is, even when it is a species
NCBI only names informally (*Clostridia bacterium*, *uncultured Collinsella sp.*). Alongside it, the
server stores the nearest ancestor NCBI names *formally* — from NCBI's `specified_species`
flag, so not a pattern on names — as `ncbi_nearest_formal_taxid`, `_formal_rank` and `_formal_name`:
the class *Clostridia* for *Clostridia bacterium*, the genus *Collinsella* for *uncultured
Collinsella sp.*, and the raw taxon itself when it is formally named. All of it is stored as
`ncbi_nearest_taxid`, `ncbi_nearest_rank`, `ncbi_nearest_name`, `ncbi_nearest_clade`,
`ncbi_nearest_lineage` and the three `formal_*` columns, never as `ncbi_taxid` (which
stays `null` for an assembly NCBI does not know), and the NCBI profile and the web NCBI view place
such hits at the raw node. See [nearest NCBI taxa](../reference/metadata.md#nearest-ncbi-taxa).

The summary reports, per source, how many keys were mapped, how many carry no assembly accession,
how many are not in the summaries, how many have a taxid without a lineage and how many GTDB's own
NCBI data covers, and how many keys without an NCBI record got a nearest taxon and at which ranks,
e.g.:

```text
  GlobDB_r232: 346,233 keys (sylphtax)
    NCBI lineage: 0 mapped, 146,310 without an assembly accession, 0 not in the summaries, 0 without a lineage, 199,923 with GTDB's NCBI data
    nearest NCBI taxon: 146,310 of 146,310 keys without an NCBI record (domain 6,201, kingdom 2,234, phylum 9,998, class 17,119, order 22,718, family 22,055, genus 21,958, species 44,027; 40,537 informal, formally named above)
  FungiRefSeq-latest: 661 keys (sylphtax)
    NCBI lineage: 659 mapped, 0 without an assembly accession, 2 not in the summaries, 0 without a lineage
```

`GET /info` and `sylphd info` report the same counts (`metadata.taxonomy_sources[].ncbi` and
`.ncbi_nearest`). The downloads happen only at `setup` time; serving does no extra work.
