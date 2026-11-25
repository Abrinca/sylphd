//! Wire-protocol JSON document model, `api_version` validation, and per-contig run merge.
//!
//! Covers the JSON documents returned by `GET /info`, `GET /messages`,
//! `POST /profile`, `POST /query`, `POST /assembly`, and `POST /contigs`
//! (see `docs/reference/http-api.md`), with no I/O or networking.

use std::collections::{BTreeMap, HashMap};
use std::fmt;
use std::marker::PhantomData;

use serde::de::{MapAccess, Visitor};
use serde::ser::SerializeMap;
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::{Map, Value};

/// Wire-protocol `api_version` supported by this client.
pub const SUPPORTED_API_VERSION: u64 = 1;

/// Default species ANI threshold (percent), matching `sylphd.assembly.SPECIES_ANI`.
pub const DEFAULT_SPECIES_THRESHOLD: f64 = 95.0;

/// Default imprecise-below ANI hint (percent), matching `sylphd.assembly.IMPRECISE_BELOW_ANI`.
pub const DEFAULT_IMPRECISE_BELOW: f64 = 85.0;

/// Default minimum contig length (bp), matching `sylphd.per_contig_client.DEFAULT_MIN_LENGTH`.
pub const DEFAULT_MIN_CONTIG_LENGTH: u64 = 20_000;

/// Default top consensus taxa kept in a finalized per-contig summary.
pub const DEFAULT_CONSENSUS_TOP: usize = 5;

/// Fixed leading 11 columns of `POST /assembly`'s hits TSV (matching `cli._write_hits_tsv`).
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

/// Fixed leading 20 columns of `POST /contigs`'s flat per-hit table (matching `per_contig.TABLE_COLUMNS`).
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
    /// Server/document `api_version` differs from [`SUPPORTED_API_VERSION`].
    UnsupportedApiVersion { server: u64, supported: u64 },
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
            Self::UnsupportedApiVersion { server, supported } => write!(
                f,
                "server API version {server}, this client supports {supported}: update sylphd"
            ),
            Self::MissingApiVersion => write!(
                f,
                "missing or invalid api_version: this client supports API version {SUPPORTED_API_VERSION}: update sylphd"
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

/// Validate a numeric `api_version` against [`SUPPORTED_API_VERSION`].
pub fn check_api_version(version: u64) -> Result<(), DocumentError> {
    if version == SUPPORTED_API_VERSION {
        Ok(())
    } else {
        Err(DocumentError::UnsupportedApiVersion {
            server: version,
            supported: SUPPORTED_API_VERSION,
        })
    }
}

/// Extract and validate `api_version` from a raw JSON document value.
///
/// Checking `api_version` before full deserialization ensures an incompatible
/// server response always reports the version mismatch rather than a schema error.
pub fn check_value_api_version(doc: &Value) -> Result<u64, DocumentError> {
    let version = doc
        .as_object()
        .and_then(|obj| obj.get("api_version"))
        .and_then(Value::as_u64)
        .ok_or(DocumentError::MissingApiVersion)?;
    check_api_version(version)?;
    Ok(version)
}

/// Extract and validate `api_version` directly from a JSON string without
/// round-tripping the whole document through `serde_json::Value` (preserving
/// object key order for `OrderedMap` fields).
pub fn check_str_api_version(json: &str) -> Result<u64, DocumentError> {
    #[derive(Deserialize)]
    struct ApiVersionProbe {
        #[serde(default)]
        api_version: Option<Value>,
    }
    let probe: ApiVersionProbe = serde_json::from_str(json)?;
    let version = probe
        .api_version
        .as_ref()
        .and_then(Value::as_u64)
        .ok_or(DocumentError::MissingApiVersion)?;
    check_api_version(version)?;
    Ok(version)
}

/// Insertion-order-preserving map backed by a `Vec<(K, V)>`, serializing as a JSON object.
///
/// Used where JSON key order is part of the contract (e.g. `contigs` in request order
/// and `ncbi_nearest.ranks` in display order) without needing extra crate dependencies.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrderedMap<K, V>(pub Vec<(K, V)>);

impl<K, V> Default for OrderedMap<K, V> {
    fn default() -> Self {
        Self(Vec::new())
    }
}

impl<K, V> OrderedMap<K, V> {
    pub fn new() -> Self {
        Self(Vec::new())
    }

    pub fn len(&self) -> usize {
        self.0.len()
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    pub fn iter(&self) -> std::slice::Iter<'_, (K, V)> {
        self.0.iter()
    }

    pub fn into_vec(self) -> Vec<(K, V)> {
        self.0
    }
}

impl<K: PartialEq, V> OrderedMap<K, V> {
    pub fn get<Q>(&self, key: &Q) -> Option<&V>
    where
        K: std::borrow::Borrow<Q>,
        Q: PartialEq + ?Sized,
    {
        self.0
            .iter()
            .find(|(k, _)| k.borrow() == key)
            .map(|(_, v)| v)
    }

    pub fn insert(&mut self, key: K, value: V) -> Option<V> {
        if let Some((_, existing)) = self.0.iter_mut().find(|(k, _)| *k == key) {
            Some(std::mem::replace(existing, value))
        } else {
            self.0.push((key, value));
            None
        }
    }
}

impl<K, V> FromIterator<(K, V)> for OrderedMap<K, V> {
    fn from_iter<T: IntoIterator<Item = (K, V)>>(iter: T) -> Self {
        Self(iter.into_iter().collect())
    }
}

impl<K, V> IntoIterator for OrderedMap<K, V> {
    type Item = (K, V);
    type IntoIter = std::vec::IntoIter<(K, V)>;

    fn into_iter(self) -> Self::IntoIter {
        self.0.into_iter()
    }
}

impl<'a, K, V> IntoIterator for &'a OrderedMap<K, V> {
    type Item = &'a (K, V);
    type IntoIter = std::slice::Iter<'a, (K, V)>;

    fn into_iter(self) -> Self::IntoIter {
        self.0.iter()
    }
}

impl<K: Serialize, V: Serialize> Serialize for OrderedMap<K, V> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut map = serializer.serialize_map(Some(self.0.len()))?;
        for (k, v) in &self.0 {
            map.serialize_entry(k, v)?;
        }
        map.end()
    }
}

