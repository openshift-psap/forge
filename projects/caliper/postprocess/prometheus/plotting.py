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
    "bytes": UnitFormat(axis_suffix="MiB", tick_format=".1f", value_scale=1 / (1024 * 1024)),
    "MiB": UnitFormat(axis_suffix="MiB", tick_format=".1f", value_scale=1.0),
    "bytes/s": UnitFormat(axis_suffix="MiB/s", tick_format=".2f", value_scale=1 / (1024 * 1024)),
    "count": UnitFormat(axis_suffix="", tick_format="d", value_scale=1.0),
}

_FALLBACK_FORMAT = UnitFormat(axis_suffix="", tick_format="", value_scale=1.0)

SUPPRESSED_LABEL_KEYS = frozenset({"id", "image", "name"})
MAX_LABEL_VALUE_LEN = 64
MAX_SHORT_NAME_LEN = 64


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

    legend_keys = metric.legend_labels if metric.legend_labels else None

    entries = []
    for series in metric.series:
        distinguishing = {k: v for k, v in series.labels.items() if k not in common}
        if legend_keys:
            display_labels = {k: v for k, v in distinguishing.items() if k in legend_keys}
        else:
            display_labels = _filter_display_labels(distinguishing)
        short_name = _build_short_name(display_labels, metric.metric_name)
        if len(short_name) > MAX_SHORT_NAME_LEN:
            short_name = short_name[: MAX_SHORT_NAME_LEN - 1] + "…"
        entries.append(LegendEntry(short_name=short_name, full_labels=dict(series.labels)))

    entries = _resolve_name_collisions(entries, common)

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


def _resolve_name_collisions(
    entries: list[LegendEntry], common: dict[str, str]
) -> list[LegendEntry]:
    """Add suffixes to colliding short names so every legend entry is unique."""
    from collections import defaultdict

    groups: dict[str, list[int]] = defaultdict(list)
    for i, entry in enumerate(entries):
        groups[entry.short_name].append(i)

    has_collisions = any(len(indices) > 1 for indices in groups.values())
    if not has_collisions:
        return entries

    resolved = list(entries)
    for name, indices in groups.items():
        if len(indices) <= 1:
            continue

        group_entries = [entries[i] for i in indices]
        all_keys: set[str] = set()
        for e in group_entries:
            all_keys.update(k for k in e.full_labels if k not in common)

        varying_keys = sorted(
            k for k in all_keys if len({e.full_labels.get(k) for e in group_entries}) > 1
        )

        for idx in indices:
            entry = entries[idx]
            if varying_keys:
                suffix = ", ".join(
                    f"{k}={entry.full_labels[k]}" for k in varying_keys if k in entry.full_labels
                )
                new_name = f"{name} ({suffix})" if suffix else name
            else:
                new_name = f"{name} ({indices.index(idx) + 1})"

            if len(new_name) > MAX_SHORT_NAME_LEN:
                new_name = new_name[: MAX_SHORT_NAME_LEN - 1] + "…"

            resolved[idx] = LegendEntry(short_name=new_name, full_labels=entry.full_labels)

    return resolved


def create_combined_figure(
    usage_metric: MetricTimeSeries,
    limit_metric: MetricTimeSeries,
    title: str,
) -> go.Figure:
    fmt = UNIT_FORMATTERS.get(usage_metric.unit, _FALLBACK_FORMAT)
    fig = go.Figure()

    usage_legend = build_legend_info(usage_metric)
    for labeled_series, entry in zip(usage_metric.series, usage_legend.entries, strict=True):
        y_values = [v * fmt.value_scale for v in labeled_series.values]
        hover_text = _build_hover_text(entry, usage_legend, "usage")
        fig.add_trace(
            go.Scatter(
                x=labeled_series.timestamps,
                y=y_values,
                mode="lines",
                name=f"usage: {entry.short_name}",
                hovertemplate="%{x}<br>%{y}<br>" + hover_text + "<extra></extra>",
            )
        )

    limit_legend = build_legend_info(limit_metric)
    for labeled_series, entry in zip(limit_metric.series, limit_legend.entries, strict=True):
        y_values = [v * fmt.value_scale for v in labeled_series.values]
        hover_text = _build_hover_text(entry, limit_legend, "limit")
        fig.add_trace(
            go.Scatter(
                x=labeled_series.timestamps,
                y=y_values,
                mode="lines",
                name=f"limit: {entry.short_name}",
                line=dict(dash="dash"),
                hovertemplate="%{x}<br>%{y}<br>" + hover_text + "<extra></extra>",
            )
        )

    y_title = title
    if fmt.axis_suffix:
        y_title = f"{title} ({fmt.axis_suffix})"

    tick_format = fmt.tick_format
    if tick_format == "d":
        tick_format = ".2f"

    fig.update_layout(
        title=title,
        xaxis_title="Time",
        yaxis_title=y_title,
        yaxis_tickformat=tick_format,
    )

    return fig


def _build_hover_text(entry: LegendEntry, legend: LegendInfo, prefix: str) -> str:
    distinguishing = {k: v for k, v in entry.full_labels.items() if k not in legend.common_labels}
    hover_lines = [f"{k}={v}" for k, v in sorted(distinguishing.items())]
    if hover_lines:
        return prefix + "<br>" + "<br>".join(hover_lines)
    return f"{prefix}: {entry.short_name}"
