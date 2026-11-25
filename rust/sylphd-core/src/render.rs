//! Result rendering: TSV tables, assembly & per-contig summaries, caveats, and `/info` text.
//!
//! Ported from Python (`sylphd.protocol`, `sylphd.cli`, `sylphd.per_contig_client`,
//! `sylphd.download`). Sylph-column TSV output is byte-identical to Python and stock `sylph`.

use std::fmt::Write;

use serde_json::Value;

use crate::document::{
    AssemblyDocument, ConsensusBpEntry, ContigsDocument, InfoDocument, MessageItem, MetadataInfo,
    NcbiCounts, NcbiNearestCounts, ProfileDocument, Table, UsageInfo, ASSEMBLY_TSV_COLUMNS,
    DEFAULT_MIN_CONTIG_LENGTH,
};

/// Program prefix used in warning/note lines (matching `cli_parser.PROG`).
pub const PROG: &str = "sylphd";

/// Metadata columns stored as SQLite `REAL` where Python's `str(float)` always
/// includes a decimal point (e.g. `100.0`), even after a JS JSON round-trip turns
/// `100.0` into `100`.
const FLOAT_METADATA_COLUMNS: [&str; 3] = [
    "checkm2_completeness",
    "checkm2_contamination",
    "gc_percentage",
];

fn is_float_metadata_column(column: &str) -> bool {
    FLOAT_METADATA_COLUMNS.contains(&column)
}

fn value_as_f64(value: &Value) -> f64 {
    match value {
        Value::Number(n) => n.as_f64().unwrap_or(0.0),
        Value::String(s) => s.parse::<f64>().unwrap_or(0.0),
        Value::Bool(b) => {
            if *b {
                1.0
            } else {
                0.0
            }
        }
        _ => 0.0,
    }
}

fn value_as_i64(value: &Value) -> i64 {
    match value {
        Value::Number(n) => n
            .as_i64()
            .or_else(|| n.as_u64().map(|u| u as i64))
            .or_else(|| n.as_f64().map(|f| f as i64))
            .unwrap_or(0),
        Value::String(s) => s.parse::<i64>().unwrap_or(0),
        Value::Bool(b) => i64::from(*b),
        _ => 0,
    }
}

/// Python-compatible `str(value)` for scalar JSON values (`None -> ""`, `bool -> True/False`).
fn py_str(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Number(n) => n.to_string(),
        Value::String(s) => s.clone(),
        Value::Array(_) | Value::Object(_) => value.to_string(),
    }
}

/// Format a metadata or unknown column cell.
fn format_metadata_value(column: &str, value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::String(s) => s.clone(),
        Value::Number(n) => {
            if is_float_metadata_column(column) && (n.is_i64() || n.is_u64()) {
                format!("{n}.0")
            } else {
                n.to_string()
            }
        }
        Value::Array(_) | Value::Object(_) => value.to_string(),
    }
}

/// Format an integer with comma thousands separators (`1150000` -> `"1,150,000"`).
pub fn format_thousands(n: u64) -> String {
    let s = n.to_string();
    let len = s.len();
    if len <= 3 {
        return s;
    }
    let mut out = String::with_capacity(len + (len - 1) / 3);
    for (i, ch) in s.chars().enumerate() {
        if i > 0 && (len - i) % 3 == 0 {
            out.push(',');
        }
        out.push(ch);
    }
    out
}

/// Format a float like Python's `{x:g}` for thresholds (`95.0` -> `"95"`, `95.5` -> `"95.5"`).
fn format_g(value: f64) -> String {
    if !value.is_finite() {
        return value.to_string();
    }
    if value == 0.0 {
        return "0".to_string();
    }
    let abs = value.abs();
    if abs >= 1e-4 && abs < 1e6 {
        let exp = abs.log10().floor() as i32;
        let frac_digits = (5 - exp).max(0) as usize;
        let formatted = format!("{value:.frac_digits$}");
        if formatted.contains('.') {
            let trimmed = formatted.trim_end_matches('0').trim_end_matches('.');
            return trimmed.to_string();
        }
        return formatted;
    }
    format!("{value:e}")
}

