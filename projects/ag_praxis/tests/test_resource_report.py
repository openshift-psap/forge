from __future__ import annotations

from datetime import UTC, datetime

from projects.ag_praxis.postprocess.ag_praxis.resource_report import (
    _group_display_name,
    _group_slug,
    find_resource_pairs,
    generate_resource_report,
)
from projects.caliper.postprocess.prometheus.parser import (
    CaptureGroupResult,
    LabeledSeries,
    MetricTimeSeries,
)

_TS = [datetime(2026, 1, 1, tzinfo=UTC)]


def _metric(metric_name: str, values: list[float] | None = None) -> MetricTimeSeries:
    if values is None:
        values = [1.0]
    return MetricTimeSeries(
        metric_name=metric_name,
        description=metric_name,
        unit="count",
        source_file="resource_cpu",
        query="",
        legend_labels=["pod"],
        series=[LabeledSeries(labels={"pod": "pod-a"}, timestamps=_TS, values=values)],
    )


def _resource_group(
    group_name: str,
    *,
    with_requests: bool = True,
    with_limits: bool = True,
) -> CaptureGroupResult:
    metrics = [
        _metric("cpu_usage_cores_by_pod"),
        _metric("memory_usage_bytes_by_pod"),
    ]
    if with_limits:
        metrics += [
            _metric("cpu_limits_by_pod", [2.0]),
            _metric("memory_limits_by_pod", [2.0]),
        ]
    if with_requests:
        metrics += [
            _metric("cpu_requests_by_pod", [1.5]),
            _metric("memory_requests_by_pod", [1.5]),
        ]
    return CaptureGroupResult(
        group_name=group_name,
        variables={"NAMESPACE": "kuadrant-system", "POD_NAME": ".*"},
        metrics=metrics,
    )


def _vllm_group() -> CaptureGroupResult:
    return CaptureGroupResult(
        group_name="000__capture_prometheus_metrics_vllm",
        variables={},
        metrics=[_metric("vllm_e2e_request_latency_seconds", [0.1])],
    )


def test_find_resource_pairs_requires_usage_and_limits():
    full = find_resource_pairs(_resource_group("005__capture_prometheus_metrics_resource_payload"))
    assert [pair.title for pair in full] == [
        "CPU Usage vs Limits and Requests",
        "Memory Usage vs Limits and Requests",
    ]

    no_limits = find_resource_pairs(
        _resource_group("001__x", with_limits=False, with_requests=False)
    )
    assert no_limits == []


def test_find_resource_pairs_ignores_non_resource_groups():
    assert find_resource_pairs(_vllm_group()) == []


def test_requests_are_optional():
    without_requests = find_resource_pairs(_resource_group("001__x", with_requests=False))
    assert len(without_requests) == 2
    assert all(pair.requests is None for pair in without_requests)

    with_requests = find_resource_pairs(_resource_group("001__x"))
    assert all(pair.requests is not None for pair in with_requests)


def test_generate_report_one_page_with_section_per_group(tmp_path):
    groups = [
        _vllm_group(),
        _resource_group("004__capture_prometheus_metrics_resource_ingress"),
        _resource_group("005__capture_prometheus_metrics_resource_payload"),
    ]

    report_path = generate_resource_report(groups, tmp_path)

    assert report_path is not None
    assert report_path.name == "resource_usage_vs_limits.html"
    html = report_path.read_text(encoding="utf-8")

    assert "<h2>Ingress</h2>" in html
    assert "<h2>Payload</h2>" in html
    assert "Vllm" not in html

    # one asset directory per captured target
    asset_root = tmp_path / "resource_usage_vs_limits"
    assert (asset_root / "resource_ingress").is_dir()
    assert (asset_root / "resource_payload").is_dir()


def test_generate_report_skips_group_without_limits(tmp_path):
    groups = [
        _resource_group("001__x", with_limits=False, with_requests=False),
    ]

    assert generate_resource_report(groups, tmp_path) is None
    assert not (tmp_path / "resource_usage_vs_limits.html").exists()


def test_generate_report_renders_capture_parameters_per_group(tmp_path):
    groups = [_resource_group("006__capture_prometheus_metrics_resource_kuadrant")]

    report_path = generate_resource_report(groups, tmp_path)

    html = report_path.read_text(encoding="utf-8")
    assert "Capture Parameters" in html
    assert "kuadrant-system" in html


def test_group_naming():
    assert _group_slug("005__capture_prometheus_metrics_resource_payload") == "resource_payload"
    assert (
        _group_slug("002__capture_prometheus_metrics_resource_load-generator")
        == "resource_load_generator"
    )
    assert _group_display_name("005__capture_prometheus_metrics_resource_payload") == "Payload"
    assert _group_display_name("006__capture_prometheus_metrics_resource_kuadrant") == "Kuadrant"
    assert (
        _group_display_name("002__capture_prometheus_metrics_resource_load-generator")
        == "Load Generator"
    )
