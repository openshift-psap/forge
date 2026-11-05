from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from projects.ag_praxis.postprocess.ag_praxis.plugin import find_prometheus_capture_dirs
from projects.caliper.engine.model import BaseTestNode


def _make_capture_tree(base: Path, group_name: str = "000__capture_prometheus_metrics_resources"):
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


@pytest.fixture()
def artifact_tree(tmp_path):
    test_dir = tmp_path / "000__svc"

    _make_capture_tree(
        test_dir / "008__prometheus_metrics", "000__capture_prometheus_metrics_resources"
    )
    _make_capture_tree(test_dir / "008__prometheus_metrics", "001__capture_prometheus_metrics_vllm")

    (test_dir / "005__run_benchmark" / "results").mkdir(parents=True)
    (test_dir / "005__run_benchmark" / "results" / "benchmarks.json").write_text("{}")

    all_paths = sorted(test_dir.rglob("*"))
    return test_dir, all_paths


def test_finds_capture_dirs(artifact_tree):
    test_dir, all_paths = artifact_tree
    node = BaseTestNode(directory=test_dir, test_labels={}, artifact_paths=all_paths)

    dirs = find_prometheus_capture_dirs(node)

    assert len(dirs) == 2
    assert all(d.name.startswith("0") for d in dirs)
    assert all((d / "prometheus_metrics" / "metrics_metadata.yaml").exists() for d in dirs)


def test_returns_empty_when_no_prom_data(tmp_path):
    test_dir = tmp_path / "test"
    test_dir.mkdir()
    (test_dir / "some_file.txt").write_text("hello")

    node = BaseTestNode(
        directory=test_dir,
        test_labels={},
        artifact_paths=sorted(test_dir.rglob("*")),
    )

    dirs = find_prometheus_capture_dirs(node)
    assert dirs == []


def test_ignores_incomplete_capture_dir(tmp_path):
    test_dir = tmp_path / "test"
    prom_dir = test_dir / "capture" / "prometheus_metrics"
    prom_dir.mkdir(parents=True)
    prom_dir.joinpath("metrics_metadata.yaml").write_text("{}")

    node = BaseTestNode(
        directory=test_dir,
        test_labels={},
        artifact_paths=sorted(test_dir.rglob("*")),
    )

    dirs = find_prometheus_capture_dirs(node)
    assert dirs == []
