from __future__ import annotations

from datetime import UTC, datetime

from projects.caliper.postprocess.prometheus.parser import LabeledSeries, MetricTimeSeries
from projects.caliper.postprocess.prometheus.plotting import (
    UNIT_FORMATTERS,
    build_legend_info,
    create_combined_figure,
    create_metric_figure,
)

_ = create_combined_figure  # referenced in tests below; keep import


def _make_metric(
    unit: str = "cores",
    series: list[LabeledSeries] | None = None,
    metric_name: str = "test_metric",
    description: str = "Test metric description",
    legend_labels: list[str] | None = None,
) -> MetricTimeSeries:
    if series is None:
        series = [
            LabeledSeries(
                labels={"pod": "pod-a"},
                timestamps=[
                    datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
                    datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
                ],
                values=[10.0, 20.0],
            ),
        ]
    return MetricTimeSeries(
        metric_name=metric_name,
        description=description,
        unit=unit,
        source_file="resource_cpu",
        query="rate(container_cpu_usage_seconds_total[5m])",
        legend_labels=legend_labels or [],
        series=series,
    )


def test_single_series_produces_one_trace():
    metric = _make_metric()
    fig = create_metric_figure(metric)
    assert len(fig.data) == 1


def test_multi_series_produces_multiple_traces():
    series = [
        LabeledSeries(
            labels={"pod": "pod-a"},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[5.0],
        ),
        LabeledSeries(
            labels={"pod": "pod-b"},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[8.0],
        ),
    ]
    metric = _make_metric(series=series)
    fig = create_metric_figure(metric)
    assert len(fig.data) == 2
    trace_names = {t.name for t in fig.data}
    assert trace_names == {"pod-a", "pod-b"}


def test_common_labels_excluded_from_trace_names():
    series = [
        LabeledSeries(
            labels={"namespace": "forge-praxis", "pod": "pod-a", "container": "main"},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[1.0],
        ),
        LabeledSeries(
            labels={"namespace": "forge-praxis", "pod": "pod-b", "container": "main"},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[2.0],
        ),
    ]
    metric = _make_metric(series=series)
    fig = create_metric_figure(metric)

    trace_names = {t.name for t in fig.data}
    assert trace_names == {"pod-a", "pod-b"}
    assert not any("forge-praxis" in t.name for t in fig.data)
    assert not any("main" in t.name for t in fig.data)


def test_multiple_distinguishing_labels_use_key_value():
    series = [
        LabeledSeries(
            labels={"namespace": "ns", "pod": "pod-a", "container": "main"},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[1.0],
        ),
        LabeledSeries(
            labels={"namespace": "ns", "pod": "pod-b", "container": "sidecar"},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[2.0],
        ),
    ]
    metric = _make_metric(series=series)
    fig = create_metric_figure(metric)

    trace_names = {t.name for t in fig.data}
    assert "container=main, pod=pod-a" in trace_names
    assert "container=sidecar, pod=pod-b" in trace_names


def test_empty_labels_uses_metric_name():
    series = [
        LabeledSeries(
            labels={},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[42.0],
        ),
    ]
    metric = _make_metric(series=series, metric_name="avg_cpu")
    fig = create_metric_figure(metric)
    assert fig.data[0].name == "avg_cpu"


def test_all_labels_common_uses_metric_name():
    series = [
        LabeledSeries(
            labels={"namespace": "ns", "pod": "pod-a"},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[1.0],
        ),
    ]
    metric = _make_metric(series=series)
    fig = create_metric_figure(metric)
    assert fig.data[0].name == "test_metric"


def test_title_from_description():
    metric = _make_metric(description="Cpu usage by pod")
    fig = create_metric_figure(metric)
    assert fig.layout.title.text == "Cpu usage by pod"


def test_unit_ratio_axis():
    metric = _make_metric(unit="ratio")
    fig = create_metric_figure(metric)
    assert "%" in fig.layout.yaxis.title.text


def test_unit_percent_axis():
    metric = _make_metric(unit="percent")
    fig = create_metric_figure(metric)
    assert "%" in fig.layout.yaxis.title.text


def test_unit_bytes_scales_to_mib():
    one_mib = 1024 * 1024
    series = [
        LabeledSeries(
            labels={},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[float(one_mib)],
        ),
    ]
    metric = _make_metric(unit="bytes", series=series)
    fig = create_metric_figure(metric)
    assert fig.data[0].y[0] == 1.0
    assert "MiB" in fig.layout.yaxis.title.text


def test_unit_mib_axis():
    metric = _make_metric(unit="MiB")
    fig = create_metric_figure(metric)
    assert "MiB" in fig.layout.yaxis.title.text


def test_unit_bytes_per_second_axis():
    metric = _make_metric(unit="bytes/s")
    fig = create_metric_figure(metric)
    assert "MiB/s" in fig.layout.yaxis.title.text


