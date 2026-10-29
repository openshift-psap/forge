"""Explicit sweep identity must govern curve membership, never matching metric IDs."""

from copy import deepcopy

import pytest

from projects.caliper.engine.kpi.html_generator import HTMLGenerator
from projects.caliper.engine.kpi.sweeps import build_sweep_report_tests


def _record(sweep_id: str | None, concurrency: int, value: float) -> dict:
    labels = {
        "benchmark_tool": "aiperf",
        "benchmark_key": "agentx",
        "model_name": "test/model",
        "deployment": "serving-a",
        "concurrency": str(concurrency),
        "run_label": f"concurrency-{concurrency}",
    }
    if sweep_id is not None:
        labels["sweep_id"] = sweep_id
    return {
        "run_id": f"{sweep_id}/c{concurrency}",
        "labels": labels,
        "kpis": [{"kpi_id": "throughput_avg", "value": value, "unit": "requests/sec"}],
    }


def _compute(records: list[dict]):
    original = deepcopy(records)
    groups = build_sweep_report_tests(records)
    assert records == original
    return groups


def test_interleaved_sweeps_are_separate_sorted_curves():
    records = [_record("a", 16, 30), _record("b", 8, 90), _record("a", 4, 10), _record("b", 4, 70)]
    groups = _compute(records)
    curves = {group["labels"]["sweep_id"]: group for group in groups}
    assert len(curves) == 2
    assert curves["a"]["kpis"][0]["values"] == [[4.0, 10], [16.0, 30]]
    assert curves["b"]["kpis"][0]["values"] == [[4.0, 70], [8.0, 90]]
    for sweep_id, group in curves.items():
        assert group["run_id"] == f"sweep/{sweep_id}"
        assert group["labels"]["model_name"] == "test/model"
        assert group["labels"]["deployment"] == "serving-a"
        assert "concurrency" not in group["labels"]
        assert "run_label" not in group["labels"]


def test_ungrouped_runs_are_not_inferred_to_be_a_sweep():
    assert _compute([_record(None, 4, 10), _record(None, 8, 20)]) == []


@pytest.mark.parametrize("key", ["model_name", "benchmark_key", "deployment", "product_version"])
def test_conflicting_sweep_conditions_raise(key):
    first, second = _record("a", 4, 10), _record("a", 8, 20)
    second["labels"][key] = "different"
    with pytest.raises(ValueError, match="Conflicting.*sweep"):
        _compute([first, second])


def test_duplicate_concurrency_is_not_averaged_or_connected():
    first = _record("a", 4, 10)
    second = _record("a", 4, 20)
    second["run_id"] = "retry/c4"
    with pytest.raises(ValueError, match="Duplicate concurrency"):
        _compute([first, second])


def test_group_requires_concurrency():
    record = _record("a", 4, 10)
    del record["labels"]["concurrency"]
    with pytest.raises(ValueError, match="concurrency"):
        _compute([record])


@pytest.mark.parametrize("sweep_id", ["", 42, True])
def test_invalid_sweep_identity_raises(sweep_id):
    record = _record("a", 4, 10)
    record["labels"]["sweep_id"] = sweep_id
    with pytest.raises(ValueError, match="sweep_id"):
        _compute([record])


def test_missing_metric_does_not_create_a_zero_point():
    first, second = _record("a", 4, 10), _record("a", 8, 20)
    second["kpis"][0]["value"] = None
    groups = _compute([first, second])
    assert groups[0]["kpis"][0]["values"] == [[4.0, 10]]


@pytest.mark.parametrize("concurrency", ["invalid", "0", "-1", "nan", "inf"])
def test_invalid_curve_concurrency_raises(concurrency):
    record = _record("a", 4, 10)
    record["labels"]["concurrency"] = concurrency
    with pytest.raises(ValueError, match="concurrency"):
        _compute([record])


def test_conflicting_units_raise():
    first, second = _record("a", 4, 10), _record("a", 8, 20)
    second["kpis"][0]["unit"] = "tokens/sec"
    with pytest.raises(ValueError, match="Conflicting units"):
        _compute([first, second])


@pytest.mark.parametrize("flat", [False, True])
def test_html_only_series_preserve_kpis_and_test_counts(tmp_path, flat):
    tests = [_record("a", 8, 20), _record("a", 4, 10), _record("b", 4, 30)]
    data = {"schema_version": "2", "tests": tests}
    if flat:
        data = [
            {**row, "run_id": test["run_id"], "labels": test["labels"]}
            for test in tests
            for row in test["kpis"]
        ]
    original = deepcopy(data)
    output = tmp_path / "kpis.html"
    HTMLGenerator().generate_kpi_html(data, output)
    assert data == original
    html = output.read_text()
    assert "Performance Metrics Report - 3 test(s), 3 KPI(s)" in html
    assert "const rawData = [[4.0, 10], [8.0, 20]];" in html
    assert "const rawData = [[4.0, 30]];" in html
    assert html.count("name: 'sweep/a'") == 1
    assert html.count("name: 'sweep/b'") == 1
    assert "Test Run: sweep/" not in html
