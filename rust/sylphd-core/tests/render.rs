//! Golden and unit tests for `sylphd_core::document` and `sylphd_core::render`.
//!
//! Fixtures under `tests/data/*_golden.json` were captured directly from the Python
//! server (`TestClient(create_app(...))`) and Python renderers (`protocol.rows_to_tsv`,
//! `cli._write_hits_tsv`, `cli._assignment_summary`, `per_contig_client.PerContigRun`,
//! `cli._print_*`). Every assertion checks byte-for-byte equality.

use serde_json::{json, Value};
use sylphd_core::document::{
    check_api_version, check_value_api_version, finalize_summary, merge_contigs_run,
    merge_summaries, AssemblyDocument, ContigsDocument, ContigsRunMergeOptions, ContigsSummary,
    DocumentError, InfoDocument, MessageItem, NotClassifiedBucket, ProfileDocument, ResultDocument,
    SkippedBucket, Table, UsageInfo, SUPPORTED_API_VERSION,
};
use sylphd_core::render::{
    assembly_species_note, format_bases, format_size, format_thousands, format_value,
    render_assembly_stderr, render_assembly_summary, render_assembly_tsv, render_contigs_summary,
    render_contigs_tsv, render_info, render_profile_caveats, render_profile_tsv,
    render_stderr_messages, render_tsv, render_usage_note,
};

const PROFILE_GOLDEN: &str = include_str!("data/profile_golden.json");
const ASSEMBLY_GOLDEN: &str = include_str!("data/assembly_golden.json");
const CONTIGS_GOLDEN: &str = include_str!("data/contigs_golden.json");
const INFO_GOLDEN: &str = include_str!("data/info_golden.json");

#[test]
fn golden_profile_and_query_tsv_byte_identical() {
    let fixture: Value = serde_json::from_str(PROFILE_GOLDEN).unwrap();
    let cases = fixture["cases"].as_array().unwrap();
    assert!(cases.len() >= 8);

    for case in cases {
        let name = case["name"].as_str().unwrap();
        let doc = ProfileDocument::from_value(case["document"].clone())
            .unwrap_or_else(|e| panic!("case {name}: {e}"));
        let got_tsv = render_profile_tsv(&doc);
        let expected_tsv = case["expected_tsv"].as_str().unwrap();
        assert_eq!(got_tsv, expected_tsv, "TSV mismatch in case {name}");

        // Lambda_status must never appear in the rendered header.
        let header = got_tsv.lines().next().unwrap();
        assert!(
            !header.split('\t').any(|c| c == "Lambda_status"),
            "Lambda_status leaked into header in case {name}"
        );

        if let Some(expected_caveats) = case.get("expected_caveats").and_then(Value::as_str) {
            assert_eq!(
                render_profile_caveats(&doc),
                expected_caveats,
                "caveats mismatch in case {name}"
            );
        }
    }
}

#[test]
fn golden_assembly_tsv_and_summaries_byte_identical() {
    let fixture: Value = serde_json::from_str(ASSEMBLY_GOLDEN).unwrap();
    let cases = fixture["cases"].as_array().unwrap();
    assert!(cases.len() >= 5);

    for case in cases {
        let name = case["name"].as_str().unwrap();
        let doc = AssemblyDocument::from_value(case["document"].clone())
            .unwrap_or_else(|e| panic!("case {name}: {e}"));

        assert_eq!(
            render_assembly_tsv(&doc),
            case["expected_tsv"].as_str().unwrap(),
            "assembly TSV mismatch in case {name}"
        );
        assert_eq!(
            render_assembly_summary(&doc),
            case["expected_summary"].as_str().unwrap(),
            "assembly summary mismatch in case {name}"
        );
        assert_eq!(
            assembly_species_note(&doc),
            case["expected_note"].as_str(),
            "assembly note mismatch in case {name}"
        );
        assert_eq!(
            render_assembly_stderr(&doc),
            case["expected_stderr"].as_str().unwrap(),
            "assembly stderr mismatch in case {name}"
        );
    }
}

