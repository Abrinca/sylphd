//! Sample naming, CLI input specification parsing, assembly stats, contig
//! filtering and native sketching helpers.

use std::collections::{HashMap, HashSet};
use std::fmt;
#[cfg(feature = "io")]
use std::fs;
#[cfg(feature = "io")]
use std::io::Read;
#[cfg(feature = "io")]
use std::path::Path;

use serde::{Deserialize, Serialize};
use sylph::seeding::Seeding;

use crate::contig::ContigRecord;
#[cfg(feature = "io")]
use crate::contig::{ContigSketcher, DEFAULT_MIN_SPACING};
#[cfg(feature = "io")]
use crate::io::for_each_chunk;
use crate::parser::RecordParser;
use crate::sketch::{default_fpr, gc_percent, SketchBuilder};
#[cfg(feature = "io")]
use crate::sketchcheck::{check_sketch_compatibility, parse_sketch_header, SketchHeader};
#[cfg(feature = "io")]
use crate::stream::StreamSketcher;

const COMPRESSION_SUFFIXES: &[&str] = &[".gz", ".bz2", ".xz", ".zst", ".bgz", ".bgzf"];
const READ_SUFFIXES: &[&str] = &[
    ".fastq", ".fq", ".fasta", ".fa", ".fna", ".fas", ".fsa", ".sam", ".sylsp",
];

/// File extension of pre-made sylph sample sketches.
pub const SKETCH_SUFFIX: &str = ".sylsp";

/// Longest accepted sample name, in Unicode scalar values (characters).
pub const MAX_SAMPLE_NAME_CHARS: usize = 1024;

/// Default minimum contig length in bases for `--per-contig`.
pub const DEFAULT_MIN_CONTIG_LENGTH: u64 = 20_000;

/// Minimum sketched k-mers required for a contig to be queried.
pub const DEFAULT_MIN_NUMBER_KMERS: u64 = 10;

/// Error returned for invalid input specifications, unreadable files or
/// incompatible sketches.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InputError(pub String);

impl fmt::Display for InputError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for InputError {}

impl From<String> for InputError {
    fn from(message: String) -> Self {
        Self(message)
    }
}

/// Validates a sample name (length <= [`MAX_SAMPLE_NAME_CHARS`], no Unicode
/// control characters `Cc`).
pub fn check_sample_name(name: &str) -> Result<&str, InputError> {
    if name.chars().count() > MAX_SAMPLE_NAME_CHARS {
        return Err(InputError(format!(
            "sample name too long (max {MAX_SAMPLE_NAME_CHARS} characters)"
        )));
    }
    if name.chars().any(char::is_control) {
        return Err(InputError(format!(
            "sample name {name:?} contains a tab, newline or other control character"
        )));
    }
    Ok(name)
}

/// Strips a trailing `[_.]R?[12](_001)?` suffix (ASCII case-insensitive),
/// matching `sketching._PAIR_SUFFIX_RE` and `web/reads.js`.
fn strip_pair_suffix(base: &str) -> &str {
    let body = base.strip_suffix("_001").unwrap_or(base);
    let bytes = body.as_bytes();
    if bytes.len() < 2 {
        return base;
    }
    let last = bytes[bytes.len() - 1];
    if last != b'1' && last != b'2' {
        return base;
    }
    let mut pos = bytes.len() - 1;
    if pos > 0 && matches!(bytes[pos - 1], b'R' | b'r') {
        pos -= 1;
    }
    if pos > 0 && matches!(bytes[pos - 1], b'_' | b'.') {
        &body[..pos - 1]
    } else {
        base
    }
}

fn strip_suffix_ascii_ci<'a>(text: &'a str, suffix: &str) -> Option<&'a str> {
    if text.len() < suffix.len() {
        return None;
    }
    let cut = text.len() - suffix.len();
    if text.is_char_boundary(cut) && text[cut..].eq_ignore_ascii_case(suffix) {
        Some(&text[..cut])
    } else {
        None
    }
}

