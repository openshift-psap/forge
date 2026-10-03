"""Tests for per-turn metric extraction and JSON trimming."""

from __future__ import annotations

import json

import pytest

from projects.guidellm.postprocess.guidellm.dashboard import (
    _extract_per_turn_curves,
    _percentile,
    _turn_metric_values,
)
from projects.guidellm.toolbox.run_guidellm_benchmark.main import (
    _trim_request_entry,
    trim_benchmark_json,
)


class TestPercentile:
    def test_empty_list(self):
        assert _percentile([], 50) is None

    def test_single_value(self):
        assert _percentile([5.0], 50) == 5.0
        assert _percentile([5.0], 0) == 5.0
        assert _percentile([5.0], 100) == 5.0

    def test_two_values(self):
        assert _percentile([1.0, 3.0], 50) == 2.0
        assert _percentile([1.0, 3.0], 0) == 1.0
        assert _percentile([1.0, 3.0], 100) == 3.0

    def test_interpolation(self):
        vals = [10.0, 20.0, 30.0, 40.0, 50.0]
        assert _percentile(vals, 50) == 30.0
        assert _percentile(vals, 25) == 20.0
        assert _percentile(vals, 75) == 40.0

    def test_p99_large_list(self):
        vals = list(range(100))
        result = _percentile(vals, 99)
        assert result == pytest.approx(98.01)


class TestExtractPerTurnCurves:
    @staticmethod
    def _make_request(turn_index, ttft=100.0, itl=10.0):
        return {
            "info": {"turn_index": turn_index, "conversation_id": "conv1"},
            "time_to_first_token_ms": ttft,
            "inter_token_latency_ms": itl,
            "time_per_output_token_ms": itl + 1,
            "request_latency": 1.5,
            "prompt_tokens": 100,
            "output_tokens": 50,
            "output_tokens_per_second": 33.0,
            "tokens_per_second": 100.0,
        }

    def test_single_turn_returns_empty(self):
        benchmarks = [
            {
                "requests": {"successful": [self._make_request(0), self._make_request(0)]},
                "config": {"strategy": {"streams": 1}},
            }
        ]
        assert _extract_per_turn_curves(benchmarks) == {}

    def test_no_requests_returns_empty(self):
        benchmarks = [{"requests": {"successful": []}, "config": {}}]
        assert _extract_per_turn_curves(benchmarks) == {}

    def test_no_turn_index_returns_empty(self):
        benchmarks = [
            {
                "requests": {"successful": [{"info": {}, "time_to_first_token_ms": 100}]},
                "config": {},
            }
        ]
        assert _extract_per_turn_curves(benchmarks) == {}

    def test_multi_turn_returns_string_keys(self):
        benchmarks = [
            {
                "requests": {
                    "successful": [
                        self._make_request(0, ttft=68.0),
                        self._make_request(1, ttft=73.0),
                        self._make_request(2, ttft=75.0),
                    ]
                },
                "config": {"strategy": {"streams": 1}},
            }
        ]
        result = _extract_per_turn_curves(benchmarks)
        assert set(result.keys()) == {"0", "1", "2"}
        assert all(isinstance(k, str) for k in result.keys())

    def test_keys_survive_json_round_trip(self):
        benchmarks = [
            {
                "requests": {
                    "successful": [
                        self._make_request(0),
                        self._make_request(1),
                    ]
                },
                "config": {"strategy": {"streams": 1}},
            }
        ]
        result = _extract_per_turn_curves(benchmarks)
        round_tripped = json.loads(json.dumps(result))
        assert result.keys() == round_tripped.keys()

    def test_multi_rate_multi_turn(self):
        benchmarks = [
            {
                "requests": {
                    "successful": [
                        self._make_request(0, ttft=60.0),
                        self._make_request(1, ttft=70.0),
                    ]
                },
                "config": {"strategy": {"streams": 1}},
            },
            {
                "requests": {
                    "successful": [
                        self._make_request(0, ttft=80.0),
                        self._make_request(1, ttft=90.0),
                    ]
                },
                "config": {"strategy": {"streams": 10}},
            },
        ]
        result = _extract_per_turn_curves(benchmarks)
        assert len(result["0"]["intended_concurrency"]) == 2
        assert result["0"]["intended_concurrency"] == [1, 10]

    def test_missing_turn_at_one_rate(self):
        benchmarks = [
            {
                "requests": {
                    "successful": [
                        self._make_request(0),
                        self._make_request(1),
                    ]
                },
                "config": {"strategy": {"streams": 1}},
            },
            {
                "requests": {"successful": [self._make_request(0)]},
                "config": {"strategy": {"streams": 10}},
            },
        ]
        result = _extract_per_turn_curves(benchmarks)
        turn1_ttft = result["1"]["ttft_median"]
        assert turn1_ttft[0] is not None
        assert turn1_ttft[1] is None

    def test_errored_requests_counted(self):
        benchmarks = [
            {
                "requests": {
                    "successful": [
                        self._make_request(0, ttft=60.0),
                        self._make_request(1, ttft=70.0),
                    ],
                    "errored": [
                        {"info": {"turn_index": 1, "conversation_id": "conv2"}},
                    ],
                },
                "config": {"strategy": {"streams": 1}},
            }
        ]
        result = _extract_per_turn_curves(benchmarks)
        assert result["0"]["errored_requests"] == [0]
        assert result["1"]["errored_requests"] == [1]

    def test_errored_only_turn_discovered(self):
        benchmarks = [
            {
                "requests": {
                    "successful": [self._make_request(0, ttft=50.0)],
                    "errored": [
                        {"info": {"turn_index": 1, "conversation_id": "conv1"}},
                    ],
                },
                "config": {"strategy": {"streams": 1}},
            }
        ]
        result = _extract_per_turn_curves(benchmarks)
        assert "0" in result
        assert "1" in result
        assert result["1"]["successful_requests"] == [0]
        assert result["1"]["errored_requests"] == [1]


