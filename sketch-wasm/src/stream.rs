//! Streaming front end: raw FASTQ/FASTA text in arbitrary chunks -> sketch.
//!
//! Single-end: `push`. Paired-end, either two streams (`push_r1` / `push_r2`,
//! mates are matched by record order) or one interleaved stream
//! (`push_interleaved`). The mode is fixed by the first push.
//!
//! In two-stream mode a mate that arrives before its partner is queued. Equal
//! byte chunks do not hold equally many records (75 bp vs 150 bp mates), so
//! feed the mate that is behind: `unmatched()` says which one.

use std::collections::VecDeque;

use crate::gzip::GzipDecoder;
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

#[derive(Clone, Copy)]
enum Stream {
    Single,
    R1,
    R2,
    Interleaved,
}

impl Stream {
    fn mode(self) -> Mode {
        match self {
            Stream::Single => Mode::Single,
            Stream::R1 | Stream::R2 => Mode::TwoStream,
            Stream::Interleaved => Mode::Interleaved,
        }
    }
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
    /// Gzip decoders, for `push*_gz` (stream 1 / stream 2).
    gz1: Option<GzipDecoder>,
    gz2: Option<GzipDecoder>,
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
            gz1: None,
            gz2: None,
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
        self.push_stream(Stream::Single, data)
    }

    pub fn push_r1(&mut self, data: &[u8]) -> Result<(), String> {
        self.push_stream(Stream::R1, data)
    }

    pub fn push_r2(&mut self, data: &[u8]) -> Result<(), String> {
        self.push_stream(Stream::R2, data)
    }

    pub fn push_interleaved(&mut self, data: &[u8]) -> Result<(), String> {
        self.push_stream(Stream::Interleaved, data)
    }

    /// Like `push`, but `data` is a chunk of a (possibly multi-member) gzip file.
    pub fn push_gz(&mut self, data: &[u8]) -> Result<(), String> {
        self.push_stream_gz(Stream::Single, data)
    }

    pub fn push_r1_gz(&mut self, data: &[u8]) -> Result<(), String> {
        self.push_stream_gz(Stream::R1, data)
    }

    pub fn push_r2_gz(&mut self, data: &[u8]) -> Result<(), String> {
        self.push_stream_gz(Stream::R2, data)
    }

    pub fn push_interleaved_gz(&mut self, data: &[u8]) -> Result<(), String> {
        self.push_stream_gz(Stream::Interleaved, data)
    }

    fn push_stream_gz(&mut self, stream: Stream, data: &[u8]) -> Result<(), String> {
        self.set_mode(stream.mode())?;
        let mut decoder = self.gz_slot(stream).take().unwrap_or_default();
        let result = decoder.push(data, &mut |plain| self.push_stream(stream, plain));
        *self.gz_slot(stream) = Some(decoder);
        result
    }

    fn gz_slot(&mut self, stream: Stream) -> &mut Option<GzipDecoder> {
        match stream {
            Stream::R2 => &mut self.gz2,
            _ => &mut self.gz1,
        }
    }

    fn push_stream(&mut self, stream: Stream, data: &[u8]) -> Result<(), String> {
        self.set_mode(stream.mode())?;
        match stream {
            Stream::Single => {
                let builder = &mut self.builder;
                self.parser1
                    .feed(data, &mut |seq, _header| builder.add_single(seq))
            }
            Stream::R1 => {
                let queue = &mut self.queue1;
                self.parser1
                    .feed(data, &mut |seq, _header| queue.push_back(seq.to_vec()))?;
                self.drain_pairs();
                Ok(())
            }
            Stream::R2 => {
                let queue = &mut self.queue2;
                self.parser2
                    .feed(data, &mut |seq, _header| queue.push_back(seq.to_vec()))?;
                self.drain_pairs();
                Ok(())
            }
            Stream::Interleaved => {
                let (builder, held) = (&mut self.builder, &mut self.held);
                self.parser1
                    .feed(data, &mut |seq, _header| match held.take() {
                        Some(first) => builder.add_pair(&first, seq),
                        None => *held = Some(seq.to_vec()),
                    })
            }
        }
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
        // a file that was pushed as gzip must end on a member boundary
        for decoder in [&mut self.gz1, &mut self.gz2] {
            if let Some(mut decoder) = decoder.take() {
                decoder.finish()?;
            }
        }
        match self.mode {
            Mode::Unset => Ok(()),
            Mode::Single => {
                let builder = &mut self.builder;
                self.parser1
                    .finish(&mut |seq, _header| builder.add_single(seq))
            }
            Mode::TwoStream => {
                let queue = &mut self.queue1;
                self.parser1
                    .finish(&mut |seq, _header| queue.push_back(seq.to_vec()))?;
                let queue = &mut self.queue2;
                self.parser2
                    .finish(&mut |seq, _header| queue.push_back(seq.to_vec()))?;
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
                self.parser1.finish(&mut |seq, _header| match held.take() {
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

    /// Two-stream mode: mates queued while waiting for their partners; positive
    /// when R1 is ahead (feed R2 next), negative when R2 is ahead.
    pub fn unmatched(&self) -> i64 {
        self.queue1.len() as i64 - self.queue2.len() as i64
    }

    /// Reads (or read pairs) sketched so far.
    pub fn reads(&self) -> u64 {
        self.builder.reads
    }

    /// Bases sketched so far (both mates for pairs).
    pub fn bases(&self) -> u64 {
        self.builder.bases
    }

    /// G and C bases among `bases()`.
    pub fn gc(&self) -> u64 {
        self.builder.gc
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