/// Derives a sample name from a file path.
///
/// Strips one compression suffix (`.gz`, `.bz2`, `.xz`, `.zst`, `.bgz`,
/// `.bgzf`), one sequence/sketch suffix (`.fastq`, `.fq`, `.fasta`, `.fa`,
/// `.fna`, `.fas`, `.fsa`, `.sam`, `.sylsp`), and when `paired` is true,
/// trailing mate markers (`_R1`, `.R2`, `_1`, `_2`, `_R1_001`, ...).
/// Falls back to `"sample"` if nothing remains.
pub fn sample_name_from_path(path: &str, paired: bool) -> String {
    let mut base = path.rsplit(['/', '\\']).next().unwrap_or("");
    for suffix in COMPRESSION_SUFFIXES {
        if let Some(stripped) = strip_suffix_ascii_ci(base, suffix) {
            base = stripped;
            break;
        }
    }
    for suffix in READ_SUFFIXES {
        if let Some(stripped) = strip_suffix_ascii_ci(base, suffix) {
            base = stripped;
            break;
        }
    }
    if paired {
        base = strip_pair_suffix(base);
    }
    if base.is_empty() {
        "sample".to_string()
    } else {
        base.to_string()
    }
}

/// Returns a unique sample name for one contig, suffixing repeated ids.
///
/// The first `contig` keeps its name, the second becomes `contig_2`, the third
/// `contig_3`, and a later record literally named `contig_2` becomes
/// `contig_2_2`. An empty `raw` name becomes `contig-{index}` (1-based).
pub fn unique_contig_name(
    raw: &str,
    counts: &mut HashMap<String, usize>,
    used: &mut HashSet<String>,
    index: u64,
) -> String {
    let base = if raw.is_empty() {
        format!("contig-{index}")
    } else {
        raw.to_string()
    };
    let mut count = counts.get(&base).copied().unwrap_or(0) + 1;
    let mut name = if count == 1 {
        base.clone()
    } else {
        format!("{base}_{count}")
    };
    while used.contains(&name) {
        count += 1;
        name = format!("{base}_{count}");
    }
    counts.insert(base, count);
    used.insert(name.clone());
    name
}

/// Stateful helper around [`unique_contig_name`].
#[derive(Debug, Default, Clone)]
pub struct ContigNameTracker {
    counts: HashMap<String, usize>,
    used: HashSet<String>,
}

impl ContigNameTracker {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn next_name(&mut self, raw: &str, index: u64) -> String {
        unique_contig_name(raw, &mut self.counts, &mut self.used, index)
    }
}

/// Files belonging to one sample, all of the same layout.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SampleFiles {
    /// One or more single-end FASTA/FASTQ files, or a single `.sylsp` sketch.
    Single(Vec<String>),
    /// One or more paired-end `(r1, r2)` lanes.
    Paired(Vec<(String, String)>),
    /// One or more interleaved paired-end FASTA/FASTQ files.
    Interleaved(Vec<String>),
}

/// One named sample ready to be sketched (or loaded if `.sylsp`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SampleInput {
    pub name: String,
    pub files: SampleFiles,
}

impl SampleInput {
    pub fn new(name: impl Into<String>, files: SampleFiles) -> Result<Self, InputError> {
        let name = name.into();
        check_sample_name(&name)?;
        let empty = match &files {
            SampleFiles::Single(v) | SampleFiles::Interleaved(v) => v.is_empty(),
            SampleFiles::Paired(v) => v.is_empty(),
        };
        if empty {
            return Err(InputError(format!("sample {name:?} has no input files")));
        }
        Ok(Self { name, files })
    }

    pub fn single(name: impl Into<String>, paths: Vec<String>) -> Result<Self, InputError> {
        Self::new(name, SampleFiles::Single(paths))
    }

    pub fn paired(
        name: impl Into<String>,
        pairs: Vec<(String, String)>,
    ) -> Result<Self, InputError> {
        Self::new(name, SampleFiles::Paired(pairs))
    }

    pub fn interleaved(name: impl Into<String>, paths: Vec<String>) -> Result<Self, InputError> {
        Self::new(name, SampleFiles::Interleaved(paths))
    }

    /// Replaces the sample name after validating it.
    pub fn with_name(mut self, name: impl Into<String>) -> Result<Self, InputError> {
        let name = name.into();
        check_sample_name(&name)?;
        self.name = name;
        Ok(self)
    }

    /// True for two-file paired inputs (false for interleaved).
    pub fn is_paired(&self) -> bool {
        matches!(self.files, SampleFiles::Paired(_))
    }

