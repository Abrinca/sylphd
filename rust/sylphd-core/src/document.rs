//! Wire-protocol JSON document model, `api_version` validation, and per-contig run merge.
//!
//! Covers the JSON documents returned by `GET /info`, `GET /messages`,
//! `POST /profile`, `POST /query`, `POST /assembly`, and `POST /contigs`
//! (see `docs/reference/http-api.md`), with no I/O or networking.

use std::collections::HashMap;
use std::fmt;

use indexmap::IndexMap;
use serde::de::{DeserializeOwned, IgnoredAny};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::batch::ContigsRequestOptions;
pub use crate::input::Skipped;
use crate::input::DEFAULT_MIN_CONTIG_LENGTH;

/// Wire-protocol `api_version` supported by this client.
pub const SUPPORTED_API_VERSION: u64 = 1;

/// Default species ANI threshold (percent), (the server's `INTERPRETATION_THRESHOLDS`).
pub const DEFAULT_SPECIES_THRESHOLD: f64 = 95.0;

/// Default imprecise-below ANI hint (percent), (the server's `INTERPRETATION_THRESHOLDS`).
pub const DEFAULT_IMPRECISE_BELOW: f64 = 85.0;

/// Default top consensus taxa kept in a finalized per-contig summary.
pub const DEFAULT_CONSENSUS_TOP: usize = 5;

/// Fixed leading 11 columns of `POST /assembly`'s hits TSV .
pub const ASSEMBLY_TSV_COLUMNS: [&str; 11] = [
    "rank",
    "ani",
    "relation",
    "shared_rank",
    "accession",
    "genome_id",
    "taxon",
    "gtdb_species",
    "taxonomy_source",
    "ncbi_organism_name",
    "ncbi_taxid",
];

/// Fixed leading 20 columns of `POST /contigs`'s flat per-hit table .
pub const PER_CONTIG_COLUMNS: [&str; 20] = [
    "contig",
    "contig_length",
    "gc",
    "status",
    "rank",
    "ani",
    "shared_kmers",
    "contig_kmers",
    "explained_fraction",
    "reference_ani",
    "relation",
    "shared_rank",
    "ani_species_level",
    "accession",
    "genome_id",
    "gtdb_species",
    "taxon",
    "n_close",
    "consensus_rank",
    "consensus_taxon",
];

/// Errors raised while parsing or validating wire-protocol JSON documents.
#[derive(Debug)]
pub enum DocumentError {
    /// `api_version` differs from [`SUPPORTED_API_VERSION`]; `source` says
    /// whose it is (`"server"` or `"document"`).
    UnsupportedApiVersion {
        found: u64,
        supported: u64,
        source: &'static str,
    },
    /// Document is missing an integer `api_version`.
    MissingApiVersion,
    /// Document structure is invalid or inconsistent.
    InvalidDocument(String),
    /// JSON parsing or deserialization error.
    Json(serde_json::Error),
}

impl fmt::Display for DocumentError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnsupportedApiVersion {
                found,
                supported,
                source,
            } if found > supported => write!(
                f,
                "{source} API version {found} is newer than this client's ({supported}): update sylphd"
            ),
            Self::UnsupportedApiVersion {
                found,
                supported,
                source,
            } => write!(
                f,
                "{source} API version {found} is older than this client's ({supported}): \
                 the {source} predates this sylphd"
            ),
            Self::MissingApiVersion => write!(
                f,
                "missing or invalid api_version: this client supports API version {SUPPORTED_API_VERSION}"
            ),
            Self::InvalidDocument(msg) => f.write_str(msg),
            Self::Json(err) => write!(f, "invalid JSON document: {err}"),
        }
    }
}

impl std::error::Error for DocumentError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Json(err) => Some(err),
            _ => None,
        }
    }
}

impl From<serde_json::Error> for DocumentError {
    fn from(err: serde_json::Error) -> Self {
        Self::Json(err)
    }
}

