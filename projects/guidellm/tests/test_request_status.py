"""Tests for request status report: error breakdown extraction and plot creation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from projects.caliper.engine.model import BaseTestNode, UnifiedResultRecord
from projects.guidellm.postprocess.guidellm.dashboard import _extract_dashboard_metrics
from projects.guidellm.postprocess.guidellm.plotting.request_status import (
    _build_error_table_html,
    _extract_status_data,
    _normalize_error,  # noqa: F401
    _shorten_error,
    create_error_breakdown_plot,  # noqa: F401
    create_successful_requests_plot,  # noqa: F401
    create_total_errors_plot,  # noqa: F401
)

try:
    import plotly  # noqa: F401

    _has_plotly = True
except ModuleNotFoundError:
    _has_plotly = False

needs_plotly = pytest.mark.skipif(not _has_plotly, reason="plotly not installed")

_BENCHMARK_WITH_ERRORS = {
    "config": {"strategy": {"type_": "concurrent", "streams": 4}},
    "scheduler_metrics": {
        "start_time": 0.0,
        "end_time": 1.0,
        "requests_made": {"successful": 10, "errored": 3},
    },
    "metrics": {
        "requests_per_second": {"successful": {"mean": 1.0}},
        "input_tokens_per_second": {"successful": {"mean": 1.0}},
        "output_tokens_per_second": {"successful": {"mean": 1.0}},
        "request_latency": {"successful": {"median": 1.0, "percentiles": {"p95": 1.0}}},
        "time_to_first_token_ms": {"successful": {"median": 1.0, "percentiles": {"p95": 1.0}}},
        "inter_token_latency_ms": {"successful": {"median": 1.0, "percentiles": {"p95": 1.0}}},
        "time_per_output_token_ms": {"successful": {"median": 1.0, "percentiles": {"p95": 1.0}}},
    },
    "requests": {
        "successful": [{"info": {"request_id": f"s{i}"}} for i in range(10)],
        "errored": [
            {"info": {"request_id": "e0", "error": "HTTPStatusError('500 Internal Server Error')"}},
            {"info": {"request_id": "e1", "error": "HTTPStatusError('500 Internal Server Error')"}},
            {"info": {"request_id": "e2", "error": "ConnectError('Connection refused')"}},
        ],
    },
}

_BENCHMARK_NO_ERRORS = {
    "config": {"strategy": {"type_": "concurrent", "streams": 8}},
    "scheduler_metrics": {
        "start_time": 0.0,
        "end_time": 1.0,
        "requests_made": {"successful": 20, "errored": 0},
    },
    "metrics": {
        "requests_per_second": {"successful": {"mean": 2.0}},
        "input_tokens_per_second": {"successful": {"mean": 2.0}},
        "output_tokens_per_second": {"successful": {"mean": 2.0}},
        "request_latency": {"successful": {"median": 1.0, "percentiles": {"p95": 1.0}}},
        "time_to_first_token_ms": {"successful": {"median": 1.0, "percentiles": {"p95": 1.0}}},
        "inter_token_latency_ms": {"successful": {"median": 1.0, "percentiles": {"p95": 1.0}}},
        "time_per_output_token_ms": {"successful": {"median": 1.0, "percentiles": {"p95": 1.0}}},
    },
    "requests": {
        "successful": [{"info": {"request_id": f"s{i}"}} for i in range(20)],
        "errored": [],
    },
}


def _make_node(tmp_path: Path, benchmarks: list[dict]) -> BaseTestNode:
    bench_file = tmp_path / "benchmarks.json"
    payload = {"benchmarks": benchmarks, "config": {"spec": {}}}
    bench_file.write_text(json.dumps(payload), encoding="utf-8")
    return BaseTestNode(directory=tmp_path, test_labels={}, artifact_paths=[bench_file])


def test_error_breakdown_extracted(tmp_path: Path) -> None:
    node = _make_node(tmp_path, [_BENCHMARK_WITH_ERRORS])
    extra, curves = _extract_dashboard_metrics(node)

    assert "error_breakdown" in extra
    assert len(extra["error_breakdown"]) == 1
    breakdown = extra["error_breakdown"][0]
    assert breakdown["HTTPStatusError('500 Internal Server Error')"] == 2
    assert breakdown["ConnectError('Connection refused')"] == 1


def test_error_breakdown_empty_when_no_errors(tmp_path: Path) -> None:
    node = _make_node(tmp_path, [_BENCHMARK_NO_ERRORS])
    extra, curves = _extract_dashboard_metrics(node)

    assert len(extra["error_breakdown"]) == 1
    assert extra["error_breakdown"][0] == {}


def test_error_breakdown_multiple_benchmarks(tmp_path: Path) -> None:
    node = _make_node(tmp_path, [_BENCHMARK_WITH_ERRORS, _BENCHMARK_NO_ERRORS])
    extra, curves = _extract_dashboard_metrics(node)

    assert len(extra["error_breakdown"]) == 2
    assert extra["error_breakdown"][0]["HTTPStatusError('500 Internal Server Error')"] == 2
    assert extra["error_breakdown"][1] == {}


def test_error_breakdown_missing_error_field(tmp_path: Path) -> None:
    benchmark = {
        **_BENCHMARK_WITH_ERRORS,
        "requests": {
            "successful": [],
            "errored": [
                {"info": {"request_id": "e0"}},
                {"info": {"request_id": "e1"}},
            ],
        },
    }
    node = _make_node(tmp_path, [benchmark])
    extra, _ = _extract_dashboard_metrics(node)

    assert extra["error_breakdown"][0] == {"unknown": 2}


def test_shorten_error_short_string() -> None:
    assert _shorten_error("short error") == "short error"


def test_shorten_error_long_string() -> None:
    long_error = "A" * 100
    shortened = _shorten_error(long_error)
    assert len(shortened) == 80
    assert shortened.endswith("...")


def test_extract_status_data() -> None:
    record = UnifiedResultRecord(
        test_base_path="/test",
        distinguishing_labels={"facet": "default"},
        metrics={
            "performance_curves": {
                "intended_concurrency": [4, 8],
                "successful_requests": [10, 20],
            },
            "error_breakdown": [
                {"HTTPStatusError('500')": 2, "ConnectError": 1},
                {},
            ],
        },
        run_identity={"guidellm": True},
        parse_notes=[],
    )
    data = _extract_status_data([record])
    assert len(data) == 1
    assert data[0]["concurrency"] == [4, 8]
    assert data[0]["successful"] == [10, 20]
    assert data[0]["error_breakdown"][0]["HTTPStatusError(<number>)"] == 2


@needs_plotly
def test_create_successful_requests_plot() -> None:
    record = UnifiedResultRecord(
        test_base_path="/test",
        distinguishing_labels={"facet": "default"},
        metrics={
            "performance_curves": {
                "intended_concurrency": [4, 8],
                "successful_requests": [10, 20],
            },
            "error_breakdown": [{}, {}],
        },
        run_identity={"guidellm": True},
        parse_notes=[],
    )
    fig = create_successful_requests_plot([record])
    assert fig is not None
    assert len(fig.data) == 1


@needs_plotly
def test_create_successful_requests_plot_empty() -> None:
    fig = create_successful_requests_plot([])
    assert fig is None


@needs_plotly
def test_create_error_breakdown_plot() -> None:
    entry = {
        "legend_name": "test-config",
        "concurrency": [4, 8],
        "successful": [10, 20],
        "error_breakdown": [
            {"HTTPStatusError('500')": 2, "ConnectError": 1},
            {"HTTPStatusError('500')": 3},
        ],
    }
    fig = create_error_breakdown_plot(entry)
    assert fig is not None
    assert len(fig.data) == 2  # 2 error categories


@needs_plotly
def test_create_error_breakdown_plot_no_errors() -> None:
    entry = {
        "legend_name": "test-config",
        "concurrency": [4, 8],
        "successful": [10, 20],
        "error_breakdown": [{}, {}],
    }
    fig = create_error_breakdown_plot(entry)
    assert fig is None


@needs_plotly
def test_create_total_errors_plot() -> None:
    record = UnifiedResultRecord(
        test_base_path="/test",
        distinguishing_labels={"facet": "default"},
        metrics={
            "performance_curves": {
                "intended_concurrency": [4, 8],
                "successful_requests": [10, 20],
            },
            "error_breakdown": [
                {"HTTPStatusError('500')": 2, "ConnectError": 1},
                {"HTTPStatusError('500')": 3},
            ],
        },
        run_identity={"guidellm": True},
        parse_notes=[],
    )
    fig = create_total_errors_plot([record])
    assert fig is not None
    assert len(fig.data) == 1
    assert list(fig.data[0].y) == [3, 3]


@needs_plotly
def test_create_total_errors_plot_no_errors() -> None:
    record = UnifiedResultRecord(
        test_base_path="/test",
        distinguishing_labels={"facet": "default"},
        metrics={
            "performance_curves": {
                "intended_concurrency": [4, 8],
                "successful_requests": [10, 20],
            },
            "error_breakdown": [{}, {}],
        },
        run_identity={"guidellm": True},
        parse_notes=[],
    )
    fig = create_total_errors_plot([record])
    assert fig is None


def test_build_error_table_html_with_errors() -> None:
    entry = {
        "legend_name": "test-config",
        "concurrency": [4, 8],
        "successful": [10, 20],
        "error_breakdown": [
            {"HTTPStatusError('500 Internal Server Error')": 2, "ConnectError('refused')": 1},
            {"HTTPStatusError('500 Internal Server Error')": 3},
        ],
    }
    html = _build_error_table_html(entry)
    assert "<details>" in html
    assert "<table" in html
    assert "HTTPStatusError(&#x27;500 Internal Server Error&#x27;)" in html
    assert "ConnectError(&#x27;refused&#x27;)" in html
    assert "<th>Concurrency</th>" in html
    assert "<td>4</td>" in html
    assert "<td>8</td>" in html
    assert "<td>3</td>" in html
    assert "<td>2</td>" in html
    assert "<td>1</td>" in html
    assert "6 total errors" in html


def test_build_error_table_html_no_errors() -> None:
    entry = {
        "legend_name": "test-config",
        "concurrency": [4],
        "successful": [10],
        "error_breakdown": [{}],
    }
    html = _build_error_table_html(entry)
    assert html == ""


def test_normalize_error_collapses_numeric_arg() -> None:
    assert _normalize_error("LocalProtocolError('2371')") == "LocalProtocolError(<number>)"
    assert _normalize_error("LocalProtocolError('999')") == "LocalProtocolError(<number>)"


def test_normalize_error_preserves_non_numeric() -> None:
    msg = "ConnectError('Connection refused')"
    assert _normalize_error(msg) == msg


def test_normalize_error_preserves_plain_string() -> None:
    assert _normalize_error("unknown") == "unknown"


def test_extract_status_data_sorts_by_concurrency() -> None:
    record = UnifiedResultRecord(
        test_base_path="/test",
        distinguishing_labels={"facet": "default"},
        metrics={
            "performance_curves": {
                "intended_concurrency": [8, 2, 4],
                "successful_requests": [20, 5, 10],
            },
            "error_breakdown": [
                {"SomeError": 3},
                {"SomeError": 1},
                {"SomeError": 2},
            ],
        },
        run_identity={"guidellm": True},
        parse_notes=[],
    )
    data = _extract_status_data([record])
    assert data[0]["concurrency"] == [2, 4, 8]
    assert data[0]["successful"] == [5, 10, 20]
    assert data[0]["error_breakdown"][0] == {"SomeError": 1}
    assert data[0]["error_breakdown"][2] == {"SomeError": 3}


def test_extract_status_data_normalizes_errors() -> None:
    record = UnifiedResultRecord(
        test_base_path="/test",
        distinguishing_labels={"facet": "default"},
        metrics={
            "performance_curves": {
                "intended_concurrency": [4],
                "successful_requests": [10],
            },
            "error_breakdown": [
                {"LocalProtocolError('2371')": 2, "LocalProtocolError('999')": 3},
            ],
        },
        run_identity={"guidellm": True},
        parse_notes=[],
    )
    data = _extract_status_data([record])
    bd = data[0]["error_breakdown"][0]
    assert "LocalProtocolError(<number>)" in bd
    assert bd["LocalProtocolError(<number>)"] == 5
