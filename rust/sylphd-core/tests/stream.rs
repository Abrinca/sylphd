//! Small behaviours of `StreamSketcher` that need no fixture files.

use sylph_sketch_wasm::stream::StreamSketcher;

fn fastq(reads: usize, len: usize) -> Vec<u8> {
    let (seq, qual) = ("ACGT".repeat(len)[..len].to_string(), "I".repeat(len));
    format!("@r\n{seq}\n+\n{qual}\n").repeat(reads).into_bytes()
}

/// Equal byte chunks of 75 bp and 150 bp mates hold different numbers of records;
/// `unmatched` tells the caller which mate to feed next.
#[test]
fn unmatched_reports_the_mate_that_is_ahead() {
    let mut s = StreamSketcher::new(200, 31, true).unwrap();
    s.push_r1(&fastq(4, 75)).unwrap();
    assert_eq!(s.unmatched(), 4);
    s.push_r2(&fastq(1, 150)).unwrap();
    assert_eq!(s.unmatched(), 3);
    s.push_r2(&fastq(5, 150)).unwrap();
    assert_eq!(s.unmatched(), -2);
    s.push_r1(&fastq(2, 75)).unwrap();
    assert_eq!(s.unmatched(), 0);
    assert_eq!(s.reads(), 6);
}

/// GC is counted on the records the parser emits, not on the raw text: FASTA
/// comments are not sequence, and a FASTQ may start with a blank line.
#[test]
fn gc_counts_sequence_only() {
    let mut fasta = StreamSketcher::new(200, 31, true).unwrap();
    fasta.push(b">a\n;GGGGGGGG comment\nACgt\nGC\n>b\nAT\n").unwrap();
    fasta.end_file().unwrap();
    assert_eq!((fasta.bases(), fasta.gc()), (8, 4));

    let mut fastq = StreamSketcher::new(200, 31, true).unwrap();
    fastq.push(b"\n@r\nGGCA\n+\nIIII\n\n@s\nTTTc\n+\nIIII\n").unwrap();
    fastq.end_file().unwrap();
    assert_eq!((fastq.bases(), fastq.gc()), (8, 4));
}