    /// True for interleaved paired-end inputs.
    pub fn is_interleaved(&self) -> bool {
        matches!(self.files, SampleFiles::Interleaved(_))
    }

    /// True when the sample is a single pre-made `.sylsp` sketch file.
    pub fn is_sketch(&self) -> bool {
        matches!(&self.files, SampleFiles::Single(paths) if paths.len() == 1 && paths[0].ends_with(SKETCH_SUFFIX))
    }

    /// Flat list of all file paths belonging to this sample, in input order.
    pub fn paths(&self) -> Vec<&str> {
        match &self.files {
            SampleFiles::Single(v) | SampleFiles::Interleaved(v) => {
                v.iter().map(String::as_str).collect()
            }
            SampleFiles::Paired(pairs) => pairs
                .iter()
                .flat_map(|(r1, r2)| [r1.as_str(), r2.as_str()])
                .collect(),
        }
    }
}

/// Parses `--sample NAME:file1,file2` or `NAME:r1+r2,r1b+r2b`.
///
/// `+` separates the two mates of a pair, `,` separates inputs of the same
/// sample. A spec without `:` uses `default_name` (or the first file's derived
/// sample name).
pub fn parse_multi_file_spec(
    spec: &str,
    default_name: Option<&str>,
) -> Result<SampleInput, InputError> {
    let (explicit_name, rest) = if let Some((name, rest)) = spec.split_once(':') {
        let trimmed = name.trim();
        if trimmed.is_empty() {
            return Err(InputError(format!("empty sample name in {spec:?}")));
        }
        (Some(trimmed), rest)
    } else {
        (default_name.filter(|s| !s.is_empty()), spec)
    };

    let mut singles: Vec<String> = Vec::new();
    let mut pairs: Vec<(String, String)> = Vec::new();

    for raw_item in rest.split(',').map(str::trim).filter(|s| !s.is_empty()) {
        let mates: Vec<&str> = raw_item
            .split('+')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .collect();
        if mates.is_empty() {
            continue;
        }
        match mates.len() {
            1 => singles.push(mates[0].to_string()),
            2 => pairs.push((mates[0].to_string(), mates[1].to_string())),
            n => {
                return Err(InputError(format!(
                    "{spec:?}: at most two files per input (R1+R2), got {n}"
                )));
            }
        }
    }

    if singles.is_empty() && pairs.is_empty() {
        return Err(InputError(format!("no input files in {spec:?}")));
    }
    if !singles.is_empty() && !pairs.is_empty() {
        return Err(InputError(format!(
            "{spec:?}: mixing single-end and paired inputs in one sample is not supported"
        )));
    }

    if !pairs.is_empty() {
        let name = match explicit_name {
            Some(n) => n.to_string(),
            None => sample_name_from_path(&pairs[0].0, true),
        };
        SampleInput::paired(name, pairs)
    } else {
        let name = match explicit_name {
            Some(n) => n.to_string(),
            None => sample_name_from_path(&singles[0], false),
        };
        SampleInput::single(name, singles)
    }
}

/// Pairs `--first` (`-1`) and `--second` (`-2`) file lists by position.
pub fn resolve_pair_paths(
    first: &[String],
    second: &[String],
) -> Result<Vec<(String, String)>, InputError> {
    if first.is_empty() && second.is_empty() {
        return Ok(Vec::new());
    }
    if first.len() != second.len() {
        return Err(InputError(format!(
            "--first/--second lists differ in length ({} != {})",
            first.len(),
            second.len()
        )));
    }
    Ok(first
        .iter()
        .cloned()
        .zip(second.iter().cloned())
        .collect())
}

/// Raw CLI sample arguments (mirroring `cli_parser._add_sample_options`).
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct SampleCliInputs {
    pub files: Vec<String>,
    pub reads: Vec<String>,
    pub first: Vec<String>,
    pub second: Vec<String>,
    pub interleaved: Vec<String>,
    pub specs: Vec<String>,
    pub sample_name: Option<String>,
}

