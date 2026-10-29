"""Tests for the Inference Playbooks postprocessing plugin."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from projects.caliper.engine.kpi import KpiComputationStatus, KpiRecord
from projects.caliper.engine.kpi.analyze import AnalysisConfig
from projects.caliper.engine.kpi.format import write_kpis_in_format
from projects.caliper.engine.kpi.html_generator import generate_kpi_html_from_file
from projects.caliper.engine.model import (
    BaseTestNode,
    ParseResult,
    UnifiedResultRecord,
    UnifiedRunModel,
)
from projects.inference_playbooks.postprocess import plugin as plugin_mod
from projects.inference_playbooks.postprocess.plugin import InferencePlaybooksPlugin

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _guidellm_node(tmp_path: Path, name: str = "bench_guidellm") -> BaseTestNode:
    """Create a minimal GuideLLM test node with a benchmarks.json artifact."""
    node_dir = tmp_path / name
    node_dir.mkdir(parents=True, exist_ok=True)
    benchmarks = {
        "benchmarks": [
            {
                "config": {"strategy": {"streams": 1}, "run_id": "abc"},
                "metrics": {
                    "requests_per_second": {"successful": {"mean": 2.5}},
                    "output_tokens_per_second": {"successful": {"mean": 100.0}},
                    "tokens_per_second": {"successful": {"mean": 120.0}},
                    "request_concurrency": {"successful": {"mean": 1.0}},
                    "time_to_first_token_ms": {
                        "successful": {
                            "mean": 50.0,
                            "median": 48.0,
                            "percentiles": {"p95": 80.0, "p99": 100.0, "p1": 20.0, "p999": 120.0},
                        },
                    },
                    "inter_token_latency_ms": {
                        "successful": {
                            "mean": 10.0,
                            "median": 9.0,
                            "percentiles": {"p95": 15.0, "p99": 20.0, "p1": 5.0, "p999": 25.0},
                        },
                    },
                    "time_per_output_token_ms": {
                        "successful": {
                            "mean": 11.0,
                            "median": 10.0,
                            "percentiles": {"p95": 16.0, "p99": 21.0, "p1": 6.0, "p999": 26.0},
                        },
                    },
                    "request_latency_ms": {
                        "successful": {"mean": 500.0, "median": 480.0, "min": 200.0, "max": 900.0},
                    },
                    "request_totals": {"successful": 100, "errored": 0},
                },
                "scheduler": {"strategy": {"streams": 1}},
                "scheduler_metrics": {"start_time": 1000.0, "end_time": 1060.0},
            }
        ],
        "metadata": {"guidellm_version": "0.5.0"},
        "args": {"data": [{"prompt_tokens": 128, "output_tokens": 256}]},
    }
    artifact = node_dir / "benchmarks.json"
    artifact.write_text(json.dumps(benchmarks), encoding="utf-8")
    return BaseTestNode(
        directory=node_dir,
        test_labels={
            "labels": {"phase": "benchmark", "benchmark_tool": "guidellm"},
            "kpi_labels": {
                "model_name": "test/model",
                "product_version": "v0.24.0",
                "benchmark_tool": "guidellm",
                "benchmark_key": "ci-quick",
            },
        },
        artifact_paths=[artifact],
        test_path=Path(name),
    )


def _aiperf_node(
    tmp_path: Path, concurrency: int = 1, name: str | None = None, sweep_id: str = "test-sweep"
) -> BaseTestNode:
    """Create a minimal AI Perf test node with a profile_export_aiperf.json."""
    if name is None:
        name = f"bench_aiperf_c{concurrency}"
    node_dir = tmp_path / name
    node_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "request_count": {"avg": 50, "unit": "requests"},
        "error_request_count": {"avg": 0, "unit": "requests"},
        "request_throughput": {"avg": 5.0, "unit": "requests/sec"},
        "output_token_throughput": {"avg": 200.0, "unit": "tokens/sec"},
        "time_to_first_token": {"avg": 45.0, "p95": 80.0, "unit": "ms"},
        "inter_token_latency": {"avg": 8.0, "p95": 12.0, "unit": "ms"},
        "request_latency": {"avg": 400.0, "p95": 600.0, "unit": "ms"},
    }
    artifact = node_dir / "profile_export_aiperf.json"
    artifact.write_text(json.dumps(summary), encoding="utf-8")
    return BaseTestNode(
        directory=node_dir,
        test_labels={
            "labels": {
                "phase": "benchmark",
                "benchmark_tool": "aiperf",
                "concurrency": concurrency,
                "sweep_id": sweep_id,
            },
            "kpi_labels": {
                "model_name": "test/model",
                "product_version": "v0.24.0",
                "benchmark_tool": "aiperf",
                "benchmark_key": "agentx-180s",
                "concurrency": str(concurrency),
            },
        },
        artifact_paths=[artifact],
        test_path=Path(name),
    )


def _model(
    tmp_path: Path, nodes: list[BaseTestNode], records: list[UnifiedResultRecord]
) -> UnifiedRunModel:
    return UnifiedRunModel(
        plugin_module="projects.inference_playbooks.postprocess.plugin",
        base_directory=str(tmp_path),
        test_nodes=nodes,
        unified_result_records=records,
    )


# ---------------------------------------------------------------------------
# Module-level contract
# ---------------------------------------------------------------------------


class TestModuleLevelContract:
    def test_analysis_config_present(self):
        assert hasattr(plugin_mod, "analysis_config")
        cfg = plugin_mod.analysis_config
        assert isinstance(cfg, AnalysisConfig)
        assert "product_version" in cfg.comparison_labels
        assert "sweep_id" in cfg.ignored_labels

    def test_get_plugin_factory(self):
        plugin = plugin_mod.get_plugin()
        assert isinstance(plugin, InferencePlaybooksPlugin)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestParsing:
    def test_guidellm_only(self, tmp_path: Path):
        plugin = InferencePlaybooksPlugin()
        node = _guidellm_node(tmp_path)
        result = plugin.parse([node])
        assert isinstance(result, ParseResult)
        assert len(result.records) == 1
        assert result.records[0].run_identity.get("guidellm")

    def test_aiperf_only(self, tmp_path: Path):
        plugin = InferencePlaybooksPlugin()
        node = _aiperf_node(tmp_path)
        result = plugin.parse([node])
        assert len(result.records) == 1
        assert result.records[0].run_identity.get("aiperf")

    def test_sweep_identity_and_conditions_survive_parsing(self, tmp_path: Path):
        node = _aiperf_node(tmp_path, concurrency=4)
        node.test_labels["kpi_labels"]["sweep_id"] = "stale-user-label"
        record = InferencePlaybooksPlugin().parse([node]).records[0]
        assert record.distinguishing_labels["sweep_id"] == "test-sweep"
        assert record.distinguishing_labels["model_name"] == "test/model"
        assert record.distinguishing_labels["benchmark_key"] == "agentx-180s"
        assert record.distinguishing_labels["concurrency"] == 4

    def test_mixed_tools(self, tmp_path: Path):
        plugin = InferencePlaybooksPlugin()
        g_node = _guidellm_node(tmp_path)
        a1_node = _aiperf_node(tmp_path, concurrency=1)
        a2_node = _aiperf_node(tmp_path, concurrency=2)
        result = plugin.parse([g_node, a1_node, a2_node])
        assert len(result.records) == 3
        tools = {"guidellm" if r.run_identity.get("guidellm") else "aiperf" for r in result.records}
        assert tools == {"guidellm", "aiperf"}

    def test_aiperf_concurrency_sweep_distinct(self, tmp_path: Path):
        plugin = InferencePlaybooksPlugin()
        c1 = _aiperf_node(tmp_path, concurrency=1)
        c2 = _aiperf_node(tmp_path, concurrency=2)
        result = plugin.parse([c1, c2])
        assert len(result.records) == 2
        paths = {r.test_base_path for r in result.records}
        assert len(paths) == 2

    def test_missing_aiperf_summary_raises(self, tmp_path: Path):
        plugin = InferencePlaybooksPlugin()
        node_dir = tmp_path / "empty_aiperf"
        node_dir.mkdir()
        node = BaseTestNode(
            directory=node_dir,
            test_labels={"labels": {"benchmark_tool": "aiperf"}},
            artifact_paths=[],
            test_path=Path("empty_aiperf"),
        )
        with pytest.raises(ValueError, match="Expected one AI Perf summary"):
            plugin.parse([node])


# ---------------------------------------------------------------------------
# KPI computation
# ---------------------------------------------------------------------------


class TestKpis:
    def test_guidellm_kpis(self, tmp_path: Path):
        plugin = InferencePlaybooksPlugin()
        node = _guidellm_node(tmp_path)
        result = plugin.parse([node])
        model = _model(tmp_path, [node], result.records)
        kpis, status = plugin.compute_kpis(model)
        assert isinstance(status, KpiComputationStatus)
        assert status.success
        assert len(kpis) > 0
        assert all(isinstance(k, KpiRecord) for k in kpis)

    def test_aiperf_kpis(self, tmp_path: Path):
        plugin = InferencePlaybooksPlugin()
        node = _aiperf_node(tmp_path)
        result = plugin.parse([node])
        model = _model(tmp_path, [node], result.records)
        kpis, status = plugin.compute_kpis(model)
        assert status.success
        aiperf_kpis = [k for k in kpis if k.kpi_id.startswith("aiperf_")]
        assert len(aiperf_kpis) > 0

    def test_mixed_kpis_both_tools(self, tmp_path: Path):
        plugin = InferencePlaybooksPlugin()
        g = _guidellm_node(tmp_path)
        a = _aiperf_node(tmp_path)
        result = plugin.parse([g, a])
        model = _model(tmp_path, [g, a], result.records)
        kpis, status = plugin.compute_kpis(model)
        assert status.success
        guidellm_present = any(not k.kpi_id.startswith("aiperf_") for k in kpis)
        aiperf_present = any(k.kpi_id.startswith("aiperf_") for k in kpis)
        assert guidellm_present
        assert aiperf_present

    def test_concurrency_in_aiperf_kpi_labels(self, tmp_path: Path):
        plugin = InferencePlaybooksPlugin()
        node = _aiperf_node(tmp_path, concurrency=4)
        result = plugin.parse([node])
        model = _model(tmp_path, [node], result.records)
        kpis, _ = plugin.compute_kpis(model)
        aiperf_kpis = [k for k in kpis if k.kpi_id.startswith("aiperf_")]
        assert len(aiperf_kpis) > 0
        for k in aiperf_kpis:
            assert k.labels["sweep_id"] == "test-sweep"
            assert k.labels["concurrency"] == "4"
            assert not k.is_curve


# ---------------------------------------------------------------------------
# Dashboard CSV
# ---------------------------------------------------------------------------


class TestDashboardCsv:
    def test_empty_model_writes_header(self, tmp_path: Path):
        plugin = InferencePlaybooksPlugin()
        model = _model(tmp_path, [], [])
        out = tmp_path / "dashboard.csv"
        plugin.export_dashboard_csv(model, out)
        assert out.exists()
        header = out.read_text().strip().split("\n")[0]
        assert "benchmark_tool" in header
        assert "workload_key" in header
        assert "concurrency" in header


# ---------------------------------------------------------------------------
# AI data payload
# ---------------------------------------------------------------------------


class TestAiData:
    def test_guidellm_payload(self, tmp_path: Path):
        plugin = InferencePlaybooksPlugin()
        node = _guidellm_node(tmp_path)
        result = plugin.parse([node])
        model = _model(tmp_path, [node], result.records)
        payload = plugin.build_ai_data_payload(model)
        assert payload["schema_version"] == "1"
        assert len(payload["test_entries"]) == 1
        assert payload["test_entries"][0]["benchmark_tool"] == "guidellm"

    def test_aiperf_payload(self, tmp_path: Path):
        plugin = InferencePlaybooksPlugin()
        node = _aiperf_node(tmp_path)
        result = plugin.parse([node])
        model = _model(tmp_path, [node], result.records)
        payload = plugin.build_ai_data_payload(model)
        assert len(payload["test_entries"]) == 1
        assert payload["test_entries"][0]["benchmark_tool"] == "aiperf"
        assert "request_count" in payload["test_entries"][0]["metrics"]

    def test_mixed_payload_all_tools(self, tmp_path: Path):
        plugin = InferencePlaybooksPlugin()
        g = _guidellm_node(tmp_path)
        a1 = _aiperf_node(tmp_path, concurrency=1)
        a2 = _aiperf_node(tmp_path, concurrency=2)
        result = plugin.parse([g, a1, a2])
        model = _model(tmp_path, [g, a1, a2], result.records)
        payload = plugin.build_ai_data_payload(model)
        assert len(payload["test_entries"]) == 3
        tools = {e["benchmark_tool"] for e in payload["test_entries"]}
        assert tools == {"guidellm", "aiperf"}


# ---------------------------------------------------------------------------
# KPI HTML and plugin visualization
# ---------------------------------------------------------------------------


class TestVisualization:
    @pytest.mark.parametrize("include_guidellm", [False, True])
    def test_kpi_html_contains_aiperf_concurrency_graphs(
        self, tmp_path: Path, include_guidellm: bool
    ):
        plugin = InferencePlaybooksPlugin()
        nodes = [_aiperf_node(tmp_path, concurrency=c) for c in (16, 4, 8)]
        if include_guidellm:
            nodes.append(_guidellm_node(tmp_path))
        model = _model(tmp_path, nodes, plugin.parse(nodes).records)
        kpis, _ = plugin.compute_kpis(model)
        output = tmp_path / "kpis" / "kpis.json"
        write_kpis_in_format([kpi.to_dict() for kpi in kpis], output, model=model)
        original_json = output.read_bytes()
        html_path = generate_kpi_html_from_file(output)
        assert output.read_bytes() == original_json

        document = json.loads(output.read_text())
        aiperf_tests = [test for test in document["tests"] if "concurrency" in test["labels"]]
        assert len(aiperf_tests) == 3
        for test in aiperf_tests:
            by_id = {kpi["kpi_id"]: kpi for kpi in test["kpis"]}
            assert all(not kpi["is_curve"] for kpi in test["kpis"])
            assert by_id["aiperf_time_to_first_token_p95"]["value"] == 80.0

        assert len(document["tests"]) == len(nodes)
        assert not any(test["run_id"].startswith("sweep/") for test in document["tests"])

        html = html_path.read_text()
        for metric in ("time_to_first_token", "inter_token_latency", "request_latency"):
            assert f'id="chart-aiperf_{metric}_p95_vs_concurrency"' in html
        assert 'id="chart-aiperf_output_token_throughput_avg_vs_concurrency"' in html
        assert "const rawData = [[4.0, 80.0], [8.0, 80.0], [16.0, 80.0]];" in html
        assert "Configured concurrency" in html
        assert "Time to first token p95 (ms)" in html
        assert "name: 'sweep/test-sweep'" in html
        assert 'id="test-sweep/test-sweep"' not in html
        assert ('id="chart-playbooks_output_tok_per_sec"' in html) is include_guidellm

    def test_visualize_returns_empty(self, tmp_path: Path):
        plugin = InferencePlaybooksPlugin()
        model = _model(tmp_path, [], [])
        result = plugin.visualize(model, tmp_path / "reports", None, None, None)
        assert result == []
