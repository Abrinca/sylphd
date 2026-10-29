# Metadata database

`sylphd setup` turns GTDB's metadata TSV files into a small SQLite file. The server joins it to
every result by accession (`--meta`), adds its columns to each row, and uses its taxonomy strings
for [taxonomic profiles](../guides/taxonomic-profiles.md) and species assignment.

## Accessions

The key is the genome **accession**. The `RS_`/`GB_` prefix of GTDB accessions is stripped, so
`RS_GCF_000005845.2` becomes `GCF_000005845.2`. A sylph genome name such as
`.../GCA_964372435.1_genomic.fna.gz` maps to `GCA_964372435.1`. Names without a recognisable
accession get no metadata, and the lookup returns `null` for each column.

## Columns

| Group | Columns |
|---|---|
| GTDB | `gtdb_taxonomy`, `gtdb_representative`, `gtdb_genome_representative`, `gtdb_type_species_of_genus`, `gtdb_type_designation_ncbi_taxa` |
| NCBI | `ncbi_taxid`, `ncbi_species_taxid`, `ncbi_taxonomy`, `ncbi_taxonomy_unfiltered`, `ncbi_organism_name`, `ncbi_strain_identifiers`, `ncbi_isolate`, `ncbi_genome_category`, `ncbi_assembly_level`, `ncbi_refseq_category`, `ncbi_type_material_designation`, `ncbi_biosample`, `ncbi_bioproject` |
| Quality | `checkm2_completeness`, `checkm2_contamination`, `genome_size`, `gc_percentage`, `contig_count`, `n50_contigs`, `mimag_high_quality` |

`--fields` of `query` and `profile` selects from these names. Taxonomy strings stay as one column
each (no per-rank split), for example `gtdb_taxonomy` is `d__Bacteria;p__...;s__Escherichia coli`.
There is no NCBI lineage of taxids and no taxdump.

Value conversions while building:

- `none`, `na` and empty values become `NULL`;
- booleans `t`/`f` become integers `1`/`0`;
- numeric columns are `INTEGER` or `REAL`.

## Layout (format version 2) { #layout }

The file contains:

- a view **`metadata`** with `accession` plus the columns above. It is the only thing readers
  need to know about;
- the base table **`metadata_data`**, keyed by `accession` as `WITHOUT ROWID`, which stores values
  and, for repetitive text columns, integer ids;
- **`dict_<column>`** tables (`id`, `value`) for the repetitive text columns: both taxonomies,
  `gtdb_genome_representative`, organism names, genome category, assembly level, RefSeq category,
  type material designation and BioProject. The view joins them back to the original values;
- an **`info`** table with the source files and row counts, the GTDB release, the build date, the
  `sylphd` version, the format version and whether the file is representatives-only
  (`reps_only`).

Dictionary-encoding the repetitive text brings the full release down to about **198 MiB** instead
of about 630 MiB, while lookups stay in the low milliseconds. The file uses 8 KiB pages.

## `--reps-only`

All ~900k GTDB genomes are stored by default. `--reps-only` stores only rows whose
`gtdb_representative` is true (~200k, about **78 MiB**). The server then finds no metadata for
other accessions, which is fine for a sylph database that contains only species
representatives. When two input files contain the same accession, the later row wins.

## Taxonomy sources (format version 3) { #taxonomy-sources }

