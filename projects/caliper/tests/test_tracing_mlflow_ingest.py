"""Stage 1: native MLflow trace ingestion against a disposable SQL server.

These tests prove the tested client/server baseline: OTLP protobuf is accepted
at ``/v1/traces``, the derived ``tr-<hex>`` trace ID resolves, traces link to
the owning run, and repeated export does not create duplicate children.

They also prove the plan's key compatibility findings:
- the server drops resource attributes, keeping only span attributes, so FORGE
  identity must be copied into ``forge.*`` span attributes in the payload;
- duplicate ingestion of the same payload is idempotent on this baseline.

All tests use the disposable ``mlflow_server`` fixture; none contact the live
tracking server.
"""

from __future__ import annotations

import gzip
import json
import tempfile
from pathlib import Path

import pytest
import yaml

from projects.caliper.engine.tracing.bundle import (
    ARCHIVE_FILENAME,
    SUMMARY_FILENAME,
    discover_bundles,
    sha256_of_file,
)
from projects.caliper.engine.tracing.exporter import (
    EXPORTER_VERSION,
    derive_mlflow_trace_ids,
    ingest_bundle,
    read_receipt,
)
from projects.caliper.engine.tracing.ingest import ingest_trace_bundles

pytestmark = pytest.mark.integration

TRACE_ID_HEX = "4bf92f3577b34da6a3ce929d0e0e4736"
ROOT_SPAN_HEX = "00f067aa0ba902b7"
CHILD_SPAN_HEX = "11f067aa0ba902b7"
# A distinct trace for the single-run tests: MLflow trace IDs are global, so
# reusing the trace above would collide with earlier tests in this module.
SINGLE_RUN_TRACE_HEX = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


def _otlp_record(
    trace_id: str = TRACE_ID_HEX,
    *,
    service_name: str = "forge-llmd-vllm",
    forge_capture_id: str = "cap-abc",
) -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": service_name}},
                        {"key": "forge.capture_id", "value": {"stringValue": forge_capture_id}},
                        {"key": "forge.component.role", "value": {"stringValue": "vllm"}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "vllm"},
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": ROOT_SPAN_HEX,
                                "name": "POST /v1/completions",
                                "kind": 2,
                                "startTimeUnixNano": "1700000000000000000",
                                "endTimeUnixNano": "1700000001000000000",
                                "attributes": [
                                    {
                                        "key": "gen_ai.usage.output_tokens",
                                        "value": {"intValue": "42"},
                                    },
                                    {
                                        "key": "http.request.method",
                                        "value": {"stringValue": "POST"},
                                    },
                                ],
                            },
                            {
                                "traceId": trace_id,
                                "spanId": CHILD_SPAN_HEX,
                                "parentSpanId": ROOT_SPAN_HEX,
                                "name": "generate",
                                "startTimeUnixNano": "1700000000100000000",
                                "endTimeUnixNano": "1700000000900000000",
                            },
                        ],
                    }
                ],
            }
        ]
    }


def _write_bundle(
    test_dir: Path,
    *,
    test_id: str,
    record: dict | None = None,
    status: str = "collected",
) -> Path:
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "__caliper_test_metadata__.yaml").write_text(
        yaml.safe_dump({"version": "1", "labels": {"test_id": test_id}}), encoding="utf-8"
    )
    bundle_dir = test_dir / "traces"
    bundle_dir.mkdir(exist_ok=True)
    archive = bundle_dir / ARCHIVE_FILENAME
    with gzip.open(archive, "wt", encoding="utf-8") as handle:
        if record is not None:
            handle.write(json.dumps(record) + "\n")
    digest = sha256_of_file(archive)
    summary = {
        "schema_version": 1,
        "identity": {"schema_version": 1, "test_id": test_id, "job_id": "job-1"},
        "config": {"mode": "standard", "sample_ratio": 1.0},
        "time": {
            "benchmark_window": {
                "start": "2023-11-14T22:13:20+00:00",
                "end": "2023-11-14T22:13:21+00:00",
            }
        },
        "data": {
            "archive_relative_path": ARCHIVE_FILENAME,
            "checksum": {"algorithm": "sha256", "value": digest},
            "byte_count": archive.stat().st_size,
            "trace_count": 1 if record else 0,
            "span_count": 2 if record else 0,
            "observed_services": ["forge-llmd-vllm"] if record else [],
        },
        "quality": {},
        "status": {"state": status, "reason": ""},
    }
    (bundle_dir / SUMMARY_FILENAME).write_text(json.dumps(summary), encoding="utf-8")
    return bundle_dir


