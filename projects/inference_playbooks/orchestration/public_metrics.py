"""Versioned native-metric projection shared by the private evidence producers.

Output retains the native parser shape, but is explicitly a projection, not the
original harness report. No unbounded text, request payloads or diagnostic keys
cross this boundary. Missing values and numeric units are never synthesized.
"""

from __future__ import annotations

import hashlib
import json
import math
import re

CONTRACT = "private-metrics-v1"
NUMBER = "number"
STATS = dict.fromkeys(
    (
        "mean",
        "median",
        "min",
        "max",
        "std",
        "std_dev",
        "variance",
        "count",
        "total",
        "avg",
        "p1",
        "p5",
        "p10",
        "p25",
        "p50",
        "p75",
        "p90",
        "p95",
        "p99",
        "p999",
    ),
    NUMBER,
)
PERCENTILES = dict.fromkeys(
    ("p001", "p01", "p1", "p5", "p10", "p25", "p50", "p75", "p90", "p95", "p99", "p999"), NUMBER
)
TOTALS = dict.fromkeys(("successful", "errored", "incomplete", "total"), NUMBER)
GUIDELLM_METRICS = {
    key: {
        status: {**STATS, "percentiles": PERCENTILES}
        for status in ("successful", "errored", "total")
    }
    for key in (
        "request_latency",
        "request_latency_ms",
        "time_to_first_token_ms",
        "inter_token_latency_ms",
        "time_per_output_token_ms",
        "requests_per_second",
        "input_tokens_per_second",
        "output_tokens_per_second",
        "tokens_per_second",
        "request_concurrency",
    )
}
GUIDELLM_METRICS["request_totals"] = TOTALS
STRATEGY = {
    "streams": NUMBER,
    "max_concurrency": NUMBER,
    "rate": NUMBER,
    "type_": ("concurrent", "constant", "synchronous", "throughput", "sweep", "poisson"),
}
TOKEN_DATA = dict.fromkeys(
    ("prompt_tokens", "output_tokens", "turns", "prefix_tokens", "prefix_count"), NUMBER
)
DATA = {**TOKEN_DATA, "prefix_buckets": [TOKEN_DATA], "kind": ("synthetic_text",)}
TIMING = {"start_time": NUMBER, "end_time": NUMBER}
GUIDELLM = {
    "benchmarks": [
        {
            "metrics": GUIDELLM_METRICS,
            "config": {"strategy": STRATEGY},
            "scheduler": {"strategy": STRATEGY, "state": TIMING},
            "scheduler_metrics": {**TIMING, "requests_made": TOTALS},
            "run_stats": {"requests_made": TOTALS},
            "benchmarker": {"requests": {"attributes": {"data": DATA}}},
            "request_totals": TOTALS,
            **TIMING,
        }
    ],
    "metadata": {"guidellm_version": "version"},
    "args": {
        "data": [DATA],
        "request_type": ("chat_completions", "text_completions", "completions"),
    },
    "config": {
        "spec": {
            "data": [DATA],
            "backend": {"request_format": ("chat_completions", "text_completions", "completions")},
        }
    },
}
AIPERF_UNITS = {
    "request_count": "requests",
    "error_request_count": "requests",
    "request_throughput": "requests/sec",
    "output_token_throughput": "tokens/sec",
    "time_to_first_token": "ms",
    "inter_token_latency": "ms",
    "request_latency": "ms",
}
AIPERF = {key: {**STATS, "unit": (unit,)} for key, unit in AIPERF_UNITS.items()}


def project_native(raw: bytes, harness: str) -> dict:
    """Export only named measurements and their typed context; report omissions."""
    if harness not in ("guidellm", "aiperf"):
        raise ValueError("unsupported private metric harness")
    omitted = 0

    def select(value, shape):
        nonlocal omitted
        if value is None:
            return None
        if shape == NUMBER:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("private metric field must be a finite JSON number")
            return value
        if shape == "version":
            if not isinstance(value, str) or not re.fullmatch(
                r"[0-9]+\.[0-9]+\.[0-9]+(?:[a-z0-9.+-]*)", value
            ):
                raise ValueError("invalid public harness version")
            return value
        if isinstance(shape, tuple):
            if value not in shape:
                raise ValueError("unsupported public metric unit or mode")
            return value
        if isinstance(shape, list):
            if not isinstance(value, list):
                raise ValueError("private metric collection must be a list")
            return [select(item, shape[0]) for item in value]
        if not isinstance(value, dict):
            # GuideLLM also records synthetic data as a string. Parse only its
            # numeric parameters, never publish the free-form source string.
            if shape is DATA and isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    pairs = [part.strip().split("=", 1) for part in value.split(",")]
                    if any(len(pair) != 2 or pair[0] not in DATA for pair in pairs):
                        raise ValueError(
                            "unsupported private synthetic data specification"
                        ) from None
                    try:
                        value = {
                            key: item if key == "kind" else json.loads(item) for key, item in pairs
                        }
                    except ValueError:
                        raise ValueError("invalid private synthetic data parameter") from None
            if not isinstance(value, dict):
                raise ValueError("private metric record must be an object")
        omitted += len(value.keys() - shape.keys())
        return {key: select(value[key], child) for key, child in shape.items() if key in value}

    try:
        source = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("invalid native metric JSON") from None
    if not isinstance(source, dict):
        raise ValueError("native metric export must be an object")
    if harness == "guidellm" and not isinstance(source.get("benchmarks"), list):
        raise ValueError("native GuideLLM benchmarks missing")
    if harness == "aiperf" and "request_count" not in source:
        raise ValueError("native AIPerf request count missing")
    result = select(source, GUIDELLM if harness == "guidellm" else AIPERF)
    result["_public_evidence"] = {
        "contract": CONTRACT,
        "source_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "omitted_fields": omitted,
    }
    return result
