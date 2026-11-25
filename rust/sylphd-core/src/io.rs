//! Native `std::io::Read` helpers with automatic gzip detection.
//!
//! Uses `flate2::read::MultiGzDecoder` (`zlib-rs` backend) so plain gzip,
//! BGZF and concatenated gzip files all stream through the same reader.

use std::fs::File;
use std::io::{self, BufReader, Chain, Cursor, Read};
use std::path::Path;

use flate2::read::MultiGzDecoder;

const GZIP_MAGIC: [u8; 2] = [0x1f, 0x8b];

/// The sniffed bytes put back in front of the rest of the stream.
type Prefixed<R> = Chain<Cursor<Vec<u8>>, R>;

/// Reader that transparently decompresses gzip (including multi-member / BGZF)
/// when the stream starts with the gzip magic bytes `1f 8b`, and passes plain
/// bytes through unchanged otherwise.
pub struct MaybeGzReader<R: Read> {
    inner: MaybeGzInner<R>,
}

enum MaybeGzInner<R: Read> {
    Plain(Prefixed<R>),
    Gzip(Box<MultiGzDecoder<Prefixed<R>>>),
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
        let prefixed = Cursor::new(prefix[..len].to_vec()).chain(reader);
        let inner = if len == 2 && prefix == GZIP_MAGIC {
            MaybeGzInner::Gzip(Box::new(MultiGzDecoder::new(prefixed)))
        } else {
            MaybeGzInner::Plain(prefixed)
        };
        Ok(Self { inner })
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

/// Size of the chunks [`for_each_chunk`] reads.
const CHUNK_SIZE: usize = 128 * 1024;

/// Reads `reader` (gzip detected automatically) to the end in chunks, passing
/// each to `chunk`. I/O errors become `E` through its `From<String>`.
pub fn for_each_chunk<R: Read, E: From<String>>(
    reader: R,
    mut chunk: impl FnMut(&[u8]) -> Result<(), E>,
) -> Result<(), E> {
    let mut reader = MaybeGzReader::new(reader).map_err(|e| E::from(e.to_string()))?;
    let mut buf = vec![0u8; CHUNK_SIZE];
    loop {
        match reader.read(&mut buf).map_err(|e| E::from(e.to_string()))? {
            0 => return Ok(()),
            n => chunk(&buf[..n])?,
        }
    }
}
