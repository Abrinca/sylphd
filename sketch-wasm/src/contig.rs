//! Per-contig sketching: every record of a FASTA/FASTQ file sketched as its
//! own sample, plus the record's *genome-style* k-mer count.
//!
//! This is what `sylphd assembly FILE --per-contig` (and `POST /contigs`)
//! needs per contig (see `src/sylphd/sketching.py` `sketch_contigs` and
//! `scratch/per-contig-ani/FINDINGS.md`):
//!
//! * the **sample** sketch of the record, i.e. what `Sketcher.sketch_single`
//!   produces for one sequence (`.sylsp` bytes, same as `sketch.rs`);
//! * the **genome-style** k-mer count, i.e.
//!   `len(Sketcher(c, k).sketch_genome(name, [seq]).kmers)`: the same
//!   c-subsampled hashes, but with every k-mer that occurs more than once in
//!   the record removed, and greedy minimum spacing of 30 bp along it.
//!   That count is the exact denominator of the contig-relative ANI.
//!
//! Ported from:
//! * sylph (MIT OR Apache-2.0, Copyright (c) 2023 Jim Shaw), commit
//!   4b5c2beb05312d9da9006529b12ad24ec47f0914:
//!   `src/seeding.rs` `fmh_seeds_positions` (lines 148-215),
//!   `src/avx2_seeding.rs` `extract_markers_avx2_positions` (lines 151-273),
//!   `src/sketch.rs` `extract_markers_positions` (lines 72-94) and the
//!   duplicate/spacing part of `sketch_genome` (lines 628-655).
//! * pysylph (MIT, Martin Larralde), branch `multi-db`,
//!   `Sketcher.sketch_genome` (`src/pysylph/lib.rs` 1056-1141), which
//!   hard-codes `min_spacing = 30`.
//!
//! The sample sketch is built by the same [`SketchBuilder`] the read sketcher
//! uses, so its bytes are the same ones `push` would produce for that record.

use std::collections::VecDeque;

use fxhash::FxHashSet;

use crate::gzip::GzipDecoder;
use crate::parser::{record_name, RecordParser};
use crate::seeding::{Seeding, BYTE_TO_SEQ};
use crate::sketch::{count_gc, SketchBuilder, DEFAULT_FPR};

/// pysylph's `Sketcher` (and sylph's CLI default) minimum spacing.
pub const DEFAULT_MIN_SPACING: usize = 30;

/// Most records a per-contig run processes in the browser. A fragmented
/// assembly with millions of records would otherwise hold a tab busy for
/// hours; `sylphd assembly --per-contig` has no such limit.
pub const MAX_RECORDS: u64 = 1_000_000;

/// Longest single record (bases) a per-contig run accepts in the browser.
/// The browser holds the record and its sketch in memory and the wasm heap is
/// capped at 4 GiB; a longer record (a whole chromosome/genome) belongs to the
/// CLI. Checked while parsing, before the buffers grow.
pub const MAX_RECORD_LENGTH: usize = 50_000_000;

/// The `(position, hash)` markers of one sequence, in the order sylph's
/// seeding emits them. `position` is the end offset of the k-mer in the
/// sequence (sylph's convention, used by the spacing filter).
pub type Markers = Vec<(usize, u64)>;

/// Port of sylph `src/seeding.rs` `fmh_seeds_positions` (lines 148-215): every
/// k-mer of the sequence, no end trimming.
pub fn fmh_seeds_positions(string: &[u8], out: &mut Markers, c: usize, k: usize) {
    if string.len() < k {
        return;
    }
    let mut rolling_f: u64 = 0;
    let mut rolling_r: u64 = 0;

    let reverse_shift = 2 * (k - 1);
    let mask = u64::MAX >> (std::mem::size_of::<u64>() * 8 - 2 * k);
    let rev_mask = !(3u64 << (2 * k - 2));
    let threshold = u64::MAX / (c as u64);

    for i in 0..k - 1 {
        let nuc_f = BYTE_TO_SEQ[string[i] as usize] as u64;
        let nuc_r = 3 - nuc_f;
        rolling_f <<= 2;
        rolling_f |= nuc_f;
        rolling_r >>= 2;
        rolling_r |= nuc_r << reverse_shift;
    }
    for (i, &byte) in string.iter().enumerate().skip(k - 1) {
        let nuc_f = BYTE_TO_SEQ[byte as usize] as u64;
        let nuc_r = 3 - nuc_f;
        rolling_f = ((rolling_f << 2) | nuc_f) & mask;
        rolling_r = ((rolling_r >> 2) & rev_mask) | (nuc_r << reverse_shift);

        let hash = crate::seeding::mm_hash64(rolling_f.min(rolling_r));
        if hash < threshold {
            out.push((i, hash));
        }
    }
}

