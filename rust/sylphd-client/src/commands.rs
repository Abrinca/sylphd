//! The `info`, `profile`, `query`, `assembly` and `render` commands.
//!
//! All rendering and document merging come from `sylphd-core`; the server
//! validates everything that depends on its own database.

use std::io::Write;
use std::path::Path;

use bytes::Bytes;
use serde_json::{json, Value};
use sylphd_core::batch::{
    BatchItem, BatchLimits, ContigBatchPlanner, ContigSpec, ContigsRequestOptions,
    DEFAULT_ASSEMBLY_TOP, DEFAULT_CONSENSUS_RATIO, DEFAULT_CONSENSUS_Z, DEFAULT_TOP,
};
use sylphd_core::document::{
    self, merge_contigs_run, AssemblyDocument, ContigsDocument, ContigsRunMergeOptions,
    DocumentError, InfoDocument, MetadataInfo, ProfileDocument, ResultDocument,
};
use sylphd_core::input::{
    build_sample_inputs, sample_name_from_path, sketch_assembly_file, sketch_contigs_file,
    sketch_samples_parallel, InputError, SketchedSample, DEFAULT_MIN_CONTIG_LENGTH,
    DEFAULT_MIN_NUMBER_KMERS,
};
use sylphd_core::render::{
    format_size, render_assembly_stderr, render_assembly_tsv, render_contigs_summary,
    render_contigs_tsv, render_info, render_profile_caveats, render_tsv,
    render_stderr_messages, render_usage_note,
};

use crate::address::{Env, ProcessEnv};
use crate::cli::{
    AssemblyArgs, Cli, ClientCli, Command, InfoArgs, ProfileArgs, QueryArgs, RenderArgs,
};
use crate::transport::{Client, ClientError, SKETCH_CONTENT_TYPE};
use crate::PROG;

/// A command failure: the exit code and the `sylphd: error:` message.
#[derive(Debug)]
pub struct Failure {
    pub code: i32,
    pub message: String,
}

impl Failure {
    pub fn usage(message: impl Into<String>) -> Self {
        Self {
            code: 2,
            message: message.into(),
        }
    }

    pub fn client(message: impl Into<String>) -> Self {
        Self {
            code: 1,
            message: message.into(),
        }
    }
}

impl From<InputError> for Failure {
    fn from(error: InputError) -> Self {
        Self::client(error.to_string())
    }
}

impl From<ClientError> for Failure {
    fn from(error: ClientError) -> Self {
        Self::client(error.0)
    }
}

type Outcome = Result<(), Failure>;

/// Runs the parsed command and returns the process exit code.
pub fn run(cli: Cli) -> i32 {
    let outcome = match cli.command {
        Command::Info(args) => cmd_info(args),
        Command::Profile(args) => cmd_profile(args),
        Command::Query(args) => cmd_query(args),
        Command::Assembly(args) => cmd_assembly(args),
        Command::Render(args) => cmd_render(args),
    };
    match outcome {
        Ok(()) => 0,
        Err(failure) => {
            eprintln!("{PROG}: error: {}", failure.message);
            failure.code
        }
    }
}

// ---------------------------------------------------------------------------
// shared plumbing
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq, Eq)]
enum Mode {
    Profile,
    Query,
}

impl Mode {
    fn endpoint(self) -> &'static str {
        match self {
            Self::Profile => "profile",
            Self::Query => "query",
        }
    }
}

fn connect(args: &ClientCli) -> Result<Client, Failure> {
    let token = resolve_token(args.token_file.as_deref(), &ProcessEnv)?;
    if let Some(timeout) = args.timeout {
        if !timeout.is_finite() || timeout < 0.0 {
            return Err(Failure::client("invalid --timeout (must be >= 0)"));
        }
    }
    Client::from_cli(
        &args.address.args(),
        &ProcessEnv,
        args.timeout,
        args.retries,
        token,
    )
    .map_err(Failure::from)
}

/// `--token-file`, else `$SYLPHD_TOKEN`.
fn resolve_token(path: Option<&str>, env: &dyn Env) -> Result<Option<String>, Failure> {
    if let Some(path) = path {
        let text = std::fs::read_to_string(path)
            .map_err(|error| Failure::client(format!("cannot read token file {path}: {error}")))?;
        let first = text
            .lines()
            .map(str::trim)
            .find(|line| !line.is_empty() && !line.starts_with('#'));
        return match first {
            Some(token) => Ok(Some(token.to_string())),
            None => Err(Failure::client(format!("token file {path} is empty"))),
        };
    }
    Ok(env
        .var("SYLPHD_TOKEN")
        .map(|token| token.trim().to_string())
        .filter(|token| !token.is_empty()))
}