def test_unit_count_axis():
    metric = _make_metric(unit="count")
    fig = create_metric_figure(metric)
    assert fig.layout.yaxis.tickformat == "d"


def test_unit_cores_axis():
    metric = _make_metric(unit="cores")
    fig = create_metric_figure(metric)
    assert "cores" in fig.layout.yaxis.title.text
    assert fig.layout.yaxis.tickformat == ".2f"


def test_unknown_unit_fallback():
    metric = _make_metric(unit="widgets")
    fig = create_metric_figure(metric)
    assert len(fig.data) == 1
    assert fig.data[0].y[0] == 10.0


def test_all_builtin_units_present():
    expected = {"ratio", "percent", "bytes", "MiB", "bytes/s", "count", "cores"}
    assert set(UNIT_FORMATTERS.keys()) == expected


def test_build_legend_info_common_labels():
    series = [
        LabeledSeries(
            labels={"namespace": "forge-praxis", "pod": "pod-a"},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[1.0],
        ),
        LabeledSeries(
            labels={"namespace": "forge-praxis", "pod": "pod-b"},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[2.0],
        ),
    ]
    metric = _make_metric(series=series)
    legend = build_legend_info(metric)

    assert legend.common_labels == {"namespace": "forge-praxis"}
    assert len(legend.entries) == 2
    assert legend.entries[0].short_name == "pod-a"
    assert legend.entries[0].full_labels == {"namespace": "forge-praxis", "pod": "pod-a"}


def test_build_legend_info_no_series():
    metric = _make_metric(series=[])
    legend = build_legend_info(metric)
    assert legend.common_labels == {}
    assert legend.entries == []


def test_suppressed_label_keys_excluded_from_short_name():
    long_id = "/kubepods.slice/kubepods-besteffort.slice/crio-abc123.scope"
    series = [
        LabeledSeries(
            labels={"pod": "pod-a", "id": long_id, "image": "ghcr.io/org/app:v1"},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[1.0],
        ),
        LabeledSeries(
            labels={"pod": "pod-b", "id": long_id, "image": "ghcr.io/org/app:v1"},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[2.0],
        ),
    ]
    metric = _make_metric(series=series)
    fig = create_metric_figure(metric)

    trace_names = {t.name for t in fig.data}
    assert trace_names == {"pod-a", "pod-b"}
    assert not any("kubepods" in t.name for t in fig.data)
    assert not any("ghcr.io" in t.name for t in fig.data)


def test_long_label_values_excluded_from_short_name():
    long_value = "x" * 100
    series = [
        LabeledSeries(
            labels={"pod": "pod-a", "annotation": long_value},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[1.0],
        ),
        LabeledSeries(
            labels={"pod": "pod-b", "annotation": long_value},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[2.0],
        ),
    ]
    metric = _make_metric(series=series)
    fig = create_metric_figure(metric)

    trace_names = {t.name for t in fig.data}
    assert trace_names == {"pod-a", "pod-b"}


def test_suppressed_labels_kept_in_full_labels():
    series = [
        LabeledSeries(
            labels={"pod": "pod-a", "id": "/cgroup/path", "name": "k8s_container_pod_ns_uid_0"},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[1.0],
        ),
    ]
    metric = _make_metric(series=series)
    legend = build_legend_info(metric)

    assert legend.entries[0].full_labels["id"] == "/cgroup/path"
    assert legend.entries[0].full_labels["name"] == "k8s_container_pod_ns_uid_0"
    assert "id" not in legend.entries[0].short_name
    assert "name" not in legend.entries[0].short_name


def test_all_distinguishing_suppressed_falls_back_to_metric_name():
    series = [
        LabeledSeries(
            labels={"id": "/cgroup/a", "name": "k8s_a"},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[1.0],
        ),
        LabeledSeries(
            labels={"id": "/cgroup/b", "name": "k8s_b"},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[2.0],
        ),
    ]
    metric = _make_metric(series=series, metric_name="cpu_usage")
    fig = create_metric_figure(metric)

    trace_names = [t.name for t in fig.data]
    assert trace_names == [
        "cpu_usage (id=/cgroup/a, name=k8s_a)",
        "cpu_usage (id=/cgroup/b, name=k8s_b)",
    ]


def test_legend_labels_selects_specific_keys():
    series = [
        LabeledSeries(
            labels={"namespace": "ns", "pod": "pod-a", "container": "main", "id": "/cgroup/a"},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[1.0],
        ),
        LabeledSeries(
            labels={"namespace": "ns", "pod": "pod-b", "container": "worker", "id": "/cgroup/b"},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[2.0],
        ),
    ]
    metric = _make_metric(series=series, legend_labels=["container", "pod"])
    fig = create_metric_figure(metric)

    trace_names = {t.name for t in fig.data}
    assert "container=main, pod=pod-a" in trace_names
    assert "container=worker, pod=pod-b" in trace_names
    assert not any("id=" in t.name for t in fig.data)


