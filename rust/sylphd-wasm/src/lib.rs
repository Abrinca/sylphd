//! Browser read sketching for sylphd, on top of the sylph library core.
//!
//! The k-mer seeding and the streaming sketchers are sylph's own
//! (`sylph::seeding`, `sylph::sketchers`, the code behind pysylph); this crate
//! adds the chunk-safe parser, the gzip decoder and a streaming wasm-bindgen
//! API:
//!
//! ```js
//! const s = new WasmSketcher(200, 31, true);
//! for await (const chunk of decompressedFastqChunks) s.push(chunk);
//! const sylsp = s.finish("sample");   // Uint8Array, a .sylsp file
//! ```

pub mod gzip;

use sylphd_core::batch::{
    plan_contig_batches, BatchLimits, ContigSpec, ContigsRequestOptions,
};
use sylphd_core::contig::{self, ContigRecord, ContigSketcher};
use sylphd_core::document::{
    self, AssemblyDocument, ContigsDocument, ContigsRunMergeOptions, ProfileDocument, Table,
};
use sylphd_core::input::{ContigNameTracker, DEFAULT_MIN_CONTIG_LENGTH};
use sylphd_core::render;
use sylphd_core::sketchcheck;
use sylphd_core::sketch;
use sylphd_core::stream::StreamSketcher;
use sylphd_core::Seeding;
use wasm_bindgen::prelude::*;

use gzip::GzipDecoder;

/// `scalable_cuckoo_filter`'s builder (used by sylph's `approx-dedup` feature)
/// calls `rand::thread_rng()` and discards the result; the filter is then given
/// a seeded RNG. Nothing depends on these bytes, so no entropy source is needed.
#[cfg(target_arch = "wasm32")]
fn unused_getrandom(buf: &mut [u8]) -> Result<(), getrandom::Error> {
    for (i, byte) in buf.iter_mut().enumerate() {
        *byte = (i as u8).wrapping_mul(131).wrapping_add(7);
    }
    Ok(())
}
#[cfg(target_arch = "wasm32")]
getrandom::register_custom_getrandom!(unused_getrandom);

/// Streaming sylph read sketcher.
#[wasm_bindgen]
pub struct WasmSketcher {
    inner: StreamSketcher,
    gz1: Option<GzipDecoder>,
    gz2: Option<GzipDecoder>,
}

fn js_err(message: String) -> JsError {
    JsError::new(&message)
}

#[wasm_bindgen]
impl WasmSketcher {
    /// `c`: subsampling rate (sylphd's database uses 200); `k`: 21 or 31;
    /// `deduplicate`: sylph's read dedup (default true). `scalar_seeding`:
    /// use this build's native seeding kernel instead of the portable
    /// emulation of sylph's x86_64 AVX2 kernel (default false). The wasm32
    /// native kernel is sylph's scalar one, i.e. pysylph on a CPU without
    /// AVX2; the default is bit-identical to pysylph on x86_64 (see
    /// `sylph::seeding::Seeding`).
    #[wasm_bindgen(constructor)]
    pub fn new(
        c: usize,
        k: usize,
        deduplicate: Option<bool>,
        scalar_seeding: Option<bool>,
    ) -> Result<WasmSketcher, JsError> {
        let seeding = if scalar_seeding.unwrap_or(false) {
            Seeding::Native
        } else {
            Seeding::Avx2Compat
        };
        Ok(Self {
            inner: StreamSketcher::with_options(
                c,
                k,
                deduplicate.unwrap_or(true),
                sketch::DEFAULT_FPR,
                seeding,
            )
            .map_err(js_err)?,
            gz1: None,
            gz2: None,
        })
    }

    /// Feeds single-end FASTQ/FASTA text (already decompressed), any chunking.
    pub fn push(&mut self, data: &[u8]) -> Result<(), JsError> {
        self.inner.push(data).map_err(js_err)
    }

    /// Paired-end, first mates. Keep `push_r1` and `push_r2` within about a
    /// chunk of records of each other (see `unmatched`) to keep memory flat.
    pub fn push_r1(&mut self, data: &[u8]) -> Result<(), JsError> {
        self.inner.push_r1(data).map_err(js_err)
    }

