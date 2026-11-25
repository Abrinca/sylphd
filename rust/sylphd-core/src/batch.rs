//! Per-contig batch planning for `POST /contigs`.
//!
//! Greedy fill bounded by three server limits:
//!
//! 1. contig count (`limits.max_contigs`, default 4096);
//! 2. JSON `params` part size (`limits.max_params_bytes`, default 1 MiB,
//!    budgeted to at most 900 KiB);
//! 3. total multipart upload size (`limits.max_upload_mb`), using the exact
//!    sketch and params byte lengths plus fixed multipart framing overhead
//!    (`PART_OVERHEAD` per contig part + `REQUEST_OVERHEAD` per request).
//!
//! Because sketch byte sizes are known before sending, there is no
//! split-in-half retry: a single contig that cannot fit fails immediately.

use std::fmt;

use serde::{Deserialize, Serialize};

use crate::input::PreparedContig;

/// Default maximum contigs per `POST /contigs` request.
pub const DEFAULT_MAX_CONTIGS: usize = 4096;

/// Default server `limits.max_params_bytes` (1 MiB) when not advertised.
pub const DEFAULT_MAX_PARAMS_BYTES: usize = 1024 * 1024;

/// Upper cap on the client's `params` JSON budget (900 KiB).
pub const DEFAULT_PARAMS_BUDGET: usize = 900 * 1024;

/// Fixed multipart framing overhead per sketch part (boundaries, headers, CRLFs),
/// excluding the UTF-8 length of the contig name.
pub const PART_OVERHEAD: usize = 512;

/// Fixed multipart framing overhead per request (outer boundaries, `params` part).
pub const REQUEST_OVERHEAD: usize = 512;

/// Default hits per contig (`per_contig.DEFAULT_TOP`).
pub const DEFAULT_TOP: usize = 1;

/// Default minimum contig-relative ANI in percent (`per_contig.DEFAULT_MIN_ANI`).
pub const DEFAULT_MIN_ANI: f64 = 80.0;

/// Default consensus divergence ratio (`per_contig.DEFAULT_CONSENSUS_RATIO`).
pub const DEFAULT_CONSENSUS_RATIO: f64 = 1.5;

/// Default consensus noise multiplier (`per_contig.DEFAULT_CONSENSUS_Z`).
pub const DEFAULT_CONSENSUS_Z: f64 = 2.0;

/// Contig count limit per request from `/info`'s `limits.max_contigs`.
pub fn chunk_size_for(max_contigs: Option<usize>) -> usize {
    max_contigs
        .filter(|&n| n > 0)
        .unwrap_or(DEFAULT_MAX_CONTIGS)
}

/// Client `params` JSON byte budget from `/info`'s `limits.max_params_bytes`.
///
/// Uses 90% of the advertised limit (at least 1024 bytes), capped at
/// [`DEFAULT_PARAMS_BUDGET`].
pub fn params_budget_for(max_params_bytes: Option<usize>) -> usize {
    let limit = max_params_bytes
        .filter(|&n| n > 0)
        .unwrap_or(DEFAULT_MAX_PARAMS_BYTES);
    DEFAULT_PARAMS_BUDGET.min((limit * 9 / 10).max(1024))
}

/// Plain-value limits governing `POST /contigs` batching (derived from `/info`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BatchLimits {
    /// Maximum contigs per batch (clamped to `>= 1`).
    pub max_contigs: usize,
    /// Maximum UTF-8 bytes of the JSON `params` part per batch.
    pub params_budget: usize,
    /// Maximum estimated multipart body bytes per batch (`0` = unlimited).
    pub max_upload_bytes: usize,
}

impl Default for BatchLimits {
    fn default() -> Self {
        Self::from_server_limits(None, None, None)
    }
}

impl BatchLimits {
    /// Builds [`BatchLimits`] from `/info`'s `limits` numbers.
    pub fn from_server_limits(
        max_contigs: Option<usize>,
        max_params_bytes: Option<usize>,
        max_upload_mb: Option<usize>,
    ) -> Self {
        Self {
            max_contigs: chunk_size_for(max_contigs),
            params_budget: params_budget_for(max_params_bytes),
            max_upload_bytes: max_upload_mb
                .unwrap_or(0)
                .saturating_mul(1024 * 1024),
        }
    }
}

/// One entry in the `params.contigs` JSON array sent to `POST /contigs`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ContigSpec {
    pub name: String,
    pub contig_kmers: u64,
    pub length: u64,
    pub gc: f64,
}

impl From<&PreparedContig> for ContigSpec {
    fn from(contig: &PreparedContig) -> Self {
        Self {
            name: contig.name.clone(),
            contig_kmers: contig.contig_kmers,
            length: contig.length,
            gc: contig.gc,
        }
    }
}

/// Server-side scoring and metadata options carried in every `/contigs` `params` JSON.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ContigsRequestOptions {
    pub top: usize,
    pub min_ani: f64,
    pub consensus_ratio: f64,
    pub consensus_z: f64,
    pub fields: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tax_sources: Option<Vec<String>>,
}

