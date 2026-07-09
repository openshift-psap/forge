from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from projects.caliper.engine.model import UnifiedRunModel


class RhaiisKpiHandler:
    @staticmethod
    def get_catalog() -> list[dict[str, Any]]:
        return [
            {
                "kpi_id": "guidellm_request_rate",
                "name": "Request Rate",
                "unit": "req/s",
                "higher_is_better": True,
            },
            {
                "kpi_id": "guidellm_tokens_per_second",
                "name": "Total Token Throughput",
                "unit": "tokens/s",
                "higher_is_better": True,
            },
            {
                "kpi_id": "guidellm_input_tokens_per_second",
                "name": "Input Token Throughput",
                "unit": "tokens/s",
                "higher_is_better": True,
            },
            {
                "kpi_id": "guidellm_output_tokens_per_second",
                "name": "Output Token Throughput",
                "unit": "tokens/s",
                "higher_is_better": True,
            },
            {
                "kpi_id": "guidellm_ttft_median",
                "name": "Time to First Token (Median)",
                "unit": "s",
                "higher_is_better": False,
            },
            {
                "kpi_id": "guidellm_ttft_p95",
                "name": "Time to First Token (P95)",
                "unit": "s",
                "higher_is_better": False,
            },
            {
                "kpi_id": "guidellm_ttft_p99",
                "name": "Time to First Token (P99)",
                "unit": "s",
                "higher_is_better": False,
            },
            {
                "kpi_id": "guidellm_itl_median",
                "name": "Inter Token Latency (Median)",
                "unit": "s",
                "higher_is_better": False,
            },
            {
                "kpi_id": "guidellm_itl_p95",
                "name": "Inter Token Latency (P95)",
                "unit": "s",
                "higher_is_better": False,
            },
            {
                "kpi_id": "guidellm_itl_p99",
                "name": "Inter Token Latency (P99)",
                "unit": "s",
                "higher_is_better": False,
            },
            {
                "kpi_id": "guidellm_tpot_median",
                "name": "Time Per Output Token (Median)",
                "unit": "s",
                "higher_is_better": False,
            },
            {
                "kpi_id": "guidellm_tpot_p95",
                "name": "Time Per Output Token (P95)",
                "unit": "s",
                "higher_is_better": False,
            },
            {
                "kpi_id": "guidellm_tpot_p99",
                "name": "Time Per Output Token (P99)",
                "unit": "s",
                "higher_is_better": False,
            },
            {
                "kpi_id": "guidellm_request_latency_median",
                "name": "End-to-End Request Latency (Median)",
                "unit": "s",
                "higher_is_better": False,
            },
            {
                "kpi_id": "guidellm_request_latency_p95",
                "name": "End-to-End Request Latency (P95)",
                "unit": "s",
                "higher_is_better": False,
            },
            {
                "kpi_id": "guidellm_input_tokens_per_request",
                "name": "Input Tokens per Request",
                "unit": "tokens",
                "higher_is_better": False,
            },
            {
                "kpi_id": "guidellm_output_tokens_per_request",
                "name": "Output Tokens per Request",
                "unit": "tokens",
                "higher_is_better": False,
            },
            {
                "kpi_id": "guidellm_request_concurrency",
                "name": "Measured Request Concurrency",
                "unit": "count",
                "higher_is_better": None,
            },
            {
                "kpi_id": "guidellm_completed_requests",
                "name": "Completed Requests",
                "unit": "count",
                "higher_is_better": True,
            },
            {
                "kpi_id": "guidellm_failed_requests",
                "name": "Failed Requests",
                "unit": "count",
                "higher_is_better": False,
            },
            {
                "kpi_id": "guidellm_duration",
                "name": "Benchmark Duration",
                "unit": "s",
                "higher_is_better": None,
            },
            {
                "kpi_id": "guidellm_prompt_token_count_mean",
                "name": "Prompt Token Count (Mean)",
                "unit": "tokens",
                "higher_is_better": None,
            },
        ]

    @staticmethod
    def compute_kpis(model: UnifiedRunModel) -> list[dict[str, Any]]:
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        out: list[dict[str, Any]] = []

        for r in model.unified_result_records:
            if not r.run_identity.get("guidellm"):
                continue
            if r.metrics.get("no_benchmarks_found"):
                continue

            base_labels = {**r.distinguishing_labels}

            kpi_mappings = [
                ("guidellm_request_rate", "request_rate", "req/s", True),
                ("guidellm_tokens_per_second", "tokens_per_second", "tokens/s", True),
                ("guidellm_input_tokens_per_second", "input_tokens_per_second", "tokens/s", True),
                ("guidellm_output_tokens_per_second", "output_tokens_per_second", "tokens/s", True),
                ("guidellm_ttft_median", "ttft_median", "s", False),
                ("guidellm_ttft_p95", "ttft_p95", "s", False),
                ("guidellm_ttft_p99", "ttft_p99", "s", False),
                ("guidellm_itl_median", "itl_median", "s", False),
                ("guidellm_itl_p95", "itl_p95", "s", False),
                ("guidellm_itl_p99", "itl_p99", "s", False),
                ("guidellm_tpot_median", "tpot_median", "s", False),
                ("guidellm_tpot_p95", "tpot_p95", "s", False),
                ("guidellm_tpot_p99", "tpot_p99", "s", False),
                ("guidellm_request_latency_median", "request_latency_median", "s", False),
                ("guidellm_request_latency_p95", "request_latency_p95", "s", False),
                ("guidellm_input_tokens_per_request", "input_tokens_per_request", "tokens", False),
                (
                    "guidellm_output_tokens_per_request",
                    "output_tokens_per_request",
                    "tokens",
                    False,
                ),
                ("guidellm_request_concurrency", "request_concurrency", "count", None),
                ("guidellm_completed_requests", "completed_requests", "count", True),
                ("guidellm_failed_requests", "failed_requests", "count", False),
                ("guidellm_duration", "duration", "s", None),
                ("guidellm_prompt_token_count_mean", "prompt_token_count_mean", "tokens", None),
            ]

            for kpi_id, metric_key, unit, higher_is_better in kpi_mappings:
                raw_value = r.metrics.get(metric_key)
                try:
                    value = float(raw_value) if raw_value is not None else None
                except (TypeError, ValueError):
                    value = None

                # Skip KPIs with null values
                if value is None:
                    continue

                labels = {**base_labels}
                if higher_is_better is not None:
                    labels["higher_is_better"] = higher_is_better

                out.append(
                    {
                        "schema_version": "1",
                        "kpi_id": kpi_id,
                        "value": value,
                        "unit": unit,
                        "run_path": r.test_base_path,
                        "timestamp": ts,
                        "labels": labels,
                        "source": {
                            "test_base_path": r.test_base_path,
                            "plugin_module": model.plugin_module,
                        },
                    }
                )

        return out