    /// Paired-end, second mates.
    pub fn push_r2(&mut self, data: &[u8]) -> Result<(), JsError> {
        self.inner.push_r2(data).map_err(js_err)
    }

    /// Paired-end, one interleaved stream (R1, R2, R1, R2, ...).
    pub fn push_interleaved(&mut self, data: &[u8]) -> Result<(), JsError> {
        self.inner.push_interleaved(data).map_err(js_err)
    }

    /// Like `push`, for raw chunks of a gzip file; multi-member files (BGZF,
    /// concatenated gzips) are fine, unlike with `DecompressionStream`.
    pub fn push_gz(&mut self, data: &[u8]) -> Result<(), JsError> {
        let mut decoder = self.gz1.take().unwrap_or_default();
        let result = decoder.push(data, &mut |plain| self.inner.push(plain));
        self.gz1 = Some(decoder);
        result.map_err(js_err)
    }

    /// Gzip variant of `push_r1`.
    pub fn push_r1_gz(&mut self, data: &[u8]) -> Result<(), JsError> {
        let mut decoder = self.gz1.take().unwrap_or_default();
        let result = decoder.push(data, &mut |plain| self.inner.push_r1(plain));
        self.gz1 = Some(decoder);
        result.map_err(js_err)
    }

    /// Gzip variant of `push_r2`.
    pub fn push_r2_gz(&mut self, data: &[u8]) -> Result<(), JsError> {
        let mut decoder = self.gz2.take().unwrap_or_default();
        let result = decoder.push(data, &mut |plain| self.inner.push_r2(plain));
        self.gz2 = Some(decoder);
        result.map_err(js_err)
    }

    /// Gzip variant of `push_interleaved`.
    pub fn push_interleaved_gz(&mut self, data: &[u8]) -> Result<(), JsError> {
        let mut decoder = self.gz1.take().unwrap_or_default();
        let result = decoder.push(data, &mut |plain| self.inner.push_interleaved(plain));
        self.gz1 = Some(decoder);
        result.map_err(js_err)
    }

    /// Ends the current file so another can follow (same sample).
    pub fn end_file(&mut self) -> Result<(), JsError> {
        for decoder in [&mut self.gz1, &mut self.gz2] {
            if let Some(mut decoder) = decoder.take() {
                decoder.finish().map_err(js_err)?;
            }
        }
        self.inner.end_file().map_err(js_err)
    }

    /// Two-stream paired mode: queued mates still waiting for their partner;
    /// positive when R1 is ahead (feed R2 next), negative when R2 is ahead.
    pub fn unmatched(&self) -> f64 {
        self.inner.unmatched() as f64
    }

    /// Reads (pairs, in paired mode) sketched so far.
    pub fn reads(&self) -> f64 {
        self.inner.reads() as f64
    }

    /// Bases sketched so far.
    pub fn bases(&self) -> f64 {
        self.inner.bases() as f64
    }

    /// G and C bases (either case) among `bases()`.
    pub fn gc(&self) -> f64 {
        self.inner.gc() as f64
    }

    /// Distinct k-mers in the sketch so far.
    pub fn kmers(&self) -> f64 {
        self.inner.kmers() as f64
    }

    /// Ends the last file and returns the serialized `.sylsp` sketch.
    pub fn finish(&mut self, sample_name: &str) -> Result<Vec<u8>, JsError> {
        for decoder in [&mut self.gz1, &mut self.gz2] {
            if let Some(mut decoder) = decoder.take() {
                decoder.finish().map_err(js_err)?;
            }
        }
        self.inner.finish(sample_name).map_err(js_err)
    }
}

/// Largest accepted `chunk_size` (keeps `chunk + chunk / 4` far from overflow).
const MAX_CHUNK: usize = 256 << 20;

