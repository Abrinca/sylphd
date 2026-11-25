//! Core client library for sylphd (no networking, no wasm-bindgen).
//!
//! Covers FASTA/FASTQ parsing, sylph read and per-contig sketching (via the
//! `sylph` library core), sample naming and input specification parsing,
//! `.sylsp` header validation, assembly stats, and per-contig batch planning.

pub mod batch;
pub mod contig;
pub mod input;
#[cfg(feature = "io")]
pub mod io;
pub mod parser;
pub mod sketch;
pub mod sketchcheck;
pub mod stream;

pub use sylph::seeding::Seeding;