/// Format a byte count for humans (`download.format_size`: `1023 B`, `1.0 KiB`, `18.7 GiB`).
pub fn format_size(size: u64) -> String {
    let mut value = size as f64;
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"] {
        if value < 1024.0 || unit == "TiB" {
            return if unit != "B" {
                format!("{value:.1} {unit}")
            } else {
                format!("{} B", value as u64)
            };
        }
        value /= 1024.0;
    }
    format!("{size} B")
}

/// Format a base-pair count for the per-contig skipped-record summary (`per_contig_client._format_bases`).
pub fn format_bases(bp: u64) -> String {
    if bp >= 1_000_000 {
        format!("{:.1} Mb", bp as f64 / 1_000_000.0)
    } else if bp >= 1_000 {
        format!("{:.1} kb", bp as f64 / 1_000.0)
    } else {
        format!("{bp} bp")
    }
}

// ---------------------------------------------------------------------------
// Profile / query TSV (protocol.format_value / format_row / rows_to_tsv)
// ---------------------------------------------------------------------------

/// Format one cell of a `/profile` or `/query` TSV, mirroring `sylphd.protocol.format_value`.
///
/// `Value::Null` maps to sylph's spellings: `NA-NA` for missing confidence intervals,
/// `inf` for `Eff_cov`/`True_cov`, `NaN` for abundances, and `""` for metadata/other
/// columns. `Eff_lambda` status substitution (`HIGH`/`LOW` from `Lambda_status`) is
/// handled in [`render_tsv`].
pub fn format_value(column: &str, value: &Value) -> String {
    if value.is_null() {
        return match column {
            "ANI_5-95_percentile" | "Lambda_5-95_percentile" => "NA-NA".to_string(),
            "Eff_cov" | "True_cov" => "inf".to_string(),
            "Taxonomic_abundance" | "Sequence_abundance" => "NaN".to_string(),
            _ => String::new(),
        };
    }
    match column {
        "Sample_file" | "Genome_file" | "Contig_name" | "accession" => py_str(value),
        "Adjusted_ANI" => format!("{:.2}", value_as_f64(value).min(100.0)),
        "Naive_ANI" => format!("{:.2}", value_as_f64(value)),
        "Eff_cov" | "True_cov" | "Mean_cov_geq1" => format!("{:.3}", value_as_f64(value)),
        "Eff_lambda" => match value {
            Value::String(s) => s.clone(),
            _ => format!("{:.3}", value_as_f64(value)),
        },
        "ANI_5-95_percentile" | "Lambda_5-95_percentile" => match value {
            Value::Array(pair) if pair.len() == 2 => {
                format!(
                    "{:.2}-{:.2}",
                    value_as_f64(&pair[0]),
                    value_as_f64(&pair[1])
                )
            }
            _ => py_str(value),
        },
        "Median_cov" => format!("{:.0}", value_as_f64(value)),
        "Containment_ind" => match value {
            Value::Array(pair) if pair.len() == 2 => {
                format!("{}/{}", value_as_i64(&pair[0]), value_as_i64(&pair[1]))
            }
            _ => py_str(value),
        },
        "Taxonomic_abundance" | "Sequence_abundance" => format!("{:.4}", value_as_f64(value)),
        "kmers_reassigned" => format!("{}", value_as_i64(value)),
        _ => format_metadata_value(column, value),
    }
}

/// Render a `/profile` or `/query` JSON table to sylph-identical TSV.
///
/// Drops the `Lambda_status` column from the output and uses its value (`HIGH`/`LOW`)
/// to populate `Eff_lambda` whenever `Eff_lambda` is `null`.
pub fn render_tsv(table: &Table) -> String {
    let lambda_status_idx = table.column_index("Lambda_status");
    let kept: Vec<(usize, &str)> = table
        .columns
        .iter()
        .enumerate()
        .filter(|(_, name)| name.as_str() != "Lambda_status")
        .map(|(i, name)| (i, name.as_str()))
        .collect();

    let mut out = String::new();
    for (pos, (_, col)) in kept.iter().enumerate() {
        if pos > 0 {
            out.push('\t');
        }
        out.push_str(col);
    }
    out.push('\n');

    for row in &table.data {
        for (pos, (idx, col)) in kept.iter().enumerate() {
            if pos > 0 {
                out.push('\t');
            }
            let cell = row.get(*idx).unwrap_or(&Value::Null);
            if *col == "Eff_lambda" && cell.is_null() {
                if let Some(status) = lambda_status_idx
                    .and_then(|si| row.get(si))
                    .and_then(Value::as_str)
                {
                    if status == "HIGH" || status == "LOW" {
                        out.push_str(status);
                        continue;
                    }
                }
            }
            out.push_str(&format_value(col, cell));
        }
        out.push('\n');
    }
    out
}