/// Port of sylph `src/avx2_seeding.rs` `extract_markers_avx2_positions`
/// (lines 151-273): the same k-mers as `fmh_seeds_positions`, except that the
/// read is split into four equal segments and the last
/// `(n - k + 1) % 4` k-mers are dropped, and nothing is emitted for
/// `n < 2k`. This is what sylph and pysylph do on x86_64 with AVX2.
pub fn fmh_seeds_avx2_positions(string: &[u8], out: &mut Markers, c: usize, k: usize) {
    if string.len() < k {
        return;
    }
    let seg = (string.len() - k + 1) / 4;
    if string.len() < 2 * k || seg == 0 {
        return;
    }
    let reverse_shift = 2 * (k - 1);
    let mask = u64::MAX >> (64 - 2 * k);
    let rev_mask = !(3u64 << (2 * k - 2));
    let threshold = u64::MAX / (c as u64);

    let mut f = [0u64; 4];
    let mut r = [0u64; 4];
    for i in 0..k - 1 {
        for s in 0..4 {
            let nuc_f = BYTE_TO_SEQ[string[s * seg + i] as usize] as u64;
            f[s] = (f[s] << 2) | nuc_f;
            r[s] = (r[s] >> 2) | ((3 - nuc_f) << reverse_shift);
        }
    }
    for i in k - 1..seg + k - 1 {
        for s in 0..4 {
            let nuc_f = BYTE_TO_SEQ[string[s * seg + i] as usize] as u64;
            f[s] = ((f[s] << 2) | nuc_f) & mask;
            r[s] = ((r[s] >> 2) & rev_mask) | ((3 - nuc_f) << reverse_shift);
            let hash = crate::seeding::mm_hash64(f[s].min(r[s]));
            if hash < threshold {
                // sylph reports the end offset as `s * seg + i`
                out.push((s * seg + i, hash));
            }
        }
    }
}

/// `(position, hash)` markers with the configured seeding behaviour.
pub fn extract_markers_positions(
    seeding: Seeding,
    string: &[u8],
    out: &mut Markers,
    c: usize,
    k: usize,
) {
    match seeding {
        Seeding::Avx2 => fmh_seeds_avx2_positions(string, out, c, k),
        Seeding::Scalar => fmh_seeds_positions(string, out, c, k),
    }
}

/// The genome-style k-mers of one sequence, in sketch order: selected hashes
/// that occur exactly once in the sequence, greedily spaced by more than
/// `min_spacing` end positions.
///
/// Line by line `Sketcher.sketch_genome(name, [seq]).kmers` for a single
/// contig (pysylph `src/pysylph/lib.rs` 1108-1140).
pub fn genome_kmers(
    seeding: Seeding,
    seq: &[u8],
    c: usize,
    k: usize,
    min_spacing: usize,
) -> Vec<u64> {
    let mut markers: Markers = Vec::new();
    extract_markers_positions(seeding, seq, &mut markers, c, k);
    // pysylph sorts `(contig, pos, hash)`; one contig leaves (pos, hash)
    markers.sort_unstable();

    let mut seen: FxHashSet<u64> = FxHashSet::default();
    let mut duplicated: FxHashSet<u64> = FxHashSet::default();
    for &(_, hash) in &markers {
        if !seen.insert(hash) {
            duplicated.insert(hash);
        }
    }

    let mut kept = Vec::with_capacity(markers.len());
    let mut last_pos = 0usize;
    for &(pos, hash) in &markers {
        if duplicated.contains(&hash) {
            continue;
        }
        // sylph: `last_pos == 0 || pos - last_pos > min_spacing`
        if last_pos == 0 || pos - last_pos > min_spacing {
            kept.push(hash);
            last_pos = pos;
        }
    }
    kept
}