/// Builds the ordered list of samples from CLI inputs, checks duplicate sample
/// names, and applies `--sample-name` when present.
pub fn build_sample_inputs(inputs: &SampleCliInputs) -> Result<Vec<SampleInput>, InputError> {
    let mut samples = Vec::new();
    for path in &inputs.files {
        samples.push(SampleInput::single(
            sample_name_from_path(path, false),
            vec![path.clone()],
        )?);
    }
    for path in &inputs.reads {
        samples.push(SampleInput::single(
            sample_name_from_path(path, false),
            vec![path.clone()],
        )?);
    }
    for (r1, r2) in resolve_pair_paths(&inputs.first, &inputs.second)? {
        let name = sample_name_from_path(&r1, true);
        samples.push(SampleInput::paired(name, vec![(r1, r2)])?);
    }
    for path in &inputs.interleaved {
        samples.push(SampleInput::interleaved(
            sample_name_from_path(path, true),
            vec![path.clone()],
        )?);
    }
    for spec in &inputs.specs {
        samples.push(parse_multi_file_spec(spec, None)?);
    }
    if samples.is_empty() {
        return Err(InputError("no input samples given".into()));
    }

    let mut counts: HashMap<&str, usize> = HashMap::new();
    for sample in &samples {
        *counts.entry(&sample.name).or_default() += 1;
    }
    let mut duplicates: Vec<&str> = counts
        .into_iter()
        .filter_map(|(name, count)| (count > 1).then_some(name))
        .collect();
    if !duplicates.is_empty() {
        duplicates.sort_unstable();
        return Err(InputError(format!(
            "duplicate sample name(s): {} (use --sample NAME:FILES or --sample-name)",
            duplicates.join(", ")
        )));
    }

    if let Some(override_name) = &inputs.sample_name {
        if samples.len() != 1 {
            return Err(InputError(
                "--sample-name requires exactly one input sample".into(),
            ));
        }
        let sample = samples.pop().unwrap().with_name(override_name.clone())?;
        samples.push(sample);
    }

    Ok(samples)
}

/// Facts about a sketched assembly that a `.sylsp` sketch does not carry.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct AssemblyStats {
    pub records: u64,
    pub length: u64,
    pub gc_count: u64,
    /// GC percentage rounded to 2 decimal places.
    pub gc: f64,
}

/// Streaming single-assembly sketcher (FASTA/FASTQ -> `.sylsp` + [`AssemblyStats`]).
///
/// Rejects empty sequences and
/// inputs with zero sequences.
pub struct AssemblySketcher {
    builder: SketchBuilder,
    parser: RecordParser,
    label: String,
}

impl AssemblySketcher {
    pub fn new(label: impl Into<String>, c: usize, k: usize) -> Result<Self, InputError> {
        Ok(Self {
            builder: SketchBuilder::new(c, k, true, default_fpr(), Seeding::Avx2Compat)?,
            parser: RecordParser::new(),
            label: label.into(),
        })
    }

    pub fn push(&mut self, data: &[u8]) -> Result<(), InputError> {
        let mut parser = std::mem::take(&mut self.parser);
        let mut error: Option<InputError> = None;
        let result = parser.feed(data, &mut |seq, _header| {
            if error.is_none() {
                if seq.is_empty() {
                    error = Some(InputError(format!(
                        "{}: sequence #{} is empty",
                        self.label,
                        self.builder.reads + 1
                    )));
                } else {
                    self.builder.add_single(seq);
                }
            }
        });
        self.parser = parser;
        result.map_err(|e| InputError(format!("cannot sketch {}: {e}", self.label)))?;
        if let Some(err) = error {
            return Err(err);
        }
        Ok(())
    }

    pub fn finish(mut self, sample_name: &str) -> Result<(Vec<u8>, AssemblyStats), InputError> {
        let mut parser = std::mem::take(&mut self.parser);
        let mut error: Option<InputError> = None;
        let result = parser.finish(&mut |seq, _header| {
            if error.is_none() {
                if seq.is_empty() {
                    error = Some(InputError(format!(
                        "{}: sequence #{} is empty",
                        self.label,
                        self.builder.reads + 1
                    )));
                } else {
                    self.builder.add_single(seq);
                }
            }
        });
        self.parser = parser;
        result.map_err(|e| InputError(format!("cannot sketch {}: {e}", self.label)))?;
        if let Some(err) = error {
            return Err(err);
        }
        if self.builder.reads == 0 || self.builder.bases == 0 {
            return Err(InputError(format!("{} contains no sequences", self.label)));
        }
        let stats = AssemblyStats {
            records: self.builder.reads,
            length: self.builder.bases,
            gc_count: self.builder.gc,
            gc: gc_percent(self.builder.gc, self.builder.bases),
        };
        let bytes = self.builder.serialize(sample_name, false);
        Ok((bytes, stats))
    }
}

