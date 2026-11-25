//! Tests for per-contig batch planning (mirroring the chunking/batching tests
//! in `tests/test_assembly_per_contig.py`).

use sylphd_core::batch::{
    chunk_size_for, contig_json_size, contigs_params_json, params_budget_for, plan_contig_batches,
    BatchError, BatchLimits, ContigBatchPlanner, ContigSpec, ContigsRequestOptions,
    DEFAULT_MAX_CONTIGS, DEFAULT_PARAMS_BUDGET, PART_OVERHEAD, REQUEST_OVERHEAD,
};

fn spec(name: impl Into<String>, contig_kmers: u64, length: u64, gc: f64) -> ContigSpec {
    ContigSpec {
        name: name.into(),
        contig_kmers,
        length,
        gc,
    }
}

#[test]
fn chunk_size_and_params_budget_follow_server_limits() {
    assert_eq!(chunk_size_for(None), DEFAULT_MAX_CONTIGS);
    assert_eq!(chunk_size_for(Some(0)), DEFAULT_MAX_CONTIGS);
    assert_eq!(chunk_size_for(Some(500)), 500);
    assert_eq!(chunk_size_for(Some(1)), 1);

    assert_eq!(params_budget_for(None), DEFAULT_PARAMS_BUDGET);
    assert_eq!(params_budget_for(Some(100 * 1024)), 90 * 1024);
    assert_eq!(params_budget_for(Some(8 * 1024 * 1024)), DEFAULT_PARAMS_BUDGET);
    assert_eq!(params_budget_for(Some(500)), 1024);
}

#[test]
fn batches_by_max_contigs_two_two_one() {
    let items: Vec<_> = (0..5)
        .map(|i| (spec(format!("c{i}"), 100, 20_000, 50.0), 1200usize, i))
        .collect();
    let limits = BatchLimits::from_server_limits(Some(2), None, None);
    let options = ContigsRequestOptions::default();

    let batches = plan_contig_batches(items, limits, &options).unwrap();
    let sizes: Vec<usize> = batches.iter().map(|b| b.items.len()).collect();
    assert_eq!(sizes, vec![2, 2, 1]);
    let names: Vec<&str> = batches
        .iter()
        .flat_map(|b| b.items.iter().map(|i| i.spec.name.as_str()))
        .collect();
    assert_eq!(names, vec!["c0", "c1", "c2", "c3", "c4"]);
}

#[test]
fn max_contigs_one_flushes_every_item() {
    let limits = BatchLimits::from_server_limits(Some(1), None, None);
    let options = ContigsRequestOptions::default();
    let mut planner = ContigBatchPlanner::new(limits, &options);

    let b0 = planner.push(spec("a", 50, 20_000, 50.0), 500, 0).unwrap();
    assert_eq!(b0.unwrap().items.len(), 1);
    assert_eq!(planner.current_len(), 0);

    let b1 = planner.push(spec("b", 50, 20_000, 50.0), 500, 1).unwrap();
    assert_eq!(b1.unwrap().items.len(), 1);
    assert!(planner.finish().is_none());
}

#[test]
fn params_bytes_is_exact_including_utf8_and_json_escapes() {
    let specs = vec![
        spec("é".repeat(100), 12, 3000, 51.25),
        spec("plain\"quote\\slash", 7, 2000, 0.0),
        spec("x".repeat(1000), 100, 25_000, 49.5),
    ];
    let options = ContigsRequestOptions {
        fields: vec!["gtdb_taxonomy".into()],
        ..Default::default()
    };
    let mut planner = ContigBatchPlanner::new(BatchLimits::default(), &options);
    for (idx, s) in specs.iter().enumerate() {
        assert!(planner.push(s.clone(), 0, idx).unwrap().is_none());
    }
    assert_eq!(
        planner.current_params_bytes(),
        contigs_params_json(&specs, &options).len()
    );
    let batch = planner.finish().unwrap();
    assert_eq!(batch.params_bytes, batch.params_json(&options).len());
    assert_eq!(
        contig_json_size(&specs[0]),
        serde_json::to_string(&specs[0]).unwrap().len()
    );
}

#[test]
fn long_names_split_batches_below_params_budget() {
    // 1000 contigs with ~1 KiB names exceed the 900 KiB params budget -> multiple batches
    let items: Vec<_> = (0..1000)
        .map(|i| {
            (
                spec(format!("c{i}_{}", "x".repeat(1000)), 50, 20_000, 50.0),
                100usize,
                i,
            )
        })
        .collect();
    let limits = BatchLimits::default();
    let options = ContigsRequestOptions::default();

    let batches = plan_contig_batches(items, limits, &options).unwrap();
    assert!(batches.len() > 1);
    let mut total = 0;
    for batch in &batches {
        let rendered = batch.params_json(&options);
        assert_eq!(batch.params_bytes, rendered.len());
        assert!(batch.params_bytes <= DEFAULT_PARAMS_BUDGET);
        total += batch.items.len();
    }
    assert_eq!(total, 1000);
}

#[test]
fn upload_limit_splits_batches_using_exact_sketch_sizes_and_overhead() {
    // 220 x 6 KiB sketches against a 1 MB upload limit -> split across multiple requests
    let items: Vec<_> = (0..220)
        .map(|i| (spec(format!("c{i}"), 300, 10_000, 52.0), 6_000usize, i))
        .collect();
    let limits = BatchLimits::from_server_limits(None, None, Some(1));
    let options = ContigsRequestOptions::default();

    let batches = plan_contig_batches(items, limits, &options).unwrap();
    assert!(batches.len() > 1);
    let mut seen = Vec::new();
    for batch in &batches {
        assert!(batch.estimated_upload_bytes <= 1024 * 1024);
        let expected_upload = batch.params_bytes
            + batch.payload_bytes
            + batch.items.iter().map(|i| i.spec.name.len()).sum::<usize>()
            + batch.items.len() * PART_OVERHEAD
            + REQUEST_OVERHEAD;
        assert_eq!(batch.estimated_upload_bytes, expected_upload);
        seen.extend(batch.items.iter().map(|i| i.item));
    }
    assert_eq!(seen, (0..220).collect::<Vec<_>>());
}

#[test]
fn single_oversized_contig_is_rejected_immediately() {
    // 1.8 MB sketch against a 1 MB upload limit -> UploadTooLarge
    let limits = BatchLimits::from_server_limits(None, None, Some(1));
    let options = ContigsRequestOptions::default();
    let mut planner = ContigBatchPlanner::<()>::new(limits, &options);
    let err = planner
        .push(spec("huge", 150_000, 3_000_000, 50.0), 1_800_000, ())
        .unwrap_err();
    match &err {
        BatchError::UploadTooLarge { name, size, limit } => {
            assert_eq!(name, "huge");
            assert!(*size > 1_800_000);
            assert_eq!(*limit, 1024 * 1024);
        }
        other => panic!("expected UploadTooLarge, got {other:?}"),
    }
    let msg = err.to_string();
    assert!(msg.contains("huge"), "{msg}");
    assert!(msg.contains("1 MB upload limit"), "{msg}");

    // Single contig whose params JSON exceeds a tiny params_budget -> ParamsTooLarge
    let tiny = BatchLimits {
        max_contigs: 100,
        params_budget: 200,
        max_upload_bytes: 0,
    };
    let mut planner = ContigBatchPlanner::<()>::new(tiny, &options);
    let err = planner
        .push(spec("x".repeat(300), 10, 20_000, 50.0), 100, ())
        .unwrap_err();
    assert!(matches!(err, BatchError::ParamsTooLarge { .. }), "{err:?}");
}