fn note_messages(client: &Client, quiet: bool) {
    if quiet {
        return;
    }
    let messages = client.messages();
    let text = render_stderr_messages(&messages);
    if !text.is_empty() {
        eprint!("{text}");
    }
}

fn note_usage(info: &InfoDocument, base_url: Option<&str>, quiet: bool) {
    if quiet {
        return;
    }
    if let Some(note) = render_usage_note(&info.usage, base_url) {
        eprint!("{note}");
    }
}

fn sketch_parameters(info: &InfoDocument) -> Result<(usize, usize), Failure> {
    match (info.database.c, info.database.k) {
        (Some(c), Some(k)) => Ok((c as usize, k as usize)),
        _ => Err(Failure::client(
            "the server does not report its sketch parameters (c, k)",
        )),
    }
}

fn metadata_column_names(metadata: Option<&MetadataInfo>) -> Vec<String> {
    metadata
        .map(|meta| meta.columns.clone())
        .unwrap_or_default()
}

/// `--fields` -> the metadata columns to request.
fn resolve_fields(
    text: Option<&str>,
    metadata: Option<&MetadataInfo>,
) -> Result<Vec<String>, Failure> {
    let available = metadata_column_names(metadata);
    let raw = text.unwrap_or("all").trim();
    if raw.is_empty() || raw.eq_ignore_ascii_case("all") {
        return Ok(if metadata.is_some() {
            available
        } else {
            Vec::new()
        });
    }
    if raw.eq_ignore_ascii_case("none") || raw == "-" {
        return Ok(Vec::new());
    }
    if metadata.is_none() {
        return Err(Failure::client(
            "the server has no metadata database; --fields is not available",
        ));
    }
    let requested: Vec<String> = raw
        .split(',')
        .map(str::trim)
        .filter(|part| !part.is_empty())
        .map(str::to_string)
        .collect();
    let unknown: Vec<&str> = requested
        .iter()
        .filter(|name| !available.iter().any(|column| column == *name))
        .map(String::as_str)
        .collect();
    if !unknown.is_empty() {
        return Err(Failure::client(format!(
            "unknown metadata column(s): {} (available: {})",
            unknown.join(", "),
            available.join(", ")
        )));
    }
    let mut deduped = Vec::new();
    for name in requested {
        if !deduped.contains(&name) {
            deduped.push(name);
        }
    }
    Ok(deduped)
}

fn check_writable_directory(directory: &Path, original: &Path) -> Result<(), Failure> {
    if !directory.is_dir() {
        return Err(Failure::client(format!(
            "cannot write into {}: not a directory",
            original.display()
        )));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if let Ok(meta) = directory.metadata() {
            if meta.permissions().mode() & 0o222 == 0 {
                return Err(Failure::client(format!(
                    "cannot write into {}: permission denied",
                    original.display()
                )));
            }
        }
    }
    Ok(())
}

/// Fails on an unwritable `-o` before the expensive work.
fn check_output_file(path: Option<&str>) -> Result<(), Failure> {
    let Some(path) = path.filter(|p| !p.is_empty() && *p != "-") else {
        return Ok(());
    };
    let path = Path::new(path);
    if path.is_dir() {
        return Err(Failure::client(format!(
            "cannot write {}: is a directory",
            path.display()
        )));
    }
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    check_writable_directory(parent, parent)
}

/// Same for a `--taxprof` prefix.
fn check_output_prefix(prefix: &str) -> Result<(), Failure> {
    let path = Path::new(prefix);
    let (directory, original) = if path.is_dir() {
        (path.to_path_buf(), path.to_path_buf())
    } else {
        let probe = format!("{prefix}x");
        let probe = Path::new(&probe);
        let parent = probe
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new("."));
        (parent.to_path_buf(), parent.to_path_buf())
    };
    check_writable_directory(&directory, &original)
}

