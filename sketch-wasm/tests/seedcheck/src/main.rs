use sylph::seeding::fmh_seeds;
use sylph_sketch_wasm::seeding as mine;
fn main() {
    let mut x: u64 = 88172645463325252;
    let mut rnd = || { x ^= x << 13; x ^= x >> 7; x ^= x << 17; x };
    let mut n = 0usize;
    for len in (0..3000).chain([10_000, 100_000, 1_000_000]) {
        for _ in 0..30 {
            let s: Vec<u8> = (0..len).map(|_| b"ACGTNacgtn"[(rnd() % 10) as usize]).collect();
            for (c, k) in [(1usize, 31usize), (200, 31), (3, 21), (1, 21), (20, 31)] {
                let (mut a, mut b, mut cc, mut d) = (vec![], vec![], vec![], vec![]);
                fmh_seeds(&s, &mut a, c, k);
                mine::fmh_seeds(&s, &mut b, c, k);
                assert_eq!(a, b, "scalar len {len} c {c} k {k}");
                sylph::sketch::extract_markers(&s, &mut cc, c, k);
                mine::fmh_seeds_avx2(&s, &mut d, c, k);
                assert_eq!(cc, d, "avx2 len {len} c {c} k {k}");
                n += 1;
            }
        }
    }
    println!("{n} comparisons OK (avx2 detected: {})", is_x86_feature_detected!("avx2"));
}
