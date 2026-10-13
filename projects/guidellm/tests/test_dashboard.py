from __future__ import annotations

import json
from pathlib import Path

from projects.caliper.engine.model import BaseTestNode, ParseResult, UnifiedResultRecord
from projects.guidellm.postprocess.guidellm import dashboard
from projects.guidellm.postprocess.guidellm.dashboard import (
    _extract_dashboard_metrics,
    enrich_guidellm_parse_result,
)

_MINIMAL_BENCHMARK = {
    "config": {"strategy": {"type_": "concurrent", "streams": 8}},
    "scheduler_metrics": {"start_time": 0.0, "end_time": 1.0},
    "metrics": {
        "requests_per_second": {"successful": {"mean": 1.0}},
        "input_tokens_per_second": {"successful": {"mean": 1.0}},
        "output_tokens_per_second": {"successful": {"mean": 1.0}},
        "request_latency": {"successful": {"median": 1.0, "percentiles": {"p95": 1.0}}},
        "time_to_first_token_ms": {
            "successful": {
                "median": 1.0,
                "percentiles": dict.fromkeys(("p10", "p25", "p50", "p75", "p90", "p95"), 1.0),
            }
        },
        "inter_token_latency_ms": {
            "successful": {
                "median": 1.0,
                "percentiles": dict.fromkeys(("p10", "p25", "p50", "p75", "p90", "p95"), 1.0),
            }
        },
        "time_per_output_token_ms": {"successful": {"median": 1.0, "percentiles": {"p95": 1.0}}},
    },
}


def _write_payload(path: Path, *, spec: dict, args: dict | None = None) -> None:
    payload: dict = {"benchmarks": [_MINIMAL_BENCHMARK], "config": {"spec": spec}}
    if args is not None:
        payload["args"] = args
    path.write_text(json.dumps(payload), encoding="utf-8")


def _extract(tmp_path: Path, *, filename: str = "benchmarks.json", **kwargs) -> dict:
    bench_file = tmp_path / filename
    _write_payload(bench_file, **kwargs)
    node = BaseTestNode(directory=tmp_path, test_labels={}, artifact_paths=[bench_file])
    extra, _ = _extract_dashboard_metrics(node)
    return extra


def test_tokens_from_config_spec(tmp_path: Path) -> None:
    extra = _extract(
        tmp_path,
        spec={"data": [{"prompt_tokens": 2048, "output_tokens": 512}]},
    )
    assert extra["prompt_toks"] == 2048
    assert extra["output_toks"] == 512


def test_tokens_from_default_benchmark_filename(tmp_path: Path) -> None:
    extra = _extract(
        tmp_path,
        filename="benchmarks-default.json",
        spec={"data": [{"prompt_tokens": 2048, "output_tokens": 512}]},
    )
    assert extra["prompt_toks"] == 2048
    assert extra["output_toks"] == 512


def test_dashboard_metrics_include_job_mlflow_destination(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        dashboard,
        "_read_job_mlflow_destination",
        lambda: {"run_id": "job-run", "experiment_id": "264"},
    )

    benchmark_file = tmp_path / "benchmarks-default.json"
    _write_payload(
        benchmark_file,
        spec={"data": [{"prompt_tokens": 2048, "output_tokens": 512}]},
    )
    node = BaseTestNode(
        directory=tmp_path,
        test_labels={},
        artifact_paths=[benchmark_file],
        test_path=Path("benchmark"),
    )
    result = enrich_guidellm_parse_result(
        ParseResult(
            records=[
                UnifiedResultRecord(
                    test_base_path="benchmark",
                    distinguishing_labels={},
                    metrics={},
                    run_identity={"guidellm": True},
                )
            ]
        ),
        [node],
    )

    metrics = result.records[0].metrics
    assert metrics["mlflow_run_id"] == "job-run"
    assert metrics["mlflow_experiment_id"] == "264"


def test_request_type_from_config_spec(tmp_path: Path) -> None:
    extra = _extract(
        tmp_path,
        spec={"backend": {"request_format": "/v1/completions"}},
    )
    assert extra["request_type"] == "/v1/completions"


def test_args_preferred_over_config_spec(tmp_path: Path) -> None:
    extra = _extract(
        tmp_path,
        spec={
            "data": [{"prompt_tokens": 999, "output_tokens": 999}],
            "backend": {"request_format": "/v1/chat/completions"},
        },
        args={
            "data": [{"prompt_tokens": 128, "output_tokens": 64}],
            "request_type": "/v1/completions",
        },
    )
    assert extra["prompt_toks"] == 128
    assert extra["output_toks"] == 64
    assert extra["request_type"] == "/v1/completions"
