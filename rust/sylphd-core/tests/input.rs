//! Tests for sample naming, input specifications, assembly stats, contig
//! filtering/skip counters, native `Read` sketching and optional `rayon`
//! parallelism (mirroring `tests/test_sketching.py` and the input/contig parts
//! of `tests/test_assembly_per_contig.py`).

use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use flate2::write::GzEncoder;
use flate2::Compression;
use sylphd_core::contig::{ContigRecord, ContigSketcher, DEFAULT_MIN_SPACING};
use sylphd_core::input::{
    build_sample_inputs, check_sample_name, parse_multi_file_spec, resolve_pair_paths,
    sample_name_from_path, sketch_assembly_file, sketch_assembly_reader, sketch_contigs_file,
    sketch_contigs_reader, sketch_sample, unique_contig_name, AssemblySketcher, ContigFilter,
    SampleCliInputs, SampleFiles, SampleInput, Skipped, DEFAULT_MIN_NUMBER_KMERS,
    MAX_SAMPLE_NAME_CHARS,
};
use sylphd_core::io::{for_each_chunk, open_maybe_gzip};
use sylphd_core::sketchcheck::{check_sketch_compatibility, parse_sketch_header};
use sylphd_core::stream::StreamSketcher;
use sylphd_core::Seeding;

fn data_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .join("tests/data")
}

// ---------------------------------------------------------------------------
// Sample names (tests/test_sketching.py::TestSampleNames)
// ---------------------------------------------------------------------------

#[test]
fn sample_name_from_path_cases() {
    let cases = [
        ("reads.fastq.gz", false, "reads"),
        ("reads.fq", false, "reads"),
        ("reads.fasta.gz", false, "reads"),
        ("reads.sylsp", false, "reads"),
        ("/data/sample-a_R1.fastq.gz", true, "sample-a"),
        ("/data/sample-a_R2.fq", true, "sample-a"),
        ("/data/sample-a.R1.fq.gz", true, "sample-a"),
        ("/data/sample-a.R2.fq.gz", true, "sample-a"),
        ("/data/sample-a_1.fq.gz", true, "sample-a"),
        ("/data/sample-a_2.fq.gz", true, "sample-a"),
        ("/data/sample-a_S1_L001_R1_001.fastq.gz", true, "sample-a_S1_L001"),
        ("/data/sample-a_R1.fastq.gz", false, "sample-a_R1"),
        ("/data/sample_001.fq.gz", true, "sample_001"),
        ("/data/sample_1_001.fq.gz", true, "sample"),
        (".fastq.gz", false, "sample"),
        ("dir/reads.FNA.BGZF", false, "reads"),
    ];
    for (path, paired, expected) in cases {
        assert_eq!(
            sample_name_from_path(path, paired),
            expected,
            "path={path:?} paired={paired}"
        );
    }
}

#[test]
fn bad_sample_names_are_input_errors() {
    let too_long = "x".repeat(MAX_SAMPLE_NAME_CHARS + 1);
    let exact_max = "x".repeat(MAX_SAMPLE_NAME_CHARS);
    assert!(check_sample_name(&exact_max).is_ok());

    for bad in ["a\tb", "a\nb", "a\x00b", "a\x7fb", too_long.as_str()] {
        let err = SampleInput::single(bad, vec!["reads.fq".into()]).unwrap_err();
        assert!(err.0.contains("sample name"), "{err}");

        let spec = format!("{bad}:reads.fq");
        let err = parse_multi_file_spec(&spec, None).unwrap_err();
        assert!(err.0.contains("sample name"), "{err}");
    }
}

// ---------------------------------------------------------------------------
// Multi-file specs (tests/test_sketching.py::TestMultiFileSpecs)
// ---------------------------------------------------------------------------

