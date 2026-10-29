//! Streaming FASTQ / FASTA record parser.
//!
//! Bytes arrive in arbitrary chunks (a record, or even a CRLF pair, may be
//! split across two chunks). The parser is a line state machine: complete
//! lines are handled straight from the chunk, an incomplete trailing line is
//! carried over in `partial`. Only the header and the sequence of the current
//! record are copied; qualities are skipped (their length is checked).
//!
//! The format is detected from the first non-empty line (`@` = FASTQ,
//! `>` = FASTA). FASTQ is the usual 4-line layout; FASTA sequences may span
//! several lines. Both accept `\n` and `\r\n` line ends and blank lines
//! between records. Every record, including one with an empty sequence, is
//! passed to the callback, as `dnaio` (what sylphd uses to feed pysylph) does.
//!
//! The callback receives the sequence and the header line (with its leading
//! `@` / `>` and a trailing CR, which is what `dnaio`'s `record.id` is derived
//! from); the read sketchers only use the sequence.

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
    /// Header line of the current record, marker included (`@`/`>`).
    header: Vec<u8>,
    /// FASTA: a header has been seen and its record has not been emitted yet.
    pending: bool,
    line: u64,
    /// 0 = unlimited. Otherwise the longest sequence (and line, and header)
    /// the parser accepts, checked before a buffer grows, so a single huge
    /// record cannot exhaust the browser's wasm heap.
    max_sequence: usize,
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
            header: Vec::new(),
            pending: false,
            line: 0,
            max_sequence: 0,
        }
    }

    /// Refuse any single line or sequence longer than `limit` bases (0 = no
    /// limit). The check runs before a buffer is extended.
    pub fn with_max_sequence(mut self, limit: usize) -> Self {
        self.max_sequence = limit;
        self
    }

    fn too_long(&self, what: &str) -> Result<(), String> {
        Err(format!(
            "line {}: {what} is longer than {} bp, the browser's per-record limit; \
             classify this input with the sylphd command line",
            self.line, self.max_sequence
        ))
    }

    /// Feeds one chunk; `emit` is called with `(sequence, header)` for every
    /// record completed by it.
    pub fn feed(
        &mut self,
        mut data: &[u8],
        emit: &mut dyn FnMut(&[u8], &[u8]),
    ) -> Result<(), String> {
        while let Some(pos) = memchr(b'\n', data) {
            let (line, rest) = (&data[..pos], &data[pos + 1..]);
            if self.partial.is_empty() {
                self.on_line(line, emit)?;
            } else {
                // `on_line` needs `&mut self`, so move the buffer out meanwhile.
                // Check the combined length before extending: `on_line` would
                // see the limit only after the buffer had already grown, and a
                // line split across many chunks could pass it by more than a
                // chunk.
                let mut joined = std::mem::take(&mut self.partial);
                // `on_line` strips one trailing CR, so ignore it here too (the
                // CR of a split CRLF may sit in `partial` or in `line`).
                let trailing_cr =
                    usize::from(line.last().or_else(|| joined.last()) == Some(&b'\r'));
                if self.max_sequence > 0
                    && joined.len() + line.len() - trailing_cr > self.max_sequence
                {
                    self.partial = joined;
                    return self.too_long("a line");
                }
                joined.extend_from_slice(line);
                let result = self.on_line(&joined, emit);
                joined.clear();
                self.partial = joined;
                result?;
            }
            data = rest;
        }
        // An incomplete last line: check before growing, so a single huge
        // record without a newline cannot fill the heap.
        if self.max_sequence > 0 && self.partial.len() + data.len() > self.max_sequence {
            return self.too_long("a line");
        }
        self.partial.extend_from_slice(data);
        Ok(())
    }

    /// Ends the current file: handles a last line without newline, emits a
    /// pending FASTA record and resets the parser for a following file.
    pub fn finish(&mut self, emit: &mut dyn FnMut(&[u8], &[u8])) -> Result<(), String> {
        let result = self.finish_inner(emit);
        *self = Self::new();
        result
    }

    fn finish_inner(&mut self, emit: &mut dyn FnMut(&[u8], &[u8])) -> Result<(), String> {
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
                    emit(&self.seq, &self.header);
                }
                Ok(())
            }
        }
    }

    fn on_line(&mut self, line: &[u8], emit: &mut dyn FnMut(&[u8], &[u8])) -> Result<(), String> {
        self.line += 1;
        let line = line.strip_suffix(b"\r").unwrap_or(line);
        if self.max_sequence > 0 && line.len() > self.max_sequence {
            return self.too_long("a line");
        }
        match self.format {
            Format::Unknown => match line.first() {
                None => Ok(()),
                Some(b'@') => {
                    self.header.clear();
                    self.header.extend_from_slice(line);
                    self.format = Format::Fastq(FastqState::Seq);
                    Ok(())
                }
                Some(b'>') => {
                    self.format = Format::Fasta;
                    self.pending = true;
                    self.header.clear();
                    self.header.extend_from_slice(line);
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
                        self.header.clear();
                        self.header.extend_from_slice(line);
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
                    emit(&self.seq, &self.header);
                    self.format = Format::Fastq(FastqState::Header);
                    Ok(())
                }
            },
            Format::Fasta => {
                if line.first() == Some(&b'>') {
                    if self.pending {
                        emit(&self.seq, &self.header);
                    }
                    self.header.clear();
                    self.header.extend_from_slice(line);
                    self.seq.clear();
                    self.pending = true;
                } else if line.is_empty() || line[0] == b';' {
                    // blank or comment line
                } else if !self.pending {
                    return Err(format!("line {}: sequence before first header", self.line));
                } else {
                    if self.max_sequence > 0
                        && self.seq.len() + line.len() > self.max_sequence
                    {
                        return self.too_long("a record");
                    }
                    self.seq.extend_from_slice(line);
                }
                Ok(())
            }
        }
    }
}

