"""Multi-run export integration: real child runs, attribution, no duplicates.

Exercises ``log_multi_run_artifacts`` against a disposable SQL-backed MLflow
server with two test nodes, each carrying a trace bundle. Verifies:
- each bundle is linked to its own child run (two-workload attribution);
- a repeated export creates zero new child runs;
- the child-run map is persisted at the artifact root.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
import yaml

from projects.caliper.engine.constants import METRICS_FILE, PARAMETERS_FILE
from projects.caliper.engine.file_export import mlflow_backend
from projects.caliper.engine.tracing.bundle import (
    ARCHIVE_FILENAME,
    SUMMARY_FILENAME,
    sha256_of_file,
)
from projects.caliper.engine.tracing.child_runs import (
    CHILD_RUN_MAP_FILENAME,
    read_child_run_map,
)
from projects.caliper.engine.tracing.exporter import derive_mlflow_trace_ids
from projects.caliper.engine.tracing.ingest import TraceExportContext

pytestmark = pytest.mark.integration


def _record(trace_hex: str, service: str) -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": service}},
                        {"key": "forge.capture_id", "value": {"stringValue": "cap-x"}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "vllm"},
                        "spans": [
                            {
                                "traceId": trace_hex,
                                "spanId": "0000000000000001",
                                "name": "root",
                                "startTimeUnixNano": "1700000000000000000",
                                "endTimeUnixNano": "1700000001000000000",
                            }
                        ],
                    }
                ],
            }
        ]
    }


def _test_node(root: Path, name: str, trace_hex: str, service: str) -> Path:
    node = root / name
    node.mkdir(parents=True, exist_ok=True)
    (node / "__caliper_test_metadata__.yaml").write_text(
        yaml.safe_dump({"version": "1", "labels": {"workload": name}}), encoding="utf-8"
    )
    (node / METRICS_FILE).write_text(json.dumps({"requests": 100}), encoding="utf-8")
    (node / PARAMETERS_FILE).write_text(json.dumps({"workload": name}), encoding="utf-8")

    bundle_dir = node / "001__capture" / "artifacts" / "traces"
    bundle_dir.mkdir(parents=True)
    archive = bundle_dir / ARCHIVE_FILENAME
    with gzip.open(archive, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(_record(trace_hex, service)) + "\n")
    summary = {
        "schema_version": 1,
        "identity": {"schema_version": 1, "test_id": name},
        "config": {"mode": "standard", "sample_ratio": 1.0},
        "time": {
            "benchmark_window": {
                "start": "2023-11-14T22:13:20+00:00",
                "end": "2023-11-14T22:13:59+00:00",
            }
        },
        "data": {
            "archive_relative_path": ARCHIVE_FILENAME,
            "checksum": {"algorithm": "sha256", "value": sha256_of_file(archive)},
            "byte_count": archive.stat().st_size,
            "trace_count": 1,
            "span_count": 1,
            "observed_services": [service],
        },
        "quality": {},
        "status": {"state": "collected", "reason": ""},
    }
    (bundle_dir / SUMMARY_FILENAME).write_text(json.dumps(summary), encoding="utf-8")
    return node


def _all_artifacts(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


def _run_export(
    root: Path, tracking_uri: str, experiment: str, trace_export, parent_run_id: str
) -> dict:
    """Re-run the export against the *same* parent run, as orchestration resumes it."""
    _detail, meta = mlflow_backend.log_multi_run_artifacts(
        all_artifact_paths=_all_artifacts(root),
        artifact_root=root,
        run_dirs=_discover(root),
        metrics_file=METRICS_FILE,
        parameters_file=PARAMETERS_FILE,
        tracking_uri=tracking_uri,
        experiment=experiment,
        run_id=parent_run_id,
        parent_run_name="parent",
        connection=None,
        verbose=False,
        workspace=None,
        trace_export=trace_export,
    )
    return meta


def _discover(root: Path) -> list[Path]:
    from projects.caliper.engine.file_export.artifacts_export_run import discover_run_dirs

    return discover_run_dirs(root)


def test_two_workloads_get_separate_children_and_traces(mlflow_server: str, tmp_path: Path):
    import mlflow

    mlflow.set_tracking_uri(mlflow_server)
    client = mlflow.tracking.MlflowClient()
    experiment_id = client.create_experiment("multi-trace")

    root = tmp_path / "artifacts"
    _test_node(root, "workload-a", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "forge-a")
    _test_node(root, "workload-b", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "forge-b")

    parent_run = client.create_run(experiment_id)
    parent_run_id = parent_run.info.run_id

    context = TraceExportContext()

    # Run export once (creates children + ingests traces).
    _detail, meta = mlflow_backend.log_multi_run_artifacts(
        all_artifact_paths=_all_artifacts(root),
        artifact_root=root,
        run_dirs=_discover(root),
        metrics_file=METRICS_FILE,
        parameters_file=PARAMETERS_FILE,
        tracking_uri=mlflow_server,
        experiment="multi-trace",
        run_id=parent_run_id,
        parent_run_name="parent",
        connection=None,
        verbose=False,
        workspace=None,
        trace_export=context,
    )

    mapping = read_child_run_map(root)
    assert (root / CHILD_RUN_MAP_FILENAME).is_file()
    assert len(mapping) == 2

    children_after_first = client.search_runs([experiment_id], max_results=100)
    child_count_first = sum(
        1 for r in children_after_first if r.data.tags.get("mlflow.parentRunId") == parent_run_id
    )
    assert child_count_first == 2

    # Traces are linked to their owning child run, not to the other workload's.
    trace_a = derive_mlflow_trace_ids(["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"])[0]
    trace_b = derive_mlflow_trace_ids(["bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"])[0]
    key_a = next(k for k in mapping if k.endswith("workload-a"))
    key_b = next(k for k in mapping if k.endswith("workload-b"))
    linked_a = {
        t.info.trace_id
        for t in client.search_traces(locations=[experiment_id], run_id=mapping[key_a]["run_id"])
    }
    linked_b = {
        t.info.trace_id
        for t in client.search_traces(locations=[experiment_id], run_id=mapping[key_b]["run_id"])
    }
    assert linked_a == {trace_a}
    assert linked_b == {trace_b}

    # Repeated export resumes children: no new child runs (same parent run).
    _run_export(root, mlflow_server, "multi-trace", context, parent_run_id)
    children_after_second = client.search_runs([experiment_id], max_results=100)
    child_count_second = sum(
        1 for r in children_after_second if r.data.tags.get("mlflow.parentRunId") == parent_run_id
    )
    assert child_count_second == 2, "repeated export created duplicate child runs"

    mapping2 = read_child_run_map(root)
    assert mapping2 == mapping


def test_resume_against_different_parent_is_rejected(mlflow_server: str, tmp_path: Path):
    """A stored child-run map must not attach children to a different parent run."""
    import mlflow

    from projects.caliper.engine.tracing.child_runs import ChildRunMapError

    mlflow.set_tracking_uri(mlflow_server)
    client = mlflow.tracking.MlflowClient()
    experiment_id = client.create_experiment("multi-trace-rescope")

    root = tmp_path / "artifacts"
    _test_node(root, "workload-a", "cccccccccccccccccccccccccccccccc", "forge-a")

    first_parent = client.create_run(experiment_id).info.run_id
    context = TraceExportContext()
    _run_export(root, mlflow_server, "multi-trace-rescope", context, first_parent)

    # Re-export against a different parent run must refuse to reuse the map.
    other_parent = client.create_run(experiment_id).info.run_id
    with pytest.raises(ChildRunMapError, match="different export destination"):
        _run_export(root, mlflow_server, "multi-trace-rescope", context, other_parent)