#[test]
fn parse_multi_file_spec_single_and_paired() {
    let single = parse_multi_file_spec("s1:a.fq,b.fq", None).unwrap();
    assert_eq!(single.name, "s1");
    assert_eq!(
        single.files,
        SampleFiles::Single(vec!["a.fq".into(), "b.fq".into()])
    );
    assert!(!single.is_paired());
    assert!(!single.is_interleaved());

    let paired = parse_multi_file_spec("s1:a1.fq+a2.fq,b1.fq+b2.fq", None).unwrap();
    assert_eq!(paired.name, "s1");
    assert_eq!(
        paired.files,
        SampleFiles::Paired(vec![
            ("a1.fq".into(), "a2.fq".into()),
            ("b1.fq".into(), "b2.fq".into()),
        ])
    );
    assert!(paired.is_paired());
    assert_eq!(paired.paths(), vec!["a1.fq", "a2.fq", "b1.fq", "b2.fq"]);

    let defaulted = parse_multi_file_spec("reads.fq.gz", None).unwrap();
    assert_eq!(defaulted.name, "reads");
    assert_eq!(defaulted.files, SampleFiles::Single(vec!["reads.fq.gz".into()]));

    let paired_default = parse_multi_file_spec("s1_R1.fq.gz+s1_R2.fq.gz", None).unwrap();
    assert_eq!(paired_default.name, "s1");
    assert!(paired_default.is_paired());

    let with_default = parse_multi_file_spec("a.fq,b.fq", Some("custom")).unwrap();
    assert_eq!(with_default.name, "custom");
}

#[test]
fn parse_multi_file_spec_errors() {
    assert!(parse_multi_file_spec(":a.fq", None)
        .unwrap_err()
        .0
        .contains("empty sample name"));
    assert!(parse_multi_file_spec("s1:a+b+c", None)
        .unwrap_err()
        .0
        .contains("at most two"));
    assert!(parse_multi_file_spec("s1:", None)
        .unwrap_err()
        .0
        .contains("no input files"));
    assert!(parse_multi_file_spec("s1:a.fq,b1.fq+b2.fq", None)
        .unwrap_err()
        .0
        .contains("mixing"));
}

// ---------------------------------------------------------------------------
// Build samples & --sample-name (tests/test_sketching.py::TestBuildSamples)
// ---------------------------------------------------------------------------

#[test]
fn build_sample_inputs_ordering_and_validation() {
    let samples = build_sample_inputs(&SampleCliInputs {
        files: vec!["a.fq".into()],
        reads: vec!["b.fq".into()],
        first: vec!["c_R1.fq".into()],
        second: vec!["c_R2.fq".into()],
        interleaved: vec!["d_R1.fq".into()],
        specs: vec!["e:x.fq".into()],
        sample_name: None,
    })
    .unwrap();
    let names: Vec<&str> = samples.iter().map(|s| s.name.as_str()).collect();
    assert_eq!(names, vec!["a", "b", "c", "d", "e"]);
    assert!(samples[2].is_paired());
    assert!(samples[3].is_interleaved());
    assert!(!samples[3].is_paired());
}

#[test]
fn build_sample_inputs_errors_and_sample_name_override() {
    assert!(resolve_pair_paths(&["a.fq".into()], &["b.fq".into(), "c.fq".into()])
        .unwrap_err()
        .0
        .contains("differ in length"));

    let err = build_sample_inputs(&SampleCliInputs {
        files: vec!["a/x.fq".into(), "b/x.fq".into()],
        ..Default::default()
    })
    .unwrap_err();
    assert!(err.0.contains("duplicate sample name"), "{err}");

    let err = build_sample_inputs(&SampleCliInputs::default()).unwrap_err();
    assert!(err.0.contains("no input samples"), "{err}");

    // --sample-name with one sample succeeds
    let one = build_sample_inputs(&SampleCliInputs {
        files: vec!["a.fq".into()],
        sample_name: Some("renamed".into()),
        ..Default::default()
    })
    .unwrap();
    assert_eq!(one[0].name, "renamed");

    // --sample-name with multiple samples fails
    let err = build_sample_inputs(&SampleCliInputs {
        files: vec!["a.fq".into(), "b.fq".into()],
        sample_name: Some("renamed".into()),
        ..Default::default()
    })
    .unwrap_err();
    assert!(err.0.contains("exactly one input sample"), "{err}");

    // --sample-name with control char fails
    let err = build_sample_inputs(&SampleCliInputs {
        files: vec!["a.fq".into()],
        sample_name: Some("bad\tname".into()),
        ..Default::default()
    })
    .unwrap_err();
    assert!(err.0.contains("sample name"), "{err}");
}

// ---------------------------------------------------------------------------
// Unique contig names (tests/test_assembly_per_contig.py::test_unique_contig_names)
// ---------------------------------------------------------------------------