/// One finished record.
#[derive(Debug, Clone)]
pub struct ContigRecord {
    /// 1-based record number in the stream.
    pub index: u64,
    /// First word of the header (see [`record_name`]); may be empty or repeat.
    pub name: String,
    pub length: u64,
    /// G and C bases (either case).
    pub gc_count: u64,
    /// Genome-style k-mer count; `None` when the record was not sketched
    /// (empty, or shorter than `min_length`).
    pub contig_kmers: Option<u64>,
    /// `.sylsp` bytes of the sample sketch; `None` when not sketched.
    pub sketch: Option<Vec<u8>>,
}

/// Streaming per-contig sketcher: FASTQ/FASTA (plain or gzip), one record at
/// a time, bounded memory (the largest record plus the queued output).
///
/// ```text
/// push(chunk); while let Some(record) = take_next() { ... }   // per chunk
/// end_file();
/// ```
pub struct ContigSketcher {
    c: usize,
    k: usize,
    min_length: u64,
    min_spacing: usize,
    seeding: Seeding,
    /// Records this run accepts (`MAX_RECORDS` by default).
    max_records: u64,
    /// Longest single record in bases (`MAX_RECORD_LENGTH` by default).
    max_record_length: usize,
    parser: RecordParser,
    gz: Option<GzipDecoder>,
    queue: VecDeque<ContigRecord>,
    /// Records seen so far (1-based `index` of the last one).
    pub seen: u64,
    /// Records that were sketched / skipped (`min_length`, empty).
    pub sketched: u64,
    pub skipped: u64,
    /// Bases seen.
    pub bases: u64,
}

impl ContigSketcher {
    pub fn new(
        c: usize,
        k: usize,
        min_length: u64,
        min_spacing: usize,
        seeding: Seeding,
    ) -> Result<Self, String> {
        Self::with_limits(
            c,
            k,
            min_length,
            min_spacing,
            seeding,
            MAX_RECORDS,
            MAX_RECORD_LENGTH,
        )
    }

    /// Like [`ContigSketcher::new`], with explicit bounds (0 = unlimited).
    /// The browser passes the defaults; tests use small values.
    #[allow(clippy::too_many_arguments)]
    pub fn with_limits(
        c: usize,
        k: usize,
        min_length: u64,
        min_spacing: usize,
        seeding: Seeding,
        max_records: u64,
        max_record_length: usize,
    ) -> Result<Self, String> {
        // validate through the same constructor the read sketcher uses
        SketchBuilder::new(c, k, true, DEFAULT_FPR, seeding)?;
        Ok(Self {
            c,
            k,
            min_length,
            min_spacing,
            seeding,
            max_records,
            max_record_length,
            parser: RecordParser::new().with_max_sequence(max_record_length),
            gz: None,
            queue: VecDeque::new(),
            seen: 0,
            sketched: 0,
            skipped: 0,
            bases: 0,
        })
    }

    /// Feeds already decompressed FASTQ/FASTA text, any chunking.
    pub fn push(&mut self, data: &[u8]) -> Result<(), String> {
        let mut parser = std::mem::take(&mut self.parser);
        let mut error = None;
        let result = parser.feed(data, &mut |seq, header| {
            if error.is_none() {
                if let Err(e) = self.add_record(header, seq) {
                    error = Some(e);
                }
            }
        });
        self.parser = parser;
        result?;
        match error {
            Some(e) => Err(e),
            None => Ok(()),
        }
    }

    /// Feeds raw chunks of a (possibly multi-member) gzip file.
    pub fn push_gz(&mut self, data: &[u8]) -> Result<(), String> {
        let mut decoder = self.gz.take().unwrap_or_default();
        let result = decoder.push(data, &mut |plain| self.push(plain));
        self.gz = Some(decoder);
        result
    }

    /// Ends the current file (all streams): flushes a last line without
    /// newline, a pending FASTA record, and checks that the gzip data is
    /// complete.
    pub fn end_file(&mut self) -> Result<(), String> {
        if let Some(mut decoder) = self.gz.take() {
            decoder.finish()?;
        }
        let mut parser = std::mem::take(&mut self.parser);
        let mut error = None;
        let result = parser.finish(&mut |seq, header| {
            if error.is_none() {
                if let Err(e) = self.add_record(header, seq) {
                    error = Some(e);
                }
            }
        });
        self.parser = parser;
        result?;
        match error {
            Some(e) => Err(e),
            None => Ok(()),
        }
    }