/// Validates a numeric `api_version` against [`SUPPORTED_API_VERSION`]; `source`
/// names whose version it is in the error (`"server"` or `"document"`).
pub fn check_api_version_of(version: u64, source: &'static str) -> Result<(), DocumentError> {
    if version == SUPPORTED_API_VERSION {
        Ok(())
    } else {
        Err(DocumentError::UnsupportedApiVersion {
            found: version,
            supported: SUPPORTED_API_VERSION,
            source,
        })
    }
}

/// [`check_api_version_of`] for a server's version.
pub fn check_api_version(version: u64) -> Result<(), DocumentError> {
    check_api_version_of(version, "server")
}

/// Reads and validates `api_version` from a JSON text without deserializing
/// the rest, so an incompatible server always reports the version mismatch
/// rather than a schema error.
pub fn check_str_api_version(json: &str, source: &'static str) -> Result<u64, DocumentError> {
    #[derive(Deserialize)]
    struct ApiVersionProbe {
        api_version: Option<Value>,
    }
    let probe: ApiVersionProbe = serde_json::from_str(json)?;
    let version = probe
        .api_version
        .as_ref()
        .and_then(Value::as_u64)
        .ok_or(DocumentError::MissingApiVersion)?;
    check_api_version_of(version, source)?;
    Ok(version)
}

/// Parses a JSON array of `/contigs` batch documents, checking each `api_version`.
pub fn parse_contigs_batches(json: &str) -> Result<Vec<ContigsDocument>, DocumentError> {
    let items: Vec<Box<serde_json::value::RawValue>> = serde_json::from_str(json)?;
    items.iter().map(|item| parse(item.get())).collect()
}

/// Parses a server response (`/info`, `/profile`, `/query`, `/assembly`,
/// `/contigs`) after checking its `api_version`.
pub fn parse<T: DeserializeOwned>(json: &str) -> Result<T, DocumentError> {
    check_str_api_version(json, "server")?;
    Ok(serde_json::from_str(json)?)
}

/// Standard JSON table (`{"columns": [...], "data": [[...], ...]}` — pandas `orient="split"`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Table {
    pub columns: Vec<String>,
    pub data: Vec<Vec<Value>>,
}

impl Table {
    pub fn empty(columns: Vec<String>) -> Self {
        Self {
            columns,
            data: Vec::new(),
        }
    }

    pub fn column_index(&self, name: &str) -> Option<usize> {
        self.columns.iter().position(|c| c == name)
    }
}

/// One sample entry in a `/profile` or `/query` document.
///
/// `kmers`, `mean_read_length` and `paired` come from the uploaded sketch header;
/// `reads` and `bases` are the values the client sent (`None` when it did not).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SampleEntry {
    pub name: String,
    #[serde(default)]
    pub kmers: Option<u64>,
    #[serde(default)]
    pub mean_read_length: Option<f64>,
    #[serde(default)]
    pub paired: Option<bool>,
    #[serde(default)]
    pub reads: Option<u64>,
    #[serde(default)]
    pub bases: Option<u64>,
}

/// Result document returned by `POST /profile` and `POST /query`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProfileDocument {
    pub api_version: u64,
    pub params: Value,
    pub samples: Vec<SampleEntry>,
    pub table: Table,
    pub caveats: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub taxprof: Option<Value>,
}

/// ANI interpretation thresholds included in `/assembly` and `/contigs` documents.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Thresholds {
    pub species: f64,
    pub imprecise_below: f64,
}

impl Default for Thresholds {
    fn default() -> Self {
        Self {
            species: DEFAULT_SPECIES_THRESHOLD,
            imprecise_below: DEFAULT_IMPRECISE_BELOW,
        }
    }
}

/// Species assignment or closest-relative summary on `/assembly` and `/contigs` results.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AssignmentSummary {
    pub rank: Option<String>,
    pub taxon: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub gtdb_taxonomy: Option<String>,
    pub accession: Option<String>,
    pub ani: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub taxonomy: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub taxonomy_source: Option<String>,
    #[serde(flatten)]
    pub extra: std::collections::BTreeMap<String, Value>,
}

/// Result document returned by `POST /assembly`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AssemblyDocument {
    pub api_version: u64,
    pub query: Value,
    pub database: Value,
    pub params: Value,
    pub thresholds: Thresholds,
    pub assignment: Option<AssignmentSummary>,
    pub closest: Option<AssignmentSummary>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub elapsed_ms: Option<f64>,
    pub table: Table,
}

