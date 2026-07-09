"""Data models for GuideLLM benchmark results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class GuideLLMBenchmark:
    """Single GuideLLM benchmark result with extracted metrics."""

    strategy: str
    duration: float
    warmup_time: float = 0.0
    cooldown_time: float = 0.0

    # Request metrics
    request_rate: float = 0.0
    request_concurrency: float = 0.0
    completed_requests: int = 0
    failed_requests: int = 0

    # Token metrics (per request)
    input_tokens_per_request: float = 0.0
    output_tokens_per_request: float = 0.0
    total_tokens_per_request: float = 0.0

    # Latency metrics (in seconds)
    request_latency_median: float = 0.0
    request_latency_p95: float = 0.0
    ttft_median: float = 0.0  # Time to First Token
    ttft_p95: float = 0.0
    itl_median: float = 0.0  # Inter Token Latency
    itl_p95: float = 0.0
    tpot_median: float = 0.0  # Time Per Output Token
    tpot_p95: float = 0.0
    tpot_p99: float = 0.0

    # Additional TTFT percentiles
    ttft_p10: float = 0.0
    ttft_p25: float = 0.0
    ttft_p50: float = 0.0
    ttft_p75: float = 0.0
    ttft_p90: float = 0.0
    ttft_p99: float = 0.0

    # Additional ITL percentiles
    itl_p10: float = 0.0
    itl_p25: float = 0.0
    itl_p50: float = 0.0
    itl_p75: float = 0.0
    itl_p90: float = 0.0
    itl_p99: float = 0.0

    # Throughput metrics
    tokens_per_second: float = 0.0
    input_tokens_per_second: float = 0.0
    output_tokens_per_second: float = 0.0

    # Output token percentiles
    output_tokens_per_second_p10: float = 0.0
    output_tokens_per_second_p25: float = 0.0
    output_tokens_per_second_p50: float = 0.0
    output_tokens_per_second_p75: float = 0.0
    output_tokens_per_second_p90: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for use in UnifiedResultRecord metrics."""
        return {
            "strategy": self.strategy,
            "duration": self.duration,
            "request_rate": self.request_rate,
            "request_concurrency": self.request_concurrency,
            "completed_requests": self.completed_requests,
            "failed_requests": self.failed_requests,
            "input_tokens_per_request": self.input_tokens_per_request,
            "output_tokens_per_request": self.output_tokens_per_request,
            "total_tokens_per_request": self.total_tokens_per_request,
            "request_latency_median": self.request_latency_median,
            "request_latency_p95": self.request_latency_p95,
            "ttft_median": self.ttft_median,
            "ttft_p95": self.ttft_p95,
            "itl_median": self.itl_median,
            "itl_p95": self.itl_p95,
            "tpot_median": self.tpot_median,
            "tpot_p95": self.tpot_p95,
            "tokens_per_second": self.tokens_per_second,
            "input_tokens_per_second": self.input_tokens_per_second,
            "output_tokens_per_second": self.output_tokens_per_second,
        }


@dataclass
class GuideLLMConfiguration:
    """GuideLLM configuration extracted from benchmark files."""

    args: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "args": self.args or {},
            "metadata": self.metadata or {},
        }