/// Per-contig sketcher: every record of a FASTA/FASTQ file (plain or gzip)
/// as its own sample, plus its genome-style k-mer count.
///
/// ```js
/// const s = new WasmContigSketcher(200, 31, 20000);   // c, k, min_length
/// s.push(chunk);                    // decompressed text, any chunking
/// for (const rec of s.drain()) {     // finished records, in file order
///   rec.index; rec.name; rec.length; rec.gcCount; rec.contigKmers; rec.sketch;
/// }
/// s.end_file();                     // still drain: the last record may finish here
/// ```
/// gzip input via `push_gz` (multi-member like `WasmSketcher.push_gz`).
///
/// Each record object has `index` (1-based), `name` (first word of the header,
/// possibly empty or repeated; the caller makes it unique if needed), `length`,
/// `gcCount` (G+C bases, either case), `contigKmers` (genome-style count,
/// `null` when the record was not sketched: empty or shorter than
/// `min_length`) and `sketch` (a `Uint8Array` with the `.sylsp` bytes of the
/// sample sketch, `null` when not sketched).
///
/// Records are produced one per `push`/`end_file` call at most per parser
/// position, but `drain` gives all finished ones, so memory is bounded by the
/// chunk size plus one record; the consumer should drain after every push.
#[wasm_bindgen]
pub struct WasmContigSketcher {
    inner: ContigSketcher,
    gz: Option<GzipDecoder>,
}

fn contig_record_to_js(record: ContigRecord) -> JsValue {
    let obj = js_sys::Object::new();
    let set = |key: &str, value: JsValue| {
        // Object::new() never fails to accept a property
        let _ = js_sys::Reflect::set(&obj, &JsValue::from_str(key), &value);
    };
    set("index", JsValue::from_f64(record.index as f64));
    set("name", JsValue::from_str(&record.name));
    set("length", JsValue::from_f64(record.length as f64));
    set("gcCount", JsValue::from_f64(record.gc_count as f64));
    // Rounded by the core: JS `toFixed()` rounds exact ties up.
    set("gc", JsValue::from_f64(record.gc_percent()));
    set(
        "contigKmers",
        match record.contig_kmers {
            Some(n) => JsValue::from_f64(n as f64),
            None => JsValue::NULL,
        },
    );
    set(
        "sketch",
        match record.sketch {
            Some(bytes) => js_sys::Uint8Array::from(bytes.as_slice()).into(),
            None => JsValue::NULL,
        },
    );
    obj.into()
}

#[wasm_bindgen]
impl WasmContigSketcher {
    /// `min_length`: records shorter than this (or empty) are reported without
    /// a sketch, like `--min-contig-length` (default 20000).
    /// `min_spacing`: sylph's genome-sketch spacing (default 30).
    /// `scalar_seeding`: as for `WasmSketcher`.
    /// `max_records`: records this run accepts (`contig::MAX_RECORDS` by
    /// default; 0 = unlimited). `max_record_length`: longest single record in
    /// bases (`contig::MAX_RECORD_LENGTH` by default; 0 = unlimited), enforced
    /// while parsing before a buffer grows.
    #[wasm_bindgen(constructor)]
    pub fn new(
        c: usize,
        k: usize,
        min_length: Option<f64>,
        min_spacing: Option<usize>,
        scalar_seeding: Option<bool>,
        max_records: Option<f64>,
        max_record_length: Option<f64>,
    ) -> Result<WasmContigSketcher, JsError> {
        let min_length = min_length.unwrap_or(DEFAULT_MIN_CONTIG_LENGTH as f64);
        if !(min_length >= 0.0 && min_length.is_finite()) {
            return Err(js_err(format!("invalid min_length: {min_length}")));
        }
        let limit = |name: &str, value: Option<f64>, default: u64| -> Result<u64, JsError> {
            let value = value.unwrap_or(default as f64);
            if !(value >= 0.0 && value.is_finite() && value.fract() == 0.0) {
                return Err(js_err(format!("invalid {name}: {value}")));
            }
            Ok(value as u64)
        };
        let max_records = limit("max_records", max_records, contig::MAX_RECORDS)?;
        let max_record_length = limit(
            "max_record_length",
            max_record_length,
            contig::MAX_RECORD_LENGTH as u64,
        )? as usize;
        let seeding = if scalar_seeding.unwrap_or(false) {
            Seeding::Native
        } else {
            Seeding::Avx2Compat
        };
        Ok(Self {
            inner: ContigSketcher::with_limits(
                c,
                k,
                min_length as u64,
                min_spacing.unwrap_or(contig::DEFAULT_MIN_SPACING),
                seeding,
                max_records,
                max_record_length,
            )
            .map_err(js_err)?,
            gz: None,
        })
    }