/// Render a `/profile` or `/query` document's table to TSV.
pub fn render_profile_tsv(doc: &ProfileDocument) -> String {
    render_tsv(&doc.table)
}

/// Format caveat warning lines (`sylphd: warning: <note>\n`), one per caveat.
pub fn render_caveats(caveats: &[String]) -> String {
    let mut out = String::new();
    for note in caveats {
        if !note.is_empty() {
            let _ = writeln!(out, "{PROG}: warning: {note}");
        }
    }
    out
}

/// Format the species-threshold caveats of a `/profile` document for stderr.
pub fn render_profile_caveats(doc: &ProfileDocument) -> String {
    render_caveats(&doc.caveats)
}

// ---------------------------------------------------------------------------
// Assembly hits TSV and summary (cli._write_hits_tsv / _assignment_summary)
// ---------------------------------------------------------------------------

fn fallback_taxon_from_taxonomy(val: &Value) -> Option<String> {
    let obj = val.as_object()?;
    let (_, last) = obj.iter().next_back()?;
    let text = py_str(last);
    if text.is_empty() {
        None
    } else {
        Some(text)
    }
}

/// Render the 11-column assembly hits TSV from a `/assembly` `Table` (`cli._write_hits_tsv`).
pub fn render_assembly_hits_tsv(table: &Table) -> String {
    let mut out = String::new();
    for (i, col) in ASSEMBLY_TSV_COLUMNS.iter().enumerate() {
        if i > 0 {
            out.push('\t');
        }
        out.push_str(col);
    }
    out.push('\n');

    let indices: [Option<usize>; 11] =
        std::array::from_fn(|i| table.column_index(ASSEMBLY_TSV_COLUMNS[i]));
    let taxonomy_idx = table.column_index("taxonomy");

    for row in &table.data {
        let cell = |slot: usize| -> &Value {
            indices[slot]
                .and_then(|idx| row.get(idx))
                .unwrap_or(&Value::Null)
        };

        // 0: rank
        let rank = cell(0);
        if !rank.is_null() {
            out.push_str(&py_str(rank));
        }
        out.push('\t');

        // 1: ani
        let ani = cell(1);
        let ani_val = if ani.is_null() {
            0.0
        } else {
            value_as_f64(ani)
        };
        let _ = write!(out, "{ani_val:.2}");
        out.push('\t');

        // 2: relation
        out.push_str(&py_str(cell(2)));
        out.push('\t');

        // 3: shared_rank
        out.push_str(&py_str(cell(3)));
        out.push('\t');

        // 4: accession
        out.push_str(&py_str(cell(4)));
        out.push('\t');

        // 5: genome_id
        out.push_str(&py_str(cell(5)));
        out.push('\t');

        // 6: taxon (server computes it; fallback to last taxonomy value if absent)
        let taxon = cell(6);
        let taxon_str = py_str(taxon);
        if !taxon_str.is_empty() {
            out.push_str(&taxon_str);
        } else if indices[6].is_none() {
            if let Some(tax_val) = taxonomy_idx.and_then(|i| row.get(i)) {
                if let Some(t) = fallback_taxon_from_taxonomy(tax_val) {
                    out.push_str(&t);
                }
            }
        }
        out.push('\t');

        // 7: gtdb_species
        out.push_str(&py_str(cell(7)));
        out.push('\t');

        // 8: taxonomy_source
        out.push_str(&py_str(cell(8)));
        out.push('\t');

        // 9: ncbi_organism_name
        out.push_str(&py_str(cell(9)));
        out.push('\t');

        // 10: ncbi_taxid
        let taxid = cell(10);
        if !taxid.is_null() {
            out.push_str(&py_str(taxid));
        }
        out.push('\n');
    }
    out
}

/// Render the 11-column assembly hits TSV from an [`AssemblyDocument`].
pub fn render_assembly_tsv(doc: &AssemblyDocument) -> String {
    render_assembly_hits_tsv(&doc.table)
}