#[test]
fn unique_contig_names_match_python_spec() {
    let mut counts = HashMap::new();
    let mut used = HashSet::new();
    let names = ["c", "c", "c_2", "c", ""];
    let got: Vec<String> = names
        .iter()
        .enumerate()
        .map(|(i, raw)| unique_contig_name(raw, &mut counts, &mut used, (i + 1) as u64))
        .collect();
    assert_eq!(got, vec!["c", "c_2", "c_2_2", "c_3", "contig-5"]);

    // Also mirror test_web_contigs_js.py::test_unique_contig_name_matches_python
    let mut counts = HashMap::new();
    let mut used = HashSet::new();
    let raws = ["c", "c", "c_2", "", "c", "c_3", "d"];
    let got: Vec<String> = raws
        .iter()
        .enumerate()
        .map(|(i, raw)| unique_contig_name(raw, &mut counts, &mut used, (i + 1) as u64))
        .collect();
    assert_eq!(
        got,
        vec!["c", "c_2", "c_2_2", "contig-4", "c_3", "c_3_2", "d"]
    );
}

// ---------------------------------------------------------------------------
// Native sketching, pre-made sketches, paired/interleaved, rayon
// ---------------------------------------------------------------------------

#[test]
fn sketch_sample_single_paired_interleaved_and_premade() {
    let data = data_dir();
    let k12_r1 = data.join("k12.R1.fq.gz");
    let k12_r2 = data.join("k12.R2.fq.gz");
    let k12_r1_sylsp = data.join("k12.R1.sylsp");

    // Single-end
    let single = SampleInput::single("k12", vec![k12_r1.to_string_lossy().into_owned()]).unwrap();
    let sketched = sketch_sample(&single, 200, 31).unwrap();
    assert_eq!(sketched.header.c, 200);
    assert_eq!(sketched.header.k, 31);
    assert!(!sketched.header.paired);
    assert!(sketched.bytes.len() > 100);

    // Pre-made .sylsp loaded unchanged and validated
    let premade =
        SampleInput::single("k12", vec![k12_r1_sylsp.to_string_lossy().into_owned()]).unwrap();
    assert!(premade.is_sketch());
    let loaded = sketch_sample(&premade, 200, 31).unwrap();
    assert_eq!(loaded.bytes, fs::read(&k12_r1_sylsp).unwrap());
    check_sketch_compatibility(&loaded.header, 200, 31, Some("k12")).unwrap();
    assert!(sketch_sample(&premade, 200, 21)
        .unwrap_err()
        .0
        .contains("k="));
    assert!(sketch_sample(&premade, 100, 31)
        .unwrap_err()
        .0
        .contains("sparser"));

    // Paired matches interleaved (build a temporary interleaved FASTQ from k12 R1+R2)
    let paired = SampleInput::paired(
        "x",
        vec![(
            k12_r1.to_string_lossy().into_owned(),
            k12_r2.to_string_lossy().into_owned(),
        )],
    )
    .unwrap();
    let paired_sketch = sketch_sample(&paired, 200, 31).unwrap();
    assert!(paired_sketch.header.paired);

    let mut d1 = String::new();
    let mut d2 = String::new();
    std::io::Read::read_to_string(&mut open_maybe_gzip(&k12_r1).unwrap(), &mut d1).unwrap();
    std::io::Read::read_to_string(&mut open_maybe_gzip(&k12_r2).unwrap(), &mut d2).unwrap();
    let l1: Vec<&str> = d1.lines().collect();
    let l2: Vec<&str> = d2.lines().collect();
    let mut inter_bytes = Vec::new();
    for (r1, r2) in l1.as_chunks::<4>().0.iter().zip(l2.as_chunks::<4>().0.iter()) {
        for line in r1.iter().chain(r2.iter()) {
            inter_bytes.extend_from_slice(line.as_bytes());
            inter_bytes.push(b'\n');
        }
    }
    let mut enc = GzEncoder::new(Vec::new(), Compression::fast());
    enc.write_all(&inter_bytes).unwrap();
    let inter_gz = enc.finish().unwrap();

    let mut inter_sketcher = StreamSketcher::new(200, 31, true).unwrap();
    inter_sketcher
        .push_interleaved_reader(&inter_gz[..])
        .unwrap();
    let inter_sketch = inter_sketcher.finish("x").unwrap();
    assert_eq!(inter_sketch, paired_sketch.bytes);

    // Missing file fails cleanly
    let missing = SampleInput::single("gone", vec!["/nonexistent/nope.fq".into()]).unwrap();
    assert!(sketch_sample(&missing, 200, 31).is_err());
}

