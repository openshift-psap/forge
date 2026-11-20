"""Attribution and quality tests for trace bundles.

Covers the plan's Attribution and Collection rows: two sequential workloads,
warmup exclusion, a long request crossing a boundary, overlapping-window
rejection, missing root/parent, and duplicate handling.
"""

from __future__ import annotations

import pytest

from projects.caliper.engine.tracing.analysis import (
    SpanRecord,
    TraceValidationError,
    analyze_quality,
    assign_traces_to_windows,
    build_trace_index,
)
from projects.cluster.library.tracing.models import TraceWindow


def _ns(seconds: float) -> int:
    return int(seconds * 1_000_000_000)


def _span(trace, span_id, name, start_s, end_s, parent="", service="svc", **attrs):
    return SpanRecord(
        trace_id=trace,
        span_id=span_id,
        parent_span_id=parent,
        name=name,
        start_time_unix_nano=_ns(start_s),
        end_time_unix_nano=_ns(end_s),
        service_name=service,
        scope_name="vllm",
        attributes=attrs,
    )


def _window(test_id, start_s, end_s):
    from datetime import UTC, datetime

    return TraceWindow(
        test_id=test_id,
        start=datetime.fromtimestamp(start_s, UTC).isoformat(),
        end=datetime.fromtimestamp(end_s, UTC).isoformat(),
        artifact_dir=test_id,
    )


def test_two_sequential_workloads_are_attributed_separately():
    spans = [
        _span("t1", "s1", "root", 100, 101),
        _span("t2", "s2", "root", 200, 201),
    ]
    index = build_trace_index(spans)
    windows = [_window("workload-a", 90, 150), _window("workload-b", 190, 250)]
    assignment = assign_traces_to_windows(index, windows)
    assert assignment.assigned == {"workload-a": ["t1"], "workload-b": ["t2"]}
    assert assignment.unassigned == []


def test_warmup_window_is_excluded_by_time():
    # A warmup request before the measured window is not attributed to the test.
    spans = [_span("warmup", "s0", "root", 50, 51), _span("t1", "s1", "root", 100, 101)]
    index = build_trace_index(spans)
    assignment = assign_traces_to_windows(index, [_window("workload-a", 90, 150)])
    assert assignment.assigned == {"workload-a": ["t1"]}
    assert ("warmup", "outside-window") in assignment.unassigned


def test_long_request_crossing_boundary_keeps_all_spans_with_root_window():
    # Root starts inside workload-a; a child finishes after its window ends.
    spans = [
        _span("t1", "root", "root", 100, 160),
        _span("t1", "child", "generate", 130, 170, parent="root"),
    ]
    index = build_trace_index(spans)
    windows = [_window("workload-a", 90, 150), _window("workload-b", 155, 200)]
    assignment = assign_traces_to_windows(index, windows)
    # The trace is assigned to workload-a as a whole (children retained).
    assert assignment.assigned == {"workload-a": ["t1"]}
    assert len(index.traces["t1"]) == 2


def test_overlapping_windows_make_trace_ambiguous():
    spans = [_span("t1", "root", "root", 100, 101)]
    index = build_trace_index(spans)
    windows = [_window("a", 90, 150), _window("b", 95, 155)]
    assignment = assign_traces_to_windows(index, windows)
    assert assignment.assigned == {}
    assert ("t1", "ambiguous") in assignment.unassigned


def test_missing_root_is_recorded_not_synthesized():
    spans = [_span("t1", "child", "generate", 100, 101, parent="absent")]
    index = build_trace_index(spans)
    quality = analyze_quality(index)
    assert quality.rootless_traces == 1
    assert quality.missing_parent_spans == 1
    assignment = assign_traces_to_windows(index, [_window("a", 90, 150)])
    assert ("t1", "missing-root") in assignment.unassigned


def test_multi_service_trace_is_not_completeness_proof():
    spans = [
        _span("t1", "root", "root", 100, 101, service="epp"),
        _span("t1", "child", "generate", 100, 101, parent="root", service="vllm"),
    ]
    index = build_trace_index(spans)
    quality = analyze_quality(index)
    assert quality.multi_service_traces == 1


def test_identical_duplicate_spans_are_deduplicated():
    spans = [_span("t1", "s1", "root", 100, 101), _span("t1", "s1", "root", 100, 101)]
    index = build_trace_index(spans)
    assert index.duplicates == 1
    assert len(index.traces["t1"]) == 1


def test_conflicting_duplicate_spans_are_a_validation_failure():
    spans = [_span("t1", "s1", "root", 100, 101), _span("t1", "s1", "different", 100, 101)]
    with pytest.raises(TraceValidationError, match="conflicting duplicate"):
        build_trace_index(spans)


def test_span_missing_ids_is_rejected():
    bad = _span("", "", "root", 100, 101)
    with pytest.raises(TraceValidationError, match="missing trace_id or span_id"):
        build_trace_index([bad])