impl Default for ContigsRequestOptions {
    fn default() -> Self {
        Self {
            top: DEFAULT_TOP,
            min_ani: DEFAULT_MIN_ANI,
            consensus_ratio: DEFAULT_CONSENSUS_RATIO,
            consensus_z: DEFAULT_CONSENSUS_Z,
            fields: Vec::new(),
            tax_sources: None,
        }
    }
}

#[derive(Serialize)]
struct ContigsParamsPayload<'a> {
    contigs: &'a [ContigSpec],
    top: usize,
    min_ani: f64,
    consensus_ratio: f64,
    consensus_z: f64,
    fields: &'a [String],
    #[serde(skip_serializing_if = "Option::is_none")]
    tax_sources: Option<&'a [String]>,
}

/// Serializes the exact `params` JSON string sent to `POST /contigs`.
pub fn contigs_params_json(contigs: &[ContigSpec], options: &ContigsRequestOptions) -> String {
    let payload = ContigsParamsPayload {
        contigs,
        top: options.top,
        min_ani: options.min_ani,
        consensus_ratio: options.consensus_ratio,
        consensus_z: options.consensus_z,
        fields: &options.fields,
        tax_sources: options.tax_sources.as_deref(),
    };
    serde_json::to_string(&payload).expect("ContigsParamsPayload is always JSON-serializable")
}

/// Exact UTF-8 byte length one [`ContigSpec`] adds to the compact `params` JSON.
pub fn contig_json_size(spec: &ContigSpec) -> usize {
    serde_json::to_string(spec)
        .expect("ContigSpec is always JSON-serializable")
        .len()
}

/// Error returned when a single contig cannot fit inside the server's limits.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BatchError {
    ParamsTooLarge {
        name: String,
        size: usize,
        budget: usize,
    },
    UploadTooLarge {
        name: String,
        size: usize,
        limit: usize,
    },
}

impl fmt::Display for BatchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            BatchError::ParamsTooLarge { name, size, budget } => write!(
                f,
                "contig {name:?} needs a {size} byte params JSON, over the {budget} byte budget"
            ),
            BatchError::UploadTooLarge { name, size, limit } => {
                let mb = limit / (1024 * 1024);
                if mb > 0 && limit % (1024 * 1024) == 0 {
                    write!(
                        f,
                        "the sketch of contig {name:?} needs a {size} byte request, over the \
                         server's {mb} MB upload limit"
                    )
                } else {
                    write!(
                        f,
                        "the sketch of contig {name:?} needs a {size} byte request, over the \
                         server's {limit} byte upload limit"
                    )
                }
            }
        }
    }
}

impl std::error::Error for BatchError {}

/// One contig entry held inside a [`PlannedBatch`].
#[derive(Debug, Clone, PartialEq)]
pub struct BatchItem<T> {
    pub spec: ContigSpec,
    /// Exact byte length of the contig's `.sylsp` sketch payload.
    pub payload_bytes: usize,
    /// Caller-owned payload (for example, the sketch `Vec<u8>` or [`PreparedContig`]).
    pub item: T,
}

/// One planned `POST /contigs` request batch.
#[derive(Debug, Clone, PartialEq)]
pub struct PlannedBatch<T> {
    pub items: Vec<BatchItem<T>>,
    /// Exact UTF-8 byte length of [`Self::params_json`].
    pub params_bytes: usize,
    /// Sum of exact `.sylsp` payload lengths in this batch.
    pub payload_bytes: usize,
    /// Conservative multipart request size (`params + payloads + names + framing`).
    pub estimated_upload_bytes: usize,
}

impl<T> PlannedBatch<T> {
    /// Serializes the exact `params` JSON for this batch.
    pub fn params_json(&self, options: &ContigsRequestOptions) -> String {
        let specs: Vec<ContigSpec> = self.items.iter().map(|i| i.spec.clone()).collect();
        contigs_params_json(&specs, options)
    }
}

/// Incremental greedy batch planner for `POST /contigs`.
#[derive(Debug, Clone)]
pub struct ContigBatchPlanner<T> {
    limits: BatchLimits,
    base_params_bytes: usize,
    items: Vec<BatchItem<T>>,
    params_bytes: usize,
    payload_bytes: usize,
    names_bytes: usize,
}

impl<T> ContigBatchPlanner<T> {
    pub fn new(limits: BatchLimits, options: &ContigsRequestOptions) -> Self {
        let limits = BatchLimits {
            max_contigs: limits.max_contigs.max(1),
            ..limits
        };
        let base_params_bytes = contigs_params_json(&[], options).len();
        Self {
            limits,
            base_params_bytes,
            items: Vec::new(),
            params_bytes: base_params_bytes,
            payload_bytes: 0,
            names_bytes: 0,
        }
    }

