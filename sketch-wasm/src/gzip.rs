//! Push-based gzip decoder that handles multi-member files.
//!
//! Why it exists: the browser's `DecompressionStream('gzip')` rejects gzip
//! files made of several members ("Junk found after end of compressed data"),
//! and those are common for sequencing reads (BGZF, ONT basecallers that
//! `cat` batches, `cat a.fq.gz b.fq.gz`). The deflate body is inflated with
//! `miniz_oxide`; the gzip framing (RFC 1952: header, CRC32/ISIZE trailer,
//! member loop) is done here. Input comes in arbitrary chunks.

use crc32fast::Hasher;
use miniz_oxide::inflate::stream::{inflate, InflateState};
use miniz_oxide::{DataFormat, MZError, MZFlush, MZStatus};

const OUT_SIZE: usize = 128 * 1024;

enum Stage {
    /// Collecting the bytes of a member header.
    Header,
    /// Inflating the deflate stream.
    Body,
    /// Collecting the 8-byte trailer.
    Trailer,
}

pub struct GzipDecoder {
    stage: Stage,
    header: Vec<u8>,
    trailer: Vec<u8>,
    state: Box<InflateState>,
    crc: Hasher,
    isize: u32,
    out: Vec<u8>,
    /// Completed members.
    pub members: u32,
    /// Bytes seen after the last member that were all zero (tape padding).
    padding: bool,
}

impl Default for GzipDecoder {
    fn default() -> Self {
        Self::new()
    }
}

/// `Ok(Some(len))`: the header is complete and `len` bytes long; `Ok(None)`:
/// more bytes are needed.
fn parse_header(h: &[u8]) -> Result<Option<usize>, String> {
    if h.len() >= 1 && h[0] != 0x1f || h.len() >= 2 && h[1] != 0x8b {
        return Err("not gzip data (bad magic)".into());
    }
    if h.len() < 10 {
        return Ok(None);
    }
    if h[2] != 8 {
        return Err(format!("unsupported gzip compression method {}", h[2]));
    }
    let flags = h[3];
    if flags & 0xe0 != 0 {
        return Err("reserved gzip flags set".into());
    }
    let mut pos = 10;
    if flags & 0x04 != 0 {
        if h.len() < pos + 2 {
            return Ok(None);
        }
        let xlen = u16::from_le_bytes([h[pos], h[pos + 1]]) as usize;
        pos += 2 + xlen;
        if h.len() < pos {
            return Ok(None);
        }
    }
    for flag in [0x08, 0x10] {
        if flags & flag != 0 {
            match h[pos.min(h.len())..].iter().position(|&b| b == 0) {
                Some(n) => pos += n + 1,
                None => return Ok(None),
            }
        }
    }
    if flags & 0x02 != 0 {
        pos += 2;
    }
    Ok((h.len() >= pos).then_some(pos))
}

impl GzipDecoder {
    pub fn new() -> Self {
        Self {
            stage: Stage::Header,
            header: Vec::new(),
            trailer: Vec::new(),
            state: InflateState::new_boxed(DataFormat::Raw),
            crc: Hasher::new(),
            isize: 0,
            out: vec![0; OUT_SIZE],
            members: 0,
            padding: false,
        }
    }

    /// Decodes one chunk, passing decompressed bytes to `sink`.
    pub fn push(
        &mut self,
        mut input: &[u8],
        sink: &mut dyn FnMut(&[u8]) -> Result<(), String>,
    ) -> Result<(), String> {
        while !input.is_empty() {
            match self.stage {
                Stage::Header => {
                    if self.padding || (self.header.is_empty() && input[0] == 0) {
                        // zero padding after the last member
                        if input.iter().any(|&b| b != 0) {
                            return Err("junk after the end of the gzip data".into());
                        }
                        self.padding = true;
                        return Ok(());
                    }
                    // append byte by byte until the header is complete
                    let mut used = 0;
                    loop {
                        if used == input.len() {
                            break;
                        }
                        self.header.push(input[used]);
                        used += 1;
                        if let Some(len) = parse_header(&self.header)? {
                            debug_assert_eq!(len, self.header.len());
                            self.header.clear();
                            self.state.reset(DataFormat::Raw);
                            self.crc = Hasher::new();
                            self.isize = 0;
                            self.stage = Stage::Body;
                            break;
                        }
                    }
                    input = &input[used..];
                }
                Stage::Body => loop {
                    let result = inflate(&mut self.state, input, &mut self.out, MZFlush::None);
                    let produced = &self.out[..result.bytes_written];
                    self.crc.update(produced);
                    self.isize = self.isize.wrapping_add(produced.len() as u32);
                    sink(produced)?;
                    input = &input[result.bytes_consumed..];
                    match result.status {
                        Ok(MZStatus::StreamEnd) => {
                            self.stage = Stage::Trailer;
                            break;
                        }
                        Ok(_) | Err(MZError::Buf) => {}
                        Err(error) => return Err(format!("corrupt gzip data ({error:?})")),
                    }
                    if result.bytes_consumed == 0 && result.bytes_written == 0 {
                        return if input.is_empty() {
                            Ok(())
                        } else {
                            Err("corrupt gzip data (stalled)".into())
                        };
                    }
                    // an unfilled output buffer means everything pending was flushed
                    if input.is_empty() && result.bytes_written < self.out.len() {
                        return Ok(());
                    }
                },
                Stage::Trailer => {
                    let need = 8 - self.trailer.len();
                    let take = need.min(input.len());
                    self.trailer.extend_from_slice(&input[..take]);
                    input = &input[take..];
                    if self.trailer.len() == 8 {
                        let crc = u32::from_le_bytes(self.trailer[..4].try_into().unwrap());
                        let size = u32::from_le_bytes(self.trailer[4..].try_into().unwrap());
                        if crc != self.crc.clone().finalize() || size != self.isize {
                            return Err("gzip CRC or length check failed (corrupt data)".into());
                        }
                        self.trailer.clear();
                        self.members += 1;
                        self.stage = Stage::Header;
                    }
                }
            }
        }
        Ok(())
    }

    /// Checks that the input ended on a member boundary and resets the decoder
    /// for another file.
    pub fn finish(&mut self) -> Result<(), String> {
        let complete =
            matches!(self.stage, Stage::Header) && (self.header.is_empty() || self.padding);
        let members = self.members;
        *self = Self::new();
        if members == 0 {
            Err("empty or invalid gzip file".into())
        } else if complete {
            Ok(())
        } else {
            Err("gzip data is truncated".into())
        }
    }
}
