from __future__ import annotations

import json

import pytest
import yaml

from projects.caliper.postprocess.prometheus.parser import (
    CaptureGroupResult,
    filter_by_source_file,
    parse_capture_group,
)


@pytest.fixture()
def capture_dir(tmp_path):
    group_dir = tmp_path / "000__capture_prometheus_metrics_resources"
    prom_dir = group_dir / "prometheus_metrics"
    profiles_dir = group_dir / "src" / "metric_profiles"
    prom_dir.mkdir(parents=True)
    profiles_dir.mkdir(parents=True)

    profiles_dir.joinpath("resource_cpu.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "query": 'rate(container_cpu_usage_seconds_total{namespace="{{.NAMESPACE}}"}[5m]) * 100',
                    "metricName": "cpu_usage_percent_by_pod",
                    "description": "Cpu usage percent by pod",
                    "unit": "ratio",
                },
                {
                    "query": 'avg(rate(container_cpu_usage_seconds_total{namespace="{{.NAMESPACE}}"}[5m])) * 100',
                    "metricName": "avg_cpu_usage_percent",
                    "description": "Avg cpu usage percent",
                    "unit": "ratio",
                },
            ]
        )
    )

    profiles_dir.joinpath("resource_memory.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "query": 'container_memory_working_set_bytes{namespace="{{.NAMESPACE}}"}',
                    "metricName": "memory_working_set_bytes_by_pod",
                    "description": "Memory working set bytes by pod",
                    "unit": "bytes",
                },
            ]
        )
    )

    prom_dir.joinpath("cpu_usage_percent_by_pod.json").write_text(
        json.dumps(
            [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "labels": {"pod": "pod-a"},
                    "value": 12.5,
                    "metricName": "cpu_usage_percent_by_pod",
                },
                {
                    "timestamp": "2026-01-01T00:01:00Z",
                    "labels": {"pod": "pod-a"},
                    "value": 15.0,
                    "metricName": "cpu_usage_percent_by_pod",
                },
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "labels": {"pod": "pod-b"},
                    "value": 8.0,
                    "metricName": "cpu_usage_percent_by_pod",
                },
                {
                    "timestamp": "2026-01-01T00:01:00Z",
                    "labels": {"pod": "pod-b"},
                    "value": 9.5,
                    "metricName": "cpu_usage_percent_by_pod",
                },
            ]
        )
    )

    prom_dir.joinpath("avg_cpu_usage_percent.json").write_text(
        json.dumps(
            [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "labels": {},
                    "value": 10.25,
                    "metricName": "avg_cpu_usage_percent",
                },
                {
                    "timestamp": "2026-01-01T00:01:00Z",
                    "labels": {},
                    "value": 12.25,
                    "metricName": "avg_cpu_usage_percent",
                },
            ]
        )
    )

    prom_dir.joinpath("memory_working_set_bytes_by_pod.json").write_text(
        json.dumps(
            [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "labels": {"pod": "pod-a"},
                    "value": 1048576.0,
                    "metricName": "memory_working_set_bytes_by_pod",
                },
            ]
        )
    )

    metadata = {
        "timestamp": "2026-01-01T00:05:00+00:00",
        "variables": {"NAMESPACE": "forge-praxis", "POD_NAME": ".*"},
        "results": {
            "cpu_usage_percent_by_pod": {
                "status": "ok",
                "description": "Cpu usage percent by pod",
                "unit": "ratio",
            },
            "avg_cpu_usage_percent": {
                "status": "ok",
                "description": "Avg cpu usage percent",
                "unit": "ratio",
            },
            "memory_working_set_bytes_by_pod": {
                "status": "ok",
                "description": "Memory working set bytes by pod",
                "unit": "bytes",
            },
        },
    }
    prom_dir.joinpath("metrics_metadata.yaml").write_text(
        yaml.safe_dump(metadata, default_flow_style=False, sort_keys=False)
    )

    return group_dir


