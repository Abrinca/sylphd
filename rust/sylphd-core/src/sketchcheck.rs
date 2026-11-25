//! Structural validation and header parsing of `.sylsp` sample-sketch bytes.
//!
//! Layout of sylph's `SequencesSketch` (bincode 1.x legacy config: fixint,
//! little-endian; trailing bytes are ignored):
//!
//! 1. `kmer_counts`: `u64 n`, then `n` x (`u64` kmer, `u32` count) = `12 * n` bytes
//! 2. `c`: `u64`
//! 3. `k`: `u64`
//! 4. `file_name`: `String` (`u64` length + UTF-8 bytes)
//! 5. `sample_name`: `Option<String>` (`u8` tag: `0` = None, `1` = Some + `String`)
//! 6. `paired`: `bool` (`u8`: `0` / `1`)
//! 7. `mean_read_length`: `f64`
//!
//! Mirrors `src/sylphd/sketchcheck.py` so the client validates pre-made `.sylsp`
//! sketches without allocating from an untrusted length prefix.

use std::fmt;
#[cfg(feature = "io")]
use std::fs::File;
#[cfg(feature = "io")]
use std::io::{Read, Seek, SeekFrom};
#[cfg(feature = "io")]
use std::path::Path;

use crate::input::InputError;

/// Size of one `(kmer, count)` entry: `u64` + `u32`.
const ENTRY_SIZE: u64 = 12;

/// Error returned when bytes cannot be a valid sylph `SequencesSketch`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SketchFormatError(pub String);

impl fmt::Display for SketchFormatError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for SketchFormatError {}

/// Validated metadata fields of a `.sylsp` (`SequencesSketch`) file.
#[derive(Debug, Clone, PartialEq)]
pub struct SketchHeader {
    pub c: u64,
    pub k: u64,
    pub file_name: String,
    pub sample_name: Option<String>,
    pub paired: bool,
    pub mean_read_length: f64,
    /// Number of k-mer entries declared by the length prefix.
    pub kmer_count: u64,
}

fn take_bytes<'a>(
    data: &'a [u8],
    offset: &mut usize,
    base_offset: u64,
    len: usize,
    field: &str,
) -> Result<&'a [u8], SketchFormatError> {
    let available = data.len().saturating_sub(*offset);
    if len > available {
        return Err(SketchFormatError(format!(
            "sketch is truncated inside '{field}' \
             (needs {len} bytes at offset {}, only {available} left)",
            base_offset + *offset as u64
        )));
    }
    let slice = &data[*offset..*offset + len];
    *offset += len;
    Ok(slice)
}

fn take_u64(
    data: &[u8],
    offset: &mut usize,
    base_offset: u64,
    field: &str,
) -> Result<u64, SketchFormatError> {
    let raw = take_bytes(data, offset, base_offset, 8, field)?;
    Ok(u64::from_le_bytes(raw.try_into().unwrap()))
}

fn take_u8(
    data: &[u8],
    offset: &mut usize,
    base_offset: u64,
    field: &str,
) -> Result<u8, SketchFormatError> {
    let raw = take_bytes(data, offset, base_offset, 1, field)?;
    Ok(raw[0])
}

fn take_f64(
    data: &[u8],
    offset: &mut usize,
    base_offset: u64,
    field: &str,
) -> Result<f64, SketchFormatError> {
    let raw = take_bytes(data, offset, base_offset, 8, field)?;
    Ok(f64::from_le_bytes(raw.try_into().unwrap()))
}

fn take_string(
    data: &[u8],
    offset: &mut usize,
    base_offset: u64,
    field: &str,
) -> Result<String, SketchFormatError> {
    let length_field = format!("{field} length");
    let length = take_u64(data, offset, base_offset, &length_field)?;
    let available = (data.len() - *offset) as u64;
    if length > available {
        return Err(SketchFormatError(format!(
            "sketch '{field}' declares {length} bytes but only {available} remain"
        )));
    }
    let len = length as usize;
    let raw = &data[*offset..*offset + len];
    *offset += len;
    std::str::from_utf8(raw)
        .map(str::to_owned)
        .map_err(|_| SketchFormatError(format!("sketch '{field}' is not valid UTF-8")))
}

