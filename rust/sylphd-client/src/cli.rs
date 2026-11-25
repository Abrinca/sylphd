//! `clap` command line of `sylphd-client`, mirroring `src/sylphd/cli_parser.py`
//! for `info`, `profile`, `query` and `assembly`, plus the offline `render`.

use clap::{Args, Parser, Subcommand, ValueEnum};

use crate::address::AddressArgs;

/// Share one in-memory sylph database between runs: clients sketch samples
/// locally and ask the server for ANI hits or taxonomies.
#[derive(Debug, Parser)]
#[command(
    name = "sylphd-client",
    version,
    about = "Share one in-memory sylph database between runs: clients sketch samples \
             locally and ask the server for ANI hits or taxonomies.",
    propagate_version = true
)]
pub struct Cli {
    #[command(subcommand)]
    pub command: Command,
}

#[derive(Debug, Subcommand)]
pub enum Command {
    /// Print server and database info
    Info(InfoArgs),
    /// Taxonomic profile of one or more samples
    Profile(ProfileArgs),
    /// ANI hits of one or more samples
    Query(QueryArgs),
    /// Find the closest database genomes to one assembly
    Assembly(AssemblyArgs),
    /// Render a saved result document as TSV (offline)
    Render(RenderArgs),
}

/// `--socket`, `--tcp` and `--url` (mutually exclusive).
#[derive(Debug, Args, Default)]
pub struct AddressCli {
    /// Unix socket of the server [default: $SYLPHD_SOCKET or $XDG_RUNTIME_DIR/sylphd.sock]
    #[arg(long, value_name = "PATH", conflicts_with_all = ["tcp", "url"])]
    pub socket: Option<String>,

    /// server address [default: $SYLPHD_URL]
    #[arg(long, value_name = "HOST:PORT", conflicts_with_all = ["socket", "url"])]
    pub tcp: Option<String>,

    /// server base URL, http:// or https://, optionally with a path prefix
    /// (e.g. https://api.example.org/sylphd) [default: $SYLPHD_URL]
    #[arg(long, value_name = "URL", conflicts_with_all = ["socket", "tcp"])]
    pub url: Option<String>,
}

impl AddressCli {
    pub fn args(&self) -> AddressArgs {
        AddressArgs {
            url: self.url.clone(),
            tcp: self.tcp.clone(),
            socket: self.socket.clone(),
        }
    }
}

/// Transport options shared by every server command.
#[derive(Debug, Args, Default)]
pub struct ClientCli {
    #[command(flatten)]
    pub address: AddressCli,

    /// file holding a privileged bearer token, sent as Authorization
    /// [default: $SYLPHD_TOKEN, the token itself]
    #[arg(long, value_name = "PATH")]
    pub token_file: Option<String>,

    /// read timeout in seconds [default: no timeout]
    #[arg(long, value_name = "SECONDS")]
    pub timeout: Option<f64>,

    /// retry up to N times when the server answers 429 or 503, waiting
    /// for its Retry-After (at most 30 s)
    #[arg(long, value_name = "N", default_value_t = 0)]
    pub retries: i64,
}

#[derive(Debug, Args)]
pub struct InfoArgs {
    #[command(flatten)]
    pub client: ClientCli,

    /// print the raw JSON
    #[arg(long)]
    pub json: bool,
}

#[derive(Debug, Args)]
pub struct SampleCli {
    /// one sample per FASTA/FASTQ file, or a .sylsp sketch
    #[arg(value_name = "FILE")]
    pub files: Vec<String>,

    /// single-end reads (or assemblies); each file is one sample
    #[arg(short = 'r', long, value_name = "FILE", num_args = 1.., action = clap::ArgAction::Append)]
    pub reads: Vec<String>,

    /// first mates of paired-end files
    #[arg(short = '1', long, value_name = "FILE", num_args = 1.., action = clap::ArgAction::Append)]
    pub first: Vec<String>,

    /// second mates of paired-end files (same count as -1)
    #[arg(short = '2', long, value_name = "FILE", num_args = 1.., action = clap::ArgAction::Append)]
    pub second: Vec<String>,

    /// interleaved paired-end FASTQ files
    #[arg(long, value_name = "FILE", num_args = 1.., action = clap::ArgAction::Append)]
    pub interleaved: Vec<String>,