    fn add_record(&mut self, header: &[u8], seq: &[u8]) -> Result<(), String> {
        if self.max_records > 0 && self.seen >= self.max_records {
            return Err(format!(
                "this input has more than {} records, the browser's per-contig limit; \
                 classify it with the sylphd command line (sylphd assembly FILE --per-contig)",
                self.max_records
            ));
        }
        if self.max_record_length > 0 && seq.len() > self.max_record_length {
            return Err(format!(
                "a record is longer than {} bp, the browser's per-contig limit; \
                 classify this input with the sylphd command line \
                 (sylphd assembly FILE --per-contig)",
                self.max_record_length
            ));
        }
        self.seen += 1;
        let length = seq.len() as u64;
        self.bases += length;
        let name = record_name(header);
        let gc_count = count_gc(seq);
        let (sketch, contig_kmers) = if length > 0 && length >= self.min_length {
            let mut builder = SketchBuilder::new(self.c, self.k, true, DEFAULT_FPR, self.seeding)?;
            builder.add_single(seq);
            let kmers = genome_kmers(self.seeding, seq, self.c, self.k, self.min_spacing);
            self.sketched += 1;
            (
                Some(builder.serialize(&name, false)),
                Some(kmers.len() as u64),
            )
        } else {
            self.skipped += 1;
            (None, None)
        };
        self.queue.push_back(ContigRecord {
            index: self.seen,
            name,
            length,
            gc_count,
            contig_kmers,
            sketch,
        });
        Ok(())
    }

    /// Next finished record, or `None` when nothing is queued.
    pub fn take_next(&mut self) -> Option<ContigRecord> {
        self.queue.pop_front()
    }

