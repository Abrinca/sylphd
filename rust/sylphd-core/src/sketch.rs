//! Read-sketch accumulation and `.sylsp` serialization for the browser.
//!
//! The k-mer extraction, PCR-duplicate removal, mate handling and mean read
//! length all come from [`sylph::sketchers`] ([`ReadSketcher`] for single-end,
//! [`PairedReadSketcher`] for paired-end); this module only adds what the
//! browser needs on top:
//!
//! * the progress counters `reads`, `bases` and `gc` (the sketchers do not
//!   track them);
//! * pysylph's mean read length in paired mode (the moving average of the
//!   *first* mate's length, which [`PairedReadSketcher`] does not expose);
//! * the deterministic writer: k-mer entries are sorted by k-mer, everything
//!   else is exactly sylph's `SequencesSketch` bincode layout (fixint, little
//!   endian; see `sketchcheck.rs`). sylph itself writes hash-map
//!   order, which readers do not depend on.

use sylph::seeding::Seeding;
use sylph::sketchers::{PairedReadSketcher, ReadSketcher};
use sylph::types::Kmer;

pub use sylph::constants::DEFAULT_FPR;

/// Default paired-read dedup false positive rate for this build (`DEFAULT_FPR`
/// when `approx-dedup` is enabled, `0.0` for exact dedup otherwise).
pub const fn default_fpr() -> f64 {
    #[cfg(feature = "approx-dedup")]
    {
        DEFAULT_FPR
    }
    #[cfg(not(feature = "approx-dedup"))]
    {
        0.0
    }
}

/// Counts G and C bases (either case) in a DNA sequence.
pub fn count_gc(seq: &[u8]) -> u64 {
    seq.iter()
        .filter(|b| matches!(b, b'G' | b'C' | b'g' | b'c'))
        .count() as u64
}

/// GC percentage rounded to 2 decimal places (ties to even, matching Python's
/// `round(100.0 * gc / length, 2)`). Returns `0.0` when `length == 0`.
pub fn gc_percent(gc_count: u64, length: u64) -> f64 {
    if length == 0 {
        return 0.0;
    }
    round_ties_even_2(100.0 * gc_count as f64 / length as f64)
}

/// Round to 2 decimal places, ties to even (matching Python's `round(x, 2)`).
pub(crate) fn round_ties_even_2(x: f64) -> f64 {
    (x * 100.0).round_ties_even() / 100.0
}

/// Accumulator behind both `sketch_single` and `sketch_paired`.
///
/// One instance sketches one sample, in either single or paired mode. The
/// sketcher is created lazily by the first `add_*` call: a paired sketch only
/// allocates the (large) cuckoo filter once a pair is actually seen.
pub struct SketchBuilder {
    pub c: usize,
    pub k: usize,
    deduplicate: bool,
    fpr: f64,
    seeding: Seeding,
    single: Option<ReadSketcher>,
    paired: Option<PairedReadSketcher>,
    mean_read_length: f64,
    /// Number of reads (or pairs) seen.
    pub reads: u64,
    /// Number of bases seen (both mates for pairs).
    pub bases: u64,
    /// G and C bases (either case) among `bases`.
    pub gc: u64,
}

impl SketchBuilder {
    pub fn new(
        c: usize,
        k: usize,
        deduplicate: bool,
        fpr: f64,
        seeding: Seeding,
    ) -> Result<Self, String> {
        if k != 21 && k != 31 {
            return Err(format!("invalid k: expected 21 or 31, got {k}"));
        }
        if c == 0 {
            return Err("invalid c: must be at least 1".into());
        }
        if !(0.0..1.0).contains(&fpr) {
            return Err(format!("invalid fpr: {fpr}"));
        }
        #[cfg(not(feature = "approx-dedup"))]
        if fpr != 0.0 {
            return Err(
                "approximate read deduplication (fpr != 0) requires the `approx-dedup` feature"
                    .into(),
            );
        }
        Ok(Self {
            c,
            k,
            deduplicate,
            fpr,
            seeding,
            single: None,
            paired: None,
            mean_read_length: 0.0,
            reads: 0,
            bases: 0,
            gc: 0,
        })
    }