fn write_output(path: Option<&str>, text: &str) -> Result<(), Failure> {
    match path.filter(|p| !p.is_empty() && *p != "-") {
        None => {
            let mut stdout = std::io::stdout().lock();
            stdout
                .write_all(text.as_bytes())
                .map_err(|error| Failure::client(format!("cannot write to stdout: {error}")))?;
            stdout
                .flush()
                .map_err(|error| Failure::client(format!("cannot write to stdout: {error}")))?;
        }
        Some(path) => {
            let mut file = std::fs::File::create(path)
                .map_err(|error| Failure::client(format!("cannot write {path}: {error}")))?;
            file.write_all(text.as_bytes())
                .map_err(|error| Failure::client(format!("cannot write {path}: {error}")))?;
        }
    }
    Ok(())
}

fn with_newline(mut text: String) -> String {
    if !text.ends_with('\n') {
        text.push('\n');
    }
    text
}

// ---------------------------------------------------------------------------
// info
// ---------------------------------------------------------------------------

fn cmd_info(args: InfoArgs) -> Outcome {
    let mut client = connect(&args.client)?;
    let (raw, info) = client.get_info()?;
    let messages = client.messages();
    if args.json {
        let mut object: serde_json::Map<String, Value> = serde_json::from_str(&raw)
            .map_err(|_| Failure::client("unexpected /info response (not a JSON object)"))?;
        object.insert(
            "messages".into(),
            serde_json::to_value(&messages)
                .map_err(|e| Failure::client(format!("cannot encode messages: {e}")))?,
        );
        let text = serde_json::to_string_pretty(&object)
            .map_err(|e| Failure::client(format!("cannot encode /info: {e}")))?;
        return write_output(None, &with_newline(text));
    }
    write_output(None, &render_info(&info, &messages))
}

// ---------------------------------------------------------------------------
// profile / query
// ---------------------------------------------------------------------------

struct ProfileParamsOut {
    minimum_ani: Option<f64>,
    seq_id: Option<f64>,
    estimate_unknown: bool,
    estimate_read_counts: bool,
    min_number_kmers: u64,
    min_contain: u64,
    screen_ani: f64,
    no_adjust: bool,
}

fn cmd_profile(args: ProfileArgs) -> Outcome {
    let common = ContainArgs {
        sample: &args.sample,
        output: &args.output,
        sylph: &args.sylph,
        verbose: args.verbose,
        client: &args.client,
    };
    cmd_contain(&common, Mode::Profile, Some(&args.tax))
}

fn cmd_query(args: QueryArgs) -> Outcome {
    let common = ContainArgs {
        sample: &args.sample,
        output: &args.output,
        sylph: &args.sylph,
        verbose: args.verbose,
        client: &args.client,
    };
    cmd_contain(&common, Mode::Query, None)
}

/// The options `profile` and `query` share.
struct ContainArgs<'a> {
    sample: &'a crate::cli::SampleCli,
    output: &'a crate::cli::OutputCli,
    sylph: &'a crate::cli::SylphCli,
    verbose: bool,
    client: &'a ClientCli,
}

/// A `--taxonomy` list checked for syntax only (`gtdb`, `ncbi`, `tax` or
/// `tax:<source>`; the server checks them against its metadata), so a typo
/// fails before anything is sketched.
fn parse_taxonomy_list(text: &str) -> Result<Vec<String>, Failure> {
    let mut names: Vec<String> = Vec::new();
    for name in text.split(',').map(str::trim).filter(|name| !name.is_empty()) {
        let lower = name.to_ascii_lowercase();
        if let Some(source) = lower.strip_prefix("tax:") {
            if source.trim().is_empty() {
                return Err(Failure::client(format!(
                    "'{name}' names no taxonomy source (tax:<source>)"
                )));
            }
        } else if !matches!(lower.as_str(), "gtdb" | "ncbi" | "tax") {
            return Err(Failure::client(format!(
                "unknown taxonomy '{name}' (choose from: gtdb, ncbi, tax, or tax:<source>)"
            )));
        }
        names.push(name.to_string());
    }
    if names.is_empty() {
        return Err(Failure::client(
            "--taxonomy needs at least one of: gtdb, ncbi, tax, tax:<source>",
        ));
    }
    Ok(names)
}

fn default_taxonomies(metadata: Option<&MetadataInfo>) -> Vec<String> {
    let mut names = vec!["gtdb".to_string(), "ncbi".to_string()];
    if metadata.is_some_and(|meta| !meta.taxonomy_sources.is_empty()) {
        names.push("tax".into());
    }
    names
}

