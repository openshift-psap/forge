"""Shared Caliper trace report generation from retained artifacts.

This helper turns a validated, sanitized trace bundle into a report without a
running collector or a repeated benchmark:

- an aggregate JSON summary of observed services/operations, span counts and
  error status, duration statistics, and capture-quality indicators;
- a small self-contained HTML view of the same data.

It is deliberately pure and artifact-only: it reads the sanitized metadata
(summary JSON and per-span attributes) and never renders request or response
content. It imports no project orchestration, so any project producing a valid
bundle gets the same report contract.

Duration statistics come from OTLP span timestamps; producer-supplied numeric
attributes are reported separately with their units so trace duration is never
confused with a client end-to-end metric.
"""

from __future__ import annotations

import html
import logging
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from projects.caliper.engine.tracing import bundle as bundle_mod
from projects.caliper.engine.tracing.analysis import (
    build_trace_index,
    iter_span_records,
    read_archive_records,
)

logger = logging.getLogger(__name__)

REPORT_JSON_FILENAME = "trace_report.json"
REPORT_HTML_FILENAME = "trace_distribution_report.html"

# OTLP status codes (see the OTLP spec): 2 = ERROR.
_STATUS_ERROR = 2


@dataclass
class SpanStats:
    """Duration/error statistics for a group of spans (one service+operation)."""

    service: str
    operation: str
    count: int = 0
    error_count: int = 0
    durations_ms: list[float] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        durations = sorted(self.durations_ms)
        out: dict[str, Any] = {
            "service": self.service,
            "operation": self.operation,
            "span_count": self.count,
            "error_count": self.error_count,
        }
        if durations:
            out["duration_ms"] = {
                "min": round(durations[0], 3),
                "max": round(durations[-1], 3),
                "mean": round(statistics.fmean(durations), 3),
                "median": round(statistics.median(durations), 3),
                "p95": round(_percentile(durations, 0.95), 3),
                "unit": "ms",
            }
        return out


def _percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    index = fraction * (len(sorted_values) - 1)
    lower = int(index)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = index - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _numeric_attributes(attributes: dict[str, Any]) -> list[dict[str, Any]]:
    """Return numeric, non-duration span attributes with provenance.

    Only sanitized numeric attributes survive in the archive; names such as
    ``gen_ai.usage.output_tokens`` are reported as producer-supplied values, not
    as duration.
    """
    out: list[dict[str, Any]] = []
    for key, value in attributes.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            out.append({"name": key, "value": value, "provenance": "producer-attribute"})
    return out


def build_report(bundle: bundle_mod.TraceBundle) -> dict[str, Any]:
    """Build the report data structure for one validated bundle."""
    records = read_archive_records(bundle.archive_path)
    spans = list(iter_span_records(records))
    index = build_trace_index(spans)

    groups: dict[tuple[str, str], SpanStats] = {}
    numeric_attributes: dict[str, dict[str, Any]] = {}
    for span in index.all_spans():
        key = (span.service_name, span.name)
        stats = groups.setdefault(key, SpanStats(service=span.service_name, operation=span.name))
        stats.count += 1
        stats.durations_ms.append(span.duration_nano / 1_000_000)
        if span.status_code == _STATUS_ERROR:
            stats.error_count += 1
        for attr in _numeric_attributes(span.attributes):
            entry = numeric_attributes.setdefault(
                attr["name"], {"name": attr["name"], "count": 0, "provenance": attr["provenance"]}
            )
            entry["count"] += 1

    summary = bundle.summary
    identity = summary.get("identity", {}) if isinstance(summary, dict) else {}
    config = summary.get("config", {}) if isinstance(summary, dict) else {}
    quality = summary.get("quality", {}) if isinstance(summary, dict) else {}

    return {
        "schema_version": 1,
        "test_id": bundle.test_id,
        "identity": identity,
        "config": {
            "mode": config.get("mode"),
            "sample_ratio": config.get("sample_ratio"),
            "sanitization_policy_version": config.get("sanitization_policy_version"),
        },
        "operations": [groups[key].summary() for key in sorted(groups)],
        "numeric_attributes": [numeric_attributes[k] for k in sorted(numeric_attributes)],
        # Capture quality is kept distinct from structural span reporting.
        "capture_quality": quality,
        "totals": {
            "trace_count": len(index.traces),
            "span_count": len(index.all_spans()),
            "error_spans": sum(1 for s in index.all_spans() if s.status_code == _STATUS_ERROR),
        },
    }


def _render_html(report: dict[str, Any]) -> str:
    """Render a small self-contained HTML view (no payload content)."""
    e = html.escape

    def _row(cells: list[Any]) -> str:
        return "<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in cells) + "</tr>"

    totals = report["totals"]
    quality = report.get("capture_quality", {})
    rows = []
    for op in report["operations"]:
        dur = op.get("duration_ms", {})
        rows.append(
            _row(
                [
                    op["service"] or "(unknown)",
                    op["operation"],
                    op["span_count"],
                    op["error_count"],
                    dur.get("median", ""),
                    dur.get("p95", ""),
                ]
            )
        )

    attr_rows = [
        _row([a["name"], a["count"], a["provenance"]]) for a in report["numeric_attributes"]
    ]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Trace distribution report — {e(str(report["test_id"]))}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; }}
table {{ border-collapse: collapse; margin: 1rem 0; }}
th, td {{ border: 1px solid #ccc; padding: 4px 8px; text-align: left; }}
h2 {{ margin-top: 2rem; }}
</style>
</head>
<body>
<h1>Trace distribution report</h1>
<p>Test: <strong>{e(str(report["test_id"]))}</strong>
 | mode: {e(str(report["config"].get("mode")))}
 | sample_ratio: {e(str(report["config"].get("sample_ratio")))}</p>
<p>Traces: {e(str(totals["trace_count"]))} |
 Spans: {e(str(totals["span_count"]))} |
 Error spans: {e(str(totals["error_spans"]))}</p>
<h2>Operations</h2>
<table>
<tr><th>Service</th><th>Operation</th><th>Spans</th><th>Errors</th>
<th>Median ms</th><th>p95 ms</th></tr>
{"".join(rows) if rows else '<tr><td colspan="6">No spans</td></tr>'}
</table>
<h2>Numeric attributes (producer-supplied)</h2>
<table>
<tr><th>Name</th><th>Count</th><th>Provenance</th></tr>
{"".join(attr_rows) if attr_rows else '<tr><td colspan="3">None</td></tr>'}
</table>
<h2>Capture quality</h2>
<pre>{e(str(quality))}</pre>
</body>
</html>
"""


def write_report(bundle: bundle_mod.TraceBundle, output_dir: Path) -> dict[str, Path]:
    """Write the JSON and HTML report next to the bundle's owning test node.

    Returns the written paths. Never raises on report failure being fatal: the
    caller decides; native ingestion never depends on report generation.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(bundle)
    json_path = output_dir / REPORT_JSON_FILENAME
    json_path.write_text(_json_dumps(report), encoding="utf-8")
    html_path = output_dir / REPORT_HTML_FILENAME
    html_path.write_text(_render_html(report), encoding="utf-8")
    return {"json": json_path, "html": html_path}


def _json_dumps(data: Any) -> str:
    import json

    return json.dumps(data, indent=2, sort_keys=True)


__all__ = [
    "REPORT_HTML_FILENAME",
    "REPORT_JSON_FILENAME",
    "SpanStats",
    "build_report",
    "write_report",
]
