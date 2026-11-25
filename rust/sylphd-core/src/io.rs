//! Native `std::io::Read` helpers with automatic gzip detection.
//!
//! Uses `flate2::read::MultiGzDecoder` (`zlib-rs` backend) so plain gzip,
//! BGZF and concatenated gzip files all stream through the same reader.

use std::fs::File;
use std::io::{self, BufReader, Read};
use std::path::Path;

use flate2::read::MultiGzDecoder;

const GZIP_MAGIC: [u8; 2] = [0x1f, 0x8b];

struct PrefixReader<R> {
    prefix: [u8; 2],
    pos: usize,
    len: usize,
    inner: R,
}

impl<R: Read> Read for PrefixReader<R> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        if self.pos < self.len {
            let n = (self.len - self.pos).min(buf.len());
            buf[..n].copy_from_slice(&self.prefix[self.pos..self.pos + n]);
            self.pos += n;
            return Ok(n);
        }
        self.inner.read(buf)
    }
}

/// Reader that transparently decompresses gzip (including multi-member / BGZF)
/// when the stream starts with the gzip magic bytes `1f 8b`, and passes plain
/// bytes through unchanged otherwise.
pub struct MaybeGzReader<R: Read> {
    inner: MaybeGzInner<R>,
}

enum MaybeGzInner<R: Read> {
    Plain(PrefixReader<R>),
    Gzip(Box<MultiGzDecoder<PrefixReader<R>>>),
}

impl<R: Read> MaybeGzReader<R> {
    /// Inspects the first two bytes of `reader` to decide between plain and
    /// multi-member gzip decoding.
    pub fn new(mut reader: R) -> io::Result<Self> {
        let mut prefix = [0u8; 2];
        let mut len = 0;
        while len < 2 {
            match reader.read(&mut prefix[len..])? {
                0 => break,
                n => len += n,
            }
        }
        let prefixed = PrefixReader {
            prefix,
            pos: 0,
            len,
            inner: reader,
        };
        let inner = if len == 2 && prefix == GZIP_MAGIC {
            MaybeGzInner::Gzip(Box::new(MultiGzDecoder::new(prefixed)))
        } else {
            MaybeGzInner::Plain(prefixed)
        };
        Ok(Self { inner })
    }

    /// Whether gzip magic bytes were detected.
    pub fn is_gzip(&self) -> bool {
        matches!(self.inner, MaybeGzInner::Gzip(_))
    }
}

impl<R: Read> Read for MaybeGzReader<R> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        match &mut self.inner {
            MaybeGzInner::Plain(r) => r.read(buf),
            MaybeGzInner::Gzip(r) => r.read(buf),
        }
    }
}

/// Opens a file with buffered reading and automatic gzip decompression.
pub fn open_maybe_gzip(path: impl AsRef<Path>) -> io::Result<MaybeGzReader<BufReader<File>>> {
    let file = File::open(path)?;
    MaybeGzReader::new(BufReader::with_capacity(128 * 1024, file))
}