fn non_empty(opt: Option<&String>) -> Option<&str> {
    opt.map(String::as_str).filter(|s| !s.is_empty())
}

/// One-line human summary of an `/assembly` result (`cli._assignment_summary`).
pub fn render_assembly_summary(doc: &AssemblyDocument) -> String {
    if let Some(assignment) = &doc.assignment {
        let taxon = non_empty(assignment.taxon.as_ref())
            .or_else(|| non_empty(assignment.accession.as_ref()))
            .unwrap_or("the best hit");
        let ani = assignment.ani.unwrap_or(0.0);
        if non_empty(assignment.note.as_ref()).is_some() {
            return format!("assigned to {taxon} at the 95 % species threshold ({ani:.2} % ANI)");
        }
        return format!("assigned to species {taxon} ({ani:.2} % ANI)");
    }
    if let Some(closest) = &doc.closest {
        let taxon = non_empty(closest.taxon.as_ref())
            .or_else(|| non_empty(closest.accession.as_ref()))
            .unwrap_or("the best hit");
        let ani = closest.ani.unwrap_or(0.0);
        let threshold = format_g(doc.thresholds.species);
        return format!(
            "no species-level match (< {threshold} % ANI); closest relative {taxon} ({ani:.2} % ANI)"
        );
    }
    "no hits".to_string()
}

/// Species-threshold caveat note of the best hit, or `None` for GTDB / no lineage (`cli._species_note`).
pub fn assembly_species_note(doc: &AssemblyDocument) -> Option<&str> {
    if let Some(note) = doc
        .assignment
        .as_ref()
        .and_then(|a| non_empty(a.note.as_ref()))
    {
        return Some(note);
    }
    if let Some(note) = doc
        .closest
        .as_ref()
        .and_then(|c| non_empty(c.note.as_ref()))
    {
        return Some(note);
    }
    None
}

/// Full stderr output of `sylphd assembly`: summary line plus optional warning note.
pub fn render_assembly_stderr(doc: &AssemblyDocument) -> String {
    let mut out = render_assembly_summary(doc);
    out.push('\n');
    if let Some(note) = assembly_species_note(doc) {
        let _ = writeln!(out, "{PROG}: warning: {note}");
    }
    out
}

// ---------------------------------------------------------------------------
// Per-contig TSV and summary (per_contig_client.PerContigRun)
// ---------------------------------------------------------------------------

/// Render a `/contigs` table to TSV (`PerContigRun.write_tsv`).
///
/// A contig without hits (`rank` is `null`) emits empty cells for every column after
/// the 4-column prefix (`contig`, `contig_length`, `gc`, `status`). Hit rows format
/// `ani_species_level` as `true`/`false` and default `null` `n_close` to `0`.
pub fn render_contigs_table_tsv(table: &Table) -> String {
    let mut out = String::new();
    for (i, col) in table.columns.iter().enumerate() {
        if i > 0 {
            out.push('\t');
        }
        out.push_str(col);
    }
    out.push('\n');

    let rank_idx = table.column_index("rank");

    for row in &table.data {
        let has_hit = rank_idx
            .and_then(|i| row.get(i))
            .is_some_and(|v| !v.is_null());

        for (i, col) in table.columns.iter().enumerate() {
            if i > 0 {
                out.push('\t');
            }
            let cell = row.get(i).unwrap_or(&Value::Null);
            match col.as_str() {
                "contig" | "status" => out.push_str(&py_str(cell)),
                "contig_length" => {
                    if !cell.is_null() {
                        out.push_str(&py_str(cell));
                    }
                }
                "gc" => {
                    if !cell.is_null() {
                        let _ = write!(out, "{:.2}", value_as_f64(cell));
                    }
                }
                _ if !has_hit => {}
                "rank" | "shared_kmers" | "contig_kmers" => {
                    if !cell.is_null() {
                        out.push_str(&py_str(cell));
                    }
                }
                "ani" | "reference_ani" => {
                    if !cell.is_null() {
                        let _ = write!(out, "{:.2}", value_as_f64(cell));
                    }
                }
                "explained_fraction" => {
                    if !cell.is_null() {
                        let _ = write!(out, "{:.4}", value_as_f64(cell));
                    }
                }
                "ani_species_level" => {
                    out.push_str(if cell.as_bool().unwrap_or(false) {
                        "true"
                    } else {
                        "false"
                    });
                }
                "n_close" => {
                    let n = if cell.is_null() {
                        0
                    } else {
                        value_as_i64(cell)
                    };
                    let _ = write!(out, "{n}");
                }
                "relation" | "shared_rank" | "accession" | "genome_id" | "gtdb_species"
                | "taxon" | "consensus_rank" | "consensus_taxon" => {
                    out.push_str(&py_str(cell));
                }
                other => {
                    out.push_str(&format_metadata_value(other, cell));
                }
            }
        }
        out.push('\n');
    }
    out
}

