//! The `info`, `profile`, `query`, `assembly` and `render` commands.
//!
//! Behaviour is the Python CLI's (`src/sylphd/cli.py`, `client.py`,
//! `per_contig_client.py`); all rendering and document merging come from
//! `sylphd-core`.

use std::io::Write;
use std::path::Path;

use serde_json::{json, Value};
use sylphd_core::batch::{
    BatchLimits, ContigBatchPlanner, ContigSpec, ContigsRequestOptions, DEFAULT_TOP,
};
use sylphd_core::document::{
    merge_contigs_run, AssemblyDocument, ContigsDocument, ContigsRunMergeOptions, DocumentError,
    InfoDocument, MetadataInfo, ProfileDocument, ResultDocument, SkippedBucket,
};
use sylphd_core::input::{
    build_sample_inputs, sample_name_from_path, sketch_assembly_file, sketch_contigs_file,
    sketch_samples_parallel, InputError, SketchedSample, DEFAULT_MIN_CONTIG_LENGTH,
    DEFAULT_MIN_NUMBER_KMERS,
};
use sylphd_core::render::{
    format_size, render_assembly_stderr, render_assembly_tsv, render_contigs_summary,
    render_contigs_tsv, render_info, render_profile_caveats, render_profile_tsv,
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

/// `_client_token`: `--token-file`, else `$SYLPHD_TOKEN`.
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

/// `_resolve_fields`: `--fields` -> the metadata columns to request.
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

/// `_check_output_file`: fail on an unwritable `-o` before the expensive work.
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

/// Same for a `--taxprof` prefix (`cli._check_output_prefix`).
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
        let messages_json = serde_json::to_string(&messages)
            .map_err(|e| Failure::client(format!("cannot encode messages: {e}")))?;
        let merged = insert_json_member(&raw, "messages", &messages_json)?;
        return write_output(None, &with_newline(merged));
    }
    write_output(None, &render_info(&info, &messages))
}

/// Appends a JSON member before the final `}` (the `/info` document is an object).
fn insert_json_member(raw: &str, name: &str, value_json: &str) -> Result<String, Failure> {
    let text = raw.trim_end();
    let Some(position) = text.rfind('}') else {
        return Err(Failure::client(
            "unexpected /info response (not a JSON object)",
        ));
    };
    let head = text[..position].trim_end().trim_end_matches(',');
    Ok(format!("{head},\n  \"{name}\": {value_json}\n}}\n"))
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
    cmd_contain(
        &args.sample,
        &args.output,
        &args.sylph,
        &args.tax,
        args.verbose,
        &args.client,
        Mode::Profile,
    )
}

fn cmd_query(args: QueryArgs) -> Outcome {
    let taxonomy = crate::cli::ProfileTaxCli {
        estimate_read_counts: false,
        taxonomy: None,
        tax_source: Vec::new(),
        taxprof: None,
    };
    cmd_contain(
        &args.sample,
        &args.output,
        &args.sylph,
        &taxonomy,
        args.verbose,
        &args.client,
        Mode::Query,
    )
}