#[test]
fn golden_contigs_merge_tsv_and_summary_byte_identical() {
    let fixture: Value = serde_json::from_str(CONTIGS_GOLDEN).unwrap();
    let batches: Vec<ContigsDocument> = fixture["batches"]
        .as_array()
        .unwrap()
        .iter()
        .cloned()
        .map(|v| ContigsDocument::from_value(v).unwrap())
        .collect();
    let opts: ContigsRunMergeOptions =
        serde_json::from_value(fixture["merge_options"].clone()).unwrap();

    let merged = merge_contigs_run(&batches, &opts).unwrap();

    // TSV byte-identical to PerContigRun.write_tsv
    assert_eq!(
        render_contigs_tsv(&merged),
        fixture["expected_tsv"].as_str().unwrap()
    );

    // Two-line summary + deduplicated caveats byte-identical to PerContigRun.print_summary
    assert_eq!(
        render_contigs_summary(&merged),
        fixture["expected_summary_text"].as_str().unwrap()
    );

    // Caveats deduplicated across batches in first-seen order
    let expected_caveats: Vec<String> =
        serde_json::from_value(fixture["expected_caveats"].clone()).unwrap();
    assert_eq!(merged.caveats, expected_caveats);

    // Summary counters and consensus_bp match Python PerContigRun.summary()
    let expected_summary = &fixture["expected_summary_json"];
    assert_eq!(
        merged.summary.contigs,
        expected_summary["contigs"].as_u64().unwrap()
    );
    assert_eq!(merged.summary.bp, expected_summary["bp"].as_u64().unwrap());
    assert_eq!(
        merged.summary.queried_contigs,
        expected_summary["queried_contigs"].as_u64().unwrap()
    );
    assert_eq!(
        merged.summary.queried_bp,
        expected_summary["queried_bp"].as_u64().unwrap()
    );
    assert_eq!(
        merged.summary.no_hit_contigs,
        expected_summary["no_hit_contigs"].as_u64().unwrap()
    );
    assert_eq!(
        serde_json::to_value(&merged.summary.skipped_short).unwrap(),
        expected_summary["skipped_short"]
    );
    assert_eq!(
        serde_json::to_value(&merged.summary.skipped_few_kmers).unwrap(),
        expected_summary["skipped_few_kmers"]
    );
    assert_eq!(
        serde_json::to_value(&merged.summary.consensus_bp).unwrap(),
        expected_summary["consensus_bp"]
    );

    // Contigs map preserves insertion/request order across batches
    let contig_names: Vec<&str> = merged.contigs.iter().map(|(k, _)| k.as_str()).collect();
    assert_eq!(
        contig_names,
        vec!["contig1", "contig2", "contig3", "contig4"]
    );
    assert_eq!(merged.elapsed_ms, Some(15.5));

    // Multi-batch >5 taxa: merge_summaries (untruncated) + finalize_summary (top 5)
    let parts: Vec<ContigsSummary> =
        serde_json::from_value(fixture["many_taxa_parts"].clone()).unwrap();
    let full = merge_summaries(&parts);
    let top5 = finalize_summary(&full, Some(5));
    let expected_full: ContigsSummary =
        serde_json::from_value(fixture["expected_many_taxa_full"].clone()).unwrap();
    let expected_top5: ContigsSummary =
        serde_json::from_value(fixture["expected_many_taxa_top5"].clone()).unwrap();
    assert_eq!(full, expected_full);
    assert_eq!(top5, expected_top5);
    assert_eq!(top5.consensus_bp.len(), 5);
}

#[test]
fn golden_info_and_messages_byte_identical() {
    #[derive(serde::Deserialize)]
    struct InfoCase {
        name: String,
        info: InfoDocument,
        messages: Vec<MessageItem>,
        expected_text: String,
    }
    #[derive(serde::Deserialize)]
    struct InfoFixture {
        cases: Vec<InfoCase>,
    }

    let fixture: InfoFixture = serde_json::from_str(INFO_GOLDEN).unwrap();
    assert!(fixture.cases.len() >= 2);

    for case in &fixture.cases {
        check_api_version(case.info.api_version).unwrap();
        let got = render_info(&case.info, &case.messages);
        assert_eq!(
            got, case.expected_text,
            "info text mismatch in case {}",
            case.name
        );
    }
}

