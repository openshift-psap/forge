from __future__ import annotations

import json
from pathlib import Path

from projects.caliper.engine.model import BaseTestNode
from projects.guidellm.postprocess.guidellm.parsing.parsers import GuideLLMParser


def _write_benchmark_file(path: Path, streams: int, request_rate: float | None = None) -> None:
    # Use provided request_rate or default to 1.5 * streams for variation
    effective_rate = request_rate if request_rate is not None else 1.5 * streams

    # Create different token rates based on streams for ordering verification
    input_tokens_rate = 2.0 + streams  # e.g., 10, 18, 34 for streams 8, 16, 32
    output_tokens_rate = 1.0 + streams  # e.g., 9, 17, 33 for streams 8, 16, 32

    payload = {
        "args": {"rate": streams},
        "metadata": {"label": f"rate-{streams}"},
        "benchmarks": [
            {
                "config": {"strategy": {"type_": "concurrent", "streams": streams}},
                "scheduler": {"state": {"start_time": 0, "end_time": 10}},
                "metrics": {
                    "requests_per_second": {"successful": {"mean": effective_rate}},
                    "input_tokens_per_second": {"successful": {"mean": input_tokens_rate}},
                    "output_tokens_per_second": {"successful": {"mean": output_tokens_rate}},
                    "request_latency": {
                        "successful": {"median": 100.0, "percentiles": {"p95": 120.0}}
                    },
                    "time_to_first_token_ms": {
                        "successful": {
                            "median": 10.0,
                            "percentiles": {
                                "p10": 8.0,
                                "p25": 9.0,
                                "p50": 10.0,
                                "p75": 11.0,
                                "p90": 12.0,
                                "p95": 13.0,
                            },
                        }
                    },
                    "inter_token_latency_ms": {
                        "successful": {
                            "median": 5.0,
                            "percentiles": {
                                "p10": 4.0,
                                "p25": 4.5,
                                "p50": 5.0,
                                "p75": 5.5,
                                "p90": 6.0,
                                "p95": 6.5,
                            },
                        }
                    },
                    "time_per_output_token_ms": {
                        "successful": {"median": 7.0, "percentiles": {"p95": 8.0}}
                    },
                },
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_parser_accepts_rate_split_benchmark_files(tmp_path: Path) -> None:
    test_dir = tmp_path / "run"
    test_dir.mkdir()
    file_a = test_dir / "benchmarks-rate-32.json"
    file_b = test_dir / "benchmarks-rate-64.json"
    _write_benchmark_file(file_a, 32)
    _write_benchmark_file(file_b, 64)

    parser = GuideLLMParser()
    node = BaseTestNode(
        directory=test_dir,
        test_labels={"labels": {"guidellm_loadshape": "multi-turn"}},
        artifact_paths=[file_b, file_a],
    )

    result = parser.parse([node])

    assert result.warnings == []
    assert len(result.records) == 1

    # Check that both rate configurations are present in the performance curves
    record = result.records[0]
    curves = record.metrics["performance_curves"]
    assert curves["intended_concurrency"] == [32, 64]
    assert record.run_identity == {"guidellm": True}


def test_parser_orders_by_intended_concurrency_not_request_rate(tmp_path: Path) -> None:
    """Test that benchmarks are ordered by intended_concurrency even when request_rate order differs."""
    test_dir = tmp_path / "run"
    test_dir.mkdir()

    # Create benchmarks where request_rate order differs from intended_concurrency order
    # intended_concurrency: 8, 16, 32 (ascending)
    # request_rate: 100, 50, 75 (not ascending)
    file_a = test_dir / "benchmarks-rate-8.json"
    file_b = test_dir / "benchmarks-rate-16.json"
    file_c = test_dir / "benchmarks-rate-32.json"

    _write_benchmark_file(file_a, streams=8, request_rate=100.0)  # highest rate, lowest concurrency
    _write_benchmark_file(file_b, streams=16, request_rate=50.0)  # lowest rate, middle concurrency
    _write_benchmark_file(file_c, streams=32, request_rate=75.0)  # middle rate, highest concurrency

    parser = GuideLLMParser()
    node = BaseTestNode(
        directory=test_dir,
        test_labels={"labels": {"guidellm_loadshape": "multi-turn"}},
        artifact_paths=[file_a, file_b, file_c],
    )

    result = parser.parse([node])

    assert result.warnings == []
    assert len(result.records) == 1

    # Check that curves are ordered by intended_concurrency (8, 16, 32) not request_rate (50, 75, 100)
    record = result.records[0]
    curves = record.metrics["performance_curves"]

    # Verify intended_concurrency curve follows intended order
    assert curves["intended_concurrency"] == [8, 16, 32]

    # Verify tokens_per_second curve follows intended_concurrency order (not rate order)
    # For streams 8: input_tokens=10, output_tokens=9 → total=19
    # For streams 16: input_tokens=18, output_tokens=17 → total=35
    # For streams 32: input_tokens=34, output_tokens=33 → total=67
    # Should be ordered by streams (8, 16, 32), not by request_rate (50, 75, 100)
    assert curves["tokens_per_second"] == [19.0, 35.0, 67.0]

    # Verify request rates are in intended_concurrency order, not ascending rate order
    request_rates = record.metrics["request_rate"]
    assert request_rates == [
        100.0,
        50.0,
        75.0,
    ]  # Rates in intended_concurrency order (8→100, 16→50, 32→75)
