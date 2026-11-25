//! Tests for `.sylsp` header validation and `c`/`k` compatibility checks
//! (mirroring `tests/test_sketchcheck.py`).

use std::fs;
use std::path::{Path, PathBuf};

use sylphd_core::sketchcheck::{
    check_sketch_compatibility, parse_sketch_header, read_sketch_header, validate_sketch_bytes,
};
use sylphd_core::stream::StreamSketcher;

fn data_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .join("tests/data")
}

struct BuildArgs<'a> {
    count: Option<u64>,
    entries: &'a [(u64, u32)],
    c: u64,
    k: u64,
    file_name: &'a [u8],
    file_name_length: Option<u64>,
    sample_name: Option<&'a [u8]>,
    sample_name_length: Option<u64>,
    sample_tag: Option<u8>,
    paired: u8,
    mean_read_length: f64,
}

impl Default for BuildArgs<'_> {
    fn default() -> Self {
        Self {
            count: None,
            entries: &[(7, 1), (9, 3)],
            c: 200,
            k: 31,
            file_name: b"sample.fq",
            file_name_length: None,
            sample_name: None,
            sample_name_length: None,
            sample_tag: None,
            paired: 0,
            mean_read_length: 100.0,
        }
    }
}

fn build_sketch(args: BuildArgs<'_>) -> Vec<u8> {
    let mut out = Vec::new();
    let count = args.count.unwrap_or(args.entries.len() as u64);
    out.extend_from_slice(&count.to_le_bytes());
    for &(kmer, cnt) in args.entries {
        out.extend_from_slice(&kmer.to_le_bytes());
        out.extend_from_slice(&cnt.to_le_bytes());
    }
    out.extend_from_slice(&args.c.to_le_bytes());
    out.extend_from_slice(&args.k.to_le_bytes());
    let fn_len = args.file_name_length.unwrap_or(args.file_name.len() as u64);
    out.extend_from_slice(&fn_len.to_le_bytes());
    out.extend_from_slice(args.file_name);

    let tag = args
        .sample_tag
        .unwrap_or(if args.sample_name.is_some() { 1 } else { 0 });
    out.push(tag);
    if tag == 1 {
        let name = args.sample_name.unwrap_or(b"");
        let sn_len = args.sample_name_length.unwrap_or(name.len() as u64);
        out.extend_from_slice(&sn_len.to_le_bytes());
        out.extend_from_slice(name);
    }
    out.push(args.paired);
    out.extend_from_slice(&args.mean_read_length.to_le_bytes());
    out
}

#[test]
fn accepts_all_stored_fixture_sketches() {
    let data = data_dir();
    let mut sylsp_files: Vec<PathBuf> = fs::read_dir(&data)
        .unwrap()
        .filter_map(|entry| {
            let path = entry.unwrap().path();
            (path.extension().and_then(|e| e.to_str()) == Some("sylsp")).then_some(path)
        })
        .collect();
    sylsp_files.sort();
    assert!(!sylsp_files.is_empty());

    for path in &sylsp_files {
        let bytes = fs::read(path).unwrap();
        let header = parse_sketch_header(&bytes).unwrap();
        assert_eq!(validate_sketch_bytes(&bytes).unwrap(), (header.c, header.k));
        // Seek-based file reader must agree with in-memory parser
        assert_eq!(read_sketch_header(path).unwrap(), header);
    }

    assert!(!parse_sketch_header(&fs::read(data.join("k12.R1.sylsp")).unwrap())
        .unwrap()
        .paired);
    assert!(parse_sketch_header(&fs::read(data.join("k12.paired.sylsp")).unwrap())
        .unwrap()
        .paired);
}

#[test]
fn accepts_fresh_single_and_paired_sketches() {
    let data = data_dir();

    let mut single = StreamSketcher::new(200, 31, true).unwrap();
    single.push_file(data.join("k12.R1.fq.gz")).unwrap();
    let expected_kmers = single.kmers() as u64;
    let bytes = single.finish("single").unwrap();
    let header = parse_sketch_header(&bytes).unwrap();
    assert_eq!((header.c, header.k), (200, 31));
    assert!(!header.paired);
    assert_eq!(header.file_name, "single");
    assert_eq!(header.sample_name, None);
    assert_eq!(header.kmer_count, expected_kmers);

    let mut paired = StreamSketcher::new(200, 31, true).unwrap();
    paired
        .push_paired_files(data.join("k12.R1.fq.gz"), data.join("k12.R2.fq.gz"))
        .unwrap();
    let expected_kmers = paired.kmers() as u64;
    let bytes = paired.finish("paired").unwrap();
    let header = parse_sketch_header(&bytes).unwrap();
    assert_eq!((header.c, header.k), (200, 31));
    assert!(header.paired);
    assert_eq!(header.file_name, "paired");
    assert_eq!(header.kmer_count, expected_kmers);
}