impl<'de, K, V> Deserialize<'de> for OrderedMap<K, V>
where
    K: Deserialize<'de> + PartialEq,
    V: Deserialize<'de>,
{
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct OrderedMapVisitor<K, V>(PhantomData<(K, V)>);

        impl<'de, K, V> Visitor<'de> for OrderedMapVisitor<K, V>
        where
            K: Deserialize<'de> + PartialEq,
            V: Deserialize<'de>,
        {
            type Value = OrderedMap<K, V>;

            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("a JSON object")
            }

            fn visit_map<A: MapAccess<'de>>(self, mut access: A) -> Result<Self::Value, A::Error> {
                let mut out = OrderedMap(Vec::with_capacity(access.size_hint().unwrap_or(0)));
                while let Some((k, v)) = access.next_entry()? {
                    out.insert(k, v);
                }
                Ok(out)
            }
        }

        deserializer.deserialize_map(OrderedMapVisitor(PhantomData))
    }
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

    /// The same rows with only `columns`, in that order.
    ///
    /// `sylphd` requests extra columns for `--taxprof` (the taxonomy columns of the
    /// requested taxonomies) so that the server renders the profiles; they must not
    /// show up in the TSV, where only the requested `--fields` appear
    /// (`protocol.rows_to_tsv(fields=...)`). A column the document does not have
    /// becomes `null`.
    pub fn select(&self, columns: &[String]) -> Self {
        let indexes: Vec<Option<usize>> = columns
            .iter()
            .map(|column| self.column_index(column))
            .collect();
        let data = self
            .data
            .iter()
            .map(|row| {
                indexes
                    .iter()
                    .map(|index| {
                        index
                            .and_then(|index| row.get(index))
                            .cloned()
                            .unwrap_or(Value::Null)
                    })
                    .collect()
            })
            .collect();
        Self {
            columns: columns.to_vec(),
            data,
        }
    }
}

/// One sample entry in a `/profile` or `/query` document.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SampleEntry {
    pub name: String,
}

/// Result document returned by `POST /profile` and `POST /query`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProfileDocument {
    pub api_version: u64,
    #[serde(default)]
    pub params: Value,
    pub samples: Vec<SampleEntry>,
    pub table: Table,
    #[serde(default)]
    pub caveats: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub taxprof: Option<Value>,
}

impl ProfileDocument {
    pub fn from_value(value: Value) -> Result<Self, DocumentError> {
        check_value_api_version(&value)?;
        Ok(serde_json::from_value(value)?)
    }

    pub fn from_json_str(json: &str) -> Result<Self, DocumentError> {
        check_str_api_version(json)?;
        Ok(serde_json::from_str(json)?)
    }
}