class TestTurnMetricValues:
    @staticmethod
    def _make_req(ttft=100.0, itl=10.0, tpot=11.0, lat=1.5, pt=100, ot=50):
        return {
            "time_to_first_token_ms": ttft,
            "inter_token_latency_ms": itl,
            "time_per_output_token_ms": tpot,
            "request_latency": lat,
            "prompt_tokens": pt,
            "output_tokens": ot,
            "output_tokens_per_second": 33.0,
            "tokens_per_second": 100.0,
        }

    def test_basic_stats(self):
        reqs = [self._make_req(ttft=100.0), self._make_req(ttft=200.0)]
        result = _turn_metric_values(reqs, {"streams": 5})
        assert result["successful_requests"] == 2
        assert result["errored_requests"] == 0
        assert result["intended_concurrency"] == 5
        assert result["ttft_median"] == pytest.approx(0.15)
        assert result["ttft_mean"] == pytest.approx(0.15)

    def test_empty_requests(self):
        result = _turn_metric_values([], {"streams": 1})
        assert result["successful_requests"] == 0
        assert result["ttft_median"] is None
        assert result["ttft_mean"] is None
        assert result["request_latency_min"] is None

    def test_single_request(self):
        reqs = [self._make_req(ttft=100.0, lat=2.0)]
        result = _turn_metric_values(reqs, {"streams": 1})
        assert result["successful_requests"] == 1
        assert result["ttft_median"] == pytest.approx(0.1)
        assert result["request_latency_median"] == 2.0
        assert result["request_latency_min"] == 2.0
        assert result["request_latency_max"] == 2.0

    def test_errored_count_propagated(self):
        reqs = [self._make_req(ttft=100.0)]
        result = _turn_metric_values(reqs, {"streams": 1}, errored_count=3)
        assert result["errored_requests"] == 3
        assert result["successful_requests"] == 1

    def test_none_fields_skipped(self):
        reqs = [
            {
                "time_to_first_token_ms": None,
                "request_latency": 1.0,
                "prompt_tokens": 50,
                "output_tokens": 20,
                "inter_token_latency_ms": 5.0,
                "time_per_output_token_ms": 6.0,
                "output_tokens_per_second": 10.0,
                "tokens_per_second": 50.0,
            },
        ]
        result = _turn_metric_values(reqs, {"streams": 1})
        assert result["ttft_median"] is None
        assert result["request_latency_median"] == 1.0


class TestTrimBenchmarkJson:
    def test_preserves_requests_structure(self):
        data = {
            "benchmarks": [
                {
                    "metrics": {"some": "data"},
                    "requests": {
                        "successful": [
                            {
                                "info": {"turn_index": 0},
                                "time_to_first_token_ms": 100,
                                "request_args": "very long prompt text" * 1000,
                                "output": "very long output" * 1000,
                                "reasoning_output": "long reasoning" * 1000,
                            }
                        ]
                    },
                }
            ]
        }
        trimmed = trim_benchmark_json(data)
        reqs = trimmed["benchmarks"][0]["requests"]["successful"]
        assert len(reqs) == 1
        assert reqs[0]["info"]["turn_index"] == 0
        assert reqs[0]["time_to_first_token_ms"] == 100
        assert "request_args" not in reqs[0]
        assert "output" not in reqs[0]
        assert "reasoning_output" not in reqs[0]

    def test_preserves_non_request_fields(self):
        data = {"metadata": {"version": "1.0"}, "config": {"model": "test"}}
        assert trim_benchmark_json(data) == data

    def test_nested_bulk_fields_only_in_requests(self):
        data = {
            "top_output": "should stay",
            "requests": {"successful": [{"output": "stripped", "info": {"keep": True}}]},
        }
        trimmed = trim_benchmark_json(data)
        assert trimmed["top_output"] == "should stay"
        assert "output" not in trimmed["requests"]["successful"][0]
        assert trimmed["requests"]["successful"][0]["info"]["keep"] is True

    def test_trim_request_entry_recursive(self):
        data = {
            "nested": {"request_args": "stripped", "keep": "yes"},
            "list": [{"output": "stripped", "value": 1}],
        }
        result = _trim_request_entry(data)
        assert "request_args" not in result["nested"]
        assert result["nested"]["keep"] == "yes"
        assert "output" not in result["list"][0]
        assert result["list"][0]["value"] == 1

    def test_empty_requests(self):
        data = {"requests": {"successful": [], "errored": []}}
        trimmed = trim_benchmark_json(data)
        assert trimmed["requests"] == {"successful": [], "errored": []}