#[test]
fn accepts_trailing_bytes_optional_sample_name_and_empty_kmers() {
    let mut data = build_sketch(BuildArgs::default());
    data.extend_from_slice(b"\x00trailing");
    assert_eq!(validate_sketch_bytes(&data).unwrap(), (200, 31));

    let with_name = parse_sketch_header(&build_sketch(BuildArgs {
        sample_name: Some(b"sample-a"),
        ..Default::default()
    }))
    .unwrap();
    assert_eq!(with_name.sample_name.as_deref(), Some("sample-a"));
    assert_eq!(
        parse_sketch_header(&build_sketch(BuildArgs::default()))
            .unwrap()
            .sample_name,
        None
    );

    let empty = parse_sketch_header(&build_sketch(BuildArgs {
        entries: &[],
        ..Default::default()
    }))
    .unwrap();
    assert_eq!(empty.kmer_count, 0);
    assert_eq!((empty.c, empty.k), (200, 31));
}

#[test]
fn rejects_malformed_and_every_truncation() {
    assert!(validate_sketch_bytes(b"")
        .unwrap_err()
        .0
        .contains("too short"));

    // 8-byte allocation bombs, including u64 overflow cases
    for count in [100_000_000u64, 1u64 << 63, u64::MAX] {
        let err = validate_sketch_bytes(&count.to_le_bytes()).unwrap_err();
        assert!(err.0.contains("declares"), "{err}");
    }

    // Count one too large
    let mut patched = build_sketch(BuildArgs {
        entries: &[(1, 1), (2, 2)],
        ..Default::default()
    });
    patched[..8].copy_from_slice(&3u64.to_le_bytes());
    assert!(validate_sketch_bytes(&patched)
        .unwrap_err()
        .0
        .contains("declares"));

    // Every single truncation point rejected
    for data in [
        build_sketch(BuildArgs::default()),
        build_sketch(BuildArgs {
            sample_name: Some(b"sample-a"),
            paired: 1,
            ..Default::default()
        }),
    ] {
        for cut in 0..data.len() {
            assert!(
                validate_sketch_bytes(&data[..cut]).is_err(),
                "cut at {cut}/{} was accepted",
                data.len()
            );
        }
    }

    // Bad sample_name tag
    for tag in [2u8, 3, 255] {
        let err = parse_sketch_header(&build_sketch(BuildArgs {
            sample_tag: Some(tag),
            ..Default::default()
        }))
        .unwrap_err();
        assert!(err.0.contains("tag"), "{err}");
    }

    // Some tag without length
    let some_empty = build_sketch(BuildArgs {
        sample_tag: Some(1),
        sample_name: Some(b""),
        ..Default::default()
    });
    let err = parse_sketch_header(&some_empty[..some_empty.len() - 17]).unwrap_err();
    assert!(err.0.contains("sample_name"), "{err}");

    // Bad paired flag
    for value in [2u8, 3, 255] {
        let err = parse_sketch_header(&build_sketch(BuildArgs {
            paired: value,
            ..Default::default()
        }))
        .unwrap_err();
        assert!(err.0.contains("paired"), "{err}");
    }

    // Invalid UTF-8 in file_name and sample_name
    assert!(parse_sketch_header(&build_sketch(BuildArgs {
        file_name: b"sample\xff.fq",
        ..Default::default()
    }))
    .unwrap_err()
    .0
    .contains("UTF-8"));
    assert!(parse_sketch_header(&build_sketch(BuildArgs {
        sample_name: Some(b"\xc3"),
        ..Default::default()
    }))
    .unwrap_err()
    .0
    .contains("UTF-8"));

    // Huge string lengths
    assert!(parse_sketch_header(&build_sketch(BuildArgs {
        file_name_length: Some(1u64 << 62),
        ..Default::default()
    }))
    .unwrap_err()
    .0
    .contains("file_name"));
    assert!(parse_sketch_header(&build_sketch(BuildArgs {
        sample_name: Some(b"x"),
        sample_name_length: Some(1u64 << 62),
        ..Default::default()
    }))
    .unwrap_err()
    .0
    .contains("sample_name"));

    // Non-finite mean_read_length
    for value in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
        let err = parse_sketch_header(&build_sketch(BuildArgs {
            mean_read_length: value,
            ..Default::default()
        }))
        .unwrap_err();
        assert!(err.0.contains("mean_read_length"), "{err}");
    }
}

#[test]
fn check_sketch_compatibility_rules() {
    let header = parse_sketch_header(&build_sketch(BuildArgs {
        c: 200,
        k: 31,
        ..Default::default()
    }))
    .unwrap();

    // Exact match and denser sketch (c=100 against db c=200) are OK
    check_sketch_compatibility(&header, 200, 31, Some("s1")).unwrap();
    check_sketch_compatibility(&header, 400, 31, None).unwrap();

    // k mismatch fails
    let err = check_sketch_compatibility(&header, 200, 21, Some("s1")).unwrap_err();
    assert!(err.0.contains("s1: "), "{err}");
    assert!(err.0.contains("k=31"), "{err}");
    assert!(err.0.contains("k=21"), "{err}");

    // Sparser sketch (c=200 > db c=100) fails
    let err = check_sketch_compatibility(&header, 100, 31, None).unwrap_err();
    assert!(err.0.contains("sparser"), "{err}");
    assert!(err.0.contains("c=200"), "{err}");
    assert!(err.0.contains("c=100"), "{err}");
}