#[test]
fn paired_count_mismatch_and_odd_interleaved_fail() {
    let r1 = b"@read1/1\nAAAA\n+\nIIII\n@read2/1\nCCCC\n+\nIIII\n";
    let r2 = b"@read1/2\nTTTT\n+\nIIII\n";
    let mut s = StreamSketcher::new(200, 31, true).unwrap();
    let err = s.push_paired_readers(&r1[..], &r2[..]).unwrap_err();
    assert!(err.contains("different numbers of reads"), "{err}");

    let odd = b"@read1/1\nAAAA\n+\nIIII\n@read1/2\nTTTT\n+\nIIII\n@read2/1\nCCCC\n+\nIIII\n";
    let mut s = StreamSketcher::new(200, 31, true).unwrap();
    let err = s.push_interleaved_reader(&odd[..]).unwrap_err();
    assert!(err.contains("odd number of reads"), "{err}");
}

#[cfg(feature = "rayon")]
#[test]
fn rayon_parallel_sketching_matches_sequential_in_order() {
    use sylphd_core::input::{sketch_samples, sketch_samples_parallel};

    let data = data_dir();
    let samples = vec![
        SampleInput::single(
            "r1",
            vec![data.join("k12.R1.fq.gz").to_string_lossy().into_owned()],
        )
        .unwrap(),
        SampleInput::single(
            "clodf13",
            vec![data.join("clodf13.fna.gz").to_string_lossy().into_owned()],
        )
        .unwrap(),
        SampleInput::paired(
            "paired",
            vec![(
                data.join("k12.R1.fq.gz").to_string_lossy().into_owned(),
                data.join("k12.R2.fq.gz").to_string_lossy().into_owned(),
            )],
        )
        .unwrap(),
    ];
    let seq = sketch_samples(&samples, 200, 31).unwrap();
    let par = sketch_samples_parallel(&samples, 200, 31, Some(2)).unwrap();
    assert_eq!(par, seq);
}

// ---------------------------------------------------------------------------
// Assembly stats & empty-record checks
// ---------------------------------------------------------------------------

#[test]
fn assembly_stats_and_errors() {
    let clodf13 = data_dir().join("clodf13.fna.gz");
    let (bytes, stats) = sketch_assembly_file(&clodf13, "clodf13", 200, 31).unwrap();
    assert_eq!(stats.records, 1);
    assert!(stats.length > 5000);
    assert!(stats.gc > 40.0 && stats.gc < 70.0);
    let header = parse_sketch_header(&bytes).unwrap();
    assert_eq!(header.c, 200);
    assert_eq!(header.k, 31);
    assert_eq!(header.file_name, "clodf13");
    assert!(!header.paired);

    // Exact GC & multi-record stats via reader
    let fasta = b">c1\nGGCCAA\n>c2\nattc\n";
    let (_, stats) = sketch_assembly_reader(&fasta[..], "mem.fa", "mem", 200, 31).unwrap();
    assert_eq!(stats.records, 2);
    assert_eq!(stats.length, 10);
    assert_eq!(stats.gc_count, 5);
    assert_eq!(stats.gc, 50.0);

    // Empty sequence is rejected
    let empty_seq = b">c1\nACGT\n>c2\n\n";
    let mut s = AssemblySketcher::new("bad.fa", 200, 31).unwrap();
    s.push(empty_seq).unwrap();
    let err = s.finish("bad").unwrap_err();
    assert!(err.0.contains("sequence #2 is empty"), "{err}");

    // Empty input is rejected
    let err = sketch_assembly_reader(&b""[..], "empty.fa", "empty", 200, 31).unwrap_err();
    assert!(err.0.contains("contains no sequences"), "{err}");
}

// ---------------------------------------------------------------------------
// Per-contig skipping & client-side skip counters
// ---------------------------------------------------------------------------