/// Count and total bases of contigs skipped client-side.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Skipped {
    pub contigs: u64,
    pub bp: u64,
}

impl Skipped {
    pub fn add(&mut self, bp: u64) {
        self.contigs += 1;
        self.bp += bp;
    }
}

/// Client-side per-contig counters accumulated while streaming an assembly.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct SkipCounters {
    pub seen_contigs: u64,
    pub seen_bp: u64,
    /// Records shorter than `min_length` (or empty), never sketched or sent.
    pub skipped_short: Skipped,
    /// Sketched records with `< min_number_kmers` sample k-mers or `0`
    /// genome-style k-mers (e.g. low-complexity / tandem repeats), never sent.
    pub skipped_few_kmers: Skipped,
}

/// One contig ready to be batched and sent to `POST /contigs`.
#[derive(Debug, Clone, PartialEq)]
pub struct PreparedContig {
    pub index: u64,
    pub name: String,
    pub length: u64,
    pub gc: f64,
    pub sketch_kmers: u64,
    pub contig_kmers: u64,
    pub sketch: Vec<u8>,
}

/// Applies client-side per-contig name validation, deduplication, and
/// short / too-few-k-mer filtering with skip counters.
#[derive(Debug, Clone)]
pub struct ContigFilter {
    min_number_kmers: u64,
    names: ContigNameTracker,
    counters: SkipCounters,
}

impl Default for ContigFilter {
    fn default() -> Self {
        Self::new(DEFAULT_MIN_NUMBER_KMERS)
    }
}

impl ContigFilter {
    pub fn new(min_number_kmers: u64) -> Self {
        Self {
            min_number_kmers,
            names: ContigNameTracker::new(),
            counters: SkipCounters::default(),
        }
    }

    /// Processes one streamed [`ContigRecord`].
    ///
    /// Returns `Ok(None)` when the record was skipped (too short or too few
    /// k-mers), `Ok(Some(contig))` when it should be sent to `/contigs`, or
    /// `Err` if its header name is invalid (control characters or > 1024 chars).
    pub fn process(&mut self, record: ContigRecord) -> Result<Option<PreparedContig>, InputError> {
        self.counters.seen_contigs += 1;
        self.counters.seen_bp += record.length;

        let raw_trimmed = record.name.trim();
        let validated_raw = if raw_trimmed.is_empty() {
            format!("contig-{}", record.index)
        } else {
            record.name.clone()
        };
        check_sample_name(&validated_raw)
            .map_err(|e| InputError(format!("record #{}: {e}", record.index)))?;

        let sketch_kmers = record.sketch_kmers().unwrap_or(0);
        let (Some(sketch), Some(contig_kmers)) = (record.sketch, record.contig_kmers) else {
            self.counters.skipped_short.add(record.length);
            return Ok(None);
        };

        let name = self.names.next_name(
            if raw_trimmed.is_empty() {
                ""
            } else {
                &record.name
            },
            record.index,
        );

        if sketch_kmers < self.min_number_kmers || contig_kmers == 0 {
            self.counters.skipped_few_kmers.add(record.length);
            return Ok(None);
        }

        let gc = gc_percent(record.gc_count, record.length);
        Ok(Some(PreparedContig {
            index: record.index,
            name,
            length: record.length,
            gc,
            sketch_kmers,
            contig_kmers,
            sketch,
        }))
    }

    pub fn into_counters(self) -> SkipCounters {
        self.counters
    }
}

// ---------------------------------------------------------------------------
// Native I/O helpers (behind the `io` feature)
// ---------------------------------------------------------------------------

/// A ready-to-upload sample sketch produced locally or loaded from a `.sylsp` file.
#[cfg(feature = "io")]
#[derive(Debug, Clone, PartialEq)]
pub struct SketchedSample {
    pub name: String,
    pub bytes: Vec<u8>,
    pub header: SketchHeader,
}