    /// Number of finished records waiting to be taken.
    pub fn pending(&self) -> usize {
        self.queue.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Straightforward reference: every k-mer window, canonical hash via
    /// `map`-style 2-bit packing, per-window hashes, dedupe on the hash, then
    /// the greedy spacing over window starts (converted to end positions).
    fn reference_genome_kmers(seq: &[u8], c: usize, k: usize, min_spacing: usize) -> Vec<u64> {
        let code = |b: u8| -> u64 {
            match b.to_ascii_uppercase() {
                b'A' => 0,
                b'C' => 1,
                b'G' => 2,
                b'T' => 3,
                // sylph maps everything else to A (BYTE_TO_SEQ)
                _ => 0,
            }
        };
        let threshold = u64::MAX / (c as u64);
        let mut hashes: Vec<(usize, u64)> = Vec::new();
        if seq.len() >= k {
            for start in 0..=seq.len() - k {
                let mut forward = 0u64;
                let mut reverse = 0u64;
                for i in 0..k {
                    let nuc = code(seq[start + i]);
                    forward = (forward << 2) | nuc;
                    reverse |= (3 - nuc) << (2 * i);
                }
                let hash = crate::seeding::mm_hash64(forward.min(reverse));
                if hash < threshold {
                    hashes.push((start + k - 1, hash));
                }
            }
        }
        let mut counts: std::collections::HashMap<u64, usize> = Default::default();
        for &(_, hash) in &hashes {
            *counts.entry(hash).or_default() += 1;
        }
        hashes.sort_unstable();
        let mut kept = Vec::new();
        let mut last = 0usize;
        for (pos, hash) in hashes {
            if counts[&hash] > 1 {
                continue;
            }
            if last == 0 || pos - last > min_spacing {
                kept.push(hash);
                last = pos;
            }
        }
        kept
    }

    fn random_seq(seed: u64, len: usize) -> Vec<u8> {
        let mut state = seed | 1;
        let mut next = move || {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            state
        };
        (0..len).map(|_| b"ACGT"[(next() % 4) as usize]).collect()
    }

    #[test]
    fn genome_kmers_matches_reference() {
        let c = 20;
        for k in [21usize, 31] {
            for len in [0usize, 1, 10, k - 1, k, k + 1, 4 * k, 1000, 5000, 20_000] {
                let seq = random_seq(len as u64 * 31 + k as u64, len);
                let got = genome_kmers(Seeding::Scalar, &seq, c, k, DEFAULT_MIN_SPACING);
                let want = reference_genome_kmers(&seq, c, k, DEFAULT_MIN_SPACING);
                assert_eq!(got, want, "k={k} len={len}");
            }
        }
    }

    #[test]
    fn genome_kmers_spacing_and_duplicates() {
        let seq = random_seq(7, 3000);
        let dense = genome_kmers(Seeding::Scalar, &seq, 1, 31, 0);
        let spaced = genome_kmers(Seeding::Scalar, &seq, 1, 31, DEFAULT_MIN_SPACING);
        assert!(spaced.len() < dense.len());
        assert_eq!(dense, reference_genome_kmers(&seq, 1, 31, 0));

        // a tandem repeat of one unit: every k-mer is repeated -> nothing left
        let unit = random_seq(11, 100);
        let tandem: Vec<u8> = unit.iter().copied().cycle().take(3000).collect();
        assert!(genome_kmers(Seeding::Scalar, &tandem, 1, 31, 30).is_empty());
        // ... while the sample sketch keeps them
        let mut builder = SketchBuilder::new(1, 31, true, DEFAULT_FPR, Seeding::Scalar).unwrap();
        builder.add_single(&tandem);
        assert!(builder.kmers() > 0);
    }

    #[test]
    fn avx2_positions_drop_the_tail_like_the_sample_path() {
        // k=21: n-k+1 = 85 for n=105, so one k-mer is dropped per segment
        let seq = random_seq(3, 105);
        let mut scalar = Vec::new();
        let mut avx2 = Vec::new();
        fmh_seeds_positions(&seq, &mut scalar, 1, 21);
        fmh_seeds_avx2_positions(&seq, &mut avx2, 1, 21);
        assert!(avx2.len() < scalar.len());
        // positions of the AVX2 markers are a subset of the scalar ones
        for (pos, hash) in &avx2 {
            assert!(scalar.contains(&(*pos, *hash)), "pos {pos}");
        }
        // reads shorter than 2k produce nothing in the AVX2 path
        assert!(genome_kmers(Seeding::Avx2, &seq[..2 * 21 - 1], 1, 21, 30).is_empty());
    }

    #[test]
    fn record_count_and_length_limits_are_enforced() {
        let fasta = b">a\nACGTACGTACGTACGTACGTACGT\n>b\nACGTACGTACGTACGTACGTACGT\n";

        // one record allowed: the second is refused
        let mut s =
            ContigSketcher::with_limits(1, 21, 1, 30, Seeding::Scalar, 1, 0).unwrap();
        s.push(fasta).unwrap();
        let error = s.end_file().unwrap_err();
        assert!(error.contains("more than 1 records"), "{error}");
        assert!(error.contains("sylphd command line"), "{error}");

        // a record longer than the limit is refused while parsing ...
        let mut s =
            ContigSketcher::with_limits(1, 21, 1, 30, Seeding::Scalar, 0, 10).unwrap();
        let error = s.push(fasta).unwrap_err();
        assert!(error.contains("per-record limit"), "{error}");

        // ... also when it is split over several FASTA lines
        let mut s =
            ContigSketcher::with_limits(1, 21, 1, 30, Seeding::Scalar, 0, 10).unwrap();
        let error = s.push(b">a\nACGTAC\nGTACGT\n").unwrap_err();
        assert!(error.contains("per-record limit"), "{error}");

        // 0 means unlimited
        let mut s =
            ContigSketcher::with_limits(1, 21, 1, 30, Seeding::Scalar, 0, 0).unwrap();
        s.push(fasta).unwrap();
        s.end_file().unwrap();
        assert_eq!(s.seen, 2);
    }

    #[test]
    fn streams_records_and_sketches_only_long_enough_ones() {
        let fasta = b">a first\nACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGT\n>b\nACGT\n>c\n";
        let mut s = ContigSketcher::new(1, 21, 20, 30, Seeding::Scalar).unwrap();
        s.push(fasta).unwrap();
        s.end_file().unwrap();
        let records: Vec<ContigRecord> = std::iter::from_fn(|| s.take_next()).collect();
        assert_eq!(records.len(), 3);
        assert_eq!(records[0].name, "a");
        assert_eq!(records[0].length, 44);
        assert!(records[0].contig_kmers.is_some());
        assert!(records[0].sketch.is_some());
        assert_eq!(records[1].name, "b");
        assert_eq!(records[1].contig_kmers, None); // 4 < min_length
        assert_eq!(records[2].length, 0);
        assert_eq!(records[2].contig_kmers, None);
    }
}