/// Render a [`ContigsDocument`]'s table to TSV.
pub fn render_contigs_tsv(doc: &ContigsDocument) -> String {
    render_contigs_table_tsv(&doc.table)
}

fn format_consensus_taxon(item: &ConsensusBpEntry) -> String {
    let mut label = item.taxon.clone();
    if let Some(rank) = non_empty(item.rank.as_ref()) {
        let _ = write!(label, " ({rank})");
    }
    format!(
        "{label} {} bp ({:.1} %)",
        format_thousands(item.bp),
        100.0 * item.fraction
    )
}

/// Render the two-line human summary plus caveats of a per-contig run (`PerContigRun.print_summary`).
pub fn render_contigs_summary(doc: &ContigsDocument) -> String {
    let s = &doc.summary;
    let min_length = s.min_contig_length.unwrap_or(DEFAULT_MIN_CONTIG_LENGTH);

    let mut out = format!(
        "per-contig: {} of {} contig(s), {} bp queried",
        s.queried_contigs,
        s.contigs,
        format_thousands(s.queried_bp)
    );

    if let Some(short) = s.skipped_short {
        if short.contigs > 0 {
            let _ = write!(
                out,
                "; {} contigs ({}) below {min_length} bp not classified",
                short.contigs,
                format_bases(short.bp)
            );
        }
    }

    let (few_contigs, few_bp) = match s.skipped_few_kmers {
        Some(few) => (few.contigs, few.bp),
        None => (s.too_few_kmers_contigs, s.too_few_kmers_bp),
    };
    if few_contigs > 0 {
        let _ = write!(
            out,
            "; {few_contigs} contigs ({}) with too few k-mers not classified",
            format_bases(few_bp)
        );
    }

    if s.no_hit_contigs > 0 {
        let _ = write!(out, "; {} without a hit", s.no_hit_contigs);
    }
    out.push('\n');

    if !s.consensus_bp.is_empty() {
        out.push_str("bp by consensus: ");
        for (i, item) in s.consensus_bp.iter().enumerate() {
            if i > 0 {
                out.push_str(", ");
            }
            out.push_str(&format_consensus_taxon(item));
        }
        out.push('\n');
    }

    out.push_str(&render_caveats(&doc.caveats));
    out
}

// ---------------------------------------------------------------------------
// /info and /messages text (cli.cmd_info, _note_messages, _note_usage)
// ---------------------------------------------------------------------------

fn opt_str_or_none(opt: Option<&String>) -> &str {
    match opt {
        Some(s) => s.as_str(),
        None => "None",
    }
}

fn opt_u64_or_none(opt: Option<u64>) -> String {
    match opt {
        Some(n) => n.to_string(),
        None => "None".to_string(),
    }
}

fn ncbi_counts_text(ncbi: &NcbiCounts) -> String {
    let mut text = format!(
        "{} mapped, {} without an assembly accession, {} not in the summaries, {} without a lineage",
        format_thousands(ncbi.mapped),
        format_thousands(ncbi.no_accession),
        format_thousands(ncbi.not_in_summary),
        format_thousands(ncbi.no_lineage),
    );
    if let Some(gtdb) = ncbi.gtdb {
        if gtdb > 0 {
            let _ = write!(text, ", {} with GTDB's NCBI data", format_thousands(gtdb));
        }
    }
    text
}