/// One entry in a per-contig `summary.consensus_bp` breakdown.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ConsensusBpEntry {
    pub rank: Option<String>,
    pub taxon: String,
    pub bp: u64,
    pub fraction: f64,
}

/// Browser-side cap bucket for contigs beyond the browser's classification limit.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct NotClassifiedBucket {
    #[serde(default)]
    pub contigs: u64,
    #[serde(default)]
    pub bp: u64,
    #[serde(default)]
    pub limit: u64,
}

/// Summary block of a single `/contigs` response or a merged per-contig run.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ContigsSummary {
    pub contigs: u64,
    pub bp: u64,
    pub queried_contigs: u64,
    pub queried_bp: u64,
    pub too_few_kmers_contigs: u64,
    pub too_few_kmers_bp: u64,
    pub no_hit_contigs: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub skipped_short: Option<Skipped>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub skipped_few_kmers: Option<Skipped>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub min_contig_length: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub not_classified: Option<NotClassifiedBucket>,
    pub consensus_bp: Vec<ConsensusBpEntry>,
}

/// Per-contig metadata and summaries stored under `doc.contigs[name]`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ContigMeta {
    pub length: u64,
    pub gc: f64,
    pub sketch_kmers: Option<u64>,
    pub contig_kmers: Option<u64>,
    pub status: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    pub assignment: Option<AssignmentSummary>,
    pub closest: Option<AssignmentSummary>,
    pub consensus: Option<Value>,
}

/// Result document returned by `POST /contigs` or produced by [`merge_contigs_run`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ContigsDocument {
    pub api_version: u64,
    pub params: Value,
    pub table: Table,
    pub contigs: IndexMap<String, ContigMeta>,
    pub summary: ContigsSummary,
    pub caveats: Vec<String>,
    pub database: Value,
    pub thresholds: Thresholds,
    pub elapsed_ms: f64,
}

/// Client-side counters of a per-contig run, merged with the server's batch summaries.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct ContigsRunMergeOptions {
    pub skipped_short: Skipped,
    pub skipped_few_kmers: Skipped,
    pub min_contig_length: u64,
    pub not_classified: Option<NotClassifiedBucket>,
    /// Fallback `database` when 0 batches were sent.
    pub database: Option<Value>,
    /// Truncation limit for `summary.consensus_bp` (`None` keeps all).
    pub top_consensus: Option<usize>,
}

impl Default for ContigsRunMergeOptions {
    fn default() -> Self {
        Self {
            skipped_short: Skipped::default(),
            skipped_few_kmers: Skipped::default(),
            min_contig_length: DEFAULT_MIN_CONTIG_LENGTH,
            not_classified: None,
            database: None,
            top_consensus: Some(DEFAULT_CONSENSUS_TOP),
        }
    }
}

/// Round to `digits` decimals using IEEE round-half-to-even (matching Python's `round(x, digits)`).
pub(crate) fn round_half_even(value: f64, digits: usize) -> f64 {
    if !value.is_finite() {
        return value;
    }
    let rounded = format!("{value:.digits$}").parse::<f64>().unwrap_or(value);
    if rounded == 0.0 {
        0.0
    } else {
        rounded
    }
}

fn breakdown(
    bases: HashMap<(Option<String>, String), u64>,
    queried_bp: u64,
    top: Option<usize>,
) -> Vec<ConsensusBpEntry> {
    let mut items: Vec<ConsensusBpEntry> = bases
        .into_iter()
        .map(|((rank, taxon), bp)| ConsensusBpEntry {
            rank,
            taxon,
            bp,
            fraction: if queried_bp > 0 {
                round_half_even(bp as f64 / queried_bp as f64, 4)
            } else {
                0.0
            },
        })
        .collect();
    // Largest first, then by taxon and rank.
    items.sort_by(|a, b| {
        b.bp.cmp(&a.bp)
            .then_with(|| a.taxon.cmp(&b.taxon))
            .then_with(|| {
                a.rank
                    .as_deref()
                    .unwrap_or("")
                    .cmp(b.rank.as_deref().unwrap_or(""))
            })
    });
    if let Some(limit) = top {
        items.truncate(limit);
    }
    items
}