    /// Feeds single-end FASTQ/FASTA text (already decompressed), any chunking.
    pub fn push(&mut self, data: &[u8]) -> Result<(), JsError> {
        self.inner.push(data).map_err(js_err)
    }

    /// Like `push`, for raw chunks of a gzip file (multi-member files fine).
    pub fn push_gz(&mut self, data: &[u8]) -> Result<(), JsError> {
        let mut decoder = self.gz.take().unwrap_or_default();
        let result = decoder.push(data, &mut |plain| self.inner.push(plain));
        self.gz = Some(decoder);
        result.map_err(js_err)
    }

    /// Ends the current file: flushes the last record (drain afterwards).
    pub fn end_file(&mut self) -> Result<(), JsError> {
        if let Some(mut decoder) = self.gz.take() {
            decoder.finish().map_err(js_err)?;
        }
        self.inner.end_file().map_err(js_err)
    }

    /// All finished records (JS objects, see the type docs), oldest first.
    pub fn drain(&mut self) -> js_sys::Array {
        let out = js_sys::Array::new();
        while let Some(record) = self.inner.take_next() {
            out.push(&contig_record_to_js(record));
        }
        out
    }

    /// Records seen so far (including not-yet-drained ones).
    pub fn seen(&self) -> f64 {
        self.inner.seen as f64
    }

    /// Records sketched so far.
    pub fn sketched(&self) -> f64 {
        self.inner.sketched as f64
    }

    /// Records skipped (empty or shorter than `min_length`) so far.
    pub fn skipped(&self) -> f64 {
        self.inner.skipped as f64
    }

    /// Bases seen so far.
    pub fn bases(&self) -> f64 {
        self.inner.bases as f64
    }
}

/// Streaming gzip decoder for the browser (multi-member / BGZF / plain gzip
/// all behave the same; flate2 + zlib-rs inside).
///
/// ```js
/// const gz = new WasmGunzip(2 << 20, 'deferred');   // output chunk size, CRC mode
/// gz.push(compressedChunk);
/// let out; while ((out = gz.pull()) !== undefined) post(out, gz.take_trailers());
/// ...
/// post(gz.finish(), gz.take_trailers());    // last partial chunk; throws if truncated
/// ```
#[wasm_bindgen]
pub struct WasmGunzip {
    decoder: gzip::GzipDecoder,
    pending: Vec<u8>,
    pos: usize,
    out: Vec<u8>,
    chunk: usize,
    trailers: Vec<(u64, u32, u32)>,
}

#[wasm_bindgen]
impl WasmGunzip {
    /// `chunk_size`: size of the decompressed pieces `pull` returns (default
    /// 2 MiB, at most 256 MiB). `crc`: `"inline"` (default, check CRC32 while decoding),
    /// `"deferred"` (do not compute it here; `take_trailers` gives what a
    /// `WasmCrc` on another thread needs to check it) or `"off"`.
    #[wasm_bindgen(constructor)]
    pub fn new(chunk_size: Option<usize>, crc: Option<String>) -> Result<WasmGunzip, JsError> {
        let chunk = chunk_size.unwrap_or(2 << 20);
        if !(1..=MAX_CHUNK).contains(&chunk) {
            return Err(js_err(format!("chunk_size must be 1..={MAX_CHUNK}")));
        }
        let mode = match crc.as_deref() {
            None | Some("inline") => gzip::CrcMode::Inline,
            Some("deferred") => gzip::CrcMode::Deferred,
            Some("off") => gzip::CrcMode::Off,
            Some(other) => return Err(js_err(format!("unknown crc mode {other:?}"))),
        };
        Ok(WasmGunzip {
            decoder: gzip::GzipDecoder::with_mode(mode),
            pending: Vec::new(),
            pos: 0,
            out: Vec::with_capacity(chunk + chunk / 4),
            chunk,
            trailers: Vec::new(),
        })
    }

