from __future__ import annotations

from datetime import UTC, datetime

from projects.caliper.postprocess.prometheus.parser import (
    CaptureGroupResult,
    LabeledSeries,
    MetricTimeSeries,
)
from projects.caliper.postprocess.prometheus.report import (
    EmbeddableMetricPlot,
    generate_group_report,
    render_embeddable,
)


def _make_group(metrics: list[MetricTimeSeries] | None = None) -> CaptureGroupResult:
    if metrics is None:
        metrics = [
            MetricTimeSeries(
                metric_name="cpu_usage_percent_by_pod",
                description="Cpu usage percent by pod",
                unit="ratio",
                source_file="resource_cpu",
                query="rate(container_cpu_usage_seconds_total[5m]) * 100",
                legend_labels=["container", "pod"],
                series=[
                    LabeledSeries(
                        labels={"pod": "pod-a"},
                        timestamps=[
                            datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
                            datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
                        ],
                        values=[10.0, 20.0],
                    ),
                ],
            ),
            MetricTimeSeries(
                metric_name="memory_working_set_bytes_by_pod",
                description="Memory working set bytes by pod",
                unit="bytes",
                source_file="resource_memory",
                query="container_memory_working_set_bytes",
                legend_labels=["container", "pod"],
                series=[
                    LabeledSeries(
                        labels={"pod": "pod-a"},
                        timestamps=[datetime(2026, 1, 1, 0, 0, tzinfo=UTC)],
                        values=[1048576.0],
                    ),
                ],
            ),
        ]
    return CaptureGroupResult(
        group_name="resources",
        variables={"NAMESPACE": "forge-praxis"},
        metrics=metrics,
    )


def test_generate_group_report_creates_file(tmp_path):
    group = _make_group()
    report_path = generate_group_report(group, tmp_path)

    assert report_path.exists()
    assert "prometheus_metrics" in report_path.name or "resources" in report_path.name


def test_generate_group_report_contains_metric_titles(tmp_path):
    group = _make_group()
    report_path = generate_group_report(group, tmp_path)
    content = report_path.read_text(encoding="utf-8")

    assert "Cpu usage percent by pod" in content
    assert "Memory working set bytes by pod" in content


def test_generate_group_report_has_png_and_html_links(tmp_path):
    group = _make_group()
    report_path = generate_group_report(group, tmp_path)
    content = report_path.read_text(encoding="utf-8")

    assert "<img" in content or "interactive" in content.lower()
    assert ".html" in content


def test_render_embeddable_returns_valid_result(tmp_path):
    metric = MetricTimeSeries(
        metric_name="avg_cpu",
        description="Avg cpu usage",
        unit="ratio",
        source_file="resource_cpu",
        query="",
        legend_labels=[],
        series=[
            LabeledSeries(
                labels={},
                timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
                values=[15.0],
            ),
        ],
    )
    result = render_embeddable(metric, tmp_path)

    assert isinstance(result, EmbeddableMetricPlot)
    assert result.summary_text
    assert "Avg cpu usage" in result.summary_text
    assert result.html_path is not None or result.png_path is not None


def test_render_embeddable_summary_includes_unit(tmp_path):
    metric = MetricTimeSeries(
        metric_name="mem",
        description="Memory usage",
        unit="bytes",
        source_file="resource_memory",
        query="",
        legend_labels=[],
        series=[
            LabeledSeries(
                labels={},
                timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
                values=[1024.0],
            ),
        ],
    )
    result = render_embeddable(metric, tmp_path)
    assert "(bytes)" in result.summary_text


def test_no_data_metric_placeholder_in_report(tmp_path):
    no_data_metric = MetricTimeSeries(
        metric_name="missing_metric",
        description="Missing metric",
        unit="count",
        source_file="workload",
        query="",
        legend_labels=[],
        series=[],
    )
    group = _make_group(metrics=[no_data_metric])
    report_path = generate_group_report(group, tmp_path)
    content = report_path.read_text(encoding="utf-8")

    assert "Missing metric" in content
    assert "No data available" in content


def test_no_data_metric_embeddable(tmp_path):
    no_data_metric = MetricTimeSeries(
        metric_name="empty",
        description="Empty metric",
        unit="",
        source_file="workload",
        query="",
        legend_labels=[],
        series=[],
    )
    result = render_embeddable(no_data_metric, tmp_path)

    assert result.png_path is None
    assert result.html_path is None
    assert result.summary_text == "Empty metric"


def test_generate_group_report_creates_plot_subdirectory(tmp_path):
    group = _make_group()
    generate_group_report(group, tmp_path)

    subdirs = [d for d in tmp_path.iterdir() if d.is_dir()]
    assert len(subdirs) >= 1