fn normalize_taxon_key(rank: &Option<String>, taxon: &str) -> (Option<String>, String) {
    let taxon = if taxon.is_empty() {
        "unassigned".to_string()
    } else {
        taxon.to_string()
    };
    (rank.clone(), taxon)
}

/// Add up per-request `/contigs` summaries, keeping every consensus taxon.
///
/// Fractions are recomputed
/// over the summed `queried_bp`.
pub fn merge_summaries(parts: &[ContigsSummary]) -> ContigsSummary {
    let mut totals = ContigsSummary::default();
    let mut bases: HashMap<(Option<String>, String), u64> = HashMap::new();
    for part in parts {
        totals.contigs += part.contigs;
        totals.bp += part.bp;
        totals.queried_contigs += part.queried_contigs;
        totals.queried_bp += part.queried_bp;
        totals.too_few_kmers_contigs += part.too_few_kmers_contigs;
        totals.too_few_kmers_bp += part.too_few_kmers_bp;
        totals.no_hit_contigs += part.no_hit_contigs;
        for item in &part.consensus_bp {
            let key = normalize_taxon_key(&item.rank, &item.taxon);
            *bases.entry(key).or_insert(0) += item.bp;
        }
    }
    totals.consensus_bp = breakdown(bases, totals.queried_bp, None);
    totals
}

/// Recompute `consensus_bp` fractions over `summary.queried_bp` and keep the top `top` taxa.
///
/// Typically `top = Some(5)`.
pub fn finalize_summary(summary: &ContigsSummary, top: Option<usize>) -> ContigsSummary {
    let mut bases: HashMap<(Option<String>, String), u64> = HashMap::new();
    for item in &summary.consensus_bp {
        let key = normalize_taxon_key(&item.rank, &item.taxon);
        *bases.entry(key).or_insert(0) += item.bp;
    }
    let mut out = summary.clone();
    out.consensus_bp = breakdown(bases, summary.queried_bp, top);
    out
}

/// Merge zero or more `/contigs` batch documents plus client-side skip counters into
/// one run document of the same shape.
///
/// `request` is the options the batches were requested with: its fields and a
/// serialisation shaped like a server batch's `params` stand in when no batch was sent.
pub fn merge_contigs_run(
    batches: &[ContigsDocument],
    request: &ContigsRequestOptions,
    options: &ContigsRunMergeOptions,
) -> Result<ContigsDocument, DocumentError> {
    for batch in batches {
        check_api_version(batch.api_version)?;
    }

    let columns: Vec<String> = if let Some(first) = batches.first() {
        for (i, batch) in batches.iter().enumerate().skip(1) {
            if batch.table.columns != first.table.columns {
                return Err(DocumentError::InvalidDocument(format!(
                    "batch {i} table columns do not match batch 0"
                )));
            }
        }
        first.table.columns.clone()
    } else {
        let mut cols: Vec<String> = PER_CONTIG_COLUMNS
            .iter()
            .map(|s| (*s).to_string())
            .collect();
        for field in &request.fields {
            if !cols.iter().any(|c| c == field) {
                cols.push(field.clone());
            }
        }
        cols
    };

    let mut data: Vec<Vec<Value>> = Vec::new();
    let mut contigs: IndexMap<String, ContigMeta> = IndexMap::new();
    let mut caveats: Vec<String> = Vec::new();
    let mut summaries: Vec<ContigsSummary> = Vec::with_capacity(batches.len());
    let mut total_elapsed_ms = 0.0;

    for batch in batches {
        data.extend(batch.table.data.iter().cloned());
        for (name, meta) in &batch.contigs {
            contigs.insert(name.clone(), meta.clone());
        }
        for note in &batch.caveats {
            if !note.is_empty() && !caveats.iter().any(|existing| existing == note) {
                caveats.push(note.clone());
            }
        }
        summaries.push(batch.summary.clone());
        total_elapsed_ms += batch.elapsed_ms;
    }

    let merged = merge_summaries(&summaries);
    let finalized = finalize_summary(&merged, options.top_consensus);

    let few_contigs = options.skipped_few_kmers.contigs + merged.too_few_kmers_contigs;
    let few_bp = options.skipped_few_kmers.bp + merged.too_few_kmers_bp;
    let not_cls_contigs = options.not_classified.map_or(0, |b| b.contigs);
    let not_cls_bp = options.not_classified.map_or(0, |b| b.bp);

    let summary = ContigsSummary {
        contigs: merged.contigs
            + options.skipped_short.contigs
            + options.skipped_few_kmers.contigs
            + not_cls_contigs,
        bp: merged.bp + options.skipped_short.bp + options.skipped_few_kmers.bp + not_cls_bp,
        queried_contigs: merged.queried_contigs,
        queried_bp: merged.queried_bp,
        too_few_kmers_contigs: few_contigs,
        too_few_kmers_bp: few_bp,
        no_hit_contigs: merged.no_hit_contigs,
        skipped_short: Some(options.skipped_short),
        skipped_few_kmers: Some(Skipped {
            contigs: few_contigs,
            bp: few_bp,
        }),
        min_contig_length: Some(options.min_contig_length),
        not_classified: options.not_classified,
        consensus_bp: finalized.consensus_bp,
    };

    let params = batches
        .first()
        .map(|b| b.params.clone())
        .unwrap_or_else(|| request.params_value());
    let database = batches
        .first()
        .map(|b| b.database.clone())
        .or_else(|| options.database.clone())
        .unwrap_or_else(|| Value::Object(Map::new()));
    let thresholds = batches
        .first()
        .map(|b| b.thresholds.clone())
        .unwrap_or_default();

    Ok(ContigsDocument {
        api_version: SUPPORTED_API_VERSION,
        params,
        table: Table { columns, data },
        contigs,
        summary,
        caveats,
        database,
        thresholds,
        elapsed_ms: round_half_even(total_elapsed_ms, 1),
    })
}