    /// Queues compressed bytes.
    pub fn push(&mut self, data: &[u8]) {
        if self.pos == self.pending.len() {
            self.pending.clear();
            self.pos = 0;
        }
        self.pending.extend_from_slice(data);
    }

    /// Decompresses queued input; returns a full chunk, or `undefined` when
    /// the queued input is used up (call `push` again).
    pub fn pull(&mut self) -> Result<Option<Vec<u8>>, JsError> {
        let mut input = &self.pending[self.pos..];
        let before = input.len();
        let result = self.decoder.decode(&mut input, &mut self.out, self.chunk);
        self.pos += before - input.len();
        self.trailers.extend(self.decoder.take_trailers());
        result.map_err(js_err)?;
        if self.out.len() >= self.chunk {
            let full = std::mem::replace(
                &mut self.out,
                Vec::with_capacity(self.chunk + self.chunk / 4),
            );
            Ok(Some(full))
        } else {
            Ok(None)
        }
    }

    /// Ends the file: checks that the gzip data is complete and returns the
    /// remaining decompressed bytes. Call after `pull` returned `undefined`
    /// for the last time.
    pub fn finish(&mut self) -> Result<Vec<u8>, JsError> {
        self.trailers.extend(self.decoder.take_trailers());
        self.decoder.finish().map_err(js_err)?;
        Ok(std::mem::take(&mut self.out))
    }

    /// Deferred CRC mode: members completed since the last call, as flat
    /// `[end offset in the decompressed file, crc32, isize]` triples (numbers).
    /// Call right after the `pull` / `finish` whose chunk they belong to, and
    /// send them along with that chunk.
    pub fn take_trailers(&mut self) -> Vec<f64> {
        std::mem::take(&mut self.trailers)
            .into_iter()
            .flat_map(|(end, crc, size)| [end as f64, crc as f64, size as f64])
            .collect()
    }

    /// Number of gzip members completed so far.
    pub fn members(&self) -> u32 {
        self.decoder.members
    }
}

/// Checks the CRC32 of gzip members on the other side of a pipeline:
/// the decompressing worker only records `(end, crc, isize)` (deferred mode),
/// the sketching worker, which has idle time, does the hashing.
///
/// ```js
/// const crc = new WasmCrc();
/// crc.update(chunkBytes, trailersOfThatChunk);   // throws on mismatch
/// crc.end_file();
/// ```
#[wasm_bindgen]
pub struct WasmCrc {
    inner: gzip::Crc32Tracker,
}

#[wasm_bindgen]
impl WasmCrc {
    #[wasm_bindgen(constructor)]
    pub fn new() -> WasmCrc {
        WasmCrc {
            inner: gzip::Crc32Tracker::default(),
        }
    }

    pub fn update(&mut self, data: &[u8], trailers: &[f64]) -> Result<(), JsError> {
        let triples: Vec<(u64, u32, u32)> = trailers
            .as_chunks::<3>()
            .0
            .iter()
            .map(|t| (t[0] as u64, t[1] as u32, t[2] as u32))
            .collect();
        self.inner.update(data, &triples).map_err(js_err)
    }

    pub fn end_file(&mut self) {
        self.inner.reset();
    }
}

impl Default for WasmCrc {
    fn default() -> Self {
        Self::new()
    }
}

// ---------------------------------------------------------------------------
// Document validation, per-contig run merge, and rendering
// ---------------------------------------------------------------------------

/// Validate a server/document `api_version`; throws on mismatch.
#[wasm_bindgen]
pub fn check_api_version(version: f64) -> Result<(), JsError> {
    if !(version >= 0.0 && version.is_finite() && version.fract() == 0.0) {
        return Err(js_err(
            document::DocumentError::MissingApiVersion.to_string(),
        ));
    }
    document::check_api_version(version as u64).map_err(|e| js_err(e.to_string()))
}