/// The server's upload limit in bytes (`None` = unlimited).
fn upload_limit(info: &InfoDocument) -> Option<u64> {
    info.limits
        .max_upload_mb
        .filter(|mb| *mb > 0)
        .map(|mb| mb * 1024 * 1024)
}

fn check_upload_size(info: &InfoDocument, size: u64, what: &str) -> Result<(), Failure> {
    match upload_limit(info) {
        Some(limit) if size > limit => Err(Failure::client(format!(
            "{what} {}, above the server's {} upload limit",
            format_size(size),
            format_size(limit)
        ))),
        _ => Ok(()),
    }
}

fn cmd_contain(args: &ContainArgs, mode: Mode, tax: Option<&crate::cli::ProfileTaxCli>) -> Outcome {
    let samples = build_sample_inputs(&args.sample.inputs())
        .map_err(|error| Failure::usage(error.to_string()))?;
    let (output, sylph) = (args.output, args.sylph);

    let params = ProfileParamsOut {
        minimum_ani: sylph.minimum_ani,
        seq_id: sylph.seq_id,
        estimate_unknown: sylph.estimate_unknown,
        estimate_read_counts: tax.is_some_and(|tax| tax.estimate_read_counts),
        min_number_kmers: sylph.min_number_kmers,
        min_contain: sylph.min_contain,
        screen_ani: sylph.screen_ani,
        no_adjust: sylph.no_adjust,
    };

    if tax.is_some_and(|tax| tax.taxprof.is_none() && !tax.tax_source.is_empty()) {
        return Err(Failure::client(
            "--tax-source needs --taxprof: it restricts the sources of the \
             aggregated 'tax' profile",
        ));
    }
    let tax = tax.filter(|tax| tax.taxprof.is_some());
    check_output_file(output.output.as_deref())?;
    let requested_taxonomies = match tax {
        Some(tax) => {
            check_output_prefix(tax.taxprof.as_deref().unwrap_or_default())?;
            tax.taxonomy.as_deref().map(parse_taxonomy_list).transpose()?
        }
        None => None,
    };

    let mut client = connect(args.client)?;
    let (_, info) = client.get_info()?;
    note_messages(&client, false);
    note_usage(&info, client.base_url(), output.wants_json());

    let metadata = info.metadata.as_ref();
    let fields = resolve_fields(Some(output.fields.as_str()), metadata)?;

    // The server validates the taxonomies and sources against its metadata and
    // adds the columns the aggregation needs; the table keeps `fields`.
    let taxonomies = match (tax, requested_taxonomies) {
        (None, _) => None,
        (Some(_), Some(requested)) => Some(requested),
        (Some(_), None) => Some(default_taxonomies(metadata)),
    };
    let tax_sources = tax.map(|tax| tax.tax_source.as_slice()).unwrap_or_default();
    if !tax_sources.is_empty()
        && !taxonomies
            .iter()
            .flatten()
            .any(|name| name.eq_ignore_ascii_case("tax"))
    {
        return Err(Failure::client(
            "--tax-source only applies to '--taxonomy tax' (it restricts \
             which sources the merged profile uses)",
        ));
    }

    let (c, k) = sketch_parameters(&info)?;
    if args.verbose {
        eprintln!(
            "{PROG}: database {} (c={c}, k={k}); sketching {} sample(s) with {} thread(s)",
            info.database.name.as_deref().unwrap_or("None"),
            samples.len(),
            sylph.threads
        );
    }
    let sketched: Vec<SketchedSample> =
        sketch_samples_parallel(&samples, c, k, Some(sylph.threads))?;
    if args.verbose {
        for sketch in &sketched {
            eprintln!(
                "{PROG}: {}: sketch c={} k={} ({})",
                sketch.name,
                sketch.header.c,
                sketch.header.k,
                format_size(sketch.bytes.len() as u64)
            );
        }
    }

    let total: u64 = sketched.iter().map(|s| s.bytes.len() as u64).sum();
    check_upload_size(&info, total, "sketches total")?;
    if args.verbose {
        eprintln!(
            "{PROG}: sending {} sketch(es), {}",
            sketched.len(),
            format_size(total)
        );
    }

    let names: Vec<String> = sketched.iter().map(|s| s.name.clone()).collect();
    let params_json = profile_params_json(
        mode,
        &params,
        &names,
        &fields,
        (!tax_sources.is_empty()).then_some(tax_sources),
        taxonomies.as_deref(),
    );
    let parts: Vec<(String, Bytes)> = sketched
        .into_iter()
        .map(|sketch| (sketch.name, Bytes::from(sketch.bytes)))
        .collect();
    let raw = client.post_sketches(mode.endpoint(), &params_json, &parts)?;
    let document: ProfileDocument =
        document::parse(&raw).map_err(|error| Failure::client(error.to_string()))?;

    if output.wants_json() {
        write_output(output.output.as_deref(), &with_newline(raw))?;
    } else {
        write_output(output.output.as_deref(), &render_tsv(&document.table))?;
    }

    if let Some(tax) = tax {
        let prefix = tax.taxprof.as_deref().unwrap_or_default();
        let written = write_taxprof(&document, prefix)?;
        for path in &written.paths {
            eprintln!("{PROG}: wrote {path}");
        }
        if written.has_sylphtax {
            let caveats = render_profile_caveats(&document);
            if !caveats.is_empty() {
                eprint!("{caveats}");
            }
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn profile_params_json(
    mode: Mode,
    params: &ProfileParamsOut,
    names: &[String],
    fields: &[String],
    tax_sources: Option<&[String]>,
    taxprof: Option<&[String]>,
) -> String {
    let mut object = serde_json::Map::new();
    object.insert(
        "minimum_ani".into(),
        params.minimum_ani.map(Value::from).unwrap_or(Value::Null),
    );
    object.insert(
        "seq_id".into(),
        params.seq_id.map(Value::from).unwrap_or(Value::Null),
    );
    object.insert(
        "estimate_unknown".into(),
        Value::Bool(params.estimate_unknown),
    );
    object.insert(
        "estimate_read_counts".into(),
        Value::Bool(params.estimate_read_counts),
    );
    object.insert(
        "min_number_kmers".into(),
        Value::from(params.min_number_kmers),
    );
    object.insert("min_contain".into(), Value::from(params.min_contain));
    object.insert("screen_ani".into(), Value::from(params.screen_ani));
    object.insert("no_adjust".into(), Value::Bool(params.no_adjust));
    object.insert("names".into(), json!(names));
    object.insert("mode".into(), Value::from(mode.endpoint()));
    object.insert("fields".into(), json!(fields));
    if let Some(sources) = tax_sources {
        object.insert("tax_sources".into(), json!(sources));
    }
    if let Some(taxprof) = taxprof {
        object.insert("taxprof".into(), json!(taxprof));
    }
    Value::Object(object).to_string()
}

fn safe_name(name: &str) -> String {
    let replaced = name.replace(['/', '\\'], "_");
    if replaced.is_empty() {
        "sample".into()
    } else {
        replaced
    }
}

fn taxonomy_file_label(taxonomy: &str) -> String {
    match taxonomy.strip_prefix("tax:") {
        Some(source) => format!("tax.{}", safe_name(source)),
        None => taxonomy.to_string(),
    }
}

struct TaxprofWritten {
    paths: Vec<String>,
    /// A sylph-tax (`tax`, `tax:<source>`) profile was written.
    has_sylphtax: bool,
}

/// Writes one `.sylphmpa` per sample and taxonomy of the response, in the
/// server's order.
fn write_taxprof(document: &ProfileDocument, prefix: &str) -> Result<TaxprofWritten, Failure> {
    let prefix = if Path::new(prefix).is_dir() && !prefix.ends_with('/') {
        format!("{prefix}/")
    } else {
        prefix.to_string()
    };
    let taxprof = document
        .taxprof
        .as_ref()
        .and_then(Value::as_object)
        .ok_or_else(|| Failure::client("the server returned no taxonomic profiles"))?;
    let mut written = TaxprofWritten {
        paths: Vec::new(),
        has_sylphtax: false,
    };
    for sample in &document.samples {
        let profiles = taxprof
            .get(&sample.name)
            .and_then(Value::as_object)
            .ok_or_else(|| {
                Failure::client(format!(
                    "the server returned no taxonomic profile for {:?}",
                    sample.name
                ))
            })?;
        for (taxonomy, profile) in profiles {
            let text = profile
                .get("sylphmpa")
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    Failure::client(format!(
                        "the server did not return the {taxonomy} profile for {:?}",
                        sample.name
                    ))
                })?;
            let path = format!(
                "{prefix}{}.{}.sylphmpa",
                safe_name(&sample.name),
                taxonomy_file_label(taxonomy)
            );
            std::fs::write(&path, text)
                .map_err(|error| Failure::client(format!("cannot write {path}: {error}")))?;
            written.has_sylphtax |= taxonomy == "tax" || taxonomy.starts_with("tax:");
            written.paths.push(path);
        }
    }
    Ok(written)
}

// ---------------------------------------------------------------------------
// assembly
// ---------------------------------------------------------------------------

fn cmd_assembly(args: AssemblyArgs) -> Outcome {
    if args.per_contig {
        return cmd_assembly_per_contig(&args);
    }
    for (flag, present) in [
        ("--fields", args.fields.is_some()),
        ("--min-contig-length", args.min_contig_length.is_some()),
        ("--consensus-ratio", args.consensus_ratio.is_some()),
        ("--consensus-z", args.consensus_z.is_some()),
    ] {
        if present {
            return Err(Failure::usage(format!(
                "{flag} is only available with --per-contig"
            )));
        }
    }

    let path = Path::new(&args.file);
    let name = args
        .name
        .clone()
        .unwrap_or_else(|| sample_name_from_path(&args.file, false));
    let top = args.top.map_or(DEFAULT_ASSEMBLY_TOP, usize::from);
    check_output_file(args.output.as_deref())?;

    let mut client = connect(&args.client)?;
    let (_, info) = client.get_info()?;
    note_messages(&client, args.quiet);
    note_usage(&info, client.base_url(), args.quiet || args.json);

    let raw = if args.local() {
        let (c, k) = sketch_parameters(&info)?;
        let (sketch, stats) = sketch_assembly_file(path, &name, c, k)?;
        check_upload_size(&info, sketch.len() as u64, "the sketch is")?;
        let query = vec![
            ("top".to_string(), top.to_string()),
            ("min_ani".to_string(), args.min_ani.to_string()),
            ("name".to_string(), name.clone()),
            ("records".to_string(), stats.records.to_string()),
            ("length".to_string(), stats.length.to_string()),
            ("gc".to_string(), stats.gc.to_string()),
        ];
        client.post_assembly(&query, Bytes::from(sketch), SKETCH_CONTENT_TYPE)?
    } else {
        let payload = std::fs::read(path)
            .map_err(|error| Failure::client(format!("cannot read {}: {error}", path.display())))?;
        let content_type = if payload.starts_with(&[0x1f, 0x8b]) {
            "application/gzip"
        } else {
            "application/octet-stream"
        };
        let query = vec![
            ("top".to_string(), top.to_string()),
            ("min_ani".to_string(), args.min_ani.to_string()),
            ("name".to_string(), name.clone()),
        ];
        client.post_assembly(&query, Bytes::from(payload), content_type)?
    };

    let document: AssemblyDocument =
        document::parse(&raw).map_err(|error| Failure::client(error.to_string()))?;
    if args.json {
        write_output(args.output.as_deref(), &with_newline(raw))?;
    } else {
        write_output(args.output.as_deref(), &render_assembly_tsv(&document.table))?;
        if !args.quiet {
            eprint!("{}", render_assembly_stderr(&document));
        }
    }
    Ok(())
}

fn cmd_assembly_per_contig(args: &AssemblyArgs) -> Outcome {
    if !args.local() {
        return Err(Failure::usage(
            "--per-contig always sketches locally; --no-local-sketch cannot be used with it",
        ));
    }
    if args.name.is_some() {
        return Err(Failure::usage(
            "--name cannot be combined with --per-contig: every contig is a sample \
             named after its record id",
        ));
    }
    let min_length = args.min_contig_length.unwrap_or(DEFAULT_MIN_CONTIG_LENGTH);
    check_output_file(args.output.as_deref())?;

    let mut client = connect(&args.client)?;
    let (_, info) = client.get_info()?;
    note_messages(&client, args.quiet);
    note_usage(&info, client.base_url(), args.quiet || args.json);

    let metadata = info.metadata.as_ref();
    let fields = resolve_fields(args.fields.as_deref(), metadata)?;
    let options = ContigsRequestOptions {
        top: args.top.map_or(DEFAULT_TOP, usize::from),
        min_ani: args.min_ani,
        consensus_ratio: args.consensus_ratio.unwrap_or(DEFAULT_CONSENSUS_RATIO),
        consensus_z: args.consensus_z.unwrap_or(DEFAULT_CONSENSUS_Z),
        fields: fields.clone(),
        tax_sources: None,
    };

    let (c, k) = sketch_parameters(&info)?;
    let limits = BatchLimits::from_server_limits(
        info.limits.max_contigs.map(|value| value as usize),
        info.limits.max_params_bytes.map(|value| value as usize),
        info.limits.max_upload_mb.map(|value| value as usize),
    );
    let mut planner = ContigBatchPlanner::new(limits, &options);
    let mut batches: Vec<ContigsDocument> = Vec::new();
    let path = Path::new(&args.file);
    let counters = sketch_contigs_file(path, c, k, min_length, DEFAULT_MIN_NUMBER_KMERS, |contig| {
        let spec = ContigSpec::from(&contig);
        let payload_len = contig.sketch.len();
        let planned = planner
            .push(spec, payload_len, contig)
            .map_err(|error| Failure::client(error.to_string()))?;
        if let Some(batch) = planned {
            batches.push(send_contigs_batch(&mut client, batch, &options)?);
        }
        Ok::<(), Failure>(())
    })?;
    if let Some(batch) = planner.finish() {
        batches.push(send_contigs_batch(&mut client, batch, &options)?);
    }

    let merge_options = ContigsRunMergeOptions {
        skipped_short: counters.skipped_short,
        skipped_few_kmers: counters.skipped_few_kmers,
        min_contig_length: min_length,
        database: Some(json!({
            "name": info.database.name,
            "genomes": info.database.genomes,
            "c": info.database.c,
            "k": info.database.k,
        })),
        ..Default::default()
    };
    let run = merge_contigs_run(&batches, &options, &merge_options)
        .map_err(|error| Failure::client(error.to_string()))?;

    if args.json {
        let text = serde_json::to_string(&run)
            .map_err(|error| Failure::client(format!("cannot encode the run: {error}")))?;
        write_output(args.output.as_deref(), &with_newline(text))?;
    } else {
        write_output(args.output.as_deref(), &render_contigs_tsv(&run.table))?;
        if !args.quiet {
            eprint!("{}", render_contigs_summary(&run));
        }
    }
    Ok(())
}

fn send_contigs_batch(
    client: &mut Client,
    batch: sylphd_core::batch::PlannedBatch<sylphd_core::input::PreparedContig>,
    options: &ContigsRequestOptions,
) -> Result<ContigsDocument, Failure> {
    let params = batch.params_json(options);
    let parts: Vec<(String, Bytes)> = batch
        .items
        .into_iter()
        .map(|BatchItem { spec, item, .. }| (spec.name, Bytes::from(item.sketch)))
        .collect();
    let raw = client.post_sketches("contigs", &params, &parts)?;
    document::parse(&raw).map_err(|error| Failure::client(error.to_string()))
}

// ---------------------------------------------------------------------------
// render (offline)
// ---------------------------------------------------------------------------

fn cmd_render(args: RenderArgs) -> Outcome {
    let text = if args.file == "-" {
        use std::io::Read;
        let mut buffer = String::new();
        std::io::stdin()
            .read_to_string(&mut buffer)
            .map_err(|error| Failure::usage(format!("cannot read stdin: {error}")))?;
        buffer
    } else {
        std::fs::read_to_string(&args.file)
            .map_err(|error| Failure::usage(format!("cannot read {}: {error}", args.file)))?
    };
    let document = match ResultDocument::parse(&text) {
        Ok(document) => document,
        Err(
            error
            @ (DocumentError::UnsupportedApiVersion { .. } | DocumentError::MissingApiVersion),
        ) => {
            return Err(Failure::client(error.to_string()));
        }
        Err(error) => {
            return Err(Failure::usage(format!("invalid result document: {error}")));
        }
    };
    match document {
        ResultDocument::Profile(document) => {
            write_output(args.output.as_deref(), &render_tsv(&document.table))?;
            if !args.quiet {
                let caveats = render_profile_caveats(&document);
                if !caveats.is_empty() {
                    eprint!("{caveats}");
                }
            }
        }
        ResultDocument::Assembly(document) => {
            write_output(args.output.as_deref(), &render_assembly_tsv(&document.table))?;
            if !args.quiet {
                eprint!("{}", render_assembly_stderr(&document));
            }
        }
        ResultDocument::Contigs(document) => {
            write_output(args.output.as_deref(), &render_contigs_tsv(&document.table))?;
            if !args.quiet {
                eprint!("{}", render_contigs_summary(&document));
            }
        }
    }
    Ok(())
}
