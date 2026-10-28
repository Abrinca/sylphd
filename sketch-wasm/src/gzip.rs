//! Push-based gzip decoder that handles multi-member files.
//!
//! Why it exists: the browser's `DecompressionStream('gzip')` rejects gzip
//! files made of several members ("Junk found after end of compressed data"),
//! and those are common for sequencing reads (BGZF, ONT basecallers that
//! `cat` batches, `cat a.fq.gz b.fq.gz`). The deflate body is inflated with
//! flate2's `zlib-rs` backend (raw deflate); the gzip framing (RFC 1952: header, CRC32/ISIZE trailer,
//! member loop) is done here. Input comes in arbitrary chunks.

use flate2::{Decompress, FlushDecompress, Status};
use zlib_rs::crc32::crc32;

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
    inflater: Decompress,
    /// Verify CRC32 and ISIZE of every member.
    verify: bool,
    crc: u32,
    isize: u32,
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
        Self::with_verify(true)
    }

    pub fn with_verify(verify: bool) -> Self {
        Self {
            stage: Stage::Header,
            header: Vec::new(),
            trailer: Vec::new(),
            inflater: Decompress::new(false),
            verify,
            crc: 0,
            isize: 0,
            members: 0,
            padding: false,
        }
    }

    /// Decodes one chunk, passing decompressed bytes to `sink` (in pieces of
    /// up to 128 KiB).
    pub fn push(
        &mut self,
        mut input: &[u8],
        sink: &mut dyn FnMut(&[u8]) -> Result<(), String>,
    ) -> Result<(), String> {
        let mut out = Vec::with_capacity(128 * 1024);
        while !input.is_empty() {
            out.clear();
            self.decode(&mut input, &mut out, 128 * 1024)?;
            if !out.is_empty() {
                sink(&out)?;
            }
        }
        Ok(())
    }

    /// Decodes from `input` (advancing it) and appends to `out` until `input`
    /// is used up or `out` holds at least `limit` bytes. Inflates straight into
    /// `out`'s spare capacity.
    pub fn decode(
        &mut self,
        input: &mut &[u8],
        out: &mut Vec<u8>,
        limit: usize,
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
                        *input = &[];
                        return Ok(());
                    }
                    // append byte by byte until the header is complete
                    let mut used = 0;
                    while used < input.len() {
                        self.header.push(input[used]);
                        used += 1;
                        if let Some(len) = parse_header(&self.header)? {
                            debug_assert_eq!(len, self.header.len());
                            self.header.clear();
                            self.inflater.reset(false);
                            self.crc = 0;
                            self.isize = 0;
                            self.stage = Stage::Body;
                            break;
                        }
                    }
                    *input = &input[used..];
                }
                Stage::Body => {
                    if out.len() >= limit {
                        return Ok(());
                    }
                    out.reserve(limit - out.len());
                    let (in_before, out_before) =
                        (self.inflater.total_in(), self.inflater.total_out());
                    let start = out.len();
                    let status = self
                        .inflater
                        .decompress_vec(input, out, FlushDecompress::None)
                        .map_err(|e| format!("corrupt gzip data ({e})"))?;
                    let consumed = (self.inflater.total_in() - in_before) as usize;
                    let produced = (self.inflater.total_out() - out_before) as usize;
                    *input = &input[consumed..];
                    if self.verify {
                        self.crc = crc32(self.crc, &out[start..]);
                    }
                    self.isize = self.isize.wrapping_add(produced as u32);
                    match status {
                        Status::StreamEnd => self.stage = Stage::Trailer,
                        Status::Ok => {}
                        Status::BufError => {
                            if consumed == 0 && produced == 0 {
                                // no progress possible: needs more input or output room
                                return if input.is_empty() {
                                    Ok(())
                                } else {
                                    Err("corrupt gzip data (stalled)".into())
                                };
                            }
                        }
                    }
                }
                Stage::Trailer => {
                    let need = 8 - self.trailer.len();
                    let take = need.min(input.len());
                    self.trailer.extend_from_slice(&input[..take]);
                    *input = &input[take..];
                    if self.trailer.len() == 8 {
                        let crc = u32::from_le_bytes(self.trailer[..4].try_into().unwrap());
                        let size = u32::from_le_bytes(self.trailer[4..].try_into().unwrap());
                        if (self.verify && crc != self.crc) || size != self.isize {
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
        let (members, verify) = (self.members, self.verify);
        *self = Self::with_verify(verify);
        if members == 0 {
            Err("empty or invalid gzip file".into())
        } else if complete {
            Ok(())
        } else {
            Err("gzip data is truncated".into())
        }
    }
}