fn default_species_threshold() -> f64 {
    DEFAULT_SPECIES_THRESHOLD
}

fn default_imprecise_below() -> f64 {
    DEFAULT_IMPRECISE_BELOW
}

/// ANI interpretation thresholds included in `/assembly` and `/contigs` documents.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Thresholds {
    #[serde(default = "default_species_threshold")]
    pub species: f64,
    #[serde(default = "default_imprecise_below")]
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
    #[serde(default)]
    pub rank: Option<String>,
    #[serde(default)]
    pub taxon: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub gtdb_taxonomy: Option<String>,
    #[serde(default)]
    pub accession: Option<String>,
    #[serde(default)]
    pub ani: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub taxonomy: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub taxonomy_source: Option<String>,
    #[serde(flatten)]
    pub extra: BTreeMap<String, Value>,
}

/// Result document returned by `POST /assembly`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AssemblyDocument {
    pub api_version: u64,
    #[serde(default)]
    pub query: Value,
    #[serde(default)]
    pub database: Value,
    #[serde(default)]
    pub params: Value,
    #[serde(default)]
    pub thresholds: Thresholds,
    #[serde(default)]
    pub assignment: Option<AssignmentSummary>,
    #[serde(default)]
    pub closest: Option<AssignmentSummary>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub elapsed_ms: Option<f64>,
    pub table: Table,
}

impl AssemblyDocument {
    pub fn from_value(value: Value) -> Result<Self, DocumentError> {
        check_value_api_version(&value)?;
        Ok(serde_json::from_value(value)?)
    }

    pub fn from_json_str(json: &str) -> Result<Self, DocumentError> {
        check_str_api_version(json)?;
        Ok(serde_json::from_str(json)?)
    }
}

/// One entry in a per-contig `summary.consensus_bp` breakdown.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ConsensusBpEntry {
    pub rank: Option<String>,
    pub taxon: String,
    pub bp: u64,
    pub fraction: f64,
}

/// Count and total bases of skipped contigs.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct SkippedBucket {
    #[serde(default)]
    pub contigs: u64,
    #[serde(default)]
    pub bp: u64,
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
    #[serde(default)]
    pub contigs: u64,
    #[serde(default)]
    pub bp: u64,
    #[serde(default)]
    pub queried_contigs: u64,
    #[serde(default)]
    pub queried_bp: u64,
    #[serde(default)]
    pub too_few_kmers_contigs: u64,
    #[serde(default)]
    pub too_few_kmers_bp: u64,
    #[serde(default)]
    pub no_hit_contigs: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub skipped_short: Option<SkippedBucket>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub skipped_few_kmers: Option<SkippedBucket>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub min_contig_length: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub not_classified: Option<NotClassifiedBucket>,
    #[serde(default)]
    pub consensus_bp: Vec<ConsensusBpEntry>,
}

/// Per-contig metadata and summaries stored under `doc.contigs[name]`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ContigMeta {
    pub length: u64,
    pub gc: f64,
    #[serde(default)]
    pub sketch_kmers: Option<u64>,
    #[serde(default)]
    pub contig_kmers: Option<u64>,
    pub status: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    #[serde(default)]
    pub assignment: Option<AssignmentSummary>,
    #[serde(default)]
    pub closest: Option<AssignmentSummary>,
    #[serde(default)]
    pub consensus: Option<Value>,
}

/// Result document returned by `POST /contigs` or produced by [`merge_contigs_run`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ContigsDocument {
    pub api_version: u64,
    #[serde(default)]
    pub params: Value,
    pub table: Table,
    #[serde(default)]
    pub contigs: OrderedMap<String, ContigMeta>,
    #[serde(default)]
    pub summary: ContigsSummary,
    #[serde(default)]
    pub caveats: Vec<String>,
    #[serde(default)]
    pub database: Value,
    #[serde(default)]
    pub thresholds: Thresholds,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub elapsed_ms: Option<f64>,
}

impl ContigsDocument {
    pub fn from_value(value: Value) -> Result<Self, DocumentError> {
        check_value_api_version(&value)?;
        Ok(serde_json::from_value(value)?)
    }

    pub fn from_json_str(json: &str) -> Result<Self, DocumentError> {
        check_str_api_version(json)?;
        Ok(serde_json::from_str(json)?)
    }
}