    /// Number of distinct sketched k-mers so far.
    pub fn kmers(&self) -> usize {
        if let Some(sketcher) = &self.single {
            return sketcher.kmer_counts().len();
        }
        self.paired
            .as_ref()
            .map_or(0, |sketcher| sketcher.kmer_counts().len())
    }

    /// The k-mer counts of whichever mode has been used (empty before the
    /// first read), sorted by k-mer for deterministic output. sylph itself
    /// writes hash-map order.
    fn counts(&self) -> Vec<(Kmer, u32)> {
        let mut entries: Vec<(Kmer, u32)> = Vec::new();
        if let Some(sketcher) = &self.single {
            entries.extend(sketcher.kmer_counts().iter().map(|(&kmer, &count)| (kmer, count)));
        } else if let Some(sketcher) = &self.paired {
            entries.extend(sketcher.kmer_counts().iter().map(|(&kmer, &count)| (kmer, count)));
        }
        entries.sort_unstable_by_key(|entry| entry.0);
        entries
    }

    /// One single-end read: `Sketcher.sketch_single`, loop body.
    pub fn add_single(&mut self, seq: &[u8]) {
        let sketcher = self.single.get_or_insert_with(|| {
            ReadSketcher::new(self.c, self.k, !self.deduplicate, self.seeding)
        });
        sketcher.push_read(seq);
        self.reads += 1;
        self.bases += seq.len() as u64;
        self.gc += count_gc(seq);
        // pysylph's `Sketcher.sketch_single`: moving average over all reads
        self.mean_read_length += ((seq.len() as f64) - self.mean_read_length) / self.reads as f64;
    }

    /// One read pair: `Sketcher.sketch_paired`, loop body.
    pub fn add_pair(&mut self, seq1: &[u8], seq2: &[u8]) {
        let sketcher = match self.paired.as_mut() {
            Some(sketcher) => sketcher,
            None => {
                let paired = PairedReadSketcher::new(
                    self.c,
                    self.k,
                    !self.deduplicate,
                    self.fpr,
                    self.seeding,
                )
                .expect("fpr was validated in SketchBuilder::new");
                self.paired.insert(paired)
            }
        };
        sketcher.push_pair(seq1, seq2);
        self.reads += 1;
        self.bases += (seq1.len() + seq2.len()) as u64;
        self.gc += count_gc(seq1) + count_gc(seq2);
        // pysylph's `Sketcher.sketch_paired`: moving average of the first mate
        // (sylph's `sketch_pair_sequences` instead uses total_bases / pairs).
        self.mean_read_length += ((seq1.len() as f64) - self.mean_read_length) / self.reads as f64;
    }

    /// Serializes to sylph's `SequencesSketch`: bincode 1.x legacy layout
    /// (fixint, little endian). The k-mer entries are written sorted by k-mer
    /// so the output is deterministic; see `sketchcheck.rs`.
    ///
    /// `sample_name` is always `None` (the request carries the name).
    pub fn serialize(&self, file_name: &str, paired: bool) -> Vec<u8> {
        let entries = self.counts();
        let mut out = Vec::with_capacity(8 + entries.len() * 12 + 64 + file_name.len());
        out.extend_from_slice(&(entries.len() as u64).to_le_bytes());
        for (kmer, count) in entries {
            out.extend_from_slice(&kmer.to_le_bytes());
            out.extend_from_slice(&count.to_le_bytes());
        }
        out.extend_from_slice(&(self.c as u64).to_le_bytes());
        out.extend_from_slice(&(self.k as u64).to_le_bytes());
        out.extend_from_slice(&(file_name.len() as u64).to_le_bytes());
        out.extend_from_slice(file_name.as_bytes());
        out.push(0); // sample_name: None
        out.push(u8::from(paired));
        out.extend_from_slice(&self.mean_read_length.to_le_bytes());
        out
    }
}
