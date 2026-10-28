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

/// What to do with each member's CRC32.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum CrcMode {
    /// Compute and compare while decoding (costs ~15 % of the decode time).
    Inline,
    /// Do not compute; record (end offset, expected CRC32, ISIZE) per member
    /// in `GzipDecoder::trailers` so that another thread can check them
    /// (`Crc32Tracker`).
    Deferred,
    /// Never check (ISIZE is still checked).
    Off,
}

/// Running CRC32 of a decompressed stream, checked against the
/// `GzipDecoder::trailers` of the decoder that produced it.
#[derive(Default)]
pub struct Crc32Tracker {
    run: u32,
    pos: u64,
}

impl Crc32Tracker {
    /// `data` is the next piece of the decompressed stream; `trailers` are the
    /// members completed within it, as flat `(end offset in the stream,
    /// expected crc, isize)` triples.
    pub fn update(&mut self, data: &[u8], trailers: &[(u64, u32, u32)]) -> Result<(), String> {
        let mut cursor = 0usize;
        for &(end, expected, _) in trailers {
            let end = end
                .checked_sub(self.pos)
                .filter(|&e| e as usize >= cursor && e as usize <= data.len())
                .ok_or("gzip member boundary outside the chunk (internal error)")?
                as usize;
            self.run = crc32(self.run, &data[cursor..end]);
            if self.run != expected {
                return Err("gzip CRC32 mismatch (corrupt data)".into());
            }
            self.run = 0;
            cursor = end;
        }
        self.run = crc32(self.run, &data[cursor..]);
        self.pos += data.len() as u64;
        Ok(())
    }

    /// Start of the next file.
    pub fn reset(&mut self) {
        *self = Self::default();
    }
}

pub struct GzipDecoder {
    stage: Stage,
    header: Vec<u8>,
    trailer: Vec<u8>,
    inflater: Decompress,
    mode: CrcMode,
    crc: u32,
    isize: u32,
    /// Decompressed bytes produced for the current file.
    file_out: u64,
    /// `CrcMode::Deferred`: members completed since the last `take_trailers`.
    trailers: Vec<(u64, u32, u32)>,
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
        Self::with_mode(CrcMode::Inline)
    }

    /// Completed members' `(end offset, crc, isize)` (deferred CRC mode).
    pub fn take_trailers(&mut self) -> Vec<(u64, u32, u32)> {
        std::mem::take(&mut self.trailers)
    }

    pub fn with_mode(mode: CrcMode) -> Self {
        Self {
            stage: Stage::Header,
            header: Vec::new(),
            trailer: Vec::new(),
            inflater: Decompress::new(false),
            mode,
            crc: 0,
            isize: 0,
            file_out: 0,
            trailers: Vec::new(),
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
                    if self.mode == CrcMode::Inline {
                        self.crc = crc32(self.crc, &out[start..]);
                    }
                    self.isize = self.isize.wrapping_add(produced as u32);
                    self.file_out += produced as u64;
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
                        if (self.mode == CrcMode::Inline && crc != self.crc) || size != self.isize {
                            return Err("gzip CRC or length check failed (corrupt data)".into());
                        }
                        if self.mode == CrcMode::Deferred {
                            self.trailers.push((self.file_out, crc, size));
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
        let (members, mode) = (self.members, self.mode);
        *self = Self::with_mode(mode);
        if members == 0 {
            Err("empty or invalid gzip file".into())
        } else if complete {
            Ok(())
        } else {
            Err("gzip data is truncated".into())
        }
    }
}