    /// explicit sample; SPEC is a comma-separated list of files, a pair being
    /// written as 'R1+R2' (e.g. 's1:a.fq,b.fq' or 's1:a1.fq+a2.fq,b1.fq+b2.fq')
    #[arg(long = "sample", value_name = "NAME:SPEC", action = clap::ArgAction::Append)]
    pub sample: Vec<String>,

    /// sample name to use instead of the file name (single sample only)
    #[arg(long, value_name = "NAME")]
    pub sample_name: Option<String>,
}

impl SampleCli {
    pub fn inputs(&self) -> sylphd_core::input::SampleCliInputs {
        sylphd_core::input::SampleCliInputs {
            files: self.files.clone(),
            reads: self.reads.clone(),
            first: self.first.clone(),
            second: self.second.clone(),
            interleaved: self.interleaved.clone(),
            specs: self.sample.clone(),
            sample_name: self.sample_name.clone(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
pub enum Format {
    Tsv,
    Json,
}

#[derive(Debug, Args)]
pub struct OutputCli {
    /// write the table here [default: stdout]
    #[arg(short = 'o', long, value_name = "FILE")]
    pub output: Option<String>,

    /// output format
    #[arg(long, value_enum, default_value_t = Format::Tsv)]
    pub format: Format,

    /// print JSON (the server document, unchanged); same as --format json
    #[arg(long)]
    pub json: bool,

    /// metadata columns to add, comma-separated, or 'all'/'none'
    #[arg(long, value_name = "COLUMNS", default_value = "all")]
    pub fields: String,
}

impl OutputCli {
    pub fn wants_json(&self) -> bool {
        self.json || matches!(self.format, Format::Json)
    }
}

#[derive(Debug, Args)]
pub struct SylphCli {
    /// threads used to sketch samples locally
    #[arg(short = 't', long, value_name = "N", default_value_t = 4, value_parser = parse_positive_usize)]
    pub threads: usize,

    /// minimum adjusted ANI, 0-100 [default: 90 query / 95 profile]
    #[arg(long, value_name = "ANI", value_parser = parse_fraction)]
    pub minimum_ani: Option<f64>,

    /// read sequence identity in percent, only used with --estimate-unknown
    #[arg(long, value_name = "PCT", value_parser = parse_fraction)]
    pub seq_id: Option<f64>,

    /// estimate true coverage and scale sequence abundance of unknowns
    #[arg(long)]
    pub estimate_unknown: bool,

    /// discard genomes with fewer sampled k-mers
    #[arg(long, value_name = "N", default_value_t = 10, value_parser = parse_non_negative_u64)]
    pub min_number_kmers: u64,

    /// minimum contained k-mers for a hit
    #[arg(long, value_name = "N", default_value_t = 7, value_parser = parse_non_negative_u64)]
    pub min_contain: u64,

    /// two-stage screen threshold, 0-100
    #[arg(long, value_name = "ANI", default_value_t = 85.0, value_parser = parse_fraction)]
    pub screen_ani: f64,

    /// report the naive containment ANI instead of the adjusted ANI; use
    /// for assemblies/genomes as samples
    #[arg(long)]
    pub no_adjust: bool,
}

#[derive(Debug, Args)]
pub struct QueryArgs {
    #[command(flatten)]
    pub sample: SampleCli,
    #[command(flatten)]
    pub output: OutputCli,
    #[command(flatten)]
    pub sylph: SylphCli,
    /// progress messages on stderr
    #[arg(short = 'v', long)]
    pub verbose: bool,
    #[command(flatten)]
    pub client: ClientCli,
}

#[derive(Debug, Args)]
pub struct ProfileTaxCli {
    /// very roughly estimate the number of reads per genome in the
    /// 'Sequence_abundance' column instead of a relative abundance
    /// (experimental in sylph); implies --estimate-unknown
    #[arg(long)]
    pub estimate_read_counts: bool,

    /// taxonomies to aggregate, comma-separated from gtdb, ncbi, tax, or
    /// tax:SOURCE for one taxonomy source [default: gtdb, ncbi, plus tax when
    /// the server has taxonomy sources]
    #[arg(long, value_name = "LIST")]
    pub taxonomy: Option<String>,

    /// with --taxonomy tax: restrict the merged profile to these taxonomy
    /// sources of the server's metadata file (repeatable; default: all)
    #[arg(long = "tax-source", value_name = "NAME", action = clap::ArgAction::Append)]
    pub tax_source: Vec<String>,

    /// write one sylph-tax-style .sylphmpa per sample and taxonomy as
    /// 'PREFIX<sample>.<taxonomy>.sylphmpa'
    #[arg(long, value_name = "PREFIX")]
    pub taxprof: Option<String>,
}

#[derive(Debug, Args)]
pub struct ProfileArgs {
    #[command(flatten)]
    pub sample: SampleCli,
    #[command(flatten)]
    pub output: OutputCli,
    #[command(flatten)]
    pub sylph: SylphCli,
    #[command(flatten)]
    pub tax: ProfileTaxCli,
    /// progress messages on stderr
    #[arg(short = 'v', long)]
    pub verbose: bool,
    #[command(flatten)]
    pub client: ClientCli,
}

#[derive(Debug, Args)]
pub struct AssemblyArgs {
    /// assembly in FASTA or FASTQ, plain or gzip
    #[arg(value_name = "FILE")]
    pub file: String,

    /// number of hits to print [default: 10, or 1 with --per-contig]
    #[arg(long, value_name = "N", value_parser = parse_top)]
    pub top: Option<u8>,

    /// minimum ANI in percent, 70-100
    #[arg(long, value_name = "ANI", default_value_t = 80.0, value_parser = parse_assembly_ani)]
    pub min_ani: f64,

    /// sketch every FASTA/FASTQ record as its own sample and report the
    /// closest genomes and a taxonomic consensus of each contig
    /// (contamination checks); always sketches locally
    #[arg(long)]
    pub per_contig: bool,

    /// with --per-contig: contigs shorter than this are not sketched or sent,
    /// produce no row and are counted in the summary [default: 20000]
    #[arg(long, value_name = "BP", value_parser = parse_non_negative_u64)]
    pub min_contig_length: Option<u64>,

    /// with --per-contig: a species joins the consensus when its divergence
    /// is at most R times the best hit's [default: 1.5]
    #[arg(long, value_name = "R", value_parser = parse_consensus_ratio)]
    pub consensus_ratio: Option<f64>,

    /// with --per-contig: ... plus Z standard deviations of the ANI sampling
    /// noise [default: 2]
    #[arg(long, value_name = "Z", value_parser = parse_non_negative_float)]
    pub consensus_z: Option<f64>,

    /// query name [default: from the file name]
    #[arg(long, value_name = "NAME")]
    pub name: Option<String>,

    /// sketch the assembly on this machine and upload only the sketch, so the
    /// sequence never leaves it and has no size limit [default].
    /// --no-local-sketch uploads the raw sequence instead, which only servers
    /// started with --allow-sequences accept; it cannot be combined with
    /// --per-contig
    #[arg(long = "local-sketch", action = clap::ArgAction::SetTrue)]
    pub local_sketch: bool,

    /// upload the raw sequence instead of a local sketch; requires a server
    /// started with --allow-sequences
    #[arg(long = "no-local-sketch", action = clap::ArgAction::SetTrue)]
    pub no_local_sketch: bool,

    /// with --per-contig: metadata columns to add to every hit ('all', 'none'
    /// or a comma-separated list) [default: all]
    #[arg(long, value_name = "COLS")]
    pub fields: Option<String>,

    /// print JSON (the raw /assembly response, or the per-contig document)
    #[arg(long)]
    pub json: bool,

    /// write the output here [default: stdout]
    #[arg(short = 'o', long, value_name = "FILE")]
    pub output: Option<String>,

    /// do not print the summary on stderr
    #[arg(short = 'q', long)]
    pub quiet: bool,

    #[command(flatten)]
    pub client: ClientCli,
}

impl AssemblyArgs {
    /// `--local-sketch` is the default; `--no-local-sketch` turns it off.
    pub fn local(&self) -> bool {
        !self.no_local_sketch
    }
}

#[derive(Debug, Args)]
pub struct RenderArgs {
    /// a saved /profile, /query, /assembly or /contigs JSON document ('-' = stdin)
    #[arg(value_name = "FILE")]
    pub file: String,

    /// write the table here [default: stdout]
    #[arg(short = 'o', long, value_name = "FILE")]
    pub output: Option<String>,

    /// do not print the summary or caveats on stderr
    #[arg(short = 'q', long)]
    pub quiet: bool,
}

// -- value parsers (argparse type= equivalents) -----------------------------

fn parse_positive_usize(text: &str) -> Result<usize, String> {
    let value: usize = text.parse().map_err(|_| "invalid number".to_string())?;
    if value < 1 {
        return Err("must be >= 1".into());
    }
    Ok(value)
}

fn parse_non_negative_u64(text: &str) -> Result<u64, String> {
    let value: i128 = text.parse().map_err(|_| "invalid number".to_string())?;
    if value < 0 {
        return Err("must be >= 0".into());
    }
    u64::try_from(value).map_err(|_| "number too large".to_string())
}

fn parse_fraction(text: &str) -> Result<f64, String> {
    let value: f64 = text.parse().map_err(|_| "invalid number".to_string())?;
    if !(0.0..=100.0).contains(&value) {
        return Err("must be between 0 and 100".into());
    }
    Ok(value)
}

fn parse_assembly_ani(text: &str) -> Result<f64, String> {
    let value: f64 = text.parse().map_err(|_| "invalid number".to_string())?;
    if !(70.0..=100.0).contains(&value) {
        return Err("must be between 70 and 100".into());
    }
    Ok(value)
}

fn parse_top(text: &str) -> Result<u8, String> {
    let value: i64 = text.parse().map_err(|_| "invalid number".to_string())?;
    if !(1..=50).contains(&value) {
        return Err("must be between 1 and 50".into());
    }
    Ok(value as u8)
}

fn parse_consensus_ratio(text: &str) -> Result<f64, String> {
    let value: f64 = text.parse().map_err(|_| "invalid number".to_string())?;
    if !value.is_finite() || value < 1.0 {
        return Err("must be >= 1".into());
    }
    Ok(value)
}

fn parse_non_negative_float(text: &str) -> Result<f64, String> {
    let value: f64 = text.parse().map_err(|_| "invalid number".to_string())?;
    if !value.is_finite() || value < 0.0 {
        return Err("must be >= 0".into());
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn profile_defaults_match_python() {
        let cli = Cli::try_parse_from(["sylphd-client", "profile", "reads.fq.gz"]).unwrap();
        let Command::Profile(args) = cli.command else {
            panic!("expected profile");
        };
        assert_eq!(args.sample.files, ["reads.fq.gz"]);
        assert_eq!(args.sylph.threads, 4);
        assert_eq!(args.sylph.minimum_ani, None);
        assert_eq!(args.sylph.min_number_kmers, 10);
        assert_eq!(args.sylph.min_contain, 7);
        assert_eq!(args.sylph.screen_ani, 85.0);
        assert_eq!(args.output.fields, "all");
        assert_eq!(args.output.format, Format::Tsv);
        assert!(!args.output.wants_json());
        assert_eq!(args.client.retries, 0);
    }

    #[test]
    fn address_flags_are_mutually_exclusive() {
        let err = Cli::try_parse_from([
            "sylphd-client",
            "info",
            "--socket",
            "/tmp/x.sock",
            "--url",
            "http://example.org",
        ])
        .unwrap_err();
        assert_eq!(err.exit_code(), 2);
    }

    #[test]
    fn assembly_parses_per_contig_options() {
        let cli = Cli::try_parse_from([
            "sylphd-client",
            "assembly",
            "mag.fasta",
            "--per-contig",
            "--top",
            "3",
            "--min-ani",
            "90",
            "--min-contig-length",
            "5000",
            "--fields",
            "none",
            "--json",
            "--no-local-sketch",
        ])
        .unwrap();
        let Command::Assembly(args) = cli.command else {
            panic!("expected assembly");
        };
        assert!(args.per_contig);
        assert_eq!(args.top, Some(3));
        assert_eq!(args.min_ani, 90.0);
        assert_eq!(args.min_contig_length, Some(5000));
        assert_eq!(args.fields.as_deref(), Some("none"));
        assert!(args.json);
        assert!(!args.local());
    }

    #[test]
    fn invalid_ranges_are_usage_errors() {
        for args in [
            vec!["sylphd-client", "profile", "--minimum-ani", "101", "x.fq"],
            vec!["sylphd-client", "assembly", "x.fa", "--top", "0"],
            vec!["sylphd-client", "assembly", "x.fa", "--min-ani", "69"],
            vec![
                "sylphd-client",
                "assembly",
                "x.fa",
                "--per-contig",
                "--consensus-ratio",
                "0.5",
            ],
        ] {
            let err = Cli::try_parse_from(args).unwrap_err();
            assert_eq!(err.exit_code(), 2);
        }
    }

    #[test]
    fn version_is_the_workspace_version() {
        let err = Cli::try_parse_from(["sylphd-client", "--version"]).unwrap_err();
        let text = err.to_string();
        assert!(text.contains(env!("CARGO_PKG_VERSION")), "{text}");
    }
}