fn parse_sketch_tail(
    tail: &[u8],
    kmer_count: u64,
    base_offset: u64,
) -> Result<SketchHeader, SketchFormatError> {
    let mut offset = 0usize;
    let c = take_u64(tail, &mut offset, base_offset, "c")?;
    let k = take_u64(tail, &mut offset, base_offset, "k")?;
    let file_name = take_string(tail, &mut offset, base_offset, "file_name")?;

    let tag = take_u8(tail, &mut offset, base_offset, "sample_name tag")?;
    let sample_name = match tag {
        0 => None,
        1 => Some(take_string(tail, &mut offset, base_offset, "sample_name")?),
        other => {
            return Err(SketchFormatError(format!(
                "sketch 'sample_name' tag is {other}, expected 0 (None) or 1 (Some)"
            )));
        }
    };

    let paired = match take_u8(tail, &mut offset, base_offset, "paired")? {
        0 => false,
        1 => true,
        other => {
            return Err(SketchFormatError(format!(
                "sketch 'paired' flag is {other}, expected 0 or 1"
            )));
        }
    };

    let mean_read_length = take_f64(tail, &mut offset, base_offset, "mean_read_length")?;
    if !mean_read_length.is_finite() {
        return Err(SketchFormatError(format!(
            "sketch 'mean_read_length' is {mean_read_length}, expected a finite float"
        )));
    }

    Ok(SketchHeader {
        c,
        k,
        file_name,
        sample_name,
        paired,
        mean_read_length,
        kmer_count,
    })
}

/// Parses and validates the header of a `.sylsp` byte slice; trailing bytes are allowed.
pub fn parse_sketch_header(data: &[u8]) -> Result<SketchHeader, SketchFormatError> {
    let size = data.len() as u64;
    if size < 8 {
        return Err(SketchFormatError(format!(
            "sketch is {size} bytes, too short for a k-mer count prefix"
        )));
    }
    let count = u64::from_le_bytes(data[..8].try_into().unwrap());
    let kmer_bytes = count.checked_mul(ENTRY_SIZE);
    let kmer_end = kmer_bytes.and_then(|n| n.checked_add(8));
    let Some(kmer_end) = kmer_end.filter(|&end| end <= size) else {
        let needed = match kmer_bytes {
            Some(n) => format!("{n} bytes"),
            None => "overflowing size".to_string(),
        };
        return Err(SketchFormatError(format!(
            "sketch declares {count} k-mers ({needed}) but is only {size} bytes long"
        )));
    };
    parse_sketch_tail(&data[kmer_end as usize..], count, kmer_end)
}

/// Validates `.sylsp` bytes and returns `(c, k)`.
pub fn validate_sketch_bytes(data: &[u8]) -> Result<(u64, u64), SketchFormatError> {
    let header = parse_sketch_header(data)?;
    Ok((header.c, header.k))
}

/// Reads and validates a `.sylsp` header from a file without loading the k-mer
/// table into memory.
#[cfg(feature = "io")]
pub fn read_sketch_header(path: impl AsRef<Path>) -> Result<SketchHeader, SketchFormatError> {
    let path = path.as_ref();
    let mut file = File::open(path)
        .map_err(|e| SketchFormatError(format!("cannot open {}: {e}", path.display())))?;
    let size = file
        .metadata()
        .map_err(|e| SketchFormatError(format!("cannot stat {}: {e}", path.display())))?
        .len();
    if size < 8 {
        return Err(SketchFormatError(format!(
            "sketch is {size} bytes, too short for a k-mer count prefix"
        )));
    }
    let mut prefix = [0u8; 8];
    file.read_exact(&mut prefix)
        .map_err(|e| SketchFormatError(format!("cannot read {}: {e}", path.display())))?;
    let count = u64::from_le_bytes(prefix);
    let kmer_bytes = count.checked_mul(ENTRY_SIZE);
    let kmer_end = kmer_bytes.and_then(|n| n.checked_add(8));
    let Some(kmer_end) = kmer_end.filter(|&end| end <= size) else {
        let needed = match kmer_bytes {
            Some(n) => format!("{n} bytes"),
            None => "overflowing size".to_string(),
        };
        return Err(SketchFormatError(format!(
            "sketch declares {count} k-mers ({needed}) but is only {size} bytes long"
        )));
    };
    file.seek(SeekFrom::Start(kmer_end))
        .map_err(|e| SketchFormatError(format!("cannot seek {}: {e}", path.display())))?;
    let mut tail = Vec::new();
    file.read_to_end(&mut tail)
        .map_err(|e| SketchFormatError(format!("cannot read {}: {e}", path.display())))?;
    parse_sketch_tail(&tail, count, kmer_end)
}

/// Checks that a sketch header is compatible with a database's `(c, k)`.
///
/// Mirrors `sylphd.sketching.check_sketch`: `k` must match exactly, and the
/// sketch's `c` must not be sparser (`>`) than the database's `c`.
pub fn check_sketch_compatibility(
    header: &SketchHeader,
    db_c: u64,
    db_k: u64,
    name: Option<&str>,
) -> Result<(), InputError> {
    let prefix = match name {
        Some(n) if !n.is_empty() => format!("{n}: "),
        _ => String::new(),
    };
    if header.k != db_k {
        return Err(InputError(format!(
            "{prefix}sketch was built with k={}, but the database uses k={db_k}",
            header.k
        )));
    }
    if header.c > db_c {
        return Err(InputError(format!(
            "{prefix}sketch was built with c={}, which is sparser than the \
             database c={db_c}; re-sketch it at c<={db_c} or send the reads instead",
            header.c
        )));
    }
    Ok(())
}