/// Any of the three result document kinds (for offline `sylphd render`).
///
/// The variants differ by a few hundred bytes, and a document is parsed, rendered once
/// and dropped: boxing the large one would only add indirection.
#[allow(clippy::large_enum_variant)]
#[derive(Debug, Clone, PartialEq)]
pub enum ResultDocument {
    Profile(ProfileDocument),
    Assembly(AssemblyDocument),
    Contigs(ContigsDocument),
}

impl ResultDocument {
    /// Parses a saved result document, detecting whether it is a `/profile` or
    /// `/query`, an `/assembly` or a `/contigs` document.
    pub fn parse(json: &str) -> Result<Self, DocumentError> {
        check_str_api_version(json, "document")?;
        #[derive(Deserialize)]
        struct KindProbe {
            samples: Option<IgnoredAny>,
            contigs: Option<IgnoredAny>,
            summary: Option<IgnoredAny>,
            query: Option<IgnoredAny>,
            table: Option<IgnoredAny>,
        }
        let probe: KindProbe = serde_json::from_str(json)?;
        if probe.samples.is_some() {
            Ok(Self::Profile(serde_json::from_str(json)?))
        } else if probe.contigs.is_some() && probe.summary.is_some() {
            Ok(Self::Contigs(serde_json::from_str(json)?))
        } else if probe.query.is_some() && probe.table.is_some() {
            Ok(Self::Assembly(serde_json::from_str(json)?))
        } else {
            Err(DocumentError::InvalidDocument(
                "unrecognized sylphd result document (expected profile/query, assembly, or contigs)"
                    .into(),
            ))
        }
    }
}

/// One operator message item returned by `GET /messages`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MessageItem {
    pub level: String,
    pub title: String,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub plain: Option<String>,
}

/// Response body of `GET /messages`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct MessagesResponse {
    #[serde(default)]
    pub messages: Vec<MessageItem>,
}

/// Pooled database summary in `GET /info`.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct DatabaseInfo {
    pub name: Option<String>,
    pub size: Option<u64>,
    pub sha256: Option<String>,
    pub genomes: Option<u64>,
    pub c: Option<u64>,
    pub k: Option<u64>,
    pub screen_c: Option<u64>,
    #[serde(default)]
    pub two_stage: bool,
}