/// The record name as `dnaio` sees it: the header without its leading `@` /
/// `>` marker up to the first whitespace (so `>c1 some description` gives
/// `c1`). Empty when the header has no name. Invalid UTF-8 becomes U+FFFD.
pub fn record_name(header: &[u8]) -> String {
    let header = header
        .strip_prefix(b">")
        .or_else(|| header.strip_prefix(b"@"))
        .unwrap_or(header);
    let end = header
        .iter()
        .position(|b| b.is_ascii_whitespace())
        .unwrap_or(header.len());
    String::from_utf8_lossy(&header[..end]).into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(data: &[u8], chunk: usize) -> Vec<(String, String)> {
        let mut parser = RecordParser::new();
        let mut out = Vec::new();
        for piece in data.chunks(chunk) {
            parser
                .feed(piece, &mut |seq, header| {
                    out.push((
                        record_name(header),
                        String::from_utf8_lossy(seq).into_owned(),
                    ))
                })
                .unwrap();
        }
        parser
            .finish(&mut |seq, header| {
                out.push((
                    record_name(header),
                    String::from_utf8_lossy(seq).into_owned(),
                ))
            })
            .unwrap();
        out
    }

    #[test]
    fn names_and_sequences_any_chunking() {
        let fasta = b">c1 description here\nACGT\nAC\n\n>c2\tx\nTT\n>c3\n";
        let expected = vec![
            ("c1".to_string(), "ACGTAC".to_string()),
            ("c2".to_string(), "TT".to_string()),
            ("c3".to_string(), "".to_string()),
        ];
        for chunk in [1, 2, 3, 7, 4096] {
            assert_eq!(parse(fasta, chunk), expected, "chunk {chunk}");
        }
        let fastq = b"@r1/1 x\nACGT\n+\nIIII\n@r2\nTT\n+\nII\n";
        let expected = vec![
            ("r1/1".to_string(), "ACGT".to_string()),
            ("r2".to_string(), "TT".to_string()),
        ];
        for chunk in [1, 3, 5, 4096] {
            assert_eq!(parse(fastq, chunk), expected, "chunk {chunk}");
        }
    }

    #[test]
    fn crlf_and_missing_final_newline() {
        let data = b">a\r\nACGT\r\n>b\r\nTT";
        assert_eq!(
            parse(data, 2),
            vec![
                ("a".to_string(), "ACGT".to_string()),
                ("b".to_string(), "TT".to_string())
            ]
        );
    }

    #[test]
    fn a_line_split_across_chunks_cannot_outgrow_the_limit() {
        // Two chunks complete the carry buffer, the third chunk closes the
        // line: the join must be refused before the buffer grows past the
        // limit (otherwise the whole 15-byte line would be assembled first).
        let mut parser = RecordParser::new().with_max_sequence(10);
        let mut feeds = 0;
        let mut feed_error = None;
        for chunk in b"AAAAAAAAAAAAAAA\n".chunks(5) {
            match parser.feed(chunk, &mut |_, _| {}) {
                Ok(()) => {
                    feeds += 1;
                    assert!(parser.partial.len() <= 10);
                }
                Err(message) => {
                    feed_error = Some(message);
                    break;
                }
            }
        }
        assert_eq!(feeds, 2);
        assert_eq!(
            parser.partial.len(),
            10,
            "the buffer must not exceed the limit"
        );
        let error = feed_error.expect("the joined line must be refused");
        assert!(error.contains("per-record limit"), "{error}");
        assert!(parser.partial.len() <= 10);

        // A line of exactly the limit, split across chunks, is still accepted.
        let mut parser = RecordParser::new().with_max_sequence(11);
        let mut records = Vec::new();
        for chunk in b">abcdefghij\nACGT\n".chunks(2) {
            parser
                .feed(chunk, &mut |seq, header| {
                    records.push((record_name(header), seq.to_vec()))
                })
                .unwrap();
        }
        parser
            .finish(&mut |seq, header| records.push((record_name(header), seq.to_vec())))
            .unwrap();
        assert_eq!(records, vec![("abcdefghij".to_string(), b"ACGT".to_vec())]);
    }
}
