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
of about 630 MiB, while lookups stay in the low milliseconds. The file uses 8 KiB pages. Files
from older `sylphd` versions (format 1: a plain `metadata` table) remain readable.

## `--reps-only`

All ~900k GTDB genomes are stored by default. `--reps-only` stores only rows whose
`gtdb_representative` is true (~200k, about **78 MiB**). The server then finds no metadata for
other accessions, which is fine for a sylph database that contains only species
representatives. When two input files contain the same accession, the later row wins.
