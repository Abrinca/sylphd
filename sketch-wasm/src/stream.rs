//! Streaming front end: raw FASTQ/FASTA text in arbitrary chunks -> sketch.
//!
//! Single-end: `push`. Paired-end, either two streams (`push_r1` / `push_r2`,
//! mates are matched by record order) or one interleaved stream
//! (`push_interleaved`). The mode is fixed by the first push.
//!
//! In two-stream mode a mate that arrives before its partner is queued, so feed
//! R1 and R2 alternately (the demo does) to keep memory flat.

use std::collections::VecDeque;

use crate::parser::RecordParser;
use crate::seeding::Seeding;
use crate::sketch::{SketchBuilder, DEFAULT_FPR};

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Mode {
    Unset,
    Single,
    TwoStream,
    Interleaved,
}

pub struct StreamSketcher {
    builder: SketchBuilder,
    mode: Mode,
    parser1: RecordParser,
    parser2: RecordParser,
    queue1: VecDeque<Vec<u8>>,
    queue2: VecDeque<Vec<u8>>,
    /// Interleaved mode: first mate waiting for its partner.
    held: Option<Vec<u8>>,
}

impl StreamSketcher {
    pub fn new(c: usize, k: usize, deduplicate: bool) -> Result<Self, String> {
        Self::with_options(c, k, deduplicate, DEFAULT_FPR, Seeding::default())
    }

    pub fn with_options(
        c: usize,
        k: usize,
        deduplicate: bool,
        fpr: f64,
        seeding: Seeding,
    ) -> Result<Self, String> {
        Ok(Self {
            builder: SketchBuilder::new(c, k, deduplicate, fpr, seeding)?,
            mode: Mode::Unset,
            parser1: RecordParser::new(),
            parser2: RecordParser::new(),
            queue1: VecDeque::new(),
            queue2: VecDeque::new(),
            held: None,
        })
    }

    fn set_mode(&mut self, mode: Mode) -> Result<(), String> {
        if self.mode == Mode::Unset || self.mode == mode {
            self.mode = mode;
            Ok(())
        } else {
            Err(format!(
                "cannot mix {mode:?} input with {:?} input in one sketch",
                self.mode
            ))
        }
    }

    pub fn push(&mut self, data: &[u8]) -> Result<(), String> {
        self.set_mode(Mode::Single)?;
        let builder = &mut self.builder;
        self.parser1.feed(data, &mut |seq| builder.add_single(seq))
    }

    pub fn push_r1(&mut self, data: &[u8]) -> Result<(), String> {
        self.set_mode(Mode::TwoStream)?;
        let queue = &mut self.queue1;
        self.parser1
            .feed(data, &mut |seq| queue.push_back(seq.to_vec()))?;
        self.drain_pairs();
        Ok(())
    }

    pub fn push_r2(&mut self, data: &[u8]) -> Result<(), String> {
        self.set_mode(Mode::TwoStream)?;
        let queue = &mut self.queue2;
        self.parser2
            .feed(data, &mut |seq| queue.push_back(seq.to_vec()))?;
        self.drain_pairs();
        Ok(())
    }

    pub fn push_interleaved(&mut self, data: &[u8]) -> Result<(), String> {
        self.set_mode(Mode::Interleaved)?;
        let (builder, held) = (&mut self.builder, &mut self.held);
        self.parser1.feed(data, &mut |seq| match held.take() {
            Some(first) => builder.add_pair(&first, seq),
            None => *held = Some(seq.to_vec()),
        })
    }

    fn drain_pairs(&mut self) {
        while !self.queue1.is_empty() && !self.queue2.is_empty() {
            let (r1, r2) = (
                self.queue1.pop_front().unwrap(),
                self.queue2.pop_front().unwrap(),
            );
            self.builder.add_pair(&r1, &r2);
        }
    }

    /// Marks the end of the current file (all streams): flushes a last line
    /// without newline and the pending FASTA record. Several files can then be
    /// pushed into one sketch, like `sylph sketch a.fq b.fq`. For a
    /// single-file sample calling `finish` alone is enough.
    pub fn end_file(&mut self) -> Result<(), String> {
        match self.mode {
            Mode::Unset => Ok(()),
            Mode::Single => {
                let builder = &mut self.builder;
                self.parser1.finish(&mut |seq| builder.add_single(seq))
            }
            Mode::TwoStream => {
                let queue = &mut self.queue1;
                self.parser1
                    .finish(&mut |seq| queue.push_back(seq.to_vec()))?;
                let queue = &mut self.queue2;
                self.parser2
                    .finish(&mut |seq| queue.push_back(seq.to_vec()))?;
                self.drain_pairs();
                if self.queue1.len() != self.queue2.len() {
                    return Err(format!(
                        "R1 and R2 have different numbers of reads ({} unmatched)",
                        self.queue1.len().max(self.queue2.len())
                    ));
                }
                Ok(())
            }
            Mode::Interleaved => {
                let (builder, held) = (&mut self.builder, &mut self.held);
                self.parser1.finish(&mut |seq| match held.take() {
                    Some(first) => builder.add_pair(&first, seq),
                    None => *held = Some(seq.to_vec()),
                })?;
                if self.held.is_some() {
                    return Err("interleaved input has an odd number of reads".into());
                }
                Ok(())
            }
        }
    }

    /// Reads (or read pairs) sketched so far.
    pub fn reads(&self) -> u64 {
        self.builder.reads
    }

    /// Bases sketched so far (both mates for pairs).
    pub fn bases(&self) -> u64 {
        self.builder.bases
    }

    /// Distinct k-mers in the sketch so far.
    pub fn kmers(&self) -> usize {
        self.builder.kmers()
    }

    /// Ends the last file and returns the serialized sketch.
    pub fn finish(&mut self, sample_name: &str) -> Result<Vec<u8>, String> {
        self.end_file()?;
        Ok(self.builder.serialize(
            sample_name,
            self.mode != Mode::Single && self.mode != Mode::Unset,
        ))
    }
}