def _create_experiment_and_run(tracking_uri: str, name: str):
    import mlflow

    mlflow.set_tracking_uri(tracking_uri)
    client = mlflow.tracking.MlflowClient()
    experiment_id = client.create_experiment(name)
    run = client.create_run(experiment_id)
    return client, experiment_id, run.info.run_id


def test_ingest_and_link_traces(mlflow_server: str) -> None:
    """A sanitized bundle is ingested natively and linked to its owning run."""
    client, experiment_id, run_id = _create_experiment_and_run(mlflow_server, "traces-happy")
    with tempfile.TemporaryDirectory() as tmp:
        test_dir = Path(tmp) / "test_node"
        _write_bundle(test_dir, test_id="t1", record=_otlp_record())
        bundle = discover_bundles(Path(tmp))[0]

        outcome = ingest_bundle(
            bundle=bundle,
            run_id=run_id,
            tracking_uri=mlflow_server,
            experiment_id=experiment_id,
            workspace="",
            connection=None,
            archive_sha256=sha256_of_file(bundle.archive_path),
        )

        assert outcome.status == "complete"
        assert outcome.ingested == 1
        assert outcome.linked == 1

        mlflow_trace_id = derive_mlflow_trace_ids([TRACE_ID_HEX])[0]
        assert mlflow_trace_id == f"tr-{TRACE_ID_HEX}"
        trace = client.get_trace(mlflow_trace_id)
        assert trace.info.trace_id == mlflow_trace_id
        assert len(trace.data.spans) == 2

        # The server keeps span attributes; FORGE identity must be on the span.
        root_span = next(s for s in trace.data.spans if s.parent_id is None)
        assert root_span.attributes.get("forge.capture_id") == "cap-abc"
        assert root_span.attributes.get("http.request.method") == "POST"


def test_repeated_export_does_not_duplicate(mlflow_server: str) -> None:
    """A scope-matching receipt short-circuits, and duplicate ingestion is idempotent."""
    client, experiment_id, run_id = _create_experiment_and_run(mlflow_server, "traces-repeat")
    with tempfile.TemporaryDirectory() as tmp:
        test_dir = Path(tmp) / "test_node"
        _write_bundle(test_dir, test_id="t1", record=_otlp_record())
        bundle = discover_bundles(Path(tmp))[0]
        sha = sha256_of_file(bundle.archive_path)

        first = ingest_bundle(
            bundle=bundle,
            run_id=run_id,
            tracking_uri=mlflow_server,
            experiment_id=experiment_id,
            workspace="",
            connection=None,
            archive_sha256=sha,
        )
        assert first.status == "complete"

        receipt = read_receipt(bundle.bundle_dir / "mlflow-export.json")
        assert receipt is not None
        assert receipt.exporter_version == EXPORTER_VERSION

        second = ingest_bundle(
            bundle=bundle,
            run_id=run_id,
            tracking_uri=mlflow_server,
            experiment_id=experiment_id,
            workspace="",
            connection=None,
            archive_sha256=sha,
            existing_receipt=receipt,
        )
        assert second.status == "complete"
        assert second.ingested == 1

        # The trace still has exactly the two original spans.
        mlflow_trace_id = derive_mlflow_trace_ids([TRACE_ID_HEX])[0]
        assert len(client.get_trace(mlflow_trace_id).data.spans) == 2


def test_receipt_scope_mismatch_is_rejected(mlflow_server: str) -> None:
    """A receipt from a different run/archive is not reused silently."""
    from projects.caliper.engine.tracing.exporter import ExportReceipt, TraceExportError

    _client, experiment_id, run_id = _create_experiment_and_run(mlflow_server, "traces-scope")
    with tempfile.TemporaryDirectory() as tmp:
        test_dir = Path(tmp) / "test_node"
        _write_bundle(test_dir, test_id="t1", record=_otlp_record())
        bundle = discover_bundles(Path(tmp))[0]

        foreign = ExportReceipt(
            test_id="t1",
            tracking_uri=mlflow_server,
            workspace="",
            experiment_id=experiment_id,
            run_id="some-other-run",
            archive_sha256="deadbeef",
        )
        with pytest.raises(TraceExportError):
            ingest_bundle(
                bundle=bundle,
                run_id=run_id,
                tracking_uri=mlflow_server,
                experiment_id=experiment_id,
                workspace="",
                connection=None,
                archive_sha256="0000",
                existing_receipt=foreign,
            )