def test_parse_capture_group(capture_dir):
    result = parse_capture_group(capture_dir)

    assert isinstance(result, CaptureGroupResult)
    assert result.group_name == capture_dir.name
    assert result.variables == {"NAMESPACE": "forge-praxis", "POD_NAME": ".*"}
    assert len(result.metrics) == 3

    cpu_by_pod = next(m for m in result.metrics if m.metric_name == "cpu_usage_percent_by_pod")
    assert cpu_by_pod.source_file == "resource_cpu"
    assert cpu_by_pod.unit == "ratio"
    assert "container_cpu_usage_seconds_total" in cpu_by_pod.query
    assert len(cpu_by_pod.series) == 2

    pod_a = next(s for s in cpu_by_pod.series if s.labels.get("pod") == "pod-a")
    assert len(pod_a.timestamps) == 2
    assert pod_a.values == [12.5, 15.0]

    pod_b = next(s for s in cpu_by_pod.series if s.labels.get("pod") == "pod-b")
    assert pod_b.values == [8.0, 9.5]


def test_parse_single_series_no_labels(capture_dir):
    result = parse_capture_group(capture_dir)
    avg_cpu = next(m for m in result.metrics if m.metric_name == "avg_cpu_usage_percent")
    assert len(avg_cpu.series) == 1
    assert avg_cpu.series[0].labels == {}
    assert avg_cpu.series[0].values == [10.25, 12.25]


def test_source_file_mapping(capture_dir):
    result = parse_capture_group(capture_dir)
    memory = next(m for m in result.metrics if m.metric_name == "memory_working_set_bytes_by_pod")
    assert memory.source_file == "resource_memory"


def test_filter_by_source_file(capture_dir):
    result = parse_capture_group(capture_dir)
    filtered = filter_by_source_file(result, ["resource_cpu"])

    assert len(filtered.metrics) == 2
    assert all(m.source_file == "resource_cpu" for m in filtered.metrics)
    assert filtered.variables == result.variables

    assert len(result.metrics) == 3, "Original must not be mutated"


def test_filter_by_source_file_no_match(capture_dir):
    result = parse_capture_group(capture_dir)
    filtered = filter_by_source_file(result, ["nonexistent"])
    assert len(filtered.metrics) == 0


def test_missing_metadata_raises(tmp_path):
    group_dir = tmp_path / "test_group"
    (group_dir / "prometheus_metrics").mkdir(parents=True)

    with pytest.raises(FileNotFoundError, match="metrics_metadata.yaml"):
        parse_capture_group(group_dir)


def test_malformed_json_raises(tmp_path):
    group_dir = tmp_path / "test_group"
    prom_dir = group_dir / "prometheus_metrics"
    prom_dir.mkdir(parents=True)

    metadata = {
        "timestamp": "2026-01-01T00:00:00Z",
        "variables": {},
        "results": {"bad_metric": {"status": "ok", "description": "bad", "unit": ""}},
    }
    prom_dir.joinpath("metrics_metadata.yaml").write_text(
        yaml.safe_dump(metadata, default_flow_style=False)
    )
    prom_dir.joinpath("bad_metric.json").write_text("{not valid json")

    with pytest.raises(ValueError, match="Failed to parse JSON"):
        parse_capture_group(group_dir)


def test_empty_json_array(tmp_path):
    group_dir = tmp_path / "test_group"
    prom_dir = group_dir / "prometheus_metrics"
    profiles_dir = group_dir / "src" / "metric_profiles"
    prom_dir.mkdir(parents=True)
    profiles_dir.mkdir(parents=True)

    metadata = {
        "timestamp": "2026-01-01T00:00:00Z",
        "variables": {},
        "results": {"empty_metric": {"status": "ok", "description": "empty", "unit": "count"}},
    }
    prom_dir.joinpath("metrics_metadata.yaml").write_text(
        yaml.safe_dump(metadata, default_flow_style=False)
    )
    prom_dir.joinpath("empty_metric.json").write_text("[]")

    result = parse_capture_group(group_dir)
    metric = result.metrics[0]
    assert metric.metric_name == "empty_metric"
    assert metric.series == []


def test_no_data_status_produces_empty_series(tmp_path):
    group_dir = tmp_path / "test_group"
    prom_dir = group_dir / "prometheus_metrics"
    prom_dir.mkdir(parents=True)

    metadata = {
        "timestamp": "2026-01-01T00:00:00Z",
        "variables": {},
        "results": {
            "missing_metric": {"status": "no_data", "description": "missing", "unit": "bytes"}
        },
    }
    prom_dir.joinpath("metrics_metadata.yaml").write_text(
        yaml.safe_dump(metadata, default_flow_style=False)
    )

    result = parse_capture_group(group_dir)
    assert len(result.metrics) == 1
    assert result.metrics[0].series == []
