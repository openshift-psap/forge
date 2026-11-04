from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import plotly.graph_objects as go

if TYPE_CHECKING:
    from projects.caliper.postprocess.prometheus.parser import MetricTimeSeries


@dataclass(frozen=True)
class UnitFormat:
    axis_suffix: str
    tick_format: str
    value_scale: float


@dataclass(frozen=True)
class LegendEntry:
    short_name: str
    full_labels: dict[str, str]


@dataclass(frozen=True)
class LegendInfo:
    common_labels: dict[str, str]
    entries: list[LegendEntry]


UNIT_FORMATTERS: dict[str, UnitFormat] = {
    "ratio": UnitFormat(axis_suffix="%", tick_format=".1f", value_scale=1.0),
    "percent": UnitFormat(axis_suffix="%", tick_format=".1f", value_scale=1.0),
    "bytes": UnitFormat(axis_suffix="MB", tick_format=".1f", value_scale=1 / (1024 * 1024)),
    "MiB": UnitFormat(axis_suffix="MiB", tick_format=".1f", value_scale=1.0),
    "bytes/s": UnitFormat(axis_suffix="MB/s", tick_format=".2f", value_scale=1 / (1024 * 1024)),
    "count": UnitFormat(axis_suffix="", tick_format="d", value_scale=1.0),
}

_FALLBACK_FORMAT = UnitFormat(axis_suffix="", tick_format="", value_scale=1.0)

SUPPRESSED_LABEL_KEYS = frozenset({"id", "image", "name"})
MAX_LABEL_VALUE_LEN = 64


def create_metric_figure(metric: MetricTimeSeries) -> go.Figure:
    fmt = UNIT_FORMATTERS.get(metric.unit, _FALLBACK_FORMAT)
    legend = build_legend_info(metric)

    fig = go.Figure()

    for labeled_series, entry in zip(metric.series, legend.entries, strict=True):
        y_values = [v * fmt.value_scale for v in labeled_series.values]
        distinguishing = {
            k: v for k, v in entry.full_labels.items() if k not in legend.common_labels
        }
        hover_lines = [f"{k}={v}" for k, v in sorted(distinguishing.items())]
        hover_text = "<br>".join(hover_lines) if hover_lines else entry.short_name

        fig.add_trace(
            go.Scatter(
                x=labeled_series.timestamps,
                y=y_values,
                mode="lines",
                name=entry.short_name,
                hovertemplate="%{x}<br>%{y}<br>" + hover_text + "<extra></extra>",
            )
        )

    y_title = metric.description
    if fmt.axis_suffix:
        y_title = f"{y_title} ({fmt.axis_suffix})"

    fig.update_layout(
        title=metric.description,
        xaxis_title="Time",
        yaxis_title=y_title,
        yaxis_tickformat=fmt.tick_format,
    )

    return fig


def build_legend_info(metric: MetricTimeSeries) -> LegendInfo:
    if not metric.series:
        return LegendInfo(common_labels={}, entries=[])

    all_labels = [s.labels for s in metric.series]
    common = _find_common_labels(all_labels)

    entries = []
    for series in metric.series:
        distinguishing = {k: v for k, v in series.labels.items() if k not in common}
        display_labels = _filter_display_labels(distinguishing)
        short_name = _build_short_name(display_labels, metric.metric_name)
        entries.append(LegendEntry(short_name=short_name, full_labels=dict(series.labels)))

    return LegendInfo(common_labels=common, entries=entries)


def _find_common_labels(all_labels: list[dict[str, str]]) -> dict[str, str]:
    if not all_labels:
        return {}

    common = dict(all_labels[0])
    for labels in all_labels[1:]:
        to_remove = []
        for k, v in common.items():
            if labels.get(k) != v:
                to_remove.append(k)
        for k in to_remove:
            del common[k]

    return common


def _filter_display_labels(labels: dict[str, str]) -> dict[str, str]:
    return {
        k: v
        for k, v in labels.items()
        if k not in SUPPRESSED_LABEL_KEYS and len(v) <= MAX_LABEL_VALUE_LEN
    }


def _build_short_name(distinguishing: dict[str, str], metric_name: str) -> str:
    if not distinguishing:
        return metric_name

    if len(distinguishing) == 1:
        return next(iter(distinguishing.values()))

    return ", ".join(f"{k}={v}" for k, v in sorted(distinguishing.items()))
