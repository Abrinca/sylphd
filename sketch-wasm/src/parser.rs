//! Streaming FASTQ / FASTA record parser.
//!
//! Bytes arrive in arbitrary chunks (a record, or even a CRLF pair, may be
//! split across two chunks). The parser is a line state machine: complete
//! lines are handled straight from the chunk, an incomplete trailing line is
//! carried over in `partial`. Only the sequence of the current record is
//! copied; qualities are skipped (their length is checked).
//!
//! The format is detected from the first non-empty line (`@` = FASTQ,
//! `>` = FASTA). FASTQ is the usual 4-line layout; FASTA sequences may span
//! several lines. Both accept `\n` and `\r\n` line ends and blank lines
//! between records. Every record, including one with an empty sequence, is
//! passed to the callback, as `dnaio` (what sylphd uses to feed pysylph) does.

use memchr::memchr;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Format {
    Unknown,
    Fastq(FastqState),
    Fasta,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum FastqState {
    Header,
    Seq,
    Plus,
    Qual,
}

pub struct RecordParser {
    format: Format,
    partial: Vec<u8>,
    seq: Vec<u8>,
    /// FASTA: a header has been seen and its record has not been emitted yet.
    pending: bool,
    line: u64,
}

impl Default for RecordParser {
    fn default() -> Self {
        Self::new()
    }
}

impl RecordParser {
    pub fn new() -> Self {
        Self {
            format: Format::Unknown,
            partial: Vec::new(),
            seq: Vec::new(),
            pending: false,
            line: 0,
        }
    }

    /// Feeds one chunk; `emit` is called with the sequence of every record
    /// completed by it.
    pub fn feed(&mut self, mut data: &[u8], emit: &mut dyn FnMut(&[u8])) -> Result<(), String> {
        while let Some(pos) = memchr(b'\n', data) {
            let (line, rest) = (&data[..pos], &data[pos + 1..]);
            if self.partial.is_empty() {
                self.on_line(line, emit)?;
            } else {
                // `on_line` needs `&mut self`, so move the buffer out meanwhile
                let mut joined = std::mem::take(&mut self.partial);
                joined.extend_from_slice(line);
                let result = self.on_line(&joined, emit);
                joined.clear();
                self.partial = joined;
                result?;
            }
            data = rest;
        }
        self.partial.extend_from_slice(data);
        Ok(())
    }

    /// Ends the current file: handles a last line without newline, emits a
    /// pending FASTA record and resets the parser for a following file.
    pub fn finish(&mut self, emit: &mut dyn FnMut(&[u8])) -> Result<(), String> {
        let result = self.finish_inner(emit);
        *self = Self::new();
        result
    }

    fn finish_inner(&mut self, emit: &mut dyn FnMut(&[u8])) -> Result<(), String> {
        if !self.partial.is_empty() {
            let last = std::mem::take(&mut self.partial);
            self.on_line(&last, emit)?;
        }
        match self.format {
            Format::Fastq(FastqState::Header) | Format::Unknown => Ok(()),
            Format::Fastq(_) => Err(format!(
                "FASTQ input ends inside a record (after line {})",
                self.line
            )),
            Format::Fasta => {
                if self.pending {
                    emit(&self.seq);
                }
                Ok(())
            }
        }
    }

    fn on_line(&mut self, line: &[u8], emit: &mut dyn FnMut(&[u8])) -> Result<(), String> {
        self.line += 1;
        let line = line.strip_suffix(b"\r").unwrap_or(line);
        match self.format {
            Format::Unknown => match line.first() {
                None => Ok(()),
                Some(b'@') => {
                    self.format = Format::Fastq(FastqState::Seq);
                    Ok(())
                }
                Some(b'>') => {
                    self.format = Format::Fasta;
                    self.pending = true;
                    self.seq.clear();
                    Ok(())
                }
                Some(_) => Err(format!(
                    "line {}: neither FASTQ ('@') nor FASTA ('>') input",
                    self.line
                )),
            },
            Format::Fastq(state) => match state {
                FastqState::Header => {
                    if line.is_empty() {
                        Ok(())
                    } else if line[0] == b'@' {
                        self.format = Format::Fastq(FastqState::Seq);
                        Ok(())
                    } else {
                        Err(format!(
                            "line {}: FASTQ header must start with '@'",
                            self.line
                        ))
                    }
                }
                FastqState::Seq => {
                    self.seq.clear();
                    self.seq.extend_from_slice(line);
                    self.format = Format::Fastq(FastqState::Plus);
                    Ok(())
                }
                FastqState::Plus => {
                    if line.first() == Some(&b'+') {
                        self.format = Format::Fastq(FastqState::Qual);
                        Ok(())
                    } else {
                        Err(format!("line {}: expected '+' separator line", self.line))
                    }
                }
                FastqState::Qual => {
                    if line.len() != self.seq.len() {
                        return Err(format!(
                            "line {}: quality length {} differs from sequence length {}",
                            self.line,
                            line.len(),
                            self.seq.len()
                        ));
                    }
                    emit(&self.seq);
                    self.format = Format::Fastq(FastqState::Header);
                    Ok(())
                }
            },
            Format::Fasta => {
                if line.first() == Some(&b'>') {
                    if self.pending {
                        emit(&self.seq);
                    }
                    self.seq.clear();
                    self.pending = true;
                } else if line.is_empty() || line[0] == b';' {
                    // blank or comment line
                } else if !self.pending {
                    return Err(format!("line {}: sequence before first header", self.line));
                } else {
                    self.seq.extend_from_slice(line);
                }
                Ok(())
            }
        }
    }
}
