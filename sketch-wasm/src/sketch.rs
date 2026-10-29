//! Read-sketch accumulation: what pysylph's `Sketcher.sketch_single` and
//! `Sketcher.sketch_paired` do, minus the Python iteration.
//!
//! Ported from:
//! * pysylph (MIT, Martin Larralde), `src/pysylph/lib.rs` on branch `multi-db`:
//!   `Sketcher::sketch_single` (lines 1148-1207) and `Sketcher::sketch_paired`
//!   (lines 1220-1318), `Sketcher::__new__` (defaults, `k` in {21, 31}).
//! * sylph (MIT OR Apache-2.0, Copyright (c) 2023 Jim Shaw), commit
//!   4b5c2beb05312d9da9006529b12ad24ec47f0914, `src/sketch.rs`:
//!   `pair_kmer_single` (lines 664-696), `pair_kmer` (lines 698-728),
//!   `dup_removal_lsh_full_exact` (lines 730-771) and `dup_removal_lsh_full`
//!   (lines 773-809); `src/constants.rs`: `MAX_DEDUP_COUNT`, `DEFAULT_FPR`,
//!   `DEFAULT_RNG_SEED`; `src/types.rs`: `SequencesSketch` (layout).

use fxhash::{FxHashMap, FxHashSet, FxHasher64};
use rand::SeedableRng;
use rand_xoshiro::Xoshiro256PlusPlus;
use scalable_cuckoo_filter::{ScalableCuckooFilter, ScalableCuckooFilterBuilder};

use crate::seeding::{extract_markers, Seeding, BYTE_TO_SEQ};

/// sylph `constants.rs`.
pub const MAX_DEDUP_COUNT: u32 = 4;
pub const DEFAULT_FPR: f64 = 0.0001;
pub const DEFAULT_RNG_SEED: u64 = 7;

/// sylph `sketch.rs` line 26: `pub type Marker = u32;`
type Marker = u32;
type Pair = ([Marker; 2], [Marker; 2]);
type PairKey = (u64, [Marker; 2]);

/// sylph's single-end read dedup only looks at reads of at most this length
/// (`sketch_single`: `if seq.len() > 400 { None }`).
const MAX_PAIR_KMER_READ_LEN: usize = 400;

/// pysylph passes `fxhash::FxHasher::default()`, i.e. the pointer-width hasher.
/// That is `FxHasher64` on the x86_64/aarch64 pysylph is built for, so the
/// 64-bit one is used explicitly: wasm32 would otherwise get `FxHasher32`.
type ApproxSet = ScalableCuckooFilter<PairKey, FxHasher64, Xoshiro256PlusPlus>;

/// sylph `sketch.rs` lines 664-696.
#[inline]
pub fn pair_kmer_single(s1: &[u8]) -> Option<Pair> {
    let k = std::mem::size_of::<Marker>() * 4;
    if s1.len() < 4 * k + 2 {
        None
    } else {
        let mut kmer_f: Marker = 0;
        let mut kmer_g: Marker = 0;
        let mut kmer_r: Marker = 0;
        let mut kmer_t: Marker = 0;
        let halfway = s1.len() / 2;
        for i in 0..k {
            let nuc_1 = BYTE_TO_SEQ[s1[2 * i] as usize] as Marker;
            let nuc_2 = BYTE_TO_SEQ[s1[2 * i + halfway] as usize] as Marker;
            let nuc_3 = BYTE_TO_SEQ[s1[1 + 2 * i] as usize] as Marker;
            let nuc_4 = BYTE_TO_SEQ[s1[1 + 2 * i + halfway] as usize] as Marker;

            kmer_f <<= 2;
            kmer_f |= nuc_1;

            kmer_r <<= 2;
            kmer_r |= nuc_2;

            kmer_g <<= 2;
            kmer_g |= nuc_3;

            kmer_t <<= 2;
            kmer_t |= nuc_4;
        }
        Some(([kmer_f, kmer_r], [kmer_g, kmer_t]))
    }
}

