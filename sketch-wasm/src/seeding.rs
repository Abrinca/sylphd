//! Minimizer-style k-mer seeding.
//!
//! Ported from sylph (https://github.com/bluenote-1577/sylph, MIT OR Apache-2.0,
//! Copyright (c) 2023 Jim Shaw), commit 4b5c2beb05312d9da9006529b12ad24ec47f0914
//! (branch `expose-library-api` of MrTomRod/sylph, the revision pysylph pins):
//!
//! * `BYTE_TO_SEQ`  <- `src/types.rs` lines 50-59
//! * `mm_hash64`    <- `src/seeding.rs` lines 3-14
//! * `fmh_seeds`    <- `src/seeding.rs` lines 86-146 (the scalar path of
//!   `sketch::extract_markers`; sylph's AVX2 variant in `avx2_seeding.rs` is
//!   tested to produce the same set of seeds, see `tests/pysylph_equality.rs`)

pub const BYTE_TO_SEQ: [u8; 256] = [
    0, 1, 2, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 1, 0, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 3, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 1, 0, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 3, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
];

#[inline]
pub fn mm_hash64(kmer: u64) -> u64 {
    // NB: sylph's own comment says "this is bugged" (`!key.wrapping_add(..)`
    // instead of `(!key).wrapping_add(..)`). It must be kept as is: the hash
    // values end up in sketches and databases and have to match.
    let mut key = kmer;
    key = !key.wrapping_add(key << 21);
    key ^= key >> 24;
    key = (key.wrapping_add(key << 3)).wrapping_add(key << 8);
    key ^= key >> 14;
    key = (key.wrapping_add(key << 2)).wrapping_add(key << 4);
    key ^= key >> 28;
    key = key.wrapping_add(key << 31);
    key
}

/// Appends the hashed canonical k-mers of `string` whose hash is below
/// `u64::MAX / c` (a ~1/c subsample) to `kmer_vec`.
///
/// Non-ACGT bytes are treated as `A` (via `BYTE_TO_SEQ`), exactly like sylph.
pub fn fmh_seeds(string: &[u8], kmer_vec: &mut Vec<u64>, c: usize, k: usize) {
    if string.len() < k {
        return;
    }

    let mut rolling_kmer_f_marker: u64 = 0;
    let mut rolling_kmer_r_marker: u64 = 0;

    let marker_reverse_shift_dist = 2 * (k - 1);
    let marker_mask = u64::MAX >> (std::mem::size_of::<u64>() * 8 - 2 * k);
    let marker_rev_mask = !(3 << (2 * k - 2));
    let len = string.len();

    let threshold_marker = u64::MAX / (c as u64);
    for &byte in &string[..k - 1] {
        let nuc_f = BYTE_TO_SEQ[byte as usize] as u64;
        let nuc_r = 3 - nuc_f;
        rolling_kmer_f_marker <<= 2;
        rolling_kmer_f_marker |= nuc_f;
        rolling_kmer_r_marker >>= 2;
        rolling_kmer_r_marker |= nuc_r << marker_reverse_shift_dist;
    }
    for &byte in &string[k - 1..len] {
        let nuc_f = BYTE_TO_SEQ[byte as usize] as u64;
        let nuc_r = 3 - nuc_f;
        rolling_kmer_f_marker <<= 2;
        rolling_kmer_f_marker |= nuc_f;
        rolling_kmer_f_marker &= marker_mask;
        rolling_kmer_r_marker >>= 2;
        rolling_kmer_r_marker &= marker_rev_mask;
        rolling_kmer_r_marker |= nuc_r << marker_reverse_shift_dist;

        let canonical_kmer_marker = if rolling_kmer_f_marker < rolling_kmer_r_marker {
            rolling_kmer_f_marker
        } else {
            rolling_kmer_r_marker
        };
        let hash_marker = mm_hash64(canonical_kmer_marker);

        if hash_marker < threshold_marker {
            kmer_vec.push(hash_marker);
        }
    }
}

/// Seeding strategy. Both pick the same hashed canonical k-mers, but sylph's
/// x86_64 AVX2 path (the one used on virtually every x86 machine) differs from
/// the scalar path at the ends of reads, see [`fmh_seeds_avx2`].
#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
pub enum Seeding {
    /// Identical to sylph / pysylph on x86_64 with AVX2 (the common case).
    #[default]
    Avx2,
    /// Identical to sylph / pysylph on CPUs without AVX2 and on aarch64
    /// (Apple silicon): every k-mer of every read is considered.
    Scalar,
}

pub fn extract_markers(
    seeding: Seeding,
    string: &[u8],
    kmer_vec: &mut Vec<u64>,
    c: usize,
    k: usize,
) {
    match seeding {
        Seeding::Avx2 => fmh_seeds_avx2(string, kmer_vec, c, k),
        Seeding::Scalar => fmh_seeds(string, kmer_vec, c, k),
    }
}

/// Scalar emulation of sylph's `extract_markers_avx2` (`src/avx2_seeding.rs`
/// lines 31-148, same commit), which `sketch::extract_markers` selects on
/// x86_64 when AVX2 is available.
///
/// The read's `n - k + 1` k-mer start positions are split into four equal
/// segments of `(n - k + 1) / 4` positions, processed in lock-step (one
/// k-mer of each segment per step, in segment order). The consequences,
/// reproduced here so sketches are bit-identical to pysylph's on x86_64:
///
/// * the last `(n - k + 1) % 4` k-mers of a read are never seen;
/// * a read of exactly `k` bases yields nothing;
/// * k-mers of one read are emitted in the interleaved order above (this only
///   matters for sylph's order-dependent read deduplication).
pub fn fmh_seeds_avx2(string: &[u8], kmer_vec: &mut Vec<u64>, c: usize, k: usize) {
    if string.len() < k + 1 {
        return;
    }
    let seg = (string.len() - k + 1) / 4;
    if seg == 0 {
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
            let hash = mm_hash64(f[s].min(r[s]));
            if hash < threshold {
                kmer_vec.push(hash);
            }
        }
    }
}
