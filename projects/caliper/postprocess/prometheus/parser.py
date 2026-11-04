from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml


@dataclass(frozen=True)
class LabeledSeries:
    labels: dict[str, str]
    timestamps: list[datetime]
    values: list[float]


@dataclass(frozen=True)
class MetricTimeSeries:
    metric_name: str
    description: str
    unit: str
    source_file: str
    query: str
    series: list[LabeledSeries]


@dataclass(frozen=True)
class CaptureGroupResult:
    group_name: str
    variables: dict[str, str]
    metrics: list[MetricTimeSeries]


def parse_capture_group(capture_dir: str | Path) -> CaptureGroupResult:
    capture_dir = Path(capture_dir)
    prom_dir = capture_dir / "prometheus_metrics"
    profiles_dir = capture_dir / "src" / "metric_profiles"

    metadata_path = prom_dir / "metrics_metadata.yaml"
    if not metadata_path.exists():
        raise FileNotFoundError(f"metrics_metadata.yaml not found in {prom_dir}")

    with metadata_path.open("r", encoding="utf-8") as f:
        metadata = yaml.safe_load(f)

    variables = metadata.get("variables", {})
    results_index = metadata.get("results", {})

    group_name = capture_dir.name
    source_map = _build_metric_source_map(profiles_dir)

    metrics: list[MetricTimeSeries] = []
    for metric_name, meta in results_index.items():
        status = meta.get("status", "no_data")
        description = meta.get("description", "")
        unit = meta.get("unit", "")
        source_info = source_map.get(metric_name)
        source_file = source_info.source_file if source_info else ""
        query = source_info.query if source_info else ""

        if status != "ok":
            metrics.append(
                MetricTimeSeries(
                    metric_name=metric_name,
                    description=description,
                    unit=unit,
                    source_file=source_file,
                    query=query,
                    series=[],
                )
            )
            continue

        json_path = prom_dir / f"{metric_name}.json"
        if not json_path.exists():
            raise FileNotFoundError(
                f"Metric {metric_name!r} has status 'ok' but {json_path} does not exist"
            )

        series = _parse_metric_json(json_path, metric_name)
        metrics.append(
            MetricTimeSeries(
                metric_name=metric_name,
                description=description,
                unit=unit,
                source_file=source_file,
                query=query,
                series=series,
            )
        )

    return CaptureGroupResult(
        group_name=group_name,
        variables=variables,
        metrics=metrics,
    )


def filter_by_source_file(
    result: CaptureGroupResult,
    source_file_names: list[str],
) -> CaptureGroupResult:
    names = set(source_file_names)
    filtered = [m for m in result.metrics if m.source_file in names]
    return CaptureGroupResult(
        group_name=result.group_name,
        variables=result.variables,
        metrics=filtered,
    )


@dataclass(frozen=True)
class _MetricSourceInfo:
    source_file: str
    query: str


def _build_metric_source_map(profiles_dir: Path) -> dict[str, _MetricSourceInfo]:
    mapping: dict[str, _MetricSourceInfo] = {}
    if not profiles_dir.exists():
        return mapping

    for yaml_path in sorted(profiles_dir.glob("*.yaml")):
        source_name = yaml_path.stem
        with yaml_path.open("r", encoding="utf-8") as f:
            entries = yaml.safe_load(f)

        if not isinstance(entries, list):
            continue

        for entry in entries:
            if isinstance(entry, dict) and "metricName" in entry:
                mapping[entry["metricName"]] = _MetricSourceInfo(
                    source_file=source_name,
                    query=entry.get("query", ""),
                )

    return mapping


def _parse_metric_json(json_path: Path, metric_name: str) -> list[LabeledSeries]:
    try:
        with json_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Failed to parse JSON for metric {metric_name!r} at {json_path}: {exc}"
        ) from exc

    if not isinstance(data, list):
        raise ValueError(
            f"Expected a JSON array for metric {metric_name!r} at {json_path}, "
            f"got {type(data).__name__}"
        )

    groups: dict[tuple[tuple[str, str], ...], tuple[list[datetime], list[float]]] = {}

    for point in data:
        if not isinstance(point, dict):
            raise ValueError(
                f"Expected dict entries in metric {metric_name!r} at {json_path}, "
                f"got {type(point).__name__}"
            )

        labels = point.get("labels", {})
        label_key = tuple(sorted(labels.items()))
        timestamp_str = point.get("timestamp", "")
        value = point.get("value", 0.0)

        ts = _parse_timestamp(timestamp_str)

        if label_key not in groups:
            groups[label_key] = ([], [])

        groups[label_key][0].append(ts)
        groups[label_key][1].append(float(value))

    series_list: list[LabeledSeries] = []
    for label_key, (timestamps, values) in sorted(groups.items()):
        series_list.append(
            LabeledSeries(
                labels=dict(label_key),
                timestamps=timestamps,
                values=values,
            )
        )

    return series_list


def _parse_timestamp(ts_str: str) -> datetime:
    if not ts_str:
        return datetime.fromtimestamp(0, tz=UTC)

    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.fromtimestamp(float(ts_str), tz=UTC)
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"Cannot parse timestamp {ts_str!r}") from exc