/// sylph `sketch.rs` lines 698-728.
#[inline]
pub fn pair_kmer(s1: &[u8], s2: &[u8]) -> Option<Pair> {
    let k = std::mem::size_of::<Marker>() * 4;
    if s1.len() < 2 * k + 1 || s2.len() < 2 * k + 1 {
        None
    } else {
        let mut kmer_f: Marker = 0;
        let mut kmer_g: Marker = 0;
        let mut kmer_r: Marker = 0;
        let mut kmer_t: Marker = 0;
        for i in 0..k {
            let nuc_1 = BYTE_TO_SEQ[s1[2 * i] as usize] as Marker;
            let nuc_2 = BYTE_TO_SEQ[s2[2 * i] as usize] as Marker;
            let nuc_3 = BYTE_TO_SEQ[s1[1 + 2 * i] as usize] as Marker;
            let nuc_4 = BYTE_TO_SEQ[s2[1 + 2 * i] as usize] as Marker;

            kmer_f <<= 2;
            kmer_f |= nuc_1;

            kmer_r <<= 2;
            kmer_r |= nuc_2;

            kmer_g <<= 2;
            kmer_g |= nuc_3;

            kmer_t <<= 2;
            kmer_t |= nuc_4;
        }
        Some(([kmer_f, kmer_r], [kmer_g, kmer_t]))
    }
}

/// sylph `sketch.rs` lines 730-771 (`dup_removal_lsh_full_exact`).
fn dup_removal_exact(
    kmer_counts: &mut FxHashMap<u64, u32>,
    pair_set: &mut FxHashSet<PairKey>,
    km: u64,
    kmer_pair: Option<Pair>,
    no_dedup: bool,
    threshold: Option<u32>,
) {
    let c = kmer_counts.entry(km).or_insert(0);
    let c_threshold = threshold.unwrap_or(u32::MAX);
    if !no_dedup && *c < c_threshold {
        if let Some(doublepairs) = kmer_pair {
            let mut ret = false;
            if pair_set.contains(&(km, doublepairs.0)) {
                if *c > 0 {
                    ret = true;
                }
            } else {
                pair_set.insert((km, doublepairs.0));
            }
            if pair_set.contains(&(km, doublepairs.1)) {
                if *c > 0 {
                    ret = true;
                }
            } else {
                pair_set.insert((km, doublepairs.1));
            }
            if ret {
                return;
            }
        }
    }
    *c += 1;
}

/// sylph `sketch.rs` lines 773-809 (`dup_removal_lsh_full`).
fn dup_removal_approx(
    kmer_counts: &mut FxHashMap<u64, u32>,
    pair_set: &mut ApproxSet,
    km: u64,
    kmer_pair: Option<Pair>,
    no_dedup: bool,
) {
    let c = kmer_counts.entry(km).or_insert(0);
    if !no_dedup {
        if let Some(doublepairs) = kmer_pair {
            let mut ret = false;
            if pair_set.contains(&(km, doublepairs.0)) {
                if *c > 0 {
                    ret = true;
                }
            } else {
                pair_set.insert(&(km, doublepairs.0));
            }
            if pair_set.contains(&(km, doublepairs.1)) {
                if *c > 0 {
                    ret = true;
                }
            } else {
                pair_set.insert(&(km, doublepairs.1));
            }
            if ret {
                return;
            }
        }
    }
    *c += 1;
}

fn new_approx_set(fpr: f64) -> ApproxSet {
    ScalableCuckooFilterBuilder::new()
        .initial_capacity(10_000_000)
        .false_positive_probability(fpr)
        .hasher(FxHasher64::default())
        .rng(Xoshiro256PlusPlus::seed_from_u64(DEFAULT_RNG_SEED))
        .finish()
}

/// Accumulator behind both `sketch_single` and `sketch_paired`.
///
/// One instance sketches one sample, in either single or paired mode; the mode
/// is fixed by the first `add_*` call.
pub struct SketchBuilder {
    pub c: usize,
    pub k: usize,
    deduplicate: bool,
    fpr: f64,
    seeding: Seeding,
    kmer_counts: FxHashMap<u64, u32>,
    exact_pairs: FxHashSet<PairKey>,
    approx_pairs: Option<ApproxSet>,
    mean_read_length: f64,
    /// Number of reads (or pairs) seen.
    pub reads: u64,
    /// Number of bases seen (both mates for pairs).
    pub bases: u64,
    /// G and C bases (either case) among `bases`.
    pub gc: u64,
    scratch1: Vec<u64>,
    scratch2: Vec<u64>,
}

