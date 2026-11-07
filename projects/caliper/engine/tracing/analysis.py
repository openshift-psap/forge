"""Pure trace analysis over sanitized OTLP JSON archives.

Reads newline-delimited OTLP JSON records, indexes spans by original trace and
span ID, deduplicates, computes structural quality, and attributes whole traces
to benchmark windows. Uses only the standard library so it can run inside the
collection toolbox as well as during Caliper export.

Preserves source trace/span IDs and timestamps: nothing here invents a root,
rewrites parent relationships, or substitutes collection time for span time.
"""

from __future__ import annotations

import gzip
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from projects.cluster.library.tracing.models import CollectionQuality, TraceWindow

# Attributes reviewed as carrying the producing service identity.
SERVICE_NAME_KEY = "service.name"


class TraceValidationError(ValueError):
    """Raised when a bundle is structurally invalid or has conflicting duplicates."""


@dataclass
class SpanRecord:
    """One span with enough context to reason about the trace tree."""

    trace_id: str
    span_id: str
    parent_span_id: str
    name: str
    start_time_unix_nano: int
    end_time_unix_nano: int
    service_name: str
    scope_name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    # OTLP status code (1=OK, 2=ERROR); the sanitizer preserves only the code.
    status_code: int = 0

    @property
    def is_root(self) -> bool:
        return not self.parent_span_id

    @property
    def duration_nano(self) -> int:
        return max(0, self.end_time_unix_nano - self.start_time_unix_nano)

    def identity(self) -> tuple[str, str]:
        return (self.trace_id, self.span_id)

    def payload_signature(self) -> tuple:
        return (
            self.name,
            self.parent_span_id,
            self.start_time_unix_nano,
            self.end_time_unix_nano,
            self.service_name,
            self.scope_name,
        )


def read_archive_records(path) -> Iterator[dict[str, Any]]:
    """Yield decoded OTLP JSON records from a gzipped or plain archive.

    Streams line by line so large captures are not loaded into memory.
    """
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            import json

            try:
                yield json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise TraceValidationError(f"invalid JSON on archive line {line_number}") from exc


def _anyvalue_to_python(value: dict[str, Any]) -> Any:
    if not isinstance(value, dict):
        return None
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            raw = value[key]
            if key == "intValue" and isinstance(raw, str):
                try:
                    return int(raw)
                except ValueError:
                    return raw
            return raw
    return None