def test_ingest_skips_when_no_run_resolved(mlflow_server: str, tmp_path: Path) -> None:
    """Enabling native export without a resolvable run reports skipped, not success."""
    import mlflow

    mlflow.set_tracking_uri(mlflow_server)
    experiment_id = mlflow.tracking.MlflowClient().create_experiment("traces-skip")

    test_dir = tmp_path / "test_node"
    _write_bundle(test_dir, test_id="t-skip", record=_otlp_record())

    report = ingest_trace_bundles(
        artifact_root=tmp_path,
        tracking_uri=mlflow_server,
        experiment_id=experiment_id,
        workspace="",
        connection=None,
        resolve_run_id=lambda _d: None,
    )
    assert report.discovered == 1
    assert report.outcomes == []
    assert report.skipped
    assert not report.failed


def test_no_bundles_is_a_clear_skip(mlflow_server: str, tmp_path: Path) -> None:
    """Enabling native export without any bundle produces a skipped result."""
    import mlflow

    mlflow.set_tracking_uri(mlflow_server)
    experiment_id = mlflow.tracking.MlflowClient().create_experiment("traces-empty")

    report = ingest_trace_bundles(
        artifact_root=tmp_path,
        tracking_uri=mlflow_server,
        experiment_id=experiment_id,
        workspace="",
        connection=None,
        resolve_run_id=lambda _d: "run-x",
    )
    assert report.discovered == 0
    assert report.skipped
    assert not report.failed


def test_single_run_export_ingests_traces(mlflow_server: str, tmp_path: Path) -> None:
    """The single-run file-export path also ingests traces into its owning run.

    Regression guard for the runner pass-through: a single-run export must
    forward ``trace_export`` to ``log_artifacts`` so native ingestion happens,
    not just the multi-run path.
    """
    import mlflow

    from projects.caliper.engine.file_export import runner
    from projects.caliper.engine.tracing.ingest import TraceExportContext

    mlflow.set_tracking_uri(mlflow_server)
    client = mlflow.tracking.MlflowClient()
    experiment_id = client.create_experiment("traces-single-run")
    run = client.create_run(experiment_id)

    test_dir = tmp_path / "test_node"
    _write_bundle(test_dir, test_id="t1", record=_otlp_record(SINGLE_RUN_TRACE_HEX))

    context = TraceExportContext(tracking_uri=mlflow_server, workspace="", connection=None)
    results = runner.run_file_export(
        source=tmp_path,
        backends=["mlflow"],
        dry_run=False,
        mlflow_tracking_uri=mlflow_server,
        mlflow_experiment="traces-single-run",
        mlflow_run_id=run.info.run_id,
        mlflow_connection=None,
        verbose=False,
        trace_export=context,
    )

    assert len(results) == 1
    assert results[0].metadata["trace_export"]["outcomes"][0]["ingested"] == 1

    mlflow_trace_id = derive_mlflow_trace_ids([SINGLE_RUN_TRACE_HEX])[0]
    linked = {
        t.info.trace_id
        for t in client.search_traces(locations=[experiment_id], run_id=run.info.run_id)
    }
    assert mlflow_trace_id in linked


def test_single_run_export_reports_trace_failure_status(mlflow_server: str, tmp_path: Path) -> None:
    """A native trace failure marks the single-run backend as failed, not success."""
    import mlflow

    from projects.caliper.engine.file_export import runner
    from projects.caliper.engine.tracing.ingest import TraceExportContext

    mlflow.set_tracking_uri(mlflow_server)
    client = mlflow.tracking.MlflowClient()
    experiment_id = client.create_experiment("traces-single-fail")
    run = client.create_run(experiment_id)

    test_dir = tmp_path / "test_node"
    _write_bundle(test_dir, test_id="t1", record=_otlp_record())

    # A tracking URI the exporter cannot reach makes native ingestion fail while
    # the ordinary artifact upload (already done above) stays independent.
    dead_uri = "http://127.0.0.1:1"
    context = TraceExportContext(tracking_uri=dead_uri, workspace="", connection=None)
    results = runner.run_file_export(
        source=tmp_path,
        backends=["mlflow"],
        dry_run=False,
        mlflow_tracking_uri=mlflow_server,
        mlflow_experiment="traces-single-fail",
        mlflow_run_id=run.info.run_id,
        mlflow_connection=None,
        verbose=False,
        trace_export=context,
    )
    assert results[0].status == "failure"
    assert "native trace export failed" in results[0].detail
