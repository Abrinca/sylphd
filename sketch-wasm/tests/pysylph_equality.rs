//! Sketches the test inputs with this crate and compares the result with
//! pysylph's `Sketcher` (tests/compare_pysylph.py, run with the sylphd venv).
//!
//! Inputs are read from the repository's `tests/data` (paths in
//! `tests/cases.json` are relative to the repository root). The ONT case is
//! large and only runs with `SKETCH_WASM_BIG=1` (put a big ONT FASTQ at
//! `scratch/big/ont.fastq.gz`; it is skipped when the file is missing).
//!
//! The Python side is skipped (with a message) if no interpreter with pysylph
//! is found: set `SYLPHD_PYTHON`, or create `../.venv` with `uv sync`.

use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::Command;

use flate2::read::MultiGzDecoder;
use sylph_sketch_wasm::stream::StreamSketcher;

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .to_path_buf()
}

fn resolve(path: &str) -> PathBuf {
    let p = Path::new(path);
    if p.is_absolute() {
        p.to_path_buf()
    } else {
        repo_root().join(p)
    }
}

/// Decompressed (or plain) reader, whatever the file's extension says.
fn open(path: &Path) -> Box<dyn Read> {
    let mut magic = [0u8; 2];
    let n = File::open(path).unwrap().read(&mut magic).unwrap();
    let file = File::open(path).unwrap();
    if n == 2 && magic == [0x1f, 0x8b] {
        Box::new(MultiGzDecoder::new(file))
    } else {
        Box::new(file)
    }
}

/// Feeds `reader` to `push` in chunks whose sizes vary (so records are split at
/// arbitrary places), starting with `first` bytes.
fn pump(reader: &mut dyn Read, first: usize, push: &mut dyn FnMut(&[u8])) {
    let mut size = first;
    let mut buf = vec![0u8; 300_000];
    loop {
        let want = size.min(buf.len());
        let n = reader.read(&mut buf[..want]).unwrap();
        if n == 0 {
            break;
        }
        push(&buf[..n]);
        size = (size * 7 + 13) % 250_000 + 1;
    }
}

fn sketch_case(case: &serde_json::Value, chunk: usize) -> Vec<u8> {
    let c = case["c"].as_u64().unwrap_or(200) as usize;
    let k = case["k"].as_u64().unwrap_or(31) as usize;
    let dedup = case["deduplicate"].as_bool().unwrap_or(true);
    let name = case["name"].as_str().unwrap();
    let files: Vec<PathBuf> = case["files"]
        .as_array()
        .unwrap()
        .iter()
        .map(|f| resolve(f.as_str().unwrap()))
        .collect();
    let raw_gz = case["raw_gz"].as_bool().unwrap_or(false);
    let mut s = StreamSketcher::new(c, k, dedup).unwrap();
    match case["kind"].as_str().unwrap() {
        "single" => {
            for f in &files {
                if raw_gz {
                    pump(&mut File::open(f).unwrap(), chunk, &mut |b| {
                        s.push_gz(b).unwrap()
                    });
                } else {
                    pump(&mut *open(f), chunk, &mut |b| s.push(b).unwrap());
                }
                s.end_file().unwrap();
            }
        }
        "paired" => {
            // alternate between the two streams, like the browser
            let raw = |p: &Path| -> Box<dyn Read> { Box::new(File::open(p).unwrap()) };
            let (mut r1, mut r2) = if raw_gz {
                (raw(&files[0]), raw(&files[1]))
            } else {
                (open(&files[0]), open(&files[1]))
            };
            let mut b1 = vec![0u8; chunk];
            let mut b2 = vec![0u8; chunk];
            loop {
                let n1 = r1.read(&mut b1).unwrap();
                let n2 = r2.read(&mut b2).unwrap();
                if raw_gz {
                    s.push_r1_gz(&b1[..n1]).unwrap();
                    s.push_r2_gz(&b2[..n2]).unwrap();
                } else {
                    s.push_r1(&b1[..n1]).unwrap();
                    s.push_r2(&b2[..n2]).unwrap();
                }
                if n1 == 0 && n2 == 0 {
                    break;
                }
            }
        }
        other => panic!("unknown kind {other}"),
    }
    s.finish(name).unwrap()
}