pub(crate) fn count_gc(seq: &[u8]) -> u64 {
    seq.iter()
        .filter(|b| matches!(b, b'G' | b'C' | b'g' | b'c'))
        .count() as u64
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
        Ok(Self {
            c,
            k,
            deduplicate,
            fpr,
            seeding,
            kmer_counts: FxHashMap::default(),
            exact_pairs: FxHashSet::default(),
            approx_pairs: None,
            mean_read_length: 0.0,
            reads: 0,
            bases: 0,
            gc: 0,
            scratch1: Vec::new(),
            scratch2: Vec::new(),
        })
    }

    /// Number of distinct sketched k-mers so far.
    pub fn kmers(&self) -> usize {
        self.kmer_counts.len()
    }

    /// One single-end read: `Sketcher.sketch_single`, loop body.
    pub fn add_single(&mut self, seq: &[u8]) {
        let kmer_pair = if seq.len() > MAX_PAIR_KMER_READ_LEN {
            None
        } else {
            pair_kmer_single(seq)
        };
        self.scratch1.clear();
        extract_markers(self.seeding, seq, &mut self.scratch1, self.c, self.k);
        for &km in &self.scratch1 {
            dup_removal_exact(
                &mut self.kmer_counts,
                &mut self.exact_pairs,
                km,
                kmer_pair,
                !self.deduplicate,
                Some(MAX_DEDUP_COUNT),
            );
        }
        self.reads += 1;
        self.bases += seq.len() as u64;
        self.gc += count_gc(seq);
        self.mean_read_length += ((seq.len() as f64) - self.mean_read_length) / self.reads as f64;
    }

    /// One read pair: `Sketcher.sketch_paired`, loop body.
    pub fn add_pair(&mut self, seq1: &[u8], seq2: &[u8]) {
        if self.approx_pairs.is_none() && self.fpr != 0.0 {
            // ~tens of MB, so only allocated once a pair is actually seen
            self.approx_pairs = Some(new_approx_set(self.fpr));
        }
        self.scratch1.clear();
        self.scratch2.clear();
        extract_markers(self.seeding, seq1, &mut self.scratch1, self.c, self.k);
        extract_markers(self.seeding, seq2, &mut self.scratch2, self.c, self.k);
        let kmer_pair = pair_kmer(seq1, seq2);

        self.reads += 1;
        self.bases += (seq1.len() + seq2.len()) as u64;
        self.gc += count_gc(seq1) + count_gc(seq2);
        self.mean_read_length += ((seq1.len() as f64) - self.mean_read_length) / self.reads as f64;

        let no_dedup = !self.deduplicate;
        for (i, &km) in self.scratch1.iter().chain(self.scratch2.iter()).enumerate() {
            // sylph skips mate-2 k-mers that mate 1 already produced
            if i >= self.scratch1.len() && self.scratch1.contains(&km) {
                continue;
            }
            match self.approx_pairs.as_mut() {
                Some(approx) => {
                    dup_removal_approx(&mut self.kmer_counts, approx, km, kmer_pair, no_dedup)
                }
                None => dup_removal_exact(
                    &mut self.kmer_counts,
                    &mut self.exact_pairs,
                    km,
                    kmer_pair,
                    no_dedup,
                    None,
                ),
            }
        }
    }

    /// Serializes to sylph's `SequencesSketch` (bincode 1.x legacy config:
    /// fixint, little endian). See `src/sylphd/sketchcheck.py` and sylph
    /// `src/types.rs` lines 145-155. The k-mer entries are written sorted by
    /// k-mer so the output is deterministic; sylph writes them in hash-map
    /// order, which readers do not depend on.
    pub fn serialize(&self, file_name: &str, paired: bool) -> Vec<u8> {
        let mut entries: Vec<(u64, u32)> = self.kmer_counts.iter().map(|(&k, &v)| (k, v)).collect();
        entries.sort_unstable_by_key(|e| e.0);

        let mut out = Vec::with_capacity(8 + entries.len() * 12 + 64 + file_name.len());
        out.extend_from_slice(&(entries.len() as u64).to_le_bytes());
        for (km, count) in entries {
            out.extend_from_slice(&km.to_le_bytes());
            out.extend_from_slice(&count.to_le_bytes());
        }
        out.extend_from_slice(&(self.c as u64).to_le_bytes());
        out.extend_from_slice(&(self.k as u64).to_le_bytes());
        out.extend_from_slice(&(file_name.len() as u64).to_le_bytes());
        out.extend_from_slice(file_name.as_bytes());
        out.push(0); // sample_name: None
        out.push(paired as u8);
        out.extend_from_slice(&self.mean_read_length.to_le_bytes());
        out
    }
}