def _attributes_to_dict(attributes: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for attr in attributes or []:
        if not isinstance(attr, dict):
            continue
        key = attr.get("key")
        if isinstance(key, str):
            result[key] = _anyvalue_to_python(attr.get("value"))
    return result


def _record_service_names(record: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for resource_span in record.get("resourceSpans", []) or []:
        resource = resource_span.get("resource") or {}
        attrs = _attributes_to_dict(resource.get("attributes"))
        name = attrs.get(SERVICE_NAME_KEY)
        names.append(str(name) if name is not None else "")
    return names


def iter_span_records(records: Iterable[dict[str, Any]]) -> Iterator[SpanRecord]:
    """Yield every span in every record with its resource/scope context."""
    for record in records:
        resource_spans = record.get("resourceSpans", []) or []
        for resource_span in resource_spans:
            resource = resource_span.get("resource") or {}
            service_name = ""
            res_attrs = _attributes_to_dict(resource.get("attributes"))
            if res_attrs.get(SERVICE_NAME_KEY) is not None:
                service_name = str(res_attrs[SERVICE_NAME_KEY])
            for scope_span in resource_span.get("scopeSpans", []) or []:
                scope_name = (scope_span.get("scope") or {}).get("name", "") or ""
                for span in scope_span.get("spans", []) or []:
                    yield SpanRecord(
                        trace_id=str(span.get("traceId", "")),
                        span_id=str(span.get("spanId", "")),
                        parent_span_id=str(span.get("parentSpanId", "") or ""),
                        name=str(span.get("name", "")),
                        start_time_unix_nano=_to_int(span.get("startTimeUnixNano")),
                        end_time_unix_nano=_to_int(span.get("endTimeUnixNano")),
                        service_name=service_name,
                        scope_name=scope_name,
                        attributes=_attributes_to_dict(span.get("attributes")),
                        status_code=_to_int((span.get("status") or {}).get("code")),
                    )


def _to_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except ValueError:
        return 0


@dataclass
class TraceIndex:
    """Spans grouped by original trace ID, with dedup and quality counts."""

    traces: dict[str, list[SpanRecord]] = field(default_factory=dict)
    duplicates: int = 0
    conflicting_duplicates: int = 0

    def all_spans(self) -> list[SpanRecord]:
        return [span for spans in self.traces.values() for span in spans]


def build_trace_index(spans: Iterable[SpanRecord]) -> TraceIndex:
    """Index spans by trace, deduplicating by (trace_id, span_id).

    Conflicting duplicates (same IDs, different payload) raise
    :class:`TraceValidationError` — they must not be silently resolved.
    """
    index = TraceIndex()
    seen: dict[tuple[str, str], SpanRecord] = {}
    for span in spans:
        if not span.trace_id or not span.span_id:
            raise TraceValidationError("span is missing trace_id or span_id")
        key = span.identity()
        existing = seen.get(key)
        if existing is not None:
            if existing.payload_signature() == span.payload_signature():
                index.duplicates += 1
                continue
            index.conflicting_duplicates += 1
            raise TraceValidationError(
                f"conflicting duplicate payload for trace {span.trace_id} span {span.span_id}"
            )
        seen[key] = span
        index.traces.setdefault(span.trace_id, []).append(span)
    return index


def analyze_quality(index: TraceIndex) -> CollectionQuality:
    """Compute structural quality indicators from an index."""
    quality = CollectionQuality()
    quality.duplicates = index.duplicates
    quality.conflicting_duplicates = index.conflicting_duplicates
    for spans in index.traces.values():
        span_ids = {s.span_id for s in spans}
        services = {s.service_name for s in spans if s.service_name}
        if len(services) > 1:
            quality.multi_service_traces += 1
        if not any(s.is_root for s in spans):
            quality.rootless_traces += 1
        for span in spans:
            if span.parent_span_id and span.parent_span_id not in span_ids:
                quality.missing_parent_spans += 1
    return quality


@dataclass
class TraceAssignment:
    """Result of attributing traces to benchmark windows."""

    assigned: dict[str, list[str]] = field(default_factory=dict)  # test_id -> trace_ids
    unassigned: list[tuple[str, str]] = field(default_factory=list)  # (trace_id, reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "assigned": {k: sorted(v) for k, v in self.assigned.items()},
            "unassigned": [{"trace_id": t, "reason": r} for t, r in self.unassigned],
        }


def _parse_window_bounds(window: TraceWindow) -> tuple[int, int]:
    start = _iso_to_ns(window.start)
    end = _iso_to_ns(window.end)
    return start, end


def _iso_to_ns(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1_000_000_000)


def assign_traces_to_windows(
    index: TraceIndex,
    windows: list[TraceWindow],
) -> TraceAssignment:
    """Assign each trace to at most one window by its root span start time.

    Pragmatic edge cases required by the plan:
    - missing root: recorded unassigned with reason ``missing-root``.
    - root start outside every window: ``outside-window``.
    - root start inside more than one window: ``ambiguous``.

    All of that trace's observed spans are retained once a trace is assigned, so
    children arriving after the benchmark end stay with their trace.
    """
    assignment = TraceAssignment()
    bounds = [(w.test_id, *_parse_window_bounds(w)) for w in windows]
    for trace_id, spans in index.traces.items():
        roots = [s for s in spans if s.is_root]
        if not roots:
            assignment.unassigned.append((trace_id, "missing-root"))
            continue
        # The earliest root is the request root of a well-formed trace.
        root_start = min(r.start_time_unix_nano for r in roots)
        matches = [test_id for test_id, start, end in bounds if start <= root_start <= end]
        if not matches:
            assignment.unassigned.append((trace_id, "outside-window"))
            continue
        if len(matches) > 1:
            assignment.unassigned.append((trace_id, "ambiguous"))
            continue
        assignment.assigned.setdefault(matches[0], []).append(trace_id)
    return assignment


def observed_services(index: TraceIndex) -> list[str]:
    services = {s.service_name for s in index.all_spans() if s.service_name}
    return sorted(services)


def count_spans(index: TraceIndex) -> int:
    return len(index.all_spans())


__all__ = [
    "SpanRecord",
    "TraceAssignment",
    "TraceIndex",
    "TraceValidationError",
    "analyze_quality",
    "assign_traces_to_windows",
    "build_trace_index",
    "count_spans",
    "iter_span_records",
    "observed_services",
    "read_archive_records",
]