/// Deterministic short-read simulation from the E. coli K12 assembly in
/// sylph's test files (ref/sylph/test_files, not in git), or, if that is
/// missing, from `tests/data/clodf13.fna.gz` (tiny, but still exercises dedup).
/// 2x(100-150 bp) pairs at ~30x, 0.5% substitutions, 8% exact PCR duplicates,
/// fragments of 200-450 bp. Writes `target/synth/sim_R{1,2}.fq`.
fn make_synth() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("target/synth");
    fs::create_dir_all(&dir).unwrap();
    make_gzip_fixtures(&dir);
    let (p1, p2) = (dir.join("sim_R1.fq"), dir.join("sim_R2.fq"));
    if p1.exists() && p2.exists() {
        gzip_sim(&dir);
        return;
    }
    let mut genome_path = repo_root().join("ref/sylph/test_files/e.coli-K12.fasta.gz");
    if !genome_path.exists() {
        genome_path = repo_root().join("tests/data/clodf13.fna.gz");
    }
    let mut text = vec![];
    open(&genome_path).read_to_end(&mut text).unwrap();
    let genome: Vec<u8> = text
        .split(|&b| b == b'\n')
        .filter(|l| !l.starts_with(b">"))
        .flatten()
        .map(|b| b.to_ascii_uppercase())
        .collect();

    let mut state = 0x9E37_79B9_7F4A_7C15u64;
    let mut rnd = move || {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        state
    };
    let comp = |b: u8| match b {
        b'A' => b'T',
        b'C' => b'G',
        b'G' => b'C',
        b'T' => b'A',
        other => other,
    };
    let mutate = |seq: &mut Vec<u8>, rnd: &mut dyn FnMut() -> u64| {
        for b in seq.iter_mut() {
            if rnd() % 200 == 0 {
                *b = b"ACGT"[(rnd() % 4) as usize];
            }
        }
    };
    let pairs = (genome.len() as u64 * 30 / 250).min(600_000);
    let (mut o1, mut o2) = (Vec::new(), Vec::new());
    let mut last: Option<(Vec<u8>, Vec<u8>)> = None;
    for i in 0..pairs {
        let (r1, r2) = match &last {
            Some(prev) if rnd() % 100 < 8 => prev.clone(),
            _ => {
                let frag_len = 200 + (rnd() % 251) as usize;
                let start = (rnd() % (genome.len() - frag_len) as u64) as usize;
                let frag = &genome[start..start + frag_len];
                let rev: Vec<u8> = frag.iter().rev().map(|&b| comp(b)).collect();
                let (l1, l2) = (100 + (rnd() % 51) as usize, 100 + (rnd() % 51) as usize);
                let mut r1 = frag[..l1].to_vec();
                let mut r2 = rev[..l2].to_vec();
                mutate(&mut r1, &mut rnd);
                mutate(&mut r2, &mut rnd);
                (r1, r2)
            }
        };
        for (out, mate, seq) in [(&mut o1, 1, &r1), (&mut o2, 2, &r2)] {
            out.extend_from_slice(format!("@sim{i}/{mate}\n").as_bytes());
            out.extend_from_slice(seq);
            out.extend_from_slice(b"\n+\n");
            out.extend(std::iter::repeat(b'I').take(seq.len()));
            out.push(b'\n');
        }
        last = Some((r1, r2));
    }
    fs::write(&p1, &o1).unwrap();
    fs::write(&p2, &o2).unwrap();
    gzip_sim(&dir);
}

fn gzip_sim(dir: &Path) {
    for mate in ["R1", "R2"] {
        let gz = dir.join(format!("sim_{mate}.fq.gz"));
        if !gz.exists() {
            let data = fs::read(dir.join(format!("sim_{mate}.fq"))).unwrap();
            let mut enc = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::fast());
            enc.write_all(&data).unwrap();
            fs::write(gz, enc.finish().unwrap()).unwrap();
        }
    }
}

