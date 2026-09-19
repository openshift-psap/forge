from __future__ import annotations

import json
from pathlib import Path

from projects.caliper.engine.model import TestBaseNode
from projects.guidellm.postprocess.guidellm.dashboard import _extract_dashboard_metrics

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


def _extract(tmp_path: Path, **kwargs) -> dict:
    bench_file = tmp_path / "benchmarks.json"
    _write_payload(bench_file, **kwargs)
    node = TestBaseNode(directory=tmp_path, test_labels={}, artifact_paths=[bench_file])
    extra, _ = _extract_dashboard_metrics(node)
    return extra


def test_tokens_from_config_spec(tmp_path: Path) -> None:
    extra = _extract(
        tmp_path,
        spec={"data": [{"prompt_tokens": 2048, "output_tokens": 512}]},
    )
    assert extra["prompt_toks"] == 2048
    assert extra["output_toks"] == 512


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