#[test]
fn contig_filter_counts_short_and_few_kmers_and_deduplicates_names() {
    // Load clodf13 sequence so we have a real sequence with plenty of k-mers
    let mut fasta_text = String::new();
    std::io::Read::read_to_string(
        &mut open_maybe_gzip(data_dir().join("clodf13.fna.gz")).unwrap(),
        &mut fasta_text,
    )
    .unwrap();
    let genome: String = fasta_text
        .lines()
        .filter(|l| !l.starts_with('>'))
        .collect();

    // Mirrors tests/test_assembly_per_contig.py::fragmented fixture:
    // 3 long contigs (one duplicate id), 1 short (500 bp), 1 low-complexity (3000 A's)
    let fragmented = format!(
        ">c1 first window\n{}\n>c2 second window\n{}\n>c1 duplicate id\n{}\n>short one\n{}\n>tiny low complexity\n{}\n",
        &genome[..6000],
        &genome[genome.len() - 6000..],
        &genome[1000..7000],
        &genome[..500],
        "A".repeat(3000),
    );

    let mut prepared = Vec::new();
    let counters = sketch_contigs_reader(
        fragmented.as_bytes(),
        "fragmented.fa",
        200,
        31,
        1000,
        DEFAULT_MIN_NUMBER_KMERS,
        |c| {
            prepared.push(c);
            Ok::<(), sylphd_core::input::InputError>(())
        },
    )
    .unwrap();

    let names: Vec<&str> = prepared.iter().map(|c| c.name.as_str()).collect();
    assert_eq!(names, vec!["c1", "c2", "c1_2"]);
    assert_eq!(counters.seen_contigs, 5);
    assert_eq!(counters.seen_bp, 6000 + 6000 + 6000 + 500 + 3000);
    assert_eq!(
        counters.skipped_short,
        Skipped {
            contigs: 1,
            bp: 500
        }
    );
    assert_eq!(
        counters.skipped_few_kmers,
        Skipped {
            contigs: 1,
            bp: 3000
        }
    );

    // Tandem repeat has >10 sample k-mers (at c=1) but 0 genome-style k-mers -> skipped_few_kmers
    let tandem = format!(">rep\n{}\n", "ACGTACGTACGTTGCATGCATGCA".repeat(200));
    let mut kept = Vec::new();
    let counters = sketch_contigs_reader(
        tandem.as_bytes(),
        "tandem.fa",
        1,
        31,
        100,
        DEFAULT_MIN_NUMBER_KMERS,
        |c| {
            kept.push(c);
            Ok::<(), sylphd_core::input::InputError>(())
        },
    )
    .unwrap();
    assert!(kept.is_empty());
    assert_eq!(counters.skipped_few_kmers.contigs, 1);
    assert_eq!(counters.skipped_few_kmers.bp, 4800);

    // Empty input is an InputError
    let err = sketch_contigs_reader(&b""[..], "empty.fa", 200, 31, 1000, 10, |_| Ok::<(), sylphd_core::input::InputError>(()))
        .unwrap_err();
    assert!(err.0.contains("contains no sequences"), "{err}");

    // Bad header name (control char) names the record
    let mut filter = ContigFilter::default();
    let err = filter
        .process(ContigRecord {
            index: 3,
            name: "bad\x07name".into(),
            length: 100,
            gc_count: 50,
            contig_kmers: None,
            sketch: None,
        })
        .unwrap_err();
    assert!(err.0.contains("record #3"), "{err}");
    assert!(err.0.contains("sample name"), "{err}");

    // File helper works on gzipped assembly
    let mut from_file = Vec::new();
    let file_counters = sketch_contigs_file(
        data_dir().join("clodf13.fna.gz"),
        200,
        31,
        1000,
        10,
        |c| {
            from_file.push(c);
            Ok::<(), sylphd_core::input::InputError>(())
        },
    )
    .unwrap();
    assert_eq!(file_counters.seen_contigs, 1);
    assert_eq!(from_file.len(), 1);
    assert!(from_file[0].contig_kmers > 0);
    assert!(from_file[0].sketch_kmers >= 10);

    // The raw ContigSketcher fed through `for_each_chunk` (multi-member gzip)
    let mut sketcher =
        ContigSketcher::with_limits(200, 31, 1000, DEFAULT_MIN_SPACING, Seeding::Avx2Compat, 0, 0)
            .unwrap();
    let mut raw_records = Vec::new();
    for_each_chunk(
        std::fs::File::open(data_dir().join("clodf13.fna.gz")).unwrap(),
        |chunk| -> Result<(), String> {
            sketcher.push(chunk)?;
            while let Some(record) = sketcher.take_next() {
                raw_records.push(record);
            }
            Ok(())
        },
    )
    .unwrap();
    sketcher.end_file().unwrap();
    raw_records.extend(std::iter::from_fn(|| sketcher.take_next()));
    assert_eq!(raw_records.len(), 1);
    assert_eq!(raw_records[0].sketch_kmers(), Some(from_file[0].sketch_kmers));
}