    /// Number of contigs currently buffered in the open batch.
    pub fn current_len(&self) -> usize {
        self.items.len()
    }

    /// Exact UTF-8 byte length of the open batch's `params` JSON.
    pub fn current_params_bytes(&self) -> usize {
        self.params_bytes
    }

    /// Sum of exact `.sylsp` payload lengths currently in the open batch.
    pub fn current_payload_bytes(&self) -> usize {
        self.payload_bytes
    }

    /// Conservative multipart body size of the open batch (`0` when empty).
    pub fn current_estimated_upload_bytes(&self) -> usize {
        if self.items.is_empty() {
            return 0;
        }
        self.params_bytes
            + self.payload_bytes
            + self.names_bytes
            + self.items.len() * PART_OVERHEAD
            + REQUEST_OVERHEAD
    }

    fn take_batch(&mut self) -> PlannedBatch<T> {
        let estimated_upload_bytes = self.current_estimated_upload_bytes();
        let batch = PlannedBatch {
            items: std::mem::take(&mut self.items),
            params_bytes: self.params_bytes,
            payload_bytes: self.payload_bytes,
            estimated_upload_bytes,
        };
        self.params_bytes = self.base_params_bytes;
        self.payload_bytes = 0;
        self.names_bytes = 0;
        batch
    }

    fn push_into_current(&mut self, spec: ContigSpec, spec_size: usize, payload_bytes: usize, item: T) {
        if !self.items.is_empty() {
            self.params_bytes += 1; // `,` separator in compact JSON
        }
        self.params_bytes += spec_size;
        self.payload_bytes += payload_bytes;
        self.names_bytes += spec.name.len();
        self.items.push(BatchItem {
            spec,
            payload_bytes,
            item,
        });
    }

    /// Adds one contig to the planner.
    ///
    /// Returns `Ok(Some(batch))` when either:
    /// - adding this contig would exceed a limit, so the previous batch is
    ///   flushed and this contig begins the next batch; or
    /// - adding this contig fills `max_contigs`, so the completed batch is
    ///   flushed immediately.
    ///
    /// Fails with [`BatchError`] if this single contig exceeds `params_budget`
    /// or `max_upload_bytes` on its own.
    pub fn push(
        &mut self,
        spec: ContigSpec,
        payload_bytes: usize,
        item: T,
    ) -> Result<Option<PlannedBatch<T>>, BatchError> {
        let spec_size = contig_json_size(&spec);
        let name_len = spec.name.len();

        let single_params = self.base_params_bytes + spec_size;
        if single_params > self.limits.params_budget {
            return Err(BatchError::ParamsTooLarge {
                name: spec.name,
                size: single_params,
                budget: self.limits.params_budget,
            });
        }
        let single_upload =
            single_params + payload_bytes + name_len + PART_OVERHEAD + REQUEST_OVERHEAD;
        if self.limits.max_upload_bytes > 0 && single_upload > self.limits.max_upload_bytes {
            return Err(BatchError::UploadTooLarge {
                name: spec.name,
                size: single_upload,
                limit: self.limits.max_upload_bytes,
            });
        }

        if !self.items.is_empty() {
            let next_params = self.params_bytes + 1 + spec_size;
            let next_upload = next_params
                + self.payload_bytes
                + payload_bytes
                + self.names_bytes
                + name_len
                + (self.items.len() + 1) * PART_OVERHEAD
                + REQUEST_OVERHEAD;
            let fits_params = next_params <= self.limits.params_budget;
            let fits_upload =
                self.limits.max_upload_bytes == 0 || next_upload <= self.limits.max_upload_bytes;
            if !fits_params || !fits_upload {
                let flushed = self.take_batch();
                self.push_into_current(spec, spec_size, payload_bytes, item);
                return Ok(Some(flushed));
            }
        }

        self.push_into_current(spec, spec_size, payload_bytes, item);
        if self.items.len() >= self.limits.max_contigs {
            Ok(Some(self.take_batch()))
        } else {
            Ok(None)
        }
    }

    /// Flushes any remaining buffered contigs as the final batch.
    pub fn finish(&mut self) -> Option<PlannedBatch<T>> {
        if self.items.is_empty() {
            None
        } else {
            Some(self.take_batch())
        }
    }
}

/// Plans all contigs into greedy batches in one call.
pub fn plan_contig_batches<T>(
    items: impl IntoIterator<Item = (ContigSpec, usize, T)>,
    limits: BatchLimits,
    options: &ContigsRequestOptions,
) -> Result<Vec<PlannedBatch<T>>, BatchError> {
    let mut planner = ContigBatchPlanner::new(limits, options);
    let mut batches = Vec::new();
    for (spec, payload_bytes, item) in items {
        if let Some(batch) = planner.push(spec, payload_bytes, item)? {
            batches.push(batch);
        }
    }
    if let Some(batch) = planner.finish() {
        batches.push(batch);
    }
    Ok(batches)
}