#[test]
fn protocol_format_value_unit_cases() {
    // Direct port of tests/test_server.py::test_format_value
    assert_eq!(format_value("Adjusted_ANI", &json!(99.789)), "99.79");
    assert_eq!(format_value("Eff_cov", &json!(0.3597986)), "0.360");
    assert_eq!(format_value("Median_cov", &json!(1.0)), "1");
    assert_eq!(
        format_value("ANI_5-95_percentile", &json!([99.6437, 99.9452])),
        "99.64-99.95"
    );
    assert_eq!(
        format_value("Containment_ind", &json!([6208, 21899])),
        "6208/21899"
    );
    assert_eq!(
        format_value("Taxonomic_abundance", &json!(100.0)),
        "100.0000"
    );
    assert_eq!(format_value("Eff_lambda", &Value::Null), "");
    assert_eq!(format_value("Eff_lambda", &json!("HIGH")), "HIGH");
    assert_eq!(format_value("ANI_5-95_percentile", &Value::Null), "NA-NA");
    assert_eq!(
        format_value("Lambda_5-95_percentile", &Value::Null),
        "NA-NA"
    );
    assert_eq!(format_value("Eff_cov", &Value::Null), "inf");
    assert_eq!(format_value("True_cov", &Value::Null), "inf");
    assert_eq!(format_value("Taxonomic_abundance", &Value::Null), "NaN");
    assert_eq!(format_value("Sequence_abundance", &Value::Null), "NaN");
    assert_eq!(format_value("Adjusted_ANI", &json!(100.4)), "100.00");
    assert_eq!(format_value("Naive_ANI", &json!(100.4)), "100.40");
    assert_eq!(format_value("Sample_file", &json!("x")), "x");
    assert_eq!(format_value("kmers_reassigned", &json!(0)), "0");

    // Eff_lambda <- Lambda_status in render_tsv, dropping Lambda_status column
    let t = Table {
        columns: vec![
            "Eff_lambda".into(),
            "Lambda_status".into(),
            "True_cov".into(),
        ],
        data: vec![
            vec![Value::Null, json!("HIGH"), json!(0.3597986)],
            vec![Value::Null, json!("LOW"), Value::Null],
            vec![json!(0.36), json!("OK"), json!(1.0)],
            vec![Value::Null, json!("OK"), json!(2.0)],
        ],
    };
    assert_eq!(
        render_tsv(&t),
        "Eff_lambda\tTrue_cov\nHIGH\t0.360\nLOW\tinf\n0.360\t1.000\n\t2.000\n"
    );
}

#[test]
fn api_version_validation_and_error_messages() {
    assert_eq!(SUPPORTED_API_VERSION, 1);
    assert!(check_api_version(1).is_ok());

    let err = check_api_version(2).unwrap_err();
    assert_eq!(
        err.to_string(),
        "server API version 2, this client supports 1: update sylphd"
    );
    let err0 = check_api_version(0).unwrap_err();
    assert_eq!(
        err0.to_string(),
        "server API version 0, this client supports 1: update sylphd"
    );

    // Missing api_version
    let err_missing = check_value_api_version(&json!({"samples": []})).unwrap_err();
    assert!(matches!(err_missing, DocumentError::MissingApiVersion));
    assert!(err_missing.to_string().contains("supports API version 1"));

    // Future incompatible schema still reports version mismatch first, not missing field!
    let future_doc = json!({"api_version": 99, "completely_new_schema": true});
    for res in [
        ProfileDocument::from_value(future_doc.clone()).map(|_| ()),
        AssemblyDocument::from_value(future_doc.clone()).map(|_| ()),
        ContigsDocument::from_value(future_doc.clone()).map(|_| ()),
        InfoDocument::from_value(future_doc.clone()).map(|_| ()),
        ResultDocument::from_value(future_doc).map(|_| ()),
    ] {
        assert_eq!(
            res.unwrap_err().to_string(),
            "server API version 99, this client supports 1: update sylphd"
        );
    }
}