/// Loads a pre-made `.sylsp` sketch file from disk and validates its header.
#[cfg(feature = "io")]
pub fn load_sketch(path: impl AsRef<Path>) -> Result<(Vec<u8>, SketchHeader), InputError> {
    let path = path.as_ref();
    let bytes =
        fs::read(path).map_err(|e| InputError(format!("cannot load sketch {}: {e}", path.display())))?;
    let header = parse_sketch_header(&bytes)
        .map_err(|e| InputError(format!("cannot load sketch {}: {e}", path.display())))?;
    Ok((bytes, header))
}

/// Sketches one [`SampleInput`] locally (or loads & validates a pre-made `.sylsp`).
#[cfg(feature = "io")]
pub fn sketch_sample(
    sample: &SampleInput,
    c: usize,
    k: usize,
) -> Result<SketchedSample, InputError> {
    if sample.is_sketch() {
        let path = sample.paths()[0];
        let (bytes, header) = load_sketch(path)?;
        check_sketch_compatibility(&header, c as u64, k as u64, Some(&sample.name))?;
        return Ok(SketchedSample {
            name: sample.name.clone(),
            bytes,
            header,
        });
    }

    let mut sketcher = StreamSketcher::new(c, k, true)
        .map_err(|e| InputError(format!("cannot sketch sample {:?}: {e}", sample.name)))?;
    let res = match &sample.files {
        SampleFiles::Single(paths) => paths.iter().try_for_each(|p| sketcher.push_file(p)),
        SampleFiles::Paired(pairs) => pairs
            .iter()
            .try_for_each(|(r1, r2)| sketcher.push_paired_files(r1, r2)),
        SampleFiles::Interleaved(paths) => paths
            .iter()
            .try_for_each(|p| sketcher.push_interleaved_file(p)),
    };
    res.map_err(|e| InputError(format!("cannot sketch sample {:?}: {e}", sample.name)))?;
    let bytes = sketcher
        .finish(&sample.name)
        .map_err(|e| InputError(format!("cannot sketch sample {:?}: {e}", sample.name)))?;
    let header = parse_sketch_header(&bytes)
        .map_err(|e| InputError(format!("internal sketch format error: {e}")))?;
    Ok(SketchedSample {
        name: sample.name.clone(),
        bytes,
        header,
    })
}

/// Sketches all samples sequentially in input order.
#[cfg(feature = "io")]
pub fn sketch_samples(
    samples: &[SampleInput],
    c: usize,
    k: usize,
) -> Result<Vec<SketchedSample>, InputError> {
    samples.iter().map(|s| sketch_sample(s, c, k)).collect()
}

/// Sketches multiple samples in parallel using `rayon`, preserving input order.
///
/// When `threads` is `Some(n)` (`n >= 1`), uses a dedicated thread pool of size
/// `n.min(samples.len().max(1))`.
#[cfg(feature = "rayon")]
pub fn sketch_samples_parallel(
    samples: &[SampleInput],
    c: usize,
    k: usize,
    threads: Option<usize>,
) -> Result<Vec<SketchedSample>, InputError> {
    use rayon::prelude::*;

    if samples.len() <= 1 || threads == Some(1) {
        return sketch_samples(samples, c, k);
    }
    let work = || {
        samples
            .par_iter()
            .map(|sample| sketch_sample(sample, c, k))
            .collect::<Result<Vec<_>, _>>()
    };
    match threads {
        Some(n) if n > 1 => {
            let pool = rayon::ThreadPoolBuilder::new()
                .num_threads(n.min(samples.len()))
                .build()
                .map_err(|e| InputError(format!("cannot create thread pool: {e}")))?;
            pool.install(work)
        }
        _ => work(),
    }
}

/// Sketches one assembly from any `Read` stream (plain or gzip), returning the
/// `.sylsp` bytes and [`AssemblyStats`].
#[cfg(feature = "io")]
pub fn sketch_assembly_reader<R: Read>(
    reader: R,
    label: &str,
    name: &str,
    c: usize,
    k: usize,
) -> Result<(Vec<u8>, AssemblyStats), InputError> {
    let mut sketcher = AssemblySketcher::new(label, c, k)?;
    for_each_chunk(reader, |chunk| sketcher.push(chunk)).map_err(|e: InputError| {
        InputError(format!("cannot sketch {label}: {e}"))
    })?;
    sketcher.finish(name)
}