/// Render a `/profile` or `/query` JSON document (or a standalone `{"columns", "data"}` table)
/// to sylph-identical TSV.
#[wasm_bindgen]
pub fn render_tsv(doc_or_table_json: &str) -> Result<String, JsError> {
    #[derive(serde::Deserialize)]
    struct ShapeProbe {
        #[serde(default)]
        table: Option<serde_json::Value>,
    }
    let probe: ShapeProbe = serde_json::from_str(doc_or_table_json)
        .map_err(|e| js_err(format!("invalid JSON: {e}")))?;
    if probe.table.is_some() {
        let doc =
            document::parse::<ProfileDocument>(doc_or_table_json).map_err(|e| js_err(e.to_string()))?;
        Ok(render::render_tsv(&doc.table))
    } else {
        let table: Table = serde_json::from_str(doc_or_table_json)
            .map_err(|e| js_err(format!("invalid table JSON: {e}")))?;
        Ok(render::render_tsv(&table))
    }
}

/// Render the 11-column hits TSV of a `/assembly` JSON document.
#[wasm_bindgen]
pub fn render_assembly_tsv(doc_json: &str) -> Result<String, JsError> {
    let doc = document::parse::<AssemblyDocument>(doc_json).map_err(|e| js_err(e.to_string()))?;
    Ok(render::render_assembly_tsv(&doc.table))
}

/// Merge a JSON array of `/contigs` batch response documents plus the run's
/// request and client-side counters into a single run document JSON.
///
/// `options_json` is a [`ContigsRunMergeOptions`] object that may carry the
/// per-contig `request` options (`top`, `min_ani`, `consensus_ratio`,
/// `consensus_z`, `fields`) the batches were requested with.
#[wasm_bindgen]
pub fn merge_contigs_run(
    batches_json: &str,
    options_json: Option<String>,
) -> Result<String, JsError> {
    #[derive(serde::Deserialize, Default)]
    struct RunOptions {
        #[serde(default)]
        request: ContigsRequestOptions,
        #[serde(flatten)]
        merge: ContigsRunMergeOptions,
    }
    let batches = document::parse_contigs_batches(batches_json).map_err(|e| js_err(e.to_string()))?;
    let options: RunOptions = match options_json.as_deref() {
        None | Some("") => RunOptions::default(),
        Some(raw) => serde_json::from_str(raw)
            .map_err(|e| js_err(format!("invalid merge options JSON: {e}")))?,
    };
    let merged = document::merge_contigs_run(&batches, &options.request, &options.merge)
        .map_err(|e| js_err(e.to_string()))?;
    serde_json::to_string(&merged)
        .map_err(|e| js_err(format!("failed to serialize run document: {e}")))
}

/// Render the flat per-contig TSV of a `/contigs` (single-batch or merged run) JSON document.
#[wasm_bindgen]
pub fn render_contigs_tsv(doc_json: &str) -> Result<String, JsError> {
    let doc = document::parse::<ContigsDocument>(doc_json).map_err(|e| js_err(e.to_string()))?;
    Ok(render::render_contigs_tsv(&doc.table))
}

// ---------------------------------------------------------------------------
// Per-contig batching, request names and ready-made sketches
// ---------------------------------------------------------------------------

/// One contig to plan: its request entry and the byte length of its sketch.
#[derive(serde::Deserialize)]
struct PlannedContig {
    #[serde(flatten)]
    spec: ContigSpec,
    payload_bytes: usize,
}

/// The `/info` `limits` that bound a `/contigs` request.
#[derive(serde::Deserialize, Default)]
#[serde(default)]
struct ServerLimits {
    max_contigs: Option<usize>,
    max_params_bytes: Option<usize>,
    max_upload_mb: Option<usize>,
}

