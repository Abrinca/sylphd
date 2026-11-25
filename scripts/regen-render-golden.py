"""Regenerate rust/sylphd-core/tests/data/*_golden.json: server documents and the Python renderer output (the spec for the Rust renderer)."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import dnaio
import pysylph
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from conftest import CLODF13_FASTA, ECOLI_PLAIN, ECOLI_TWO_STAGE, K12_PAIRED_SKETCH, K12_R1  # noqa: E402
from sylphd import cli, per_contig, per_contig_client, protocol  # noqa: E402
from sylphd.client import _assembly_doc_to_legacy, _contigs_doc_to_legacy, _profile_doc_to_samples  # noqa: E402
from sylphd.metadata import METADATA_COLUMNS  # noqa: E402
from sylphd.metadata_build import build_metadata_db  # noqa: E402
from sylphd.server import SKETCH_CONTENT_TYPE, ServerConfig, create_app  # noqa: E402

OUT_DIR = ROOT / "rust" / "sylphd-core" / "tests" / "data"
OUT_DIR.mkdir(parents=True, exist_ok=True)
WORK = Path(tempfile.mkdtemp(prefix="sylphd-render-golden-"))


def write_fixture(name: str, payload: dict[str, Any]) -> None:
    path = OUT_DIR / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")


def py_profile_tsv(doc: dict[str, Any], mode: str) -> str:
    samples = _profile_doc_to_samples(doc, mode)
    params = protocol.ProfileParams.from_mapping(doc["params"])
    base = set(protocol.header_for(mode, params, [])) | {"Lambda_status"}
    meta_cols = [c for c in doc["table"]["columns"] if c not in base]
    return protocol.rows_to_tsv(samples, mode, params=params, fields=meta_cols)


def py_assembly_tsv(doc: dict[str, Any]) -> str:
    legacy = _assembly_doc_to_legacy(doc)
    buf = io.StringIO()
    cli._write_hits_tsv(legacy, buf)
    return buf.getvalue()


def py_assembly_stderr(doc: dict[str, Any]) -> str:
    legacy = _assembly_doc_to_legacy(doc)
    lines = [cli._assignment_summary(legacy)]
    note = cli._species_note(legacy)
    if note:
        lines.append(f"sylphd: warning: {note}")
    return "\n".join(lines) + "\n"


def py_info_text(info: dict[str, Any], messages: list[dict[str, str]]) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli._print_database(info)
        cli._print_metadata(info.get("metadata"))
        cli._print_limits(info)
        cli._print_usage(info["usage"])
        cli._print_messages(messages)
    return buf.getvalue()


def generate_profile_and_query() -> None:
    cases: list[dict[str, Any]] = []

    with dnaio.open(CLODF13_FASTA) as reader:
        plasmid = "".join(r.sequence for r in reader)

    # 1. E. coli two-stage: profile, query, profile -u, profile --estimate-read-counts, empty hits
    with TestClient(create_app(ServerConfig(db_path=ECOLI_TWO_STAGE))) as client:
        k12_bytes = K12_PAIRED_SKETCH.read_bytes()
        with dnaio.open(K12_R1) as reader:
            k12_r1_seqs = [r.sequence for r in reader]
        k12_single_bytes = pysylph.Sketcher(c=200, k=31).sketch_single("k12_single", k12_r1_seqs).to_bytes()

        for case_name, mode, sketch_bytes, extra_params in [
            ("profile_k12_paired", "profile", k12_bytes, {"names": ["k12_paired"], "fields": []}),
            ("query_k12_paired", "query", k12_bytes, {"names": ["k12_paired"], "fields": []}),
            ("profile_k12_estimate_unknown", "profile", k12_bytes, {"names": ["k12_paired"], "fields": [], "estimate_unknown": True}),
            ("profile_k12_read_counts", "profile", k12_bytes, {"names": ["k12_paired"], "fields": [], "estimate_read_counts": True}),
            ("profile_empty_hits", "profile", k12_single_bytes, {"names": ["k12_single"], "fields": []}),
            ("query_empty_hits", "query", k12_single_bytes, {"names": ["k12_single"], "fields": []}),
        ]:
            resp = client.post(
                f"/{mode}",
                data={"params": json.dumps(extra_params)},
                files=[("sketch", ("s.sylsp", sketch_bytes))],
            )
            assert resp.status_code == 200, resp.text
            doc = resp.json()
            cases.append({"name": case_name, "document": doc, "expected_tsv": py_profile_tsv(doc, mode)})

    # 2. Deep plasmid (Eff_lambda = HIGH, NA-NA) + contig-like under -u (True_cov = inf, NaN, LOW)
    tmp = WORK
    tmp.mkdir(parents=True, exist_ok=True)
    genome = pysylph.Sketcher(c=200, k=31).sketch_genome("GCA_000000001.1_genomic.fna.gz", [plasmid])
    tiny_db = tmp / "tiny.syldb"
    pysylph.Database([genome]).dump(str(tiny_db))

    tsv_meta = tmp / "meta.tsv"
    header = ["accession", *METADATA_COLUMNS]
    row = ["RS_GCA_000000001.1"] + [""] * len(METADATA_COLUMNS)
    row[1 + METADATA_COLUMNS.index("gtdb_taxonomy")] = (
        "d__Bacteria;p__Pseudomonadota;c__Gammaproteobacteria;o__Enterobacterales;"
        "f__Enterobacteriaceae;g__Escherichia;s__Escherichia coli"
    )
    row[1 + METADATA_COLUMNS.index("gtdb_representative")] = "t"
    row[1 + METADATA_COLUMNS.index("gtdb_type_species_of_genus")] = "f"
    row[1 + METADATA_COLUMNS.index("ncbi_taxid")] = "562"
    row[1 + METADATA_COLUMNS.index("ncbi_organism_name")] = "Escherichia coli K-12"
    row[1 + METADATA_COLUMNS.index("checkm2_completeness")] = "100.0"
    row[1 + METADATA_COLUMNS.index("checkm2_contamination")] = "0.25"
    row[1 + METADATA_COLUMNS.index("genome_size")] = "9957"
    row[1 + METADATA_COLUMNS.index("gc_percentage")] = "54.4"
    tsv_meta.write_text("\t".join(header) + "\n" + "\t".join(row) + "\n", encoding="utf-8")
    tiny_meta = Path(build_metadata_db([tsv_meta], tmp / "meta.sqlite", release=232, force=True)["path"])

    deep_reads = [plasmid[i : i + 100] for i in range(0, len(plasmid) - 100, 3)]
    deep_sketch = pysylph.Sketcher(c=200, k=31).sketch_single("deep", deep_reads).to_bytes()
    contig_like_sketch = pysylph.Sketcher(c=200, k=31).sketch_single("contig_like", [plasmid]).to_bytes()

    with TestClient(create_app(ServerConfig(db_path=tiny_db, meta_path=tiny_meta))) as client:
        resp = client.post(
            "/profile",
            data={"params": json.dumps({"names": ["deep", "contig_like"], "estimate_unknown": True})},
            files=[
                ("sketch", ("deep.sylsp", deep_sketch)),
                ("sketch", ("contig_like.sylsp", contig_like_sketch)),
            ],
        )
        assert resp.status_code == 200, resp.text
        doc = resp.json()
        cases.append({
            "name": "profile_high_low_inf_nan_metadata",
            "document": doc,
            "expected_tsv": py_profile_tsv(doc, "profile"),
        })

    # 3. Synthetic edge cases: Adjusted_ANI > 100 capped, Naive_ANI > 100 uncapped, ties-to-even,
    #    Eff_lambda HIGH/LOW/OK/null, NA-NA, inf, NaN, bools, floats.
    synth_doc = {
        "api_version": 1,
        "params": {
            "minimum_ani": None,
            "seq_id": None,
            "estimate_unknown": True,
            "estimate_read_counts": False,
            "min_number_kmers": 10,
            "min_contain": 7,
            "screen_ani": 85.0,
            "no_adjust": False,
        },
        "samples": [{"name": "s1"}, {"name": "s2"}],
        "table": {
            "columns": [
                "Sample_file", "Genome_file", "Taxonomic_abundance", "Sequence_abundance",
                "Adjusted_ANI", "True_cov", "ANI_5-95_percentile", "Eff_lambda",
                "Lambda_5-95_percentile", "Median_cov", "Mean_cov_geq1", "Containment_ind",
                "Naive_ANI", "kmers_reassigned", "Contig_name", "Lambda_status",
                "accession", "gtdb_representative", "checkm2_completeness", "ncbi_taxid",
            ],
            "data": [
                [
                    "s1", "GCF_000005845.2_genomic.fna.gz", 100.0, 42.12555,
                    100.4, 0.3597986, [99.6437, 99.9452], 0.36,
                    [0.34, 0.38], 1.0, 1.1875, [6208, 21899],
                    100.4, 0, "NC_000913.3", "OK",
                    "GCF_000005845.2", True, 100.0, 511145,
                ],
                [
                    "s2", "GCA_000000001.1_genomic.fna.gz", None, None,
                    95.125, None, None, None,
                    None, 30.0, 31.25, [44, 44],
                    95.125, 17, "contig1", "HIGH",
                    "GCA_000000001.1", False, 99.5, None,
                ],
                [
                    "s2", "GCA_000000002.1_genomic.fna.gz", 0.0, 0.0,
                    95.135, 1.0, None, None,
                    None, 1.0, 1.0, [10, 100],
                    90.005, 3, "contig2", "LOW",
                    None, None, None, 0,
                ],
                [
                    "s2", "GCA_000000003.1_genomic.fna.gz", 12.34565, 98.76545,
                    99.789, 2.5, [98.0, 100.0], None,
                    None, 2.5, 2.5, [50, 100],
                    99.11, 0, "contig3", "OK",
                    "GCA_000000003.1", True, 0.0, 42,
                ],
            ],
        },
        "caveats": [
            "UHGV_default (viruses): 95 % ANI matches the vOTU convention",
        ],
    }
    cases.append({
        "name": "profile_synthetic_edge_cases",
        "document": synth_doc,
        "expected_tsv": py_profile_tsv(synth_doc, "profile"),
        "expected_caveats": "sylphd: warning: UHGV_default (viruses): 95 % ANI matches the vOTU convention\n",
    })

    write_fixture("profile_golden", {"cases": cases})


def generate_assembly() -> None:
    cases: list[dict[str, Any]] = []
    tmp = WORK
    tiny_db = tmp / "tiny.syldb"
    tiny_meta = tmp / "meta.sqlite"

    # 1. Real server /assembly with metadata (species assigned)
    with TestClient(create_app(ServerConfig(db_path=tiny_db, meta_path=tiny_meta, allow_sequences=True))) as client:
        resp = client.post(
            "/assembly?top=5&min_ani=80&name=clodf13",
            content=CLODF13_FASTA.read_bytes(),
            headers={"content-type": "application/gzip"},
        )
        assert resp.status_code == 200, resp.text
        doc = resp.json()
        cases.append({
            "name": "assembly_gtdb_assigned",
            "document": doc,
            "expected_tsv": py_assembly_tsv(doc),
            "expected_summary": cli._assignment_summary(_assembly_doc_to_legacy(doc)),
            "expected_note": cli._species_note(_assembly_doc_to_legacy(doc)),
            "expected_stderr": py_assembly_stderr(doc),
        })

        # 2. Real server /assembly with no hits
        resp_empty = client.post(
            "/assembly?top=5&min_ani=80&name=empty",
            content=b">a\n" + b"ACGT" * 2000 + b"\n",
            headers={"content-type": "text/plain"},
        )
        assert resp_empty.status_code == 200, resp_empty.text
        doc_empty = resp_empty.json()
        cases.append({
            "name": "assembly_no_hits",
            "document": doc_empty,
            "expected_tsv": py_assembly_tsv(doc_empty),
            "expected_summary": cli._assignment_summary(_assembly_doc_to_legacy(doc_empty)),
            "expected_note": cli._species_note(_assembly_doc_to_legacy(doc_empty)),
            "expected_stderr": py_assembly_stderr(doc_empty),
        })

    # 3. Synthetic non-GTDB assigned (with note)
    uhgv_doc = {
        "api_version": 1,
        "query": {"name": "phage", "records": 1, "length": 48502, "gc": 49.86, "sketch_kmers": 212, "input": "sketch"},
        "database": {"name": "uhgv.syldb", "genomes": 100, "c": 200, "k": 31},
        "params": {"top": 5, "min_ani": 80.0},
        "thresholds": {"species": 95.0, "imprecise_below": 85.0},
        "assignment": {
            "rank": "level 5",
            "taxon": "vOTU-052719",
            "gtdb_taxonomy": None,
            "accession": None,
            "ani": 99.12,
            "taxonomy": {"level 1": "vFAM-00013", "level 5": "vOTU-052719"},
            "taxonomy_source": "UHGV_default",
            "note": "UHGV_default (viruses): 95 % ANI matches the vOTU convention (MIUViG: 95 % ANI over 85 % of the shorter genome), but sylph's ANI ignores the aligned fraction, so a species call may be wrong",
        },
        "closest": {
            "rank": "level 5",
            "taxon": "vOTU-052719",
            "gtdb_taxonomy": None,
            "accession": None,
            "ani": 99.12,
            "taxonomy": {"level 1": "vFAM-00013", "level 5": "vOTU-052719"},
            "taxonomy_source": "UHGV_default",
            "note": "UHGV_default (viruses): 95 % ANI matches the vOTU convention (MIUViG: 95 % ANI over 85 % of the shorter genome), but sylph's ANI ignores the aligned fraction, so a species call may be wrong",
        },
        "elapsed_ms": 1.2,
        "table": {
            "columns": [
                "rank", "ani", "relation", "shared_rank", "accession", "genome_id",
                "taxon", "gtdb_species", "taxonomy_source", "ncbi_organism_name", "ncbi_taxid",
                "containment", "containment_fraction", "ani_species_level", "genome_file", "taxonomy",
            ],
            "data": [
                [
                    1, 99.12, "same level 5", "level 5", None, "UHGV-0001",
                    "vOTU-052719", None, "UHGV_default", None, None,
                    [200, 212], 0.943, True, "votus_full.fna.gz",
                    {"level 1": "vFAM-00013", "level 5": "vOTU-052719"},
                ],
            ],
        },
    }
    cases.append({
        "name": "assembly_non_gtdb_assigned_with_note",
        "document": uhgv_doc,
        "expected_tsv": py_assembly_tsv(uhgv_doc),
        "expected_summary": cli._assignment_summary(_assembly_doc_to_legacy(uhgv_doc)),
        "expected_note": cli._species_note(_assembly_doc_to_legacy(uhgv_doc)),
        "expected_stderr": py_assembly_stderr(uhgv_doc),
    })

    # 4. Synthetic closest relative (below threshold, with note + fractional threshold)
    closest_doc = {
        "api_version": 1,
        "query": {"name": "novel", "records": 1, "length": 100000, "gc": 50.0, "sketch_kmers": 400, "input": "sketch"},
        "database": {"name": "fungi.syldb", "genomes": 50, "c": 200, "k": 31},
        "params": {"top": 5, "min_ani": 80.0},
        "thresholds": {"species": 95.0, "imprecise_below": 85.0},
        "assignment": None,
        "closest": {
            "rank": "species",
            "taxon": "Candida albicans",
            "gtdb_taxonomy": None,
            "accession": "GCF_000182965.3",
            "ani": 91.4,
            "taxonomy": {"kingdom": "Fungi", "species": "Candida albicans"},
            "taxonomy_source": "FungiRefSeq-latest",
            "note": "FungiRefSeq-latest (fungi): there is no established ANI species boundary, so 95 % is a heuristic and species calls may be wrong",
        },
        "elapsed_ms": 2.0,
        "table": {
            "columns": [
                "rank", "ani", "relation", "shared_rank", "accession", "genome_id",
                "taxon", "gtdb_species", "taxonomy_source", "ncbi_organism_name", "ncbi_taxid",
                "containment", "containment_fraction", "ani_species_level", "genome_file", "taxonomy",
            ],
            "data": [
                [
                    1, 91.4, "closest relative", None, "GCF_000182965.3", "GCF_000182965.3",
                    "Candida albicans", None, "FungiRefSeq-latest", "Candida albicans SC5314", 237561,
                    [120, 400], 0.3, False, "GCF_000182965.3_genomic.fna.gz",
                    {"kingdom": "Fungi", "species": "Candida albicans"},
                ],
            ],
        },
    }
    cases.append({
        "name": "assembly_closest_relative_with_note",
        "document": closest_doc,
        "expected_tsv": py_assembly_tsv(closest_doc),
        "expected_summary": cli._assignment_summary(_assembly_doc_to_legacy(closest_doc)),
        "expected_note": cli._species_note(_assembly_doc_to_legacy(closest_doc)),
        "expected_stderr": py_assembly_stderr(closest_doc),
    })

    # 5. Synthetic assignment without taxon (fallback to accession, and without accession -> "the best hit")
    fallback_doc = {
        "api_version": 1,
        "query": {},
        "database": {},
        "params": {},
        "thresholds": {"species": 95.5, "imprecise_below": 85.0},
        "assignment": None,
        "closest": {"rank": None, "taxon": None, "accession": None, "ani": 88.0},
        "table": {
            "columns": [
                "rank", "ani", "relation", "shared_rank", "accession", "genome_id",
                "taxon", "gtdb_species", "taxonomy_source", "ncbi_organism_name", "ncbi_taxid",
            ],
            "data": [],
        },
    }
    cases.append({
        "name": "assembly_fallback_the_best_hit",
        "document": fallback_doc,
        "expected_tsv": py_assembly_tsv(fallback_doc),
        "expected_summary": cli._assignment_summary(_assembly_doc_to_legacy(fallback_doc)),
        "expected_note": cli._species_note(_assembly_doc_to_legacy(fallback_doc)),
        "expected_stderr": py_assembly_stderr(fallback_doc),
    })

    write_fixture("assembly_golden", {"cases": cases})


def generate_contigs() -> None:
    # Build two realistic batch documents matching POST /contigs wire shape,
    # run Python's PerContigRun on the exact same data, and capture expected TSV & stderr summary.
    fields = ("gtdb_taxonomy", "ncbi_organism_name", "checkm2_completeness", "tax_host")

    def make_hit(rank: int, ani: float, *, accession: str | None, taxon: str | None, mixed: bool = False) -> dict[str, Any]:
        return {
            "rank": rank,
            "ani": ani,
            "shared_kmers": 78,
            "contig_kmers": 84,
            "explained_fraction": 0.9286,
            "reference_ani": 96.16,
            "reference_containment": [78, 8413],
            "relation": "same species" if rank == 1 else "same genus",
            "shared_rank": "species" if rank == 1 else "genus",
            "ani_species_level": rank == 1,
            "genome_file": "GCF_000005845.2_genomic.fna.gz",
            "genome_id": accession or "UHGV-0001",
            "accession": accession,
            "gtdb_species": "Escherichia coli" if not mixed else None,
            "taxon": taxon,
            "gtdb_taxonomy": "d__Bacteria;p__Pseudomonadota;c__Gammaproteobacteria;o__Enterobacterales;f__Enterobacteriaceae;g__Escherichia;s__Escherichia coli" if not mixed else None,
            "ncbi_organism_name": "Escherichia coli K-12" if not mixed else None,
            "checkm2_completeness": 100.0 if rank == 1 else 99.5,
            "tax_host": None,
        }

    batch1_records = [
        {
            "name": "contig1", "id": "contig1", "index": 1,
            "length": 1954431, "gc": 50.76, "status": "ok",
            "sketch_kmers": 8413, "contig_kmers": 8413,
            "hits": [
                make_hit(1, 99.12, accession="GCA_000005845.2", taxon="Escherichia coli"),
                make_hit(2, 90.5, accession=None, taxon="Escherichia sp002"),
            ],
            "assignment": {"rank": "species", "taxon": "Escherichia coli", "accession": "GCA_000005845.2", "ani": 99.12},
            "closest": {"rank": "species", "taxon": "Escherichia coli", "accession": "GCA_000005845.2", "ani": 99.12},
            "consensus": {"rank": "family", "taxon": "Enterobacteriaceae", "n_close": 2, "species": ["Escherichia coli", "Escherichia sp002"]},
        },
        {
            "name": "contig2", "id": "contig2", "index": 2,
            "length": 40000, "gc": 44.1, "status": "ok",
            "sketch_kmers": 190, "contig_kmers": 185,
            "hits": [make_hit(1, 97.0, accession=None, taxon="Lambda", mixed=True)],
            "assignment": None,
            "closest": {"rank": None, "taxon": None, "accession": None, "ani": 97.0, "note": "UHGV_default (viruses): 95 % ANI matches the vOTU convention."},
            "consensus": {"rank": None, "taxon": None, "n_close": 2, "species": ["Lambda", "other"], "mixed_sources": True},
        },
    ]

    batch2_records = [
        {
            "name": "contig3", "id": "contig3", "index": 3,
            "length": 30000, "gc": 55.0, "status": "no_hit",
            "sketch_kmers": 150, "contig_kmers": 146,
            "hits": [],
            "assignment": None,
            "closest": None,
            "consensus": None,
        },
        {
            "name": "contig4", "id": "contig4", "index": 4,
            "length": 50000, "gc": 48.25, "status": "ok",
            "sketch_kmers": 240, "contig_kmers": 230,
            "hits": [make_hit(1, 98.5, accession=None, taxon="Lambda", mixed=True)],
            "assignment": {"rank": "level 5", "taxon": "Lambda", "accession": None, "ani": 98.5, "note": "UHGV_default (viruses): 95 % ANI matches the vOTU convention."},
            "closest": {"rank": "level 5", "taxon": "Lambda", "accession": None, "ani": 98.5, "note": "UHGV_default (viruses): 95 % ANI matches the vOTU convention."},
            "consensus": {"rank": "level 5", "taxon": "Lambda", "n_close": 0, "species": ["Lambda"]},
        },
    ]

    def batch_doc(records: list[dict[str, Any]], elapsed_ms: float) -> dict[str, Any]:
        return {
            "api_version": 1,
            "params": {"top": 2, "min_ani": 80.0, "consensus_ratio": 1.5, "consensus_z": 2.0, "fields": list(fields)},
            "table": per_contig.contigs_table(records, fields),
            "contigs": per_contig.contigs_map(records),
            "summary": per_contig.summarize(records),
            "caveats": per_contig.caveats(records),
            "database": {"name": "fixture.syldb", "genomes": 10, "c": 200, "k": 31},
            "thresholds": {"species": 95.0, "imprecise_below": 85.0},
            "elapsed_ms": elapsed_ms,
        }

    b1 = batch_doc(batch1_records, 10.3)
    b2 = batch_doc(batch2_records, 5.2)

    options = per_contig_client.PerContigOptions(
        top=2, min_ani=80.0, min_length=3000, fields=fields, consensus_ratio=1.5, consensus_z=2.0
    )
    skipped_short = per_contig_client.Skipped(contigs=3, bp=2500)
    skipped_few = per_contig_client.Skipped(contigs=2, bp=18000)
    all_records = [*batch1_records, *batch2_records]
    run = per_contig_client.PerContigRun(
        all_records,
        summaries=[b1["summary"], b2["summary"]],
        options=options,
        params=per_contig.QUERY_PARAMS,
        database=b1["database"],
        skipped_short=skipped_short,
        skipped_few_kmers=skipped_few,
    )
    tsv_buf = io.StringIO()
    run.write_tsv(tsv_buf)
    sum_buf = io.StringIO()
    run.print_summary(sum_buf)

    # Also a multi-batch >5 taxa case to test top-5 truncation + tie-breaking
    many_taxa_parts = [
        {
            "contigs": 4, "bp": 40000, "queried_contigs": 4, "queried_bp": 40000,
            "too_few_kmers_contigs": 0, "too_few_kmers_bp": 0, "no_hit_contigs": 0,
            "consensus_bp": [
                {"rank": "species", "taxon": "Beta", "bp": 10000, "fraction": 0.25},
                {"rank": "species", "taxon": "Alpha", "bp": 10000, "fraction": 0.25},
                {"rank": "genus", "taxon": "Gamma", "bp": 15000, "fraction": 0.375},
                {"rank": "family", "taxon": "Delta", "bp": 5000, "fraction": 0.125},
            ],
        },
        {
            "contigs": 4, "bp": 40000, "queried_contigs": 4, "queried_bp": 40000,
            "too_few_kmers_contigs": 0, "too_few_kmers_bp": 0, "no_hit_contigs": 0,
            "consensus_bp": [
                {"rank": "species", "taxon": "Epsilon", "bp": 10000, "fraction": 0.25},
                {"rank": "genus", "taxon": "Alpha", "bp": 10000, "fraction": 0.25},
                {"rank": "species", "taxon": "Zeta", "bp": 12000, "fraction": 0.3},
                {"rank": "species", "taxon": "Alpha", "bp": 8000, "fraction": 0.2},
            ],
        },
    ]
    merged_full = per_contig.merge_summaries(many_taxa_parts)
    merged_top5 = per_contig.finalize_summary(merged_full, top=5)

    write_fixture("contigs_golden", {
        "batches": [b1, b2],
        "merge_options": {
            "skipped_short": {"contigs": 3, "bp": 2500},
            "skipped_few_kmers": {"contigs": 2, "bp": 18000},
            "min_contig_length": 3000,
        },
        "expected_tsv": tsv_buf.getvalue(),
        "expected_summary_text": sum_buf.getvalue(),
        "expected_summary_json": run.summary(),
        "expected_caveats": run.caveats(),
        "many_taxa_parts": many_taxa_parts,
        "expected_many_taxa_full": merged_full,
        "expected_many_taxa_top5": merged_top5,
    })


def generate_info() -> None:
    cases: list[dict[str, Any]] = []

    # 1. Real single-database /info without metadata
    with TestClient(create_app(ServerConfig(db_path=ECOLI_TWO_STAGE))) as client:
        info = client.get("/info").json()
        cases.append({
            "name": "info_single_db_minimal",
            "info": info,
            "messages": [],
            "expected_text": py_info_text(info, []),
        })

    # 2. Rich multi-database /info with metadata, taxonomy sources, ncbi + ncbi_nearest,
    #    overlaps, rate_limit, allow_sequences, max_queue, usage on, and messages
    rich_info = {
        "api_version": 1,
        "service": "sylphd",
        "version": "0.7.0",
        "pysylph_version": "0.3.0",
        "python_version": "3.14.0",
        "database": {
            "name": "gtdb, uhgv",
            "size": 1234567890,
            "sha256": None,
            "genomes": 250000,
            "c": 200,
            "k": 31,
            "screen_c": None,
            "two_stage": False,
        },
        "databases": [
            {
                "name": "gtdb", "file": "gtdb-r232-c200-dbv2.syl2db", "kind": "two-stage",
                "size": 1000000000, "sha256": "abc123", "genomes": 199937, "c": 200, "k": 31,
                "screen_c": 1000, "two_stage": True, "gtdb": True,
            },
            {
                "name": "uhgv", "file": "uhgv.syldb", "kind": "plain",
                "size": 234567890, "sha256": None, "genomes": 50063, "c": 200, "k": 31,
                "screen_c": None, "two_stage": False, "gtdb": False,
            },
        ],
        "metadata": {
            "file": "gtdb-r232.sqlite",
            "size": 52428800,
            "columns": ["gtdb_taxonomy", "ncbi_taxid", "tax_source", "tax_lineage"],
            "rows": 596859,
            "release": 232,
            "built": "2026-01-15T12:00:00Z",
            "format_version": "3",
            "reps_only": False,
            "taxonomy_sources": [
                {
                    "name": "GTDB_r232",
                    "kind": "metadata",
                    "keys": 596859,
                    "has_host": False,
                    "md5": None,
                    "ncbi_nearest": {
                        "keys": 1200,
                        "assigned": 1150,
                        "informal": 0,
                        "ranks": {"species": 800, "genus": 300, "family": 50},
                    },
                },
                {
                    "name": "UHGV_default",
                    "kind": "sylphtax",
                    "keys": 168536,
                    "has_host": True,
                    "md5": "deadbeef1234",
                    "ncbi": {
                        "mapped": 1000,
                        "no_accession": 200,
                        "not_in_summary": 50,
                        "no_lineage": 10,
                        "gtdb": 5,
                    },
                    "ncbi_nearest": {
                        "keys": 3,
                        "assigned": 3,
                        "informal": 1,
                        "ranks": {"domain": 1, "species": 2},
                    },
                },
                {
                    "name": "Custom",
                    "kind": "sylphtax",
                    "keys": None,
                    "has_host": False,
                    "ncbi_nearest": {
                        "keys": 10,
                        "assigned": 2,
                        "informal": 2,
                        "ranks": {},
                    },
                },
            ],
            "taxonomy_overlaps": [
                {
                    "sources": ["GTDB_r232", "UHGV_default"],
                    "keys": 1234,
                    "examples": ["GCF_000005845.2", "GCA_000001405.1"],
                },
            ],
        },
        "limits": {
            "max_jobs": 4,
            "max_upload_mb": 200,
            "max_sketches": 4096,
            "max_contigs": 100,
            "max_params_bytes": 1048576,
            "allow_sequences": True,
            "max_sequence_mb": 20,
            "rate_limit": {"count": 10, "window_seconds": 60.0},
            "max_queue": 16,
        },
        "usage": {"enabled": True, "policy": 1, "window_minutes": 10},
        "endpoints": ["/health", "/info", "/messages", "/profile", "/query", "/assembly", "/contigs", "/usage"],
    }
    messages = [
        {"level": "info", "title": "Maintenance", "message": "Planned **Sunday**.", "plain": "Planned Sunday."},
        {"level": "warning", "title": "Slow disk", "message": "Expect [delays](https://example.org).", "plain": "Expect delays (https://example.org)."},
        {"level": "danger", "title": "Outage", "message": "Down soon", "plain": "Down soon"},
    ]
    cases.append({
        "name": "info_multi_db_full",
        "info": rich_info,
        "messages": messages,
        "expected_text": py_info_text(rich_info, messages),
    })

    write_fixture("info_golden", {"cases": cases})


if __name__ == "__main__":
    generate_profile_and_query()
    generate_assembly()
    generate_contigs()
    generate_info()
