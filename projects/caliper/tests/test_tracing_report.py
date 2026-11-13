"""Shared Caliper trace report tests.

Covers the plan's Caliper reporting row: a report is generated from retained,
sanitized artifacts (no collector, no benchmark), distinguishes span duration
from producer-supplied numeric attributes, and never fails native ingestion.

The report is pure and artifact-only, so these tests use a synthetic bundle
rather than a running server.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import yaml

from projects.caliper.engine.tracing.bundle import (
    ARCHIVE_FILENAME,
    SUMMARY_FILENAME,
    discover_bundles,
    sha256_of_file,
)
from projects.caliper.postprocess.helpers import tracing_report


def _span(trace_id, span_id, name, *, service, status_code=0, attrs=None, parent=""):
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "parentSpanId": parent,
        "name": name,
        "startTimeUnixNano": "1700000000000000000",
        "endTimeUnixNano": "1700000000500000000",
        "status": {"code": status_code},
        "attributes": attrs or [],
    }


def _record(trace_id, service, spans):
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": service}}]
                },
                "scopeSpans": [{"scope": {"name": "vllm"}, "spans": spans}],
            }
        ]
    }


def _write_bundle(root: Path) -> Path:
    test_dir = root / "000__workload"
    test_dir.mkdir(parents=True)
    (test_dir / "__caliper_test_metadata__.yaml").write_text(
        yaml.safe_dump({"version": "1", "labels": {"workload": "w"}}), encoding="utf-8"
    )
    bundle_dir = test_dir / "traces"
    bundle_dir.mkdir()
    archive = bundle_dir / ARCHIVE_FILENAME
    with gzip.open(archive, "wt", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                _record(
                    "a" * 32,
                    "forge-svc",
                    [
                        _span(
                            "a" * 32,
                            "0" * 16,
                            "POST /v1/completions",
                            service="forge-svc",
                            attrs=[
                                {
                                    "key": "gen_ai.usage.output_tokens",
                                    "value": {"intValue": "42"},
                                },
                            ],
                        ),
                        _span(
                            "a" * 32,
                            "1" * 16,
                            "generate",
                            service="forge-svc",
                            parent="0" * 16,
                            status_code=2,
                        ),
                    ],
                )
            )
            + "\n"
        )
    summary = {
        "schema_version": 1,
        "identity": {"schema_version": 1, "test_id": "000__workload"},
        "config": {"mode": "standard", "sample_ratio": 1.0, "sanitization_policy_version": 1},
        "time": {},
        "data": {
            "archive_relative_path": ARCHIVE_FILENAME,
            "checksum": {"algorithm": "sha256", "value": sha256_of_file(archive)},
            "byte_count": archive.stat().st_size,
            "trace_count": 1,
            "span_count": 2,
            "observed_services": ["forge-svc"],
        },
        "quality": {"rootless_traces": 0},
        "status": {"state": "collected", "reason": ""},
    }
    (bundle_dir / SUMMARY_FILENAME).write_text(json.dumps(summary), encoding="utf-8")
    return test_dir


def test_build_report_summarizes_operations_and_quality(tmp_path: Path):
    _write_bundle(tmp_path)
    bundle = discover_bundles(tmp_path)[0]
    report = tracing_report.build_report(bundle)

    assert report["test_id"] == "000__workload"
    assert report["totals"]["span_count"] == 2
    assert report["totals"]["error_spans"] == 1
    assert report["config"]["mode"] == "standard"

    ops = {(o["service"], o["operation"]) for o in report["operations"]}
    assert ("forge-svc", "generate") in ops
    gen = next(o for o in report["operations"] if o["operation"] == "generate")
    assert gen["error_count"] == 1
    assert gen["duration_ms"]["unit"] == "ms"

    # Producer-supplied numeric attributes are reported separately with provenance.
    names = {a["name"] for a in report["numeric_attributes"]}
    assert "gen_ai.usage.output_tokens" in names
    assert all(a["provenance"] == "producer-attribute" for a in report["numeric_attributes"])


def test_write_report_creates_json_and_html(tmp_path: Path):
    _write_bundle(tmp_path)
    bundle = discover_bundles(tmp_path)[0]
    out = tmp_path / "000__workload" / "reports"
    paths = tracing_report.write_report(bundle, out)

    assert paths["json"].is_file()
    assert paths["html"].is_file()
    html_text = paths["html"].read_text(encoding="utf-8")
    assert "Trace distribution report" in html_text
    # Reviewed span names appear in the table.
    assert "generate" in html_text


def test_orchestrator_scans_artifact_tree_not_output_dir(tmp_path: Path, monkeypatch):
    """Postprocess runs in NNN__postprocessing; bundles live in the artifact tree.

    A report must be generated from ``artifacts_dir`` even when the current
    output directory is somewhere else entirely.
    """
    from projects.caliper.orchestration.postprocess import CaliperPostprocessOrchestrator

    artifacts_dir = tmp_path / "artifacts"
    _write_bundle(artifacts_dir)

    postprocessing_dir = tmp_path / "05__postprocessing"
    postprocessing_dir.mkdir()

    orchestrator = CaliperPostprocessOrchestrator(
        {}, artifacts_dir=artifacts_dir, output_dir=postprocessing_dir
    )
    orchestrator._generate_trace_reports()

    report = artifacts_dir / "000__workload" / "reports" / tracing_report.REPORT_HTML_FILENAME
    assert report.is_file()


def test_report_failure_notifies_and_marks_result_failed(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from projects.caliper.orchestration.postprocess import CaliperPostprocessOrchestrator
    from projects.core.library import ci, postprocess, reports_index

    obj = CaliperPostprocessOrchestrator({}, artifacts_dir=tmp_path, output_dir=tmp_path)
    monkeypatch.setattr(reports_index, "generate_caliper_reports_index", Mock())
    monkeypatch.setattr(postprocess, "generate_postprocess_status_report", Mock())
    monkeypatch.setattr(
        obj, "_generate_trace_reports", Mock(side_effect=ValueError("private payload"))
    )
    notify = Mock()
    monkeypatch.setattr(ci, "add_notification_file", notify)
    result = {"success": True, "final_status": "success", "steps": obj.steps}
    obj._generate_reports(result)
    assert result["success"] is False
    assert result["steps"][-1]["trace_reports"]["status"] == "failed"
    notify.assert_called_once()
    assert "private payload" not in str(notify.call_args)
