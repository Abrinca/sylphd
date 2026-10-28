//! Browser read sketching for sylphd.
//!
//! A minimal port of what pysylph's `Sketcher.sketch_single` /
//! `sketch_paired` do (see `sketch.rs`), behind a streaming wasm-bindgen API:
//!
//! ```js
//! const s = new WasmSketcher(200, 31, true);
//! for await (const chunk of decompressedFastqChunks) s.push(chunk);
//! const sylsp = s.finish("sample");   // Uint8Array, a .sylsp file
//! ```

pub mod parser;
pub mod seeding;
pub mod sketch;
pub mod stream;

use wasm_bindgen::prelude::*;

use seeding::Seeding;
use stream::StreamSketcher;

/// `rand::thread_rng()` is called (and its result discarded) by
/// `ScalableCuckooFilterBuilder::new()`; the filter is then given a seeded RNG.
/// Nothing depends on these bytes, so no entropy source is needed.
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
}

fn js_err(message: String) -> JsError {
    JsError::new(&message)
}

#[wasm_bindgen]
impl WasmSketcher {
    /// `c`: subsampling rate (sylphd's database uses 200); `k`: 21 or 31;
    /// `deduplicate`: sylph's read dedup (default true). `scalar_seeding`:
    /// emulate sylph on CPUs without AVX2 instead of on x86_64 with AVX2
    /// (default false; see `seeding::Seeding`).
    #[wasm_bindgen(constructor)]
    pub fn new(
        c: usize,
        k: usize,
        deduplicate: Option<bool>,
        scalar_seeding: Option<bool>,
    ) -> Result<WasmSketcher, JsError> {
        let seeding = if scalar_seeding.unwrap_or(false) {
            Seeding::Scalar
        } else {
            Seeding::Avx2
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
        })
    }

    /// Feeds single-end FASTQ/FASTA text (already decompressed), any chunking.
    pub fn push(&mut self, data: &[u8]) -> Result<(), JsError> {
        self.inner.push(data).map_err(js_err)
    }

    /// Paired-end, first mates. Alternate with `push_r2` to keep memory flat.
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

    /// Ends the current file so another can follow (same sample).
    pub fn end_file(&mut self) -> Result<(), JsError> {
        self.inner.end_file().map_err(js_err)
    }

    /// Reads (pairs, in paired mode) sketched so far.
    pub fn reads(&self) -> f64 {
        self.inner.reads() as f64
    }

    /// Bases sketched so far.
    pub fn bases(&self) -> f64 {
        self.inner.bases() as f64
    }

    /// Distinct k-mers in the sketch so far.
    pub fn kmers(&self) -> f64 {
        self.inner.kmers() as f64
    }

    /// Ends the last file and returns the serialized `.sylsp` sketch.
    pub fn finish(&mut self, sample_name: &str) -> Result<Vec<u8>, JsError> {
        self.inner.finish(sample_name).map_err(js_err)
    }
}