/// Client-side counters and fallbacks when merging `/contigs` batch responses into a run document.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ContigsRunMergeOptions {
    #[serde(default)]
    pub skipped_short: SkippedBucket,
    #[serde(default)]
    pub skipped_few_kmers: SkippedBucket,
    #[serde(default = "default_min_contig_length")]
    pub min_contig_length: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub not_classified: Option<NotClassifiedBucket>,
    /// Extra metadata fields requested (used to build the header when 0 batches were sent).
    #[serde(default)]
    pub fields: Vec<String>,
    /// Fallback `params` when 0 batches were sent.
    #[serde(default)]
    pub params: Option<Value>,
    /// Fallback `database` when 0 batches were sent.
    #[serde(default)]
    pub database: Option<Value>,
    /// Fallback `thresholds` when 0 batches were sent.
    #[serde(default)]
    pub thresholds: Option<Thresholds>,
    /// Truncation limit for `summary.consensus_bp` (`None` keeps all; default `Some(5)`).
    #[serde(default = "default_top_consensus")]
    pub top_consensus: Option<usize>,
}

fn default_min_contig_length() -> u64 {
    DEFAULT_MIN_CONTIG_LENGTH
}

fn default_top_consensus() -> Option<usize> {
    Some(DEFAULT_CONSENSUS_TOP)
}

