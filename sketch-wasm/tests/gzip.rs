//! The wasm build cannot use `DecompressionStream` for multi-member gzip, so
//! it carries its own decoder; compare it with flate2's `MultiGzDecoder`.

use std::io::{Read, Write};

use flate2::read::MultiGzDecoder;
use flate2::write::GzEncoder;
use flate2::{Compression, GzBuilder};
use sylph_sketch_wasm::gzip::{Crc32Tracker, CrcMode, GzipDecoder};

fn payload(n: usize) -> Vec<u8> {
    let mut x = 12345u64;
    (0..n)
        .map(|i| {
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            // compressible, but not trivially
            if i % 7 < 4 {
                b"ACGT"[(x % 4) as usize]
            } else {
                b"ACGTTGCA"[i % 8]
            }
        })
        .collect()
}

fn member(data: &[u8], level: Compression) -> Vec<u8> {
    let mut enc = GzEncoder::new(Vec::new(), level);
    enc.write_all(data).unwrap();
    enc.finish().unwrap()
}

fn decode(gz: &[u8], chunk: usize) -> Result<Vec<u8>, String> {
    let mut decoder = GzipDecoder::new();
    let mut out = vec![];
    for piece in gz.chunks(chunk) {
        decoder.push(piece, &mut |b| {
            out.extend_from_slice(b);
            Ok(())
        })?;
    }
    decoder.finish()?;
    Ok(out)
}

#[test]
fn multi_member_any_chunking() {
    let parts = [
        payload(0),
        payload(1),
        payload(300_000),
        payload(77),
        payload(1_000_000),
    ];
    let mut gz = vec![];
    let mut plain = vec![];
    for (i, part) in parts.iter().enumerate() {
        let level = [
            Compression::none(),
            Compression::fast(),
            Compression::best(),
        ][i % 3];
        gz.extend(member(part, level));
        plain.extend_from_slice(part);
    }
    // sanity: the reference decoder agrees on what the right answer is
    let mut reference = vec![];
    MultiGzDecoder::new(&gz[..])
        .read_to_end(&mut reference)
        .unwrap();
    assert_eq!(reference, plain);
    for chunk in [1, 2, 3, 10, 11, 4093, 65_536, usize::MAX / 2] {
        assert!(decode(&gz, chunk).unwrap() == plain, "chunk size {chunk}");
    }
}

#[test]
fn header_fields() {
    let data = payload(5000);
    let mut enc = GzBuilder::new()
        .filename("reads.fastq")
        .comment("hello")
        .extra(vec![b'B', b'C', 2, 0, 1, 2])
        .write(Vec::new(), Compression::default());
    enc.write_all(&data).unwrap();
    let gz = enc.finish().unwrap();
    for chunk in [1, 5, 1000] {
        assert_eq!(decode(&gz, chunk).unwrap(), data);
    }
}

#[test]
fn trailing_zero_padding_is_ignored_but_junk_is_not() {
    let data = payload(1000);
    let mut gz = member(&data, Compression::default());
    gz.extend([0u8; 100]);
    assert_eq!(decode(&gz, 7).unwrap(), data);
    gz.extend(b"junk");
    assert!(decode(&gz, 7).is_err());
}

#[test]
fn corruption_and_truncation_are_errors() {
    let data = payload(100_000);
    let gz = member(&data, Compression::default());
    assert!(
        decode(&gz[..gz.len() - 1], 100).is_err(),
        "truncated trailer"
    );
    assert!(decode(&gz[..gz.len() / 2], 100).is_err(), "truncated body");
    let mut bad_crc = gz.clone();
    let n = bad_crc.len();
    bad_crc[n - 8] ^= 1;
    assert!(decode(&bad_crc, 100).is_err(), "bad crc");
    let mut bad_body = gz.clone();
    bad_body[gz.len() / 2] ^= 0xff;
    assert!(decode(&bad_body, 100).is_err(), "bad body");
    assert!(decode(b"not gzip at all", 4).is_err());
    assert!(decode(b"", 4).is_err());
}

#[test]
fn decode_respects_the_output_limit() {
    // 20 MB of zeros compress to ~20 KB: one input chunk must not expand
    // into one huge output
    let data = vec![0u8; 20_000_000];
    let gz = member(&data, Compression::best());
    let mut decoder = GzipDecoder::new();
    let mut input = &gz[..];
    let (mut total, mut max_out) = (0, 0);
    while !input.is_empty() {
        let mut out = Vec::new();
        decoder.decode(&mut input, &mut out, 1 << 20).unwrap();
        max_out = max_out.max(out.len());
        total += out.len();
    }
    decoder.finish().unwrap();
    assert_eq!(total, data.len());
    assert!(max_out < 3 << 20, "output piece of {max_out} bytes");
}

/// Deferred mode: the decoder records member trailers, a `Crc32Tracker` fed
/// with the decompressed pieces (cut elsewhere than the members) checks them.
fn deferred(gz: &[u8], piece: usize) -> Result<Vec<u8>, String> {
    let mut decoder = GzipDecoder::with_mode(CrcMode::Deferred);
    let mut tracker = Crc32Tracker::default();
    let mut all = vec![];
    let mut input = gz;
    while !input.is_empty() {
        let mut out = Vec::new();
        decoder.decode(&mut input, &mut out, piece)?;
        let trailers = decoder.take_trailers();
        tracker.update(&out, &trailers)?;
        all.extend(out);
    }
    decoder.finish()?;
    Ok(all)
}

#[test]
fn deferred_crc_is_checked_elsewhere() {
    let parts = [payload(10), payload(300_000), payload(0), payload(5000)];
    let mut gz = vec![];
    let mut plain = vec![];
    for part in &parts {
        gz.extend(member(part, Compression::none()));
        plain.extend_from_slice(part);
    }
    for piece in [1, 100, 4096, 1 << 20] {
        assert_eq!(deferred(&gz, piece).unwrap(), plain, "piece {piece}");
    }
    // flip one payload byte inside a stored block: deflate stays valid, CRC does not
    let mut bad = gz.clone();
    let at = 10 + 5 + 100;
    bad[at] ^= 1;
    assert!(deferred(&bad, 4096).is_err());
    assert!(decode(&bad, 4096).is_err());
}
