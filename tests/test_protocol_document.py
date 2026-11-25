"""``protocol.result_document``: the ``/query`` and ``/profile`` response, without HTTP."""

from __future__ import annotations

from sylphd.protocol import API_VERSION, ProfileParams, result_document, table_columns


def test_the_document_lays_rows_out_in_table_column_order() -> None:
    params = ProfileParams()
    columns = table_columns("profile", params, None)
    row = {column: f"v-{column}" for column in columns}
    row.pop("True_cov", None)
    row["Eff_cov"] = 3.5
    samples = [
        {"name": "a", "rows": [row], "caveats": ["note 1"]},
        {"name": "b", "rows": [], "caveats": ["note 1", "note 2"]},
    ]
    document = result_document("profile", params, None, ["a", "b", "c"], samples)
    assert document["api_version"] == API_VERSION
    assert document["params"] == params.to_dict()
    assert [sample["name"] for sample in document["samples"]] == ["a", "b", "c"]
    assert document["samples"][0]["reads"] is None  # statistics default to unknown
    assert document["table"]["columns"] == columns
    (data_row,) = document["table"]["data"]
    # a row without True_cov falls back to Eff_cov in that column
    if "True_cov" in columns:
        assert data_row[columns.index("True_cov")] == 3.5
    assert document["caveats"] == ["note 1", "note 2"]
    assert "taxprof" not in document


def test_taxprof_is_keyed_by_sample_name() -> None:
    params = ProfileParams()
    samples = [
        {"name": "a", "rows": [], "taxprof": {"gtdb": {"sylphmpa": "x"}}},
        {"name": "b", "rows": []},
    ]
    document = result_document("profile", params, None, ["a", "b"], samples)
    assert document["taxprof"] == {"a": {"gtdb": {"sylphmpa": "x"}}}


def test_sample_statistics_are_merged_in_request_order() -> None:
    info = [
        {"kmers": 10, "mean_read_length": 150.0, "paired": False, "reads": 5, "bases": 750},
        {"kmers": 20, "mean_read_length": 100.0, "paired": True, "reads": None, "bases": None},
    ]
    document = result_document("query", ProfileParams(), None, ["a", "b"], [], info)
    assert document["samples"] == [{"name": "a", **info[0]}, {"name": "b", **info[1]}]