/// One per-database entry in `GET /info`'s `databases` list.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct DatabaseEntry {
    pub name: Option<String>,
    pub file: Option<String>,
    pub kind: Option<String>,
    pub size: Option<u64>,
    pub sha256: Option<String>,
    pub genomes: Option<u64>,
    pub c: Option<u64>,
    pub k: Option<u64>,
    pub screen_c: Option<u64>,
    #[serde(default)]
    pub two_stage: bool,
    #[serde(default)]
    pub gtdb: bool,
}

/// Per-source NCBI mapping counts from `setup --taxonomy-db`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct NcbiCounts {
    #[serde(default)]
    pub mapped: u64,
    #[serde(default)]
    pub no_accession: u64,
    #[serde(default)]
    pub not_in_summary: u64,
    #[serde(default)]
    pub no_lineage: u64,
    pub gtdb: Option<u64>,
}

/// Per-source nearest NCBI taxon counts from `setup --taxonomy-db`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct NcbiNearestCounts {
    #[serde(default)]
    pub keys: u64,
    #[serde(default)]
    pub assigned: u64,
    #[serde(default)]
    pub informal: u64,
    #[serde(default)]
    pub ranks: IndexMap<String, u64>,
}

/// One taxonomy source entry in `GET /info`'s `metadata.taxonomy_sources`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct TaxonomySourceInfo {
    pub name: Option<String>,
    pub kind: Option<String>,
    pub origin: Option<String>,
    pub md5: Option<String>,
    pub keys: Option<u64>,
    #[serde(default)]
    pub has_host: bool,
    pub ncbi: Option<NcbiCounts>,
    pub ncbi_nearest: Option<NcbiNearestCounts>,
}

/// One overlap entry in `GET /info`'s `metadata.taxonomy_overlaps`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct TaxonomyOverlapInfo {
    #[serde(default)]
    pub sources: Vec<String>,
    #[serde(default)]
    pub keys: u64,
    #[serde(default)]
    pub examples: Vec<String>,
}

/// Metadata summary in `GET /info`.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct MetadataInfo {
    pub file: Option<String>,
    pub size: Option<u64>,
    #[serde(default)]
    pub columns: Vec<String>,
    pub rows: Option<u64>,
    pub release: Option<Value>,
    pub built: Option<String>,
    pub format_version: Option<Value>,
    pub reps_only: Option<bool>,
    #[serde(default)]
    pub taxonomy_sources: Vec<TaxonomySourceInfo>,
    #[serde(default)]
    pub taxonomy_overlaps: Vec<TaxonomyOverlapInfo>,
    #[serde(default)]
    pub taxonomies: Vec<String>,
}

/// Rate-limit specification in `GET /info`.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct RateLimitInfo {
    pub count: u64,
    pub window_seconds: f64,
}

/// Server limits in `GET /info`.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct LimitsInfo {
    pub max_jobs: Option<u64>,
    pub max_upload_mb: Option<u64>,
    pub max_sketches: Option<u64>,
    pub max_contigs: Option<u64>,
    pub max_params_bytes: Option<u64>,
    #[serde(default)]
    pub allow_sequences: bool,
    pub max_sequence_mb: Option<u64>,
    pub rate_limit: Option<RateLimitInfo>,
    pub max_queue: Option<u64>,
}

/// Usage statistics status in `GET /info`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct UsageInfo {
    #[serde(default)]
    pub enabled: bool,
    pub policy: Option<u64>,
    pub window_minutes: Option<u64>,
}

/// Server document returned by `GET /info`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct InfoDocument {
    pub api_version: u64,
    pub service: Option<String>,
    pub version: Option<String>,
    pub pysylph_version: Option<String>,
    pub python_version: Option<String>,
    #[serde(default)]
    pub database: DatabaseInfo,
    #[serde(default)]
    pub databases: Vec<DatabaseEntry>,
    pub metadata: Option<MetadataInfo>,
    #[serde(default)]
    pub limits: LimitsInfo,
    #[serde(default)]
    pub usage: UsageInfo,
    #[serde(default)]
    pub endpoints: Vec<String>,
}