fn nearest_counts_text(nearest: &NcbiNearestCounts) -> String {
    let ranks: Vec<String> = nearest
        .ranks
        .iter()
        .map(|(rank, count)| format!("{rank} {}", format_thousands(*count)))
        .collect();
    let mut text = format!(
        "nearest NCBI taxon: {} of {} keys without an NCBI record",
        format_thousands(nearest.assigned),
        format_thousands(nearest.keys),
    );
    if !ranks.is_empty() {
        text.push_str(" (");
        text.push_str(&ranks.join(", "));
        if nearest.informal > 0 {
            let _ = write!(
                text,
                "; {} informal, formally named above",
                format_thousands(nearest.informal)
            );
        }
        text.push(')');
    } else if nearest.informal > 0 {
        let _ = write!(
            text,
            ", {} informal, formally named above",
            format_thousands(nearest.informal)
        );
    }
    text
}

fn write_database(out: &mut String, info: &InfoDocument) {
    let db = &info.database;
    let _ = writeln!(out, "database: {}", opt_str_or_none(db.name.as_ref()));
    let _ = writeln!(out, "  size: {}", format_size(db.size.unwrap_or(0)));
    let _ = writeln!(out, "  genomes: {}", opt_u64_or_none(db.genomes));
    let screen_c = match db.screen_c {
        Some(n) => n.to_string(),
        None => "-".to_string(),
    };
    let two_stage = if db.two_stage { "yes" } else { "no" };
    let _ = writeln!(
        out,
        "  c: {}  k: {}  screen_c: {screen_c}  two-stage: {two_stage}",
        opt_u64_or_none(db.c),
        opt_u64_or_none(db.k),
    );
    let sha = non_empty(db.sha256.as_ref()).unwrap_or("-");
    let _ = writeln!(out, "  sha256: {sha}");

    if info.databases.len() > 1 {
        let _ = writeln!(out, "databases: {}", info.databases.len());
        for entry in &info.databases {
            let kind = if entry.two_stage {
                "two-stage"
            } else {
                "plain"
            };
            let _ = writeln!(
                out,
                "  {}: {} ({kind}, c={}, k={}, {} genomes)",
                opt_str_or_none(entry.name.as_ref()),
                opt_str_or_none(entry.file.as_ref()),
                opt_u64_or_none(entry.c),
                opt_u64_or_none(entry.k),
                opt_u64_or_none(entry.genomes),
            );
        }
    }
}

fn release_display(release: Option<&Value>) -> String {
    match release {
        None | Some(Value::Null) => "?".to_string(),
        Some(Value::String(s)) if s.is_empty() => "?".to_string(),
        // Python's ``value or '?'``: any numeric zero and ``False`` are falsy.
        Some(Value::Number(n)) if n.as_f64() == Some(0.0) => "?".to_string(),
        Some(Value::Bool(false)) => "?".to_string(),
        Some(other) => py_str(other),
    }
}

fn write_metadata(out: &mut String, metadata: Option<&MetadataInfo>) {
    let Some(meta) = metadata else {
        out.push_str("metadata: none\n");
        return;
    };
    let release = release_display(meta.release.as_ref());
    let built = non_empty(meta.built.as_ref()).unwrap_or("?");
    let _ = writeln!(
        out,
        "metadata: {} (release {release}, {} rows, built {built})",
        opt_str_or_none(meta.file.as_ref()),
        opt_u64_or_none(meta.rows),
    );
    let _ = writeln!(out, "  columns: {}", meta.columns.len());

    if !meta.taxonomy_sources.is_empty() {
        let _ = writeln!(out, "  taxonomy sources: {}", meta.taxonomy_sources.len());
        for source in &meta.taxonomy_sources {
            let mut details = Vec::new();
            match source.keys {
                Some(keys) => details.push(format!("{} keys", format_thousands(keys))),
                None => details.push("key count unknown".to_string()),
            }
            if source.kind.as_deref() == Some("metadata") {
                details.push("virtual, from -m".to_string());
            }
            if source.has_host {
                details.push("with hosts".to_string());
            }
            if let Some(md5) = non_empty(source.md5.as_ref()) {
                details.push(format!("md5 {md5}"));
            }
            if let Some(ncbi) = &source.ncbi {
                details.push(format!("NCBI: {}", ncbi_counts_text(ncbi)));
            }
            if let Some(nearest) = &source.ncbi_nearest {
                details.push(nearest_counts_text(nearest));
            }
            let _ = writeln!(
                out,
                "    {}: {}",
                opt_str_or_none(source.name.as_ref()),
                details.join(", ")
            );
        }
    }

    if !meta.taxonomy_overlaps.is_empty() {
        let _ = writeln!(out, "  taxonomy overlaps: {}", meta.taxonomy_overlaps.len());
        for pair in &meta.taxonomy_overlaps {
            let names = pair.sources.join(" and ");
            let mut details = vec![format!("{} shared keys", format_thousands(pair.keys))];
            if !pair.examples.is_empty() {
                details.push(format!("examples: {}", pair.examples.join(", ")));
            }
            details.push("the source imported later wins".to_string());
            let _ = writeln!(out, "    {names}: {}", details.join("; "));
        }
    }
}