impl Default for ContigsRunMergeOptions {
    fn default() -> Self {
        Self {
            skipped_short: SkippedBucket::default(),
            skipped_few_kmers: SkippedBucket::default(),
            min_contig_length: DEFAULT_MIN_CONTIG_LENGTH,
            not_classified: None,
            fields: Vec::new(),
            params: None,
            database: None,
            thresholds: None,
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
    // Matches `per_contig._breakdown`: (-bp, taxon, rank or "")
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
/// Direct port of `sylphd.per_contig.merge_summaries`. Fractions are recomputed
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
/// Direct port of `sylphd.per_contig.finalize_summary` (default `top = Some(5)`).
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
pub fn merge_contigs_run(
    batches: &[ContigsDocument],
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
        for field in &options.fields {
            if !cols.iter().any(|c| c == field) {
                cols.push(field.clone());
            }
        }
        cols
    };

    let mut data: Vec<Vec<Value>> = Vec::new();
    let mut contigs: OrderedMap<String, ContigMeta> = OrderedMap::new();
    let mut caveats: Vec<String> = Vec::new();
    let mut summaries: Vec<ContigsSummary> = Vec::with_capacity(batches.len());
    let mut total_elapsed_ms: Option<f64> = None;

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
        if let Some(ms) = batch.elapsed_ms {
            total_elapsed_ms = Some(total_elapsed_ms.unwrap_or(0.0) + ms);
        }
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
        skipped_few_kmers: Some(SkippedBucket {
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
        .or_else(|| options.params.clone())
        .unwrap_or_else(|| Value::Object(Map::new()));
    let database = batches
        .first()
        .map(|b| b.database.clone())
        .or_else(|| options.database.clone())
        .unwrap_or_else(|| Value::Object(Map::new()));
    let thresholds = batches
        .first()
        .map(|b| b.thresholds.clone())
        .or_else(|| options.thresholds.clone())
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
        elapsed_ms: total_elapsed_ms.map(|ms| round_half_even(ms, 1)),
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
    /// Parse a result JSON document and detect whether it is `/profile`|`/query`,
    /// `/assembly`, or `/contigs`.
    pub fn from_value(value: Value) -> Result<Self, DocumentError> {
        check_value_api_version(&value)?;
        let obj = value.as_object().ok_or_else(|| {
            DocumentError::InvalidDocument("document must be a JSON object".into())
        })?;
        if obj.contains_key("samples") {
            Ok(Self::Profile(serde_json::from_value(value)?))
        } else if obj.contains_key("contigs") && obj.contains_key("summary") {
            Ok(Self::Contigs(serde_json::from_value(value)?))
        } else if obj.contains_key("query") && obj.contains_key("table") {
            Ok(Self::Assembly(serde_json::from_value(value)?))
        } else {
            Err(DocumentError::InvalidDocument(
                "unrecognized sylphd result document (expected profile/query, assembly, or contigs)"
                    .into(),
            ))
        }
    }

    pub fn from_json_str(json: &str) -> Result<Self, DocumentError> {
        check_str_api_version(json)?;
        #[derive(Deserialize)]
        struct KindProbe {
            #[serde(default)]
            samples: Option<Value>,
            #[serde(default)]
            contigs: Option<Value>,
            #[serde(default)]
            summary: Option<Value>,
            #[serde(default)]
            query: Option<Value>,
            #[serde(default)]
            table: Option<Value>,
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
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub size: Option<u64>,
    #[serde(default)]
    pub sha256: Option<String>,
    #[serde(default)]
    pub genomes: Option<u64>,
    #[serde(default)]
    pub c: Option<u64>,
    #[serde(default)]
    pub k: Option<u64>,
    #[serde(default)]
    pub screen_c: Option<u64>,
    #[serde(default)]
    pub two_stage: bool,
}

/// One per-database entry in `GET /info`'s `databases` list.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct DatabaseEntry {
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub file: Option<String>,
    #[serde(default)]
    pub kind: Option<String>,
    #[serde(default)]
    pub size: Option<u64>,
    #[serde(default)]
    pub sha256: Option<String>,
    #[serde(default)]
    pub genomes: Option<u64>,
    #[serde(default)]
    pub c: Option<u64>,
    #[serde(default)]
    pub k: Option<u64>,
    #[serde(default)]
    pub screen_c: Option<u64>,
    #[serde(default)]
    pub two_stage: bool,
    #[serde(default)]
    pub gtdb: bool,
}

/// Per-source NCBI mapping counts from `setup --ncbi-taxonomy`.
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
    #[serde(default)]
    pub gtdb: Option<u64>,
}

/// Per-source nearest NCBI taxon counts from `setup --ncbi-taxonomy`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct NcbiNearestCounts {
    #[serde(default)]
    pub keys: u64,
    #[serde(default)]
    pub assigned: u64,
    #[serde(default)]
    pub informal: u64,
    #[serde(default)]
    pub ranks: OrderedMap<String, u64>,
}

/// One taxonomy source entry in `GET /info`'s `metadata.taxonomy_sources`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct TaxonomySourceInfo {
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub kind: Option<String>,
    #[serde(default)]
    pub origin: Option<String>,
    #[serde(default)]
    pub md5: Option<String>,
    #[serde(default)]
    pub keys: Option<u64>,
    #[serde(default)]
    pub has_host: bool,
    #[serde(default)]
    pub ncbi: Option<NcbiCounts>,
    #[serde(default)]
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
    #[serde(default)]
    pub file: Option<String>,
    #[serde(default)]
    pub size: Option<u64>,
    #[serde(default)]
    pub columns: Vec<String>,
    #[serde(default)]
    pub rows: Option<u64>,
    #[serde(default)]
    pub release: Option<Value>,
    #[serde(default)]
    pub built: Option<String>,
    #[serde(default)]
    pub format_version: Option<Value>,
    #[serde(default)]
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
    #[serde(default)]
    pub max_jobs: Option<u64>,
    #[serde(default)]
    pub max_upload_mb: Option<u64>,
    #[serde(default)]
    pub max_sketches: Option<u64>,
    #[serde(default)]
    pub max_contigs: Option<u64>,
    #[serde(default)]
    pub max_params_bytes: Option<u64>,
    #[serde(default)]
    pub allow_sequences: bool,
    #[serde(default)]
    pub max_sequence_mb: Option<u64>,
    #[serde(default)]
    pub rate_limit: Option<RateLimitInfo>,
    #[serde(default)]
    pub max_queue: Option<u64>,
}

/// Usage statistics status in `GET /info`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct UsageInfo {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default)]
    pub policy: Option<u64>,
    #[serde(default)]
    pub window_minutes: Option<u64>,
}

/// Server document returned by `GET /info`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct InfoDocument {
    pub api_version: u64,
    #[serde(default)]
    pub service: Option<String>,
    #[serde(default)]
    pub version: Option<String>,
    #[serde(default)]
    pub pysylph_version: Option<String>,
    #[serde(default)]
    pub python_version: Option<String>,
    #[serde(default)]
    pub database: DatabaseInfo,
    #[serde(default)]
    pub databases: Vec<DatabaseEntry>,
    #[serde(default)]
    pub metadata: Option<MetadataInfo>,
    #[serde(default)]
    pub limits: LimitsInfo,
    #[serde(default)]
    pub usage: UsageInfo,
    #[serde(default)]
    pub endpoints: Vec<String>,
}

impl InfoDocument {
    pub fn from_value(value: Value) -> Result<Self, DocumentError> {
        check_value_api_version(&value)?;
        Ok(serde_json::from_value(value)?)
    }

    pub fn from_json_str(json: &str) -> Result<Self, DocumentError> {
        check_str_api_version(json)?;
        Ok(serde_json::from_str(json)?)
    }
}