#[allow(clippy::too_many_arguments)]
fn cmd_contain(
    sample_args: &crate::cli::SampleCli,
    output: &crate::cli::OutputCli,
    sylph: &crate::cli::SylphCli,
    tax: &crate::cli::ProfileTaxCli,
    verbose: bool,
    client_args: &ClientCli,
    mode: Mode,
) -> Outcome {
    let samples = build_sample_inputs(&sample_args.inputs())
        .map_err(|error| Failure::usage(error.to_string()))?;

    let params = ProfileParamsOut {
        minimum_ani: sylph.minimum_ani,
        seq_id: sylph.seq_id,
        estimate_unknown: sylph.estimate_unknown,
        estimate_read_counts: mode == Mode::Profile && tax.estimate_read_counts,
        min_number_kmers: sylph.min_number_kmers,
        min_contain: sylph.min_contain,
        screen_ani: sylph.screen_ani,
        no_adjust: sylph.no_adjust,
    };

    let write_taxonomy = mode == Mode::Profile && tax.taxprof.is_some();
    if !tax.tax_source.is_empty() && !write_taxonomy {
        return Err(Failure::client(
            "--tax-source needs --taxprof: it restricts the sources of the \
             aggregated 'tax' profile",
        ));
    }
    check_output_file(output.output.as_deref())?;

    // A bad --taxonomy must fail before the server is contacted; only the
    // default depends on what the server has.
    let requested_taxonomies = if write_taxonomy {
        match tax.taxonomy.as_deref() {
            Some(text) => Some(parse_taxonomies(Some(text))?),
            None => None,
        }
    } else {
        None
    };
    if write_taxonomy {
        if let Some(prefix) = &tax.taxprof {
            check_output_prefix(prefix)?;
        }
    }

    let mut client = connect(client_args)?;
    let (_, info) = client.get_info()?;
    note_messages(&client, false);
    note_usage(&info, client.base_url(), false);

    let metadata = info.metadata.as_ref();
    let fields = resolve_fields(Some(output.fields.as_str()), metadata)?;

    let (taxonomies, tax_sources, request_fields, extra_fields) = if write_taxonomy {
        let taxonomies = match requested_taxonomies {
            Some(taxonomies) => taxonomies,
            None => default_taxonomies(metadata),
        };
        let tax_sources = select_tax_sources(&tax.tax_source, &taxonomies, metadata)?;
        let extra = resolve_taxonomy_columns(&taxonomies, metadata)?;
        let mut request_fields = fields.clone();
        for column in &extra {
            if !request_fields.contains(column) {
                request_fields.push(column.clone());
            }
        }
        (taxonomies, tax_sources, request_fields, extra)
    } else {
        (Vec::new(), Vec::new(), fields.clone(), Vec::new())
    };

    let (c, k) = sketch_parameters(&info)?;
    if verbose {
        eprintln!(
            "{PROG}: database {} (c={c}, k={k}); sketching {} sample(s) with {} thread(s)",
            info.database.name.as_deref().unwrap_or("None"),
            samples.len(),
            sylph.threads
        );
    }
    let sketched: Vec<SketchedSample> =
        sketch_samples_parallel(&samples, c, k, Some(sylph.threads))?;
    if verbose {
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

    let limit = info.limits.max_upload_mb.unwrap_or(0) * 1024 * 1024;
    let total: u64 = sketched.iter().map(|s| s.bytes.len() as u64).sum();
    if limit > 0 && total > limit {
        return Err(Failure::client(format!(
            "sketches total {}, above the server's {} upload limit; send fewer \
             samples per request",
            format_size(total),
            format_size(limit)
        )));
    }
    if verbose {
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
        &request_fields,
        (!tax_sources.is_empty()).then_some(tax_sources.as_slice()),
        write_taxonomy.then_some(taxonomies.as_slice()),
    );
    let parts: Vec<(String, Vec<u8>)> = sketched
        .iter()
        .map(|sketch| (sketch.name.clone(), sketch.bytes.clone()))
        .collect();
    let raw = client.post_sketches(mode.endpoint(), &params_json, &parts)?;
    let mut document =
        ProfileDocument::from_json_str(&raw).map_err(|error| Failure::client(error.to_string()))?;
    if request_fields.len() != fields.len() {
        // The extra taxonomy columns were requested for the taxprof only: they do
        // not show up in the TSV (`rows_to_tsv(fields=...)` printed `fields`, which
        // for `--fields none` is "no metadata columns", not "no columns"). The
        // `--json` document keeps them, because it is the server's answer.
        let hidden: Vec<&String> = extra_fields.iter().filter(|c| !fields.contains(c)).collect();
        let display: Vec<String> = document
            .table
            .columns
            .iter()
            .filter(|column| !hidden.contains(column))
            .cloned()
            .collect();
        let table = document.table.select(&display);
        document.table = table;
    }

    if output.wants_json() {
        write_output(output.output.as_deref(), &with_newline(raw))?;
    } else {
        write_output(output.output.as_deref(), &render_profile_tsv(&document))?;
    }

    if write_taxonomy {
        let prefix = tax.taxprof.as_deref().unwrap_or_default();
        let written = write_taxprof(&document, prefix, &taxonomies, &tax_sources)?;
        for path in written {
            eprintln!("{PROG}: wrote {path}");
        }
        if taxonomies.iter().any(|name| is_sylphtax(name)) {
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

const TAXONOMIES: [&str; 3] = ["gtdb", "ncbi", "tax"];
const SYLPHTAX_PROFILE_COLUMNS: [&str; 3] = ["tax_source", "tax_key", "tax_lineage"];
const NEAREST_PROFILE_COLUMNS: [&str; 8] = [
    "ncbi_nearest_lineage",
    "ncbi_nearest_taxid",
    "ncbi_nearest_rank",
    "ncbi_nearest_name",
    "ncbi_nearest_clade",
    "ncbi_nearest_formal_taxid",
    "ncbi_nearest_formal_rank",
    "ncbi_nearest_formal_name",
];

fn is_sylphtax(taxonomy: &str) -> bool {
    taxonomy == "tax" || taxonomy.starts_with("tax:")
}

fn source_of(taxonomy: &str) -> Option<&str> {
    taxonomy
        .strip_prefix("tax:")
        .filter(|source| !source.is_empty())
}

/// `taxonomy.parse_taxonomies`.
fn parse_taxonomies(text: Option<&str>) -> Result<Vec<String>, Failure> {
    let Some(text) = text else {
        return Ok(vec!["gtdb".into(), "ncbi".into()]);
    };
    let mut selected: Vec<String> = Vec::new();
    for part in text.split(',') {
        let name = part.trim();
        if name.is_empty() {
            continue;
        }
        let name = if name.to_ascii_lowercase().starts_with("tax:") {
            let source = name["tax:".len()..].trim();
            if source.is_empty() {
                return Err(Failure::client(format!(
                    "'{}' names no taxonomy source (tax:<source>)",
                    part.trim()
                )));
            }
            format!("tax:{source}")
        } else {
            name.to_ascii_lowercase()
        };
        if source_of(&name).is_none() && !TAXONOMIES.contains(&name.as_str()) {
            return Err(Failure::client(format!(
                "unknown taxonomy '{}' (choose from: {}, or tax:<source>)",
                part.trim(),
                TAXONOMIES.join(", ")
            )));
        }
        if !selected.contains(&name) {
            selected.push(name);
        }
    }
    if selected.is_empty() {
        return Err(Failure::client(format!(
            "--taxonomy needs at least one of: {}",
            TAXONOMIES.join(", ")
        )));
    }
    Ok(selected)
}

fn source_names(metadata: Option<&MetadataInfo>) -> Vec<String> {
    metadata
        .map(|meta| {
            meta.taxonomy_sources
                .iter()
                .filter_map(|source| source.name.clone())
                .collect()
        })
        .unwrap_or_default()
}

fn default_taxonomies(metadata: Option<&MetadataInfo>) -> Vec<String> {
    let mut names = vec!["gtdb".to_string(), "ncbi".to_string()];
    if metadata.is_some_and(|meta| !meta.taxonomy_sources.is_empty()) {
        names.push("tax".into());
    }
    names
}

/// `taxonomy.resolve_taxonomy_columns`.
fn resolve_taxonomy_columns(
    taxonomies: &[String],
    metadata: Option<&MetadataInfo>,
) -> Result<Vec<String>, Failure> {
    let Some(metadata) = metadata else {
        return Err(Failure::client(
            "the server has no metadata database; --taxprof needs \
             'gtdb_taxonomy'/'ncbi_taxonomy' (start it with 'sylphd serve --meta')",
        ));
    };
    if taxonomies.iter().any(|name| is_sylphtax(name)) && metadata.taxonomy_sources.is_empty() {
        return Err(Failure::client(
            "the server has no sylph-tax taxonomy sources, so there is nothing \
             for '--taxonomy tax' to aggregate; add sources with \
             'sylphd setup --taxonomy' and restart the server",
        ));
    }
    let known = source_names(Some(metadata));
    for taxonomy in taxonomies {
        if let Some(source) = source_of(taxonomy) {
            if !known.iter().any(|name| name == source) {
                return Err(Failure::client(format!(
                    "the server has no taxonomy source {source:?} (it has: {})",
                    if known.is_empty() {
                        "none".to_string()
                    } else {
                        known.join(", ")
                    }
                )));
            }
        }
    }
    let available = &metadata.columns;
    let mut columns: Vec<String> = Vec::new();
    let mut missing: Vec<String> = Vec::new();
    for taxonomy in taxonomies {
        let needed: Vec<&str> = if is_sylphtax(taxonomy) {
            SYLPHTAX_PROFILE_COLUMNS.to_vec()
        } else {
            vec![match taxonomy.as_str() {
                "gtdb" => "gtdb_taxonomy",
                "ncbi" => "ncbi_taxonomy",
                _ => unreachable!("validated taxonomy"),
            }]
        };
        for column in needed {
            if !available.iter().any(|name| name == column) {
                missing.push(format!("{column} ({taxonomy})"));
            } else if !columns.iter().any(|name| name == column) {
                columns.push(column.to_string());
            }
        }
        if taxonomy == "ncbi" {
            for column in NEAREST_PROFILE_COLUMNS {
                if available.iter().any(|name| name == column)
                    && !columns.iter().any(|name| name == column)
                {
                    columns.push(column.to_string());
                }
            }
        }
    }
    if !missing.is_empty() {
        return Err(Failure::client(format!(
            "the server metadata has no {}; available columns: {}",
            missing.join(", "),
            if available.is_empty() {
                "(none)".to_string()
            } else {
                available.join(", ")
            }
        )));
    }
    Ok(columns)
}

/// `cli._select_tax_sources`.
fn select_tax_sources(
    values: &[String],
    taxonomies: &[String],
    metadata: Option<&MetadataInfo>,
) -> Result<Vec<String>, Failure> {
    if !taxonomies.iter().any(|name| name == "tax") {
        if !values.is_empty() {
            return Err(Failure::client(
                "--tax-source only applies to '--taxonomy tax' (it restricts \
                 which sources the merged profile uses)",
            ));
        }
        return Ok(Vec::new());
    }
    let available = source_names(metadata);
    if values.is_empty() {
        return Ok(available);
    }
    let unknown: Vec<&str> = values
        .iter()
        .filter(|name| !available.iter().any(|known| known == *name))
        .map(String::as_str)
        .collect();
    if !unknown.is_empty() {
        return Err(Failure::client(format!(
            "unknown taxonomy source(s): {} (available: {})",
            unknown.join(", "),
            if available.is_empty() {
                "none".to_string()
            } else {
                available.join(", ")
            }
        )));
    }
    Ok(available
        .into_iter()
        .filter(|name| values.iter().any(|value| value == name))
        .collect())
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
    match source_of(taxonomy) {
        None => taxonomy.to_string(),
        Some(source) => format!("tax.{}", safe_name(source)),
    }
}

fn write_taxprof(
    document: &ProfileDocument,
    prefix: &str,
    taxonomies: &[String],
    tax_sources: &[String],
) -> Result<Vec<String>, Failure> {
    if taxonomies.iter().any(|name| name == "tax") && tax_sources.is_empty() {
        return Err(Failure::client(
            "the 'tax' profile needs the names of the taxonomy sources it was \
             resolved against",
        ));
    }
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
    let mut written = Vec::new();
    for taxonomy in taxonomies {
        for sample in &document.samples {
            let text = taxprof
                .get(&sample.name)
                .and_then(|profiles| profiles.get(taxonomy))
                .and_then(|profile| profile.get("sylphmpa"))
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
            written.push(path);
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
    let top = args.top.unwrap_or(10);
    check_output_file(args.output.as_deref())?;

    let mut client = connect(&args.client)?;
    let (_, info) = client.get_info()?;
    note_messages(&client, args.quiet);
    note_usage(&info, client.base_url(), args.quiet || args.json);

    let raw = if args.local() {
        let (c, k) = sketch_parameters(&info)?;
        let (sketch, stats) = sketch_assembly_file(path, &name, c, k)?;
        let limit = info.limits.max_upload_mb.unwrap_or(0) * 1024 * 1024;
        if limit > 0 && sketch.len() as u64 > limit {
            return Err(Failure::client(format!(
                "the sketch is {} bytes, over the server's {} MB upload limit",
                sketch.len(),
                limit / (1024 * 1024)
            )));
        }
        let query = vec![
            ("top".to_string(), top.to_string()),
            ("min_ani".to_string(), args.min_ani.to_string()),
            ("name".to_string(), name.clone()),
            ("records".to_string(), stats.records.to_string()),
            ("length".to_string(), stats.length.to_string()),
            ("gc".to_string(), stats.gc.to_string()),
        ];
        client.post_assembly(&query, sketch, SKETCH_CONTENT_TYPE)?
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
        client.post_assembly(&query, payload, content_type)?
    };

    let document = AssemblyDocument::from_json_str(&raw)
        .map_err(|error| Failure::client(error.to_string()))?;
    if args.json {
        write_output(args.output.as_deref(), &with_newline(raw))?;
    } else {
        write_output(args.output.as_deref(), &render_assembly_tsv(&document))?;
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
        top: args.top.unwrap_or(DEFAULT_TOP as u8) as usize,
        min_ani: args.min_ani,
        consensus_ratio: args.consensus_ratio.unwrap_or(1.5),
        consensus_z: args.consensus_z.unwrap_or(2.0),
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
    let counters =
        sketch_contigs_file(path, c, k, min_length, DEFAULT_MIN_NUMBER_KMERS, |contig| {
            let spec = ContigSpec::from(&contig);
            let payload_len = contig.sketch.len();
            let planned = planner
                .push(spec, payload_len, contig)
                .map_err(|error| InputError(error.to_string()))?;
            if let Some(batch) = planned {
                let document = send_contigs_batch(&mut client, &batch, &options)
                    .map_err(|error| InputError(error.0))?;
                batches.push(document);
            }
            Ok(())
        })?;
    if let Some(batch) = planner.finish() {
        let document = send_contigs_batch(&mut client, &batch, &options).map_err(Failure::from)?;
        batches.push(document);
    }

    let merge_options = ContigsRunMergeOptions {
        skipped_short: SkippedBucket {
            contigs: counters.skipped_short.contigs,
            bp: counters.skipped_short.bp,
        },
        skipped_few_kmers: SkippedBucket {
            contigs: counters.skipped_few_kmers.contigs,
            bp: counters.skipped_few_kmers.bp,
        },
        min_contig_length: min_length,
        fields: fields.clone(),
        params: Some(contig_params_fallback(args.min_ani, min_length, &options)),
        database: Some(json!({
            "name": info.database.name,
            "genomes": info.database.genomes,
            "c": info.database.c,
            "k": info.database.k,
        })),
        thresholds: None,
        ..Default::default()
    };
    let run = merge_contigs_run(&batches, &merge_options)
        .map_err(|error| Failure::client(error.to_string()))?;

    if args.json {
        let text = serde_json::to_string(&run)
            .map_err(|error| Failure::client(format!("cannot encode the run: {error}")))?;
        write_output(args.output.as_deref(), &with_newline(text))?;
    } else {
        write_output(args.output.as_deref(), &render_contigs_tsv(&run))?;
        if !args.quiet {
            eprint!("{}", render_contigs_summary(&run));
        }
    }
    Ok(())
}

fn send_contigs_batch(
    client: &mut Client,
    batch: &sylphd_core::batch::PlannedBatch<sylphd_core::input::PreparedContig>,
    options: &ContigsRequestOptions,
) -> Result<ContigsDocument, ClientError> {
    let params = batch.params_json(options);
    let parts: Vec<(String, Vec<u8>)> = batch
        .items
        .iter()
        .map(|item| (item.spec.name.clone(), item.item.sketch.clone()))
        .collect();
    let raw = client.post_sketches("contigs", &params, &parts)?;
    ContigsDocument::from_json_str(&raw).map_err(|error| ClientError(error.to_string()))
}

fn contig_params_fallback(min_ani: f64, min_length: u64, options: &ContigsRequestOptions) -> Value {
    json!({
        "top": options.top,
        "min_ani": min_ani,
        "min_contig_length": min_length,
        "consensus_ratio": options.consensus_ratio,
        "consensus_z": options.consensus_z,
        "no_adjust": true,
        "minimum_ani": 0.0,
        "screen_ani": 0.0,
        "min_contain": 3,
        "min_number_kmers": 10,
    })
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
    let document = match ResultDocument::from_json_str(&text) {
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
            write_output(args.output.as_deref(), &render_profile_tsv(&document))?;
            if !args.quiet {
                let caveats = render_profile_caveats(&document);
                if !caveats.is_empty() {
                    eprint!("{caveats}");
                }
            }
        }
        ResultDocument::Assembly(document) => {
            write_output(args.output.as_deref(), &render_assembly_tsv(&document))?;
            if !args.quiet {
                eprint!("{}", render_assembly_stderr(&document));
            }
        }
        ResultDocument::Contigs(document) => {
            write_output(args.output.as_deref(), &render_contigs_tsv(&document))?;
            if !args.quiet {
                eprint!("{}", render_contigs_summary(&document));
            }
        }
    }
    Ok(())
}
