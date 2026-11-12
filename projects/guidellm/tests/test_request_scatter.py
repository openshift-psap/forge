"""Tests for request scatter report: token count extraction and token scatter plot."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from projects.caliper.engine.model import BaseTestNode
from projects.guidellm.postprocess.guidellm.dashboard import (
    _extract_dashboard_metrics,
    _merge_replica_performance_curves,
)
from projects.guidellm.postprocess.guidellm.plotting.request_scatter import (
    create_token_scatter_plot,
)

try:
    import plotly  # noqa: F401

    _has_plotly = True
except ModuleNotFoundError:
    _has_plotly = False

needs_plotly = pytest.mark.skipif(not _has_plotly, reason="plotly not installed")

_BENCHMARK_WITH_TOKENS = {
    "config": {"strategy": {"type_": "concurrent", "streams": 4}},
    "scheduler_metrics": {
        "start_time": 100.0,
        "end_time": 200.0,
        "requests_made": {"successful": 2, "errored": 0},
    },
    "metrics": {},
    "requests": {
        "successful": [
            {
                "request_start_time": 101.0,
                "request_latency": 1.5,
                "prompt_tokens": 235,
                "output_tokens": 128,
            },
            {
                "request_start_time": 102.0,
                "request_latency": 2.5,
                "prompt_tokens": 300,
                "output_tokens": 64,
            },
        ],
        "errored": [],
    },
}


def _make_node(tmp_path: Path, benchmarks: list[dict]) -> BaseTestNode:
    bench_file = tmp_path / "benchmarks.json"
    payload = {"benchmarks": benchmarks, "config": {"spec": {}}}
    bench_file.write_text(json.dumps(payload), encoding="utf-8")
    return BaseTestNode(directory=tmp_path, test_labels={}, artifact_paths=[bench_file])


def test_request_scatter_tokens_extracted(tmp_path: Path) -> None:
    node = _make_node(tmp_path, [_BENCHMARK_WITH_TOKENS])
    extra, _curves = _extract_dashboard_metrics(node)

    scatter = extra["request_scatter"]
    assert len(scatter) == 1
    point = scatter[0]
    assert point["start_times"] == [1.0, 2.0]
    assert point["latencies"] == [1.5, 2.5]
    assert point["output_tokens"] == [128, 64]
    assert point["prompt_tokens"] == [235, 300]


def test_request_scatter_tokens_skipped_for_incomplete_requests(tmp_path: Path) -> None:
    benchmark = {
        **_BENCHMARK_WITH_TOKENS,
        "requests": {
            "successful": _BENCHMARK_WITH_TOKENS["requests"]["successful"]
            + [{"request_start_time": 103.0}],  # no latency: whole request skipped
            "errored": [],
        },
    }
    node = _make_node(tmp_path, [benchmark])
    extra, _curves = _extract_dashboard_metrics(node)

    point = extra["request_scatter"][0]
    assert len(point["start_times"]) == 2
    assert point["output_tokens"] == [128, 64]
    assert point["prompt_tokens"] == [235, 300]


def test_merge_replica_scatter_concatenates_tokens() -> None:
    curves = {"intended_concurrency": [4, 4]}
    scatter = [
        {
            "concurrency": 4,
            "start_times": [1.0, 2.0],
            "latencies": [1.5, 2.5],
            "output_tokens": [128, 64],
            "prompt_tokens": [235, 300],
        },
        {
            "concurrency": 4,
            "start_times": [3.0],
            "latencies": [0.5],
            "output_tokens": [32],
            "prompt_tokens": [100],
        },
    ]

    _merged_curves, _breakdown, merged_scatter = _merge_replica_performance_curves(
        curves, None, scatter
    )

    assert merged_scatter is not None
    assert len(merged_scatter) == 1
    point = merged_scatter[0]
    assert point["concurrency"] == 8
    assert point["start_times"] == [1.0, 2.0, 3.0]
    assert point["latencies"] == [1.5, 2.5, 0.5]
    assert point["output_tokens"] == [128, 64, 32]
    assert point["prompt_tokens"] == [235, 300, 100]


def test_merge_replica_scatter_drops_misaligned_tokens() -> None:
    curves = {"intended_concurrency": [4, 4]}
    scatter = [
        {
            "concurrency": 4,
            "start_times": [1.0, 2.0],
            "latencies": [1.5, 2.5],
            "output_tokens": [128, 64],
            "prompt_tokens": [235, 300],
        },
        # replica without token data: token counts must be dropped, not misaligned
        {
            "concurrency": 4,
            "start_times": [3.0],
            "latencies": [0.5],
        },
    ]

    _merged_curves, _breakdown, merged_scatter = _merge_replica_performance_curves(
        curves, None, scatter
    )

    assert merged_scatter is not None
    point = merged_scatter[0]
    assert point["start_times"] == [1.0, 2.0, 3.0]
    assert point["latencies"] == [1.5, 2.5, 0.5]
    assert "output_tokens" not in point
    assert "prompt_tokens" not in point


@needs_plotly
def test_token_scatter_plot_created() -> None:
    entry = {
        "legend_name": "test-config",
        "scatter": [
            {
                "concurrency": 4,
                "start_times": [1.0, 2.0, 3.0],
                "latencies": [1.5, 2.5, 0.5],
                "output_tokens": [128, 64, None],
                "prompt_tokens": [235, 300, 999],
            }
        ],
    }

    fig = create_token_scatter_plot(entry, " (test)")

    assert fig is not None
    assert len(fig.data) == 1
    trace = fig.data[0]
    assert list(trace.x) == [1.0, 2.0]
    assert list(trace.y) == [128, 64]
    assert list(trace.customdata) == [235, 300]
    assert "Output Tokens Per Response" in fig.layout.title.text


@needs_plotly
def test_token_scatter_plot_skipped_without_tokens() -> None:
    entry = {
        "legend_name": "test-config",
        "scatter": [
            {
                "concurrency": 4,
                "start_times": [1.0, 2.0],
                "latencies": [1.5, 2.5],
            }
        ],
    }

    assert create_token_scatter_plot(entry) is None