fn write_limits(out: &mut String, info: &InfoDocument) {
    let limits = &info.limits;
    let _ = writeln!(
        out,
        "versions: sylphd {}, pysylph {}, python {}",
        opt_str_or_none(info.version.as_ref()),
        opt_str_or_none(info.pysylph_version.as_ref()),
        opt_str_or_none(info.python_version.as_ref()),
    );
    let _ = writeln!(
        out,
        "limits: max-jobs {}, max-contigs {}, max-upload {} MB, max-sketches {}",
        opt_u64_or_none(limits.max_jobs),
        opt_u64_or_none(limits.max_contigs),
        opt_u64_or_none(limits.max_upload_mb),
        opt_u64_or_none(limits.max_sketches),
    );
    let rate = match &limits.rate_limit {
        Some(rl) => format!("{}/{}s", rl.count, rl.window_seconds as i64),
        None => "off".to_string(),
    };
    let sequences = if limits.allow_sequences {
        format!("on (max {} MB)", opt_u64_or_none(limits.max_sequence_mb))
    } else {
        "off".to_string()
    };
    let max_queue = match limits.max_queue {
        Some(n) => n.to_string(),
        None => "unlimited".to_string(),
    };
    let _ = writeln!(
        out,
        "web: raw-sequence assembly {sequences}, rate-limit {rate}, max-queue {max_queue}"
    );
}

fn write_usage(out: &mut String, usage: &UsageInfo) {
    if !usage.enabled {
        out.push_str("usage statistics: off\n");
        return;
    }
    let _ = writeln!(
        out,
        "usage statistics: on (policy {}, {}-minute windows, public at /usage)",
        opt_u64_or_none(usage.policy),
        opt_u64_or_none(usage.window_minutes),
    );
}

fn message_body(item: &MessageItem) -> &str {
    non_empty(item.plain.as_ref()).unwrap_or(item.message.as_str())
}

fn write_messages(out: &mut String, messages: &[MessageItem]) {
    if messages.is_empty() {
        return;
    }
    out.push_str("messages:\n");
    for item in messages {
        let _ = writeln!(
            out,
            "  [{}] {}: {}",
            item.level,
            item.title,
            message_body(item)
        );
    }
}

/// Render human-readable `sylphd info` text from `/info` and `/messages` (`cli.cmd_info`).
pub fn render_info(info: &InfoDocument, messages: &[MessageItem]) -> String {
    let mut out = String::new();
    write_database(&mut out, info);
    write_metadata(&mut out, info.metadata.as_ref());
    write_limits(&mut out, info);
    write_usage(&mut out, &info.usage);
    write_messages(&mut out, messages);
    out
}

/// Render `warning` and `danger` operator messages for CLI stderr (`cli._note_messages`).
///
/// `info` messages are skipped (they are for the web page and `sylphd info`).
pub fn render_stderr_messages(messages: &[MessageItem]) -> String {
    let mut out = String::new();
    for item in messages {
        if item.level == "info" {
            continue;
        }
        let label = if item.level == "danger" {
            "DANGER"
        } else {
            "warning"
        };
        let _ = writeln!(
            out,
            "{PROG}: {label}: {}: {}",
            item.title,
            message_body(item)
        );
    }
    out
}

/// Render the one-line usage-statistics stderr note (`cli._note_usage`), if enabled.
pub fn render_usage_note(usage: &UsageInfo, base_url: Option<&str>) -> Option<String> {
    if !usage.enabled {
        return None;
    }
    let where_str = match base_url.filter(|s| !s.is_empty()) {
        Some(url) => format!("{}/usage", url.trim_end_matches('/')),
        None => "GET /usage".to_string(),
    };
    Some(format!(
        "{PROG}: note: this server keeps usage statistics, see {where_str}\n"
    ))
}
