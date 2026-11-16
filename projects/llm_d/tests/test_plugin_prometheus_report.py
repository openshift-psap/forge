from __future__ import annotations

from pathlib import Path

import yaml

from projects.caliper.engine.model import BaseTestNode, UnifiedRunModel
from projects.llm_d.postprocess.llm_d.plugin import (
    PROMETHEUS_REPORT_ID,
    LlmDGuideLLMPlugin,
    find_prometheus_capture_dirs,
)


def _make_capture_tree(base: Path, group_name: str):
    capture_dir = base / group_name
    prom_dir = capture_dir / "prometheus_metrics"
    profiles_dir = capture_dir / "src" / "metric_profiles"
    prom_dir.mkdir(parents=True)
    profiles_dir.mkdir(parents=True)

    prom_dir.joinpath("metrics_metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "variables": {},
                "results": {},
            }
        )
    )
    profiles_dir.joinpath("resource_cpu.yaml").write_text(
        yaml.safe_dump([{"query": "x", "metricName": "cpu", "unit": "ratio"}])
    )
    return capture_dir


def _make_node(test_dir: Path) -> BaseTestNode:
    return BaseTestNode(
        directory=test_dir,
        test_labels={},
        artifact_paths=sorted(test_dir.rglob("*")),
    )


def test_finds_capture_dirs(tmp_path):
    test_dir = tmp_path / "000__llmd"
    _make_capture_tree(test_dir / "008__prometheus_metrics", "000__capture_prometheus_metrics")
    _make_capture_tree(test_dir / "008__prometheus_metrics", "001__capture_prometheus_metrics_vllm")

    dirs = find_prometheus_capture_dirs(_make_node(test_dir))

    assert len(dirs) == 2
    assert all((d / "prometheus_metrics" / "metrics_metadata.yaml").exists() for d in dirs)


def test_returns_empty_when_no_prom_data(tmp_path):
    test_dir = tmp_path / "test"
    test_dir.mkdir()
    (test_dir / "some_file.txt").write_text("hello")

    assert find_prometheus_capture_dirs(_make_node(test_dir)) == []


def test_visualize_prometheus_report_requested_without_capture_data(tmp_path):
    """Requesting the Prometheus report without capture artifacts must not fail."""
    plugin = LlmDGuideLLMPlugin()
    model = UnifiedRunModel(
        plugin_module="projects.llm_d.postprocess.llm_d.plugin",
        base_directory=str(tmp_path),
        test_nodes=[],
        unified_result_records=[],
    )

    reports = plugin.visualize(
        model,
        output_dir=tmp_path / "reports",
        report_ids=[PROMETHEUS_REPORT_ID],
        group_id=None,
        visualize_config=None,
    )

    # no capture data and no guidellm records: no reports, but no error either
    assert reports == []