def test_legend_labels_single_key_uses_value_only():
    series = [
        LabeledSeries(
            labels={"namespace": "ns", "pod": "pod-a", "id": "/cgroup/a"},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[1.0],
        ),
        LabeledSeries(
            labels={"namespace": "ns", "pod": "pod-b", "id": "/cgroup/b"},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[2.0],
        ),
    ]
    metric = _make_metric(series=series, legend_labels=["pod"])
    fig = create_metric_figure(metric)

    trace_names = {t.name for t in fig.data}
    assert trace_names == {"pod-a", "pod-b"}


def test_short_name_truncated_at_64_chars():
    long_pod = "my-very-long-pod-name-that-goes-on-and-on-" + "x" * 40
    series = [
        LabeledSeries(
            labels={"pod": long_pod},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[1.0],
        ),
        LabeledSeries(
            labels={"pod": "short"},
            timestamps=[datetime(2026, 1, 1, tzinfo=UTC)],
            values=[2.0],
        ),
    ]
    metric = _make_metric(series=series, legend_labels=["pod"])
    fig = create_metric_figure(metric)

    long_trace = [t for t in fig.data if t.name != "short"][0]
    assert len(long_trace.name) == 64
    assert long_trace.name.endswith("…")


def test_combined_figure_has_usage_and_limit_traces():
    ts = [datetime(2026, 1, 1, tzinfo=UTC)]
    usage = _make_metric(
        unit="count",
        metric_name="cpu_usage_cores",
        series=[
            LabeledSeries(labels={"pod": "pod-a"}, timestamps=ts, values=[0.5]),
            LabeledSeries(labels={"pod": "pod-b"}, timestamps=ts, values=[0.8]),
        ],
    )
    limits = _make_metric(
        unit="count",
        metric_name="cpu_limits",
        series=[
            LabeledSeries(labels={"pod": "pod-a"}, timestamps=ts, values=[2.0]),
            LabeledSeries(labels={"pod": "pod-b"}, timestamps=ts, values=[2.0]),
        ],
    )
    fig = create_combined_figure(usage, limits, "CPU Usage vs Limits")

    assert len(fig.data) == 4
    names = [t.name for t in fig.data]
    assert any(n.startswith("usage:") for n in names)
    assert any(n.startswith("limit:") for n in names)


def test_combined_figure_limits_are_dashed():
    ts = [datetime(2026, 1, 1, tzinfo=UTC)]
    usage = _make_metric(
        unit="count",
        metric_name="cpu_usage",
        series=[LabeledSeries(labels={"pod": "a"}, timestamps=ts, values=[1.0])],
    )
    limits = _make_metric(
        unit="count",
        metric_name="cpu_limits",
        series=[LabeledSeries(labels={"pod": "a"}, timestamps=ts, values=[2.0])],
    )
    fig = create_combined_figure(usage, limits, "CPU")

    limit_traces = [t for t in fig.data if t.name.startswith("limit:")]
    assert all(t.line.dash == "dash" for t in limit_traces)

    usage_traces = [t for t in fig.data if t.name.startswith("usage:")]
    assert all(t.line.dash is None for t in usage_traces)


def test_combined_figure_title():
    ts = [datetime(2026, 1, 1, tzinfo=UTC)]
    usage = _make_metric(
        unit="count",
        metric_name="cpu_usage",
        series=[LabeledSeries(labels={}, timestamps=ts, values=[1.0])],
    )
    limits = _make_metric(
        unit="count",
        metric_name="cpu_limits",
        series=[LabeledSeries(labels={}, timestamps=ts, values=[2.0])],
    )
    fig = create_combined_figure(usage, limits, "CPU Usage vs Limits")

    assert fig.layout.title.text == "CPU Usage vs Limits"


def test_combined_figure_with_requests_adds_dotted_traces():
    ts = [datetime(2026, 1, 1, tzinfo=UTC)]
    usage = _make_metric(
        unit="count",
        metric_name="cpu_usage",
        series=[LabeledSeries(labels={"pod": "a"}, timestamps=ts, values=[1.0])],
    )
    limits = _make_metric(
        unit="count",
        metric_name="cpu_limits",
        series=[LabeledSeries(labels={"pod": "a"}, timestamps=ts, values=[2.0])],
    )
    requests = _make_metric(
        unit="count",
        metric_name="cpu_requests",
        series=[LabeledSeries(labels={"pod": "a"}, timestamps=ts, values=[1.5])],
    )
    fig = create_combined_figure(usage, limits, "CPU", request_metric=requests)

    request_traces = [t for t in fig.data if t.name.startswith("request:")]
    assert len(request_traces) == 1
    assert request_traces[0].line.dash == "dot"

    # without requests, no request traces are added
    fig_no_requests = create_combined_figure(usage, limits, "CPU")
    assert not [t for t in fig_no_requests.data if t.name.startswith("request:")]