Format version 3 adds [sylph-tax](https://github.com/bluenote-1577/sylph-tax) taxonomy files to the
file, so hits from non-GTDB databases (UHGV viruses, fungi, MAGs, ...) can be given a lineage. The
`metadata` view and every metadata column are unchanged: version 3 is a superset of version 2.

A taxonomy file is plain text, one row per line, tab-separated, optionally gzip-compressed
(detected by magic bytes, not by the name):

```text
key<TAB>lineage[<TAB>host]
UHGV-0405173	vFAM-00013;root;Monodnaviria	Homo sapiens
```

Parsing follows sylph-tax (`sylph_to_taxprof.py`, v1.9.2): `line.rstrip().split("\t")`, the key
**verbatim** (case sensitive, never normalised, unlike GTDB accessions), the lineage an opaque `;`
string, and an optional third column with the virus host. A header row, as IMG/VR 4.1 has, is
stored like any other row and simply never matches a hit; an empty key is stored too (sylph-tax
accepts it). A blank line or a line without a tab is an error naming the file and the line, as it is
in sylph-tax (which would raise there) — a broken source never half-imports.

A key repeated within one file keeps the **last** lineage but only takes a new host from a row that
has a third column, so a later row without a host leaves the earlier host in place (this is exactly
sylph-tax's two-dict behaviour). Note that a trailing tab is *not* an empty host: `rstrip()` runs
before the split, so it counts as no host column. Across files sylph-tax keeps one host dictionary,
so for a key in several sources its lineage comes from the last file and its host from the last file
*that had a host column*; the database keeps one row per source, so a future resolution step must
pick the two by `ord` independently.

The file contains:

- **`taxonomy_source`** — one row per source: `ord` (the order given to `setup`; a key found in two
  sources resolves to the larger `ord`, as the later file wins in sylph-tax; the virtual GTDB
  source has `ord` 0), `name`, `kind`, `origin`, `md5`, `rows` (stored keys; duplicate keys within
  a source collapse), `has_host` and `built`;
- **`taxonomy_entry`** — `(source_id, key)` → `lineage`/`host`, dictionary ids, `WITHOUT ROWID`,
  with an index on `key` for batched lookups;
- **`taxonomy_overlap`** — `(source_id, other_id)` → `keys`: how many keys two sources share,
  written in both directions; see [overlapping sources](#overlapping-sources);
- **`dict_tax_lineage`**, **`dict_tax_host`** — `id`, `value`, like the other dictionary tables;
- the `info` keys `sylphtax_rules` (the sylph-tax version whose file formats and rules were
  implemented, currently `1.9.2`) and `taxonomy_sources` (a JSON copy of `taxonomy_source`,
  including each source's `overlaps`).

Two kinds of source exist:

| `kind` | Meaning |
|---|---|
| `metadata` | the **virtual GTDB source** over `metadata_data`: its keys are `metadata_data.accession` and its lineages `gtdb_taxonomy`, so no lineage is stored twice. It is created from `-m` and named `GTDB_r<release>` (`GTDB` without `--release`). Importing the sylph-tax catalogue file of the *same* release (`GTDB_r232` with `--release 232`) and `-m` would duplicate it, so that source is skipped with a message; a different release or a local file is imported. |
| `sylphtax` | one imported file; `md5` is always computed from the file, `origin` is the path or the download URL |

`GET /info` reports the sources under `metadata.taxonomy_sources` (name, kind, origin, md5, rows,
has_host, `overlaps`) and `metadata.sylphtax_rules`; `sylphd info` prints one line per source. A
format 2 file reports neither key, so its output is unchanged.

### Overlapping sources { #overlapping-sources }

**Import one taxonomy per database.** Sources that share keys are allowed — key spaces of GTDB
(`GCA_/GCF_...`), UHGV (`UHGV-...`), fungi and GlobDB overlap in the wild — but they are never
silent: when a source is imported (a build or `--update`), `setup` counts, in SQL, the keys it has
in common with the sources already in the file, **including the virtual GTDB source** (compared on
`metadata_data.accession`, the key `resolve` looks up, so an accession in the file and the same key
in a sylph-tax file count), and warns once per pair:

```text
sylphd: warning: taxonomy sources 'UHGV_default' and 'UHGV_ictv' share 1,234 key(s)
(examples: UHGV-0001, UHGV-0002, ...): the source imported later (UHGV_ictv) wins for
these keys; import only one of them, or rebuild without one
```

The warning goes to the log and to stderr (it is not progress, so `-q` does not hide it) and the
import still succeeds. For a shared key the source imported later wins (larger `ord`); `profile
--tax-source NAME` restricts a request to one source. The counts are stored in `taxonomy_overlap`
and reported as `overlaps: {"<other source>": <keys>}` per source by `sylphd info` and `GET /info`
(`{}` when the source is disjoint). A version 3 file written before the table existed still reads;
the next `--update` adds the table and fills the counts once.

### Resolving a hit { #resolving-a-hit }

The server gives every hit of `query`, `profile` and `/assembly` four extra columns, and lists
them in `metadata.columns`:

| Column | Meaning |
|---|---|
| `tax_source` | the source whose lineage was used, or `null` |
| `tax_key` | the key that matched, verbatim, or `null` |
| `tax_lineage` | that source's lineage string (opaque), or `null` |
| `tax_host` | the host of the key, or `null` |

Resolution replicates `sylph-to-taxprof` (sylph-tax 1.9.2) key for key over all sources, using
the hit's `Genome_file` and `Contig_name` exactly as sylph reports them:

| | pass 0 | pass 1 |
|---|---|---|
| `gf` | basename, cut at `_ASM` when `ASM` occurs anywhere in the full path, else at `_genomic` | basename |
| `glob` | `basename.split(".")[0]` | `gf` |
| `contig` | `Contig_name.split(" ")[0].split("\|")[0]` (space only) | `Contig_name.split()[0]` (any whitespace) |

Per pass the probes are `gf`, `gf + ".gz"`, `glob`, `contig`, then `gf.split(".fa")[0]`,
`gf.split(".fasta")[0]` and `gf.split(".fna")[0]`; the first key that exists wins, and a **pass-1
match overrides a pass-0 match**. Keys are compared verbatim: never normalised, never
version-stripped (the GTDB *accession* column is a different thing, and stays as it was).

When several sources contain the same key, **the source imported later wins** (the larger `ord`,
as if its file had been passed later to `sylph-tax -t`): the **lineage** comes from the largest
`ord` and the **host** from the largest `ord` whose source has a host column for that key — two
different sources, possibly, exactly like the two dictionaries sylph-tax fills while reading its
files. `sylphd profile --tax-source NAME` selects the sources per request when both files hold the
same keys (e.g. `UHGV_default` next to `UHGV_ictv`). A hit that matches nothing has `null` in all
four columns; `sylphd profile --taxonomy tax` then writes sylph-tax's
`NO_TAXONOMY;t__<genome file>:<contig>`.

Keys are not tied to a database: the probe list is what makes UHGV (`UHGV-0320751` in the contig
name) and GlobDB (`GCF_003697165` without the version) resolve from the same table. The rules are
in one function, `sylphd.metadata.sylphtax_probes`, and `tests/test_sylphtax_parity.py` compares
the whole path with the real sylph-tax.

### Compatibility and `--update`

A version 2 file keeps loading and serving exactly as before; `MetadataDB` accepts
`format_version` 2 and 3 and a version 2 reader ignores the new tables. Upgrading a version 2 file
copies it and adds the tables without the GTDB TSV files:

```bash
sylphd setup --update -m old.sqlite --taxonomy UHGV_default -o new.sqlite  # copy
sylphd setup --update --taxonomy UHGV_default -o old.sqlite                # in place
```

The copy is built next to the output, in a uniquely named temporary file (so parallel runs and a
source named `*.tmp` are safe), and replaces the output only on success, exactly like
`setup --force`, so a failed update leaves the old file untouched. The copy keeps the permission
bits of the file it replaces; a build to a new path uses the normal umask-derived mode.
`--update` can also add sources to an existing version 3 file; a source with an existing name is
replaced (its `ord` is kept).