/// Multi-member gzip fixtures made from the k12 reads: the file twice in a row
/// (`cat a.gz a.gz`) and one member per 100 reads (BGZF-like).
fn make_gzip_fixtures(dir: &Path) {
    let original = fs::read(repo_root().join("tests/data/k12.R1.fq.gz")).unwrap();
    let mut twice = original.clone();
    twice.extend_from_slice(&original);
    fs::write(dir.join("k12_twice.fq.gz"), twice).unwrap();

    let mut plain = vec![];
    open(&repo_root().join("tests/data/k12.R1.fq.gz"))
        .read_to_end(&mut plain)
        .unwrap();
    let lines: Vec<&[u8]> = plain.split_inclusive(|&b| b == b'\n').collect();
    let mut out = vec![];
    for block in lines.chunks(400) {
        let mut enc = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
        for line in block {
            enc.write_all(line).unwrap();
        }
        out.extend(enc.finish().unwrap());
    }
    fs::write(dir.join("k12_bgzf_like.fq.gz"), out).unwrap();
}

fn python() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("SYLPHD_PYTHON") {
        return Some(PathBuf::from(p));
    }
    let venv = repo_root().join(".venv/bin/python");
    venv.exists().then_some(venv)
}

#[test]
fn matches_pysylph() {
    let cases: Vec<serde_json::Value> = serde_json::from_slice(
        &fs::read(Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/cases.json")).unwrap(),
    )
    .unwrap();
    let big = std::env::var("SKETCH_WASM_BIG").is_ok();
    let out = Path::new(env!("CARGO_MANIFEST_DIR")).join("target/equality");
    fs::create_dir_all(&out).unwrap();

    make_synth();
    let mut run = vec![];
    for case in &cases {
        if case["big"].as_bool().unwrap_or(false) && !big {
            eprintln!("skipping {} (set SKETCH_WASM_BIG=1)", case["name"]);
            continue;
        }
        if !resolve(case["files"][0].as_str().unwrap()).exists() {
            eprintln!("skipping {}: input missing", case["name"]);
            continue;
        }
        let name = case["name"].as_str().unwrap();
        let t = std::time::Instant::now();
        let bytes = sketch_case(case, 65_536);
        eprintln!(
            "{name}: sketched in {:.2?}, {} bytes",
            t.elapsed(),
            bytes.len()
        );
        fs::write(out.join(format!("{name}.sylsp")), &bytes).unwrap();
        run.push(name.to_string());
    }
    assert!(!run.is_empty());

    let Some(python) = python() else {
        eprintln!("no python with pysylph found (SYLPHD_PYTHON / ../.venv): comparison skipped");
        return;
    };
    let status = Command::new(python)
        .arg(Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/compare_pysylph.py"))
        .arg(&out)
        .args(&run)
        .current_dir(repo_root())
        .status()
        .unwrap();
    assert!(status.success(), "pysylph comparison failed");
}

/// Chunk boundaries must not matter: byte-identical sketches for any chunking.
#[test]
fn chunking_is_irrelevant() {
    let cases: Vec<serde_json::Value> = serde_json::from_slice(
        &fs::read(Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/cases.json")).unwrap(),
    )
    .unwrap();
    let case = &cases[0];
    let reference = sketch_case(case, 1_000_000);
    for chunk in [1usize, 7, 64, 4093] {
        // 1-byte chunks over 100 MB of reads would be slow: truncate the input
        let limited = chunk < 64;
        let a = if limited {
            sketch_limited(case, chunk)
        } else {
            sketch_case(case, chunk)
        };
        let b = if limited {
            sketch_limited(case, 65_536)
        } else {
            reference.clone()
        };
        assert_eq!(a, b, "chunk size {chunk}");
    }
}

fn sketch_limited(case: &serde_json::Value, chunk: usize) -> Vec<u8> {
    let path = resolve(case["files"][0].as_str().unwrap());
    let mut data = vec![];
    open(&path).take(2_000_000).read_to_end(&mut data).unwrap();
    // cut at a record boundary so the truncated FASTQ stays valid
    let cut = data.iter().rposition(|&b| b == b'\n').unwrap();
    let lines = data[..cut].iter().filter(|&&b| b == b'\n').count() + 1;
    let keep = lines - lines % 4;
    let mut end = 0;
    for _ in 0..keep {
        end += data[end..].iter().position(|&b| b == b'\n').unwrap() + 1;
    }
    let mut s = StreamSketcher::new(200, 31, true).unwrap();
    for piece in data[..end].chunks(chunk) {
        s.push(piece).unwrap();
    }
    s.finish("x").unwrap()
}

#[test]
fn crlf_and_missing_final_newline() {
    let fastq = b"@r1 desc\nACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTAGCTAGCTAGCTAGCTAGCATCGATCGACTGACT\n+\n@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n";
    let crlf: Vec<u8> = fastq
        .iter()
        .flat_map(|&b| {
            if b == b'\n' {
                vec![b'\r', b'\n']
            } else {
                vec![b]
            }
        })
        .collect();
    let nonl = &fastq[..fastq.len() - 1];
    let sketch = |data: &[u8], chunk: usize| {
        let mut s = StreamSketcher::new(20, 31, true).unwrap();
        for piece in data.chunks(chunk) {
            s.push(piece).unwrap();
        }
        let bytes = s.finish("x").unwrap();
        (s.reads(), s.bases(), bytes)
    };
    let base = sketch(fastq, 1000);
    assert_eq!(base.0, 1);
    assert_eq!(base.1, 74);
    assert_eq!(sketch(&crlf, 3), base);
    assert_eq!(sketch(nonl, 5), base);
}

#[test]
fn multiline_fasta_and_quality_starting_with_at() {
    let mut s = StreamSketcher::new(1, 31, true).unwrap();
    s.push(b">a\nACGTACGTAC\nGTACGTAC\n\n>b\n>c\nAC").unwrap();
    s.push(b"GT\n").unwrap();
    s.finish("x").unwrap();
    assert_eq!(s.reads(), 3);
    assert_eq!(s.bases(), 18 + 0 + 4);

    let mut s = StreamSketcher::new(1, 31, true).unwrap();
    s.push(b"@r\nACGT\n+\n@@@@\n@r2\nAC\n+r2\n@@\n").unwrap();
    s.finish("x").unwrap();
    assert_eq!(s.reads(), 2);
}

#[test]
fn rejects_garbage() {
    let mut s = StreamSketcher::new(200, 31, true).unwrap();
    assert!(s.push(b"hello\n").is_err());
    let mut s = StreamSketcher::new(200, 31, true).unwrap();
    s.push(b"@r\nACGT\n+\n@@@\n").unwrap_err();
    let mut s = StreamSketcher::new(200, 31, true).unwrap();
    s.push(b"@r\nACGT\n+\n").unwrap();
    assert!(s.finish("x").is_err());
    assert!(StreamSketcher::new(200, 25, true).is_err());
}

/// An interleaved stream gives the same sketch as the two separate streams.
#[test]
fn interleaved_equals_two_streams() {
    let (p1, p2) = (
        resolve("tests/data/k12.R1.fq.gz"),
        resolve("tests/data/k12.R2.fq.gz"),
    );
    let (mut d1, mut d2) = (vec![], vec![]);
    open(&p1).take(3_000_000).read_to_end(&mut d1).unwrap();
    open(&p2).take(3_000_000).read_to_end(&mut d2).unwrap();
    let records = |d: &[u8]| -> Vec<Vec<u8>> {
        let lines: Vec<&[u8]> = d.split(|&b| b == b'\n').collect();
        lines.chunks_exact(4).map(|r| r.join(&b'\n')).collect()
    };
    let (r1, r2) = (records(&d1), records(&d2));
    let n = r1.len().min(r2.len()) - 1; // last record may be cut
    let mut two = StreamSketcher::new(200, 31, true).unwrap();
    let mut inter = StreamSketcher::new(200, 31, true).unwrap();
    for i in 0..n {
        two.push_r1(&r1[i]).unwrap();
        two.push_r1(b"\n").unwrap();
        two.push_r2(&r2[i]).unwrap();
        two.push_r2(b"\n").unwrap();
        for rec in [&r1[i], &r2[i]] {
            inter.push_interleaved(rec).unwrap();
            inter.push_interleaved(b"\n").unwrap();
        }
    }
    assert_eq!(two.finish("p").unwrap(), inter.finish("p").unwrap());
    assert!(two.reads() > 1000);
}