/// Opens `path` for reading, with the path in the error.
#[cfg(feature = "io")]
fn open_buffered(path: &Path, label: &str) -> Result<std::io::BufReader<fs::File>, InputError> {
    fs::File::open(path)
        .map(|file| std::io::BufReader::with_capacity(128 * 1024, file))
        .map_err(|e| InputError(format!("cannot sketch {label}: {e}")))
}

/// Sketches one assembly file (FASTA/FASTQ, plain or gzip), returning the
/// `.sylsp` bytes and [`AssemblyStats`].
#[cfg(feature = "io")]
pub fn sketch_assembly_file(
    path: impl AsRef<Path>,
    name: &str,
    c: usize,
    k: usize,
) -> Result<(Vec<u8>, AssemblyStats), InputError> {
    let path = path.as_ref();
    let label = path.display().to_string();
    sketch_assembly_reader(open_buffered(path, &label)?, &label, name, c, k)
}

/// Why [`sketch_contigs_reader`] stopped: reading or sketching failed
/// (`Sketch`, reported with the input's label) or the caller's `on_contig` did.
#[cfg(feature = "io")]
enum Stop<E> {
    Sketch(String),
    Caller(E),
}

#[cfg(feature = "io")]
impl<E> From<String> for Stop<E> {
    fn from(message: String) -> Self {
        Self::Sketch(message)
    }
}

/// Streams a FASTA/FASTQ source record by record for `--per-contig`, applying
/// [`ContigFilter`] and invoking `on_contig` for each sendable contig.
///
/// Unlike the browser's `WasmContigSketcher`, imposes no record-count or
/// record-length cap. Fails with an [`InputError`] if the input has no records.
#[cfg(feature = "io")]
pub fn sketch_contigs_reader<R: Read, E: From<InputError>>(
    reader: R,
    label: &str,
    c: usize,
    k: usize,
    min_length: u64,
    min_number_kmers: u64,
    mut on_contig: impl FnMut(PreparedContig) -> Result<(), E>,
) -> Result<SkipCounters, E> {
    let sketch_error = |e: String| E::from(InputError(format!("cannot sketch {label}: {e}")));
    let mut sketcher = ContigSketcher::with_limits(
        c,
        k,
        min_length,
        DEFAULT_MIN_SPACING,
        Seeding::Avx2Compat,
        0,
        0,
    )
    .map_err(sketch_error)?;
    let mut filter = ContigFilter::new(min_number_kmers);
    let mut drain = |sketcher: &mut ContigSketcher| -> Result<(), Stop<E>> {
        while let Some(record) = sketcher.take_next() {
            let prepared = filter.process(record).map_err(|e| Stop::Caller(E::from(e)))?;
            if let Some(prepared) = prepared {
                on_contig(prepared).map_err(Stop::Caller)?;
            }
        }
        Ok(())
    };
    for_each_chunk(reader, |chunk| {
        sketcher.push(chunk)?;
        drain(&mut sketcher)
    })
    .map_err(|stop| match stop {
        Stop::Sketch(message) => sketch_error(message),
        Stop::Caller(e) => e,
    })?;
    sketcher.end_file().map_err(sketch_error)?;
    drain(&mut sketcher).map_err(|stop| match stop {
        Stop::Sketch(message) => sketch_error(message),
        Stop::Caller(e) => e,
    })?;
    let counters = filter.into_counters();
    if counters.seen_contigs == 0 {
        return Err(E::from(InputError(format!("{label} contains no sequences"))));
    }
    Ok(counters)
}

/// Streams a FASTA/FASTQ file (plain or gzip) for `--per-contig`.
#[cfg(feature = "io")]
pub fn sketch_contigs_file<E: From<InputError>>(
    path: impl AsRef<Path>,
    c: usize,
    k: usize,
    min_length: u64,
    min_number_kmers: u64,
    on_contig: impl FnMut(PreparedContig) -> Result<(), E>,
) -> Result<SkipCounters, E> {
    let path = path.as_ref();
    let label = path.display().to_string();
    sketch_contigs_reader(
        open_buffered(path, &label)?,
        &label,
        c,
        k,
        min_length,
        min_number_kmers,
        on_contig,
    )
}
