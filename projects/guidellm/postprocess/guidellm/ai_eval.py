"""AI evaluation payload builder for GuideLLM benchmarks."""

from __future__ import annotations

from typing import Any

from projects.caliper.engine.model import UnifiedRunModel


class GuideLLMAIEvaluator:
    """Handles AI evaluation payload generation for GuideLLM benchmark results."""

    def __init__(self):
        """Initialize the AI evaluator."""
        self.schema_version = "1"

    def build_payload(self, model: UnifiedRunModel, plugin=None) -> dict[str, Any]:
        """Build AI evaluation payload from the unified model.

        Args:
            model: Unified model containing benchmark results
            plugin: Plugin instance to get artifact files per test

        Returns:
            Dictionary containing structured AI evaluation data with:
            - schema_version: Version of the payload format
            - run_id: Identifier for the benchmark run
            - test_entries: List of individual test entries (each with their own artifact_files)
        """
        from pathlib import Path

        test_entries = []
        base_dir = Path(model.base_directory)

        for idx, record in enumerate(model.unified_result_records):
            if (
                record.run_identity.get("guidellm")
                and not record.metrics.get("no_benchmarks_found")
                and record.metrics.get("performance_curves")
            ):
                # Get artifact files specific to this test directory only
                test_dir = base_dir / record.test_base_path
                if plugin and hasattr(plugin, "get_ai_data_artifact_files_for_test"):
                    test_relative_files = plugin.get_ai_data_artifact_files_for_test(test_dir)
                    # Convert test-relative paths to base-relative paths for artifact copying
                    relevant_artifact_files = [
                        str(Path(record.test_base_path) / test_relative_file)
                        for test_relative_file in test_relative_files
                    ]
                else:
                    # Fallback if no plugin provided
                    relevant_artifact_files = []

                curves = record.metrics.get("performance_curves", {})

                # Extract peak performance metrics from curves
                max_request_rate = max(curves.get("request_rate", [0.0]))
                max_tokens_per_second = max(curves.get("tokens_per_second", [0.0]))
                min_ttft_median = min(
                    [x for x in curves.get("ttft_median", [0.0]) if x > 0], default=0.0
                )
                min_itl_median = min(
                    [x for x in curves.get("itl_median", [0.0]) if x > 0], default=0.0
                )
                min_request_latency_p95 = min(
                    [x for x in curves.get("request_latency_p95", [0.0]) if x > 0], default=0.0
                )

                strategy_info = {
                    "strategy": record.metrics.get("strategy", "unknown"),
                    "concurrency": record.metrics.get("request_concurrency", 1.0),
                    "max_request_rate": max_request_rate,
                    "max_tokens_per_second": max_tokens_per_second,
                    "best_ttft_median": min_ttft_median,
                    "best_itl_median": min_itl_median,
                    "best_request_latency_p95": min_request_latency_p95,
                    "rate_points": len(curves.get("request_rate", [])),
                }

                # Create test entry
                test_entry = {
                    "test_id": f"test_entry_{idx:03d}",
                    "test_base_path": record.test_base_path,
                    "distinguishing_labels": record.distinguishing_labels,
                    "metrics": strategy_info,
                    "performance_curves": {
                        "request_rate": curves.get("request_rate", []),
                        "tokens_per_second": curves.get("tokens_per_second", []),
                        "ttft_median": curves.get("ttft_median", []),
                        "ttft_p95": curves.get("ttft_p95", []),
                        "ttft_p99": curves.get("ttft_p99", []),
                        "tpot_median": curves.get("tpot_median", []),
                        "tpot_p95": curves.get("tpot_p95", []),
                        "tpot_p99": curves.get("tpot_p99", []),
                    },
                    "artifact_files": relevant_artifact_files,
                }
                test_entries.append(test_entry)

        return {
            "schema_version": self.schema_version,
            "run_id": str(model.base_directory),
            "test_entries": test_entries,
        }

    def get_schema_version(self) -> str:
        """Get the current schema version for AI evaluation payloads."""
        return self.schema_version

    def validate_payload(self, payload: dict[str, Any]) -> bool:
        """Validate that a payload has the expected structure.

        Args:
            payload: AI evaluation payload to validate

        Returns:
            True if payload structure is valid, False otherwise
        """
        required_keys = {"schema_version", "run_id", "test_entries"}
        if not all(key in payload for key in required_keys):
            return False

        # Validate test entries structure
        test_entries = payload.get("test_entries", [])
        if not isinstance(test_entries, list):
            return False

        for entry in test_entries:
            required_entry_keys = {
                "test_id",
                "test_base_path",
                "distinguishing_labels",
                "metrics",
                "performance_curves",
                "artifact_files",
            }
            if not all(key in entry for key in required_entry_keys):
                return False

        return True