#[test]
fn result_document_autodetects_all_three_kinds() {
    let p: Value = serde_json::from_str(PROFILE_GOLDEN).unwrap();
    let a: Value = serde_json::from_str(ASSEMBLY_GOLDEN).unwrap();
    let c: Value = serde_json::from_str(CONTIGS_GOLDEN).unwrap();

    assert!(matches!(
        ResultDocument::from_value(p["cases"][0]["document"].clone()).unwrap(),
        ResultDocument::Profile(_)
    ));
    assert!(matches!(
        ResultDocument::from_value(a["cases"][0]["document"].clone()).unwrap(),
        ResultDocument::Assembly(_)
    ));
    assert!(matches!(
        ResultDocument::from_value(c["batches"][0].clone()).unwrap(),
        ResultDocument::Contigs(_)
    ));
}

#[test]
fn contigs_merge_zero_batches_and_browser_cap() {
    let opts = ContigsRunMergeOptions {
        skipped_short: SkippedBucket {
            contigs: 3,
            bp: 30_000,
        },
        skipped_few_kmers: SkippedBucket {
            contigs: 7,
            bp: 20_000,
        },
        min_contig_length: 20_000,
        not_classified: Some(NotClassifiedBucket {
            contigs: 140,
            bp: 3_500_000,
            limit: 100,
        }),
        fields: vec!["gtdb_taxonomy".into()],
        database: Some(json!({"name": "db.syldb", "genomes": 1, "c": 200, "k": 31})),
        ..Default::default()
    };
    let doc = merge_contigs_run(&[], &opts).unwrap();
    assert_eq!(doc.api_version, 1);
    assert_eq!(doc.table.columns.len(), 21);
    assert!(doc.table.data.is_empty());
    assert_eq!(doc.summary.contigs, 150);
    assert_eq!(doc.summary.bp, 3_550_000);
    assert_eq!(doc.summary.queried_contigs, 0);
    assert_eq!(doc.summary.queried_bp, 0);
    assert_eq!(
        doc.summary.not_classified,
        Some(NotClassifiedBucket {
            contigs: 140,
            bp: 3_500_000,
            limit: 100
        })
    );
    assert_eq!(
        render_contigs_summary(&doc),
        "per-contig: 0 of 150 contig(s), 0 bp queried; 3 contigs (30.0 kb) below 20000 bp not classified; 7 contigs (20.0 kb) with too few k-mers not classified\n"
    );
}

#[test]
fn stderr_messages_usage_note_and_format_helpers() {
    let messages = vec![
        MessageItem {
            level: "info".into(),
            title: "Info".into(),
            message: "ignored".into(),
            plain: Some("ignored".into()),
        },
        MessageItem {
            level: "warning".into(),
            title: "Slow".into(),
            message: "**bold**".into(),
            plain: Some("bold".into()),
        },
        MessageItem {
            level: "danger".into(),
            title: "Down".into(),
            message: "fallback body".into(),
            plain: None,
        },
    ];
    assert_eq!(
        render_stderr_messages(&messages),
        "sylphd: warning: Slow: bold\nsylphd: DANGER: Down: fallback body\n"
    );

    let off = UsageInfo {
        enabled: false,
        policy: None,
        window_minutes: None,
    };
    assert_eq!(render_usage_note(&off, None), None);
    let on = UsageInfo {
        enabled: true,
        policy: Some(1),
        window_minutes: Some(10),
    };
    assert_eq!(
        render_usage_note(&on, None).as_deref(),
        Some("sylphd: note: this server keeps usage statistics, see GET /usage\n")
    );
    assert_eq!(
        render_usage_note(&on, Some("https://sylphd.abrinca.com/")).as_deref(),
        Some("sylphd: note: this server keeps usage statistics, see https://sylphd.abrinca.com/usage\n")
    );

    assert_eq!(format_thousands(0), "0");
    assert_eq!(format_thousands(999), "999");
    assert_eq!(format_thousands(1_000), "1,000");
    assert_eq!(format_thousands(1_150_000), "1,150,000");

    assert_eq!(format_size(0), "0 B");
    assert_eq!(format_size(1023), "1023 B");
    assert_eq!(format_size(1024), "1.0 KiB");
    assert_eq!(format_size(1_048_576), "1.0 MiB");

    assert_eq!(format_bases(999), "999 bp");
    assert_eq!(format_bases(1_000), "1.0 kb");
    assert_eq!(format_bases(1_250_000), "1.2 Mb"); // 1.25 rounds half-to-even -> 1.2
    assert_eq!(format_bases(1_350_000), "1.4 Mb"); // 1.35 rounds half-to-even -> 1.4
}