/// Splits contigs into `/contigs` requests that fit the server's limits.
///
/// `contigs_json`: `[{name, contig_kmers, length, gc, payload_bytes}, ...]`;
/// `limits_json`: the `limits` object of `/info`; `request_json`: the request
/// options (`top`, `min_ani`, `consensus_ratio`, `consensus_z`, `fields`,
/// optional `tax_sources`). Returns `[{"indices": [...], "params": "<json>"}, ...]`
/// in request order: the positions in `contigs_json` of each request's contigs
/// and the exact `params` text to send. Throws when one contig cannot fit.
#[wasm_bindgen]
pub fn plan_contig_requests(
    contigs_json: &str,
    limits_json: &str,
    request_json: &str,
) -> Result<String, JsError> {
    let contigs: Vec<PlannedContig> = serde_json::from_str(contigs_json)
        .map_err(|e| js_err(format!("invalid contigs JSON: {e}")))?;
    let limits: ServerLimits = serde_json::from_str(limits_json)
        .map_err(|e| js_err(format!("invalid limits JSON: {e}")))?;
    let request: ContigsRequestOptions = serde_json::from_str(request_json)
        .map_err(|e| js_err(format!("invalid request JSON: {e}")))?;
    let limits = BatchLimits::from_server_limits(
        limits.max_contigs,
        limits.max_params_bytes,
        limits.max_upload_mb,
    );
    let items = contigs
        .into_iter()
        .enumerate()
        .map(|(index, c)| (c.spec, c.payload_bytes, index));
    let batches = plan_contig_batches(items, limits, &request).map_err(|e| js_err(e.to_string()))?;
    let out: Vec<serde_json::Value> = batches
        .iter()
        .map(|batch| {
            serde_json::json!({
                "indices": batch.items.iter().map(|item| item.item).collect::<Vec<_>>(),
                "params": batch.params_json(&request),
            })
        })
        .collect();
    serde_json::to_string(&out).map_err(|e| js_err(e.to_string()))
}

/// Makes contig sample names unique within one request set: a repeated id
/// gets `_2`, `_3`, ...; an empty id is `contig-<index>`.
#[wasm_bindgen]
pub struct WasmContigNames {
    inner: ContigNameTracker,
}

#[wasm_bindgen]
impl WasmContigNames {
    #[wasm_bindgen(constructor)]
    pub fn new() -> WasmContigNames {
        WasmContigNames {
            inner: ContigNameTracker::new(),
        }
    }

    /// The unique name of the record `raw` (its header id) at 1-based `index`.
    pub fn next_name(&mut self, raw: &str, index: f64) -> String {
        self.inner.next_name(raw, index as u64)
    }
}

impl Default for WasmContigNames {
    fn default() -> Self {
        Self::new()
    }
}

/// Where the tail of a `.sylsp` file starts, from its first 8 bytes and its
/// size: returns `[kmer count, tail offset]`. Throws when the file cannot be a
/// sketch.
#[wasm_bindgen]
pub fn sylsp_tail_start(prefix: &[u8], size: f64) -> Result<Vec<f64>, JsError> {
    let (count, start) =
        sketchcheck::sketch_tail_start(prefix, size as u64).map_err(|e| js_err(e.to_string()))?;
    Ok(vec![count as f64, start as f64])
}

/// Validates the tail of a `.sylsp` file (everything after the k-mer table)
/// and returns its header as JSON: `{kmers, c, k, fileName, sampleName, paired}`.
#[wasm_bindgen]
pub fn sylsp_header(tail: &[u8], kmer_count: f64, tail_offset: f64) -> Result<String, JsError> {
    let header = sketchcheck::parse_sketch_tail(tail, kmer_count as u64, tail_offset as u64)
        .map_err(|e| js_err(e.to_string()))?;
    Ok(serde_json::json!({
        "kmers": header.kmer_count,
        "c": header.c,
        "k": header.k,
        "fileName": header.file_name,
        "sampleName": header.sample_name,
        "paired": header.paired,
    })
    .to_string())
}

/// Why a sketch made with `(c, k)` cannot be searched in a database with
/// `(db_c, db_k)`, or `undefined` when it can.
#[wasm_bindgen]
pub fn sketch_incompatibility(c: f64, k: f64, db_c: f64, db_k: f64) -> Option<String> {
    let header = sketchcheck::SketchHeader {
        c: c as u64,
        k: k as u64,
        file_name: String::new(),
        sample_name: None,
        paired: false,
        mean_read_length: 0.0,
        kmer_count: 0,
    };
    sketchcheck::check_sketch_compatibility(&header, db_c as u64, db_k as u64, None)
        .err()
        .map(|e| e.to_string())
}
