"""RHAIIS CSV dashboard export functionality."""

from __future__ import annotations

import json
import re

# RHAIIS fieldnames with RHAIIS-specific fields
RHAIIS_FIELDNAMES = [
    "run",
    "accelerator",
    "model",
    "version",
    "prompt toks",
    "output toks",
    "turn_index",
    "turns",
    "TP",
    "measured concurrency",
    "intended concurrency",
    "measured rps",
    "output_tok/sec",
    "total_tok/sec",
    "prompt_token_count_mean",
    "prompt_token_count_p99",
    "output_token_count_mean",
    "output_token_count_p99",
    "ttft_median",
    "ttft_p95",
    "ttft_p1",
    "ttft_p999",
    "tpot_median",
    "tpot_p95",
    "tpot_p99",
    "tpot_p999",
    "tpot_p1",
    "itl_median",
    "itl_p95",
    "itl_p999",
    "itl_p1",
    "request_latency_median",
    "request_latency_min",
    "request_latency_max",
    "successful_requests",
    "errored_requests",
    "uuid",
    "ttft_mean",
    "ttft_p99",
    "itl_mean",
    "itl_p99",
    "runtime_args",
    "env_vars",
    "guidellm_start_time_ms",
    "guidellm_end_time_ms",
    "image_tag",
    "guidellm_version",
    "DP",
    "dataset",
    "spec_decoding",
    "prefix_caching",
    "prefix_tokens",
    "prefix_count",
    "request_type",
    "mlflow_run_id",
    "mlflow_experiment_id",
]

_SENSITIVE_ENV_VAR_NAME = re.compile(
    r"(?:^|_)(?:ACCESS_KEY|API_KEY|AUTH|AUTHORIZATION|BEARER|CREDENTIALS?|"
    r"PASSWORD|PASSWD|PRIVATE_KEY|SECRET|TOKEN)(?:_|$)",
    re.IGNORECASE,
)


def serialize_env_vars(env_vars: dict[str, object] | None) -> str:
    """Serialize configured serving env vars, redacting secret-looking names."""
    if not env_vars:
        return ""

    safe_env_vars = {
        name: "<redacted>" if _SENSITIVE_ENV_VAR_NAME.search(name) else str(value)
        for name, value in env_vars.items()
    }
    return json.dumps(safe_env_vars, sort_keys=True, separators=(",", ":"))