#[test]
fn contigs_merge_preserves_non_alphabetical_order_and_combines_server_too_few_kmers() {
    let batch_json = r#"{
      "api_version": 1,
      "params": {"top": 1, "min_ani": 80.0, "consensus_ratio": 1.5, "consensus_z": 2.0, "fields": []},
      "table": {
        "columns": ["contig","contig_length","gc","status","rank","ani","shared_kmers","contig_kmers","explained_fraction","reference_ani","relation","shared_rank","ani_species_level","accession","genome_id","gtdb_species","taxon","n_close","consensus_rank","consensus_taxon"],
        "data": [
          ["z_contig", 25000, 50.0, "ok", 1, 99.0, 90, 95, 0.9474, 96.0, "same species", "species", true, "GCA_1", "GCA_1", "E. coli", "E. coli", null, "species", "E. coli"],
          ["a_contig", 21000, 45.0, "too_few_kmers", null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null]
        ]
      },
      "contigs": {
        "z_contig": {"length": 25000, "gc": 50.0, "sketch_kmers": 100, "contig_kmers": 95, "status": "ok"},
        "a_contig": {"length": 21000, "gc": 45.0, "sketch_kmers": 5, "contig_kmers": 4, "status": "too_few_kmers", "reason": "too few k-mers (5 < 10)"}
      },
      "summary": {
        "contigs": 2, "bp": 46000, "queried_contigs": 1, "queried_bp": 25000,
        "too_few_kmers_contigs": 1, "too_few_kmers_bp": 21000, "no_hit_contigs": 0,
        "consensus_bp": [{"rank": "species", "taxon": "E. coli", "bp": 25000, "fraction": 1.0}]
      },
      "caveats": [],
      "database": {"name": "db.syldb", "genomes": 1, "c": 200, "k": 31},
      "thresholds": {"species": 95.0, "imprecise_below": 85.0}
    }"#;
    let batch = ContigsDocument::from_json_str(batch_json).unwrap();
    let opts = ContigsRunMergeOptions {
        skipped_short: SkippedBucket {
            contigs: 2,
            bp: 5000,
        },
        skipped_few_kmers: SkippedBucket {
            contigs: 3,
            bp: 60000,
        },
        min_contig_length: 20000,
        ..Default::default()
    };
    let merged = merge_contigs_run(&[batch], &opts).unwrap();

    // Order preserved: z_contig before a_contig
    let keys: Vec<&str> = merged.contigs.iter().map(|(k, _)| k.as_str()).collect();
    assert_eq!(keys, vec!["z_contig", "a_contig"]);
    let serialized = serde_json::to_string(&merged).unwrap();
    assert!(
        serialized.find("\"z_contig\":{").unwrap() < serialized.find("\"a_contig\":{").unwrap()
    );

    // Server-side too_few_kmers (1, 21000) combined with client-side (3, 60000) without double-counting totals
    assert_eq!(merged.summary.contigs, 2 + 2 + 3);
    assert_eq!(merged.summary.bp, 46000 + 5000 + 60000);
    assert_eq!(
        merged.summary.skipped_few_kmers,
        Some(SkippedBucket {
            contigs: 4,
            bp: 81000
        })
    );
    assert_eq!(merged.summary.too_few_kmers_contigs, 4);
    assert_eq!(merged.summary.too_few_kmers_bp, 81000);

    // n_close null defaults to "0" on hit rows, "" on non-hit rows
    let tsv = render_contigs_tsv(&merged);
    let lines: Vec<&str> = tsv.lines().collect();
    assert_eq!(lines.len(), 3);
    let hit_cells: Vec<&str> = lines[1].split('\t').collect();
    assert_eq!(hit_cells[12], "true");
    assert_eq!(hit_cells[17], "0");
    let no_hit_cells: Vec<&str> = lines[2].split('\t').collect();
    assert_eq!(no_hit_cells[3], "too_few_kmers");
    assert!(no_hit_cells[4..].iter().all(|c| c.is_empty()));
}
