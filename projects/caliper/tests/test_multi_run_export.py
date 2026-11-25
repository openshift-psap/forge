"""
Tests for the multi-run caliper export pipeline.

Covers:
- Run directory auto-detection via caliper metadata file markers
- Shared vs run-specific file partitioning
- metrics.json / parameters.json reading and MLflow logging
- Parent + nested child run creation
- Single-run directory triggers flat export
- Workspace propagation
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from projects.caliper.engine.constants import METADATA_FILE, METRICS_FILE, PARAMETERS_FILE
from projects.caliper.engine.file_export.artifacts_export_run import discover_run_dirs

# Use new format for tests
TEST_LABELS_MARKER = METADATA_FILE


@contextmanager
def _mock_mlflow_modules(mock_ml: MagicMock):
    """Install a mock ``mlflow`` in ``sys.modules`` without evicting unrelated modules.

    ``patch.dict("sys.modules", ...)`` deletes from ``sys.modules`` every module
    imported while the patch is active, not just the patched keys. Modules first
    imported inside a test would then be re-imported with fresh classes in later
    tests, breaking ``isinstance`` against the surviving copies. Only the
    ``mlflow`` entries are installed and restored here.
    """
    saved = {key: sys.modules.get(key) for key in ("mlflow", "mlflow.tracking")}
    sys.modules["mlflow"] = mock_ml
    sys.modules["mlflow.tracking"] = mock_ml.tracking
    try:
        yield mock_ml
    finally:
        for key in [k for k in sys.modules if k.startswith("mlflow") and k not in saved]:
            sys.modules.pop(key, None)
        for key, previous in saved.items():
            if previous is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = previous


def _create_test_metadata(directory: Path, labels: dict) -> None:
    """Helper to write a test metadata marker file."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / TEST_LABELS_MARKER).write_text(
        yaml.safe_dump({"version": "1", "labels": labels}, sort_keys=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def artifact_tree(tmp_path: Path) -> Path:
    """Build a realistic Fournos-style artifact tree with two test runs."""
    base = tmp_path / "artifacts"

    # Shared phase directories
    pre_cleanup = base / "000__pre-cleanup"
    pre_cleanup.mkdir(parents=True)
    (pre_cleanup / "run.log").write_text("pre-cleanup log")

    prepare = base / "001__prepare"
    prepare.mkdir(parents=True)
    (prepare / "run.log").write_text("prepare log")
    (prepare / "config.yaml").write_text("prepare: true")

    # Test phase with runs/ subdirectory
    test_phase = base / "002__test"
    test_phase.mkdir(parents=True)
    (test_phase / "config.yaml").write_text("test: true")
    (test_phase / "run.log").write_text("test log")

    runs = test_phase / "runs"
    runs.mkdir()

    # Run A
    run_a = runs / "mcp-smoke-s1-u16-gateway"
    run_a.mkdir()
    _create_test_metadata(
        run_a, {"preset": "smoke", "target": "gateway", "users": "16", "num_servers": "1"}
    )
    (run_a / METRICS_FILE).write_text(
        json.dumps(
            {
                "total_requests": 1000,
                "total_failures": 5,
                "failure_rate": 0.005,
                "avg_response_time_ms": 45.2,
                "p95_ms": 120.0,
                "requests_per_second": 31.5,
            }
        )
    )
    (run_a / PARAMETERS_FILE).write_text(
        json.dumps(
            {
                "preset": "smoke",
                "target": "gateway",
                "users": 16,
                "num_servers": 1,
            }
        )
    )
    (run_a / "stats.csv").write_text("Type,Name,Request Count\nPOST,/mcp,1000")
    pod_logs_a = run_a / "pod_logs"
    pod_logs_a.mkdir()
    (pod_logs_a / "gateway.log").write_text("gateway log A")

    # Run B
    run_b = runs / "mcp-smoke-s1-u64-gateway"
    run_b.mkdir()
    _create_test_metadata(
        run_b, {"preset": "smoke", "target": "gateway", "users": "64", "num_servers": "1"}
    )
    (run_b / METRICS_FILE).write_text(
        json.dumps(
            {
                "total_requests": 5000,
                "total_failures": 10,
                "failure_rate": 0.002,
                "avg_response_time_ms": 80.1,
                "p95_ms": 250.0,
                "requests_per_second": 55.2,
            }
        )
    )
    (run_b / PARAMETERS_FILE).write_text(
        json.dumps(
            {
                "preset": "smoke",
                "target": "gateway",
                "users": 64,
                "num_servers": 1,
            }
        )
    )
    (run_b / "stats.csv").write_text("Type,Name,Request Count\nPOST,/mcp,5000")

    # Export artifacts phase (shared)
    export = base / "003__export-artifacts"
    export.mkdir(parents=True)
    (export / "run.log").write_text("export log")

    return base


@pytest.fixture()
def single_run_tree(tmp_path: Path) -> Path:
    """Artifact tree without any caliper metadata file markers."""
    base = tmp_path / "artifacts"

    prepare = base / "001__prepare"
    prepare.mkdir(parents=True)
    (prepare / "run.log").write_text("prepare log")

    test_phase = base / "002__test"
    test_phase.mkdir(parents=True)
    (test_phase / "results.csv").write_text("some results")

    return base


# ---------------------------------------------------------------------------
# Test: discover_run_dirs
# ---------------------------------------------------------------------------


class TestDiscoverRunDirs:
    def test_detects_via_test_labels_markers(self, artifact_tree: Path):
        run_dirs = discover_run_dirs(artifact_tree)

        assert len(run_dirs) == 2
        names = [d.name for d in run_dirs]
        assert "mcp-smoke-s1-u16-gateway" in names
        assert "mcp-smoke-s1-u64-gateway" in names

    def test_returns_empty_when_no_markers(self, single_run_tree: Path):
        run_dirs = discover_run_dirs(single_run_tree)
        assert run_dirs == []

    def test_returns_sorted(self, artifact_tree: Path):
        run_dirs = discover_run_dirs(artifact_tree)
        names = [d.name for d in run_dirs]
        assert names == sorted(names)

    def test_ignores_dirs_without_markers(self, tmp_path: Path):
        """Directories without __test_labels__.yaml are not discovered."""
        base = tmp_path / "artifacts" / "002__test"
        runs = base / "runs"
        (runs / "run-a").mkdir(parents=True)
        (runs / "run-a" / "stats.csv").write_text("data")

        run_dirs = discover_run_dirs(tmp_path / "artifacts")
        assert run_dirs == []


# ---------------------------------------------------------------------------
# Test: shared vs run-specific file partitioning
# ---------------------------------------------------------------------------


class TestArtifactCollection:
    def test_all_artifacts_collected(self, artifact_tree: Path):
        all_files = [p for p in artifact_tree.rglob("*") if p.is_file()]
        all_names = {p.name for p in all_files}

        assert "run.log" in all_names
        assert "config.yaml" in all_names
        assert "stats.csv" in all_names
        assert METRICS_FILE in all_names
        assert PARAMETERS_FILE in all_names

    def test_run_dir_files_are_correct(self, artifact_tree: Path):
        run_dirs = discover_run_dirs(artifact_tree)
        run_a = [d for d in run_dirs if "u16" in d.name][0]
        run_a_files = {p.name for p in run_a.rglob("*") if p.is_file()}

        assert METRICS_FILE in run_a_files
        assert PARAMETERS_FILE in run_a_files
        assert "stats.csv" in run_a_files
        assert "gateway.log" in run_a_files


# ---------------------------------------------------------------------------
# Test: metrics.json / parameters.json loading
# ---------------------------------------------------------------------------


class TestMetricsParametersLoading:
    def test_load_metrics_json(self, artifact_tree: Path):
        from projects.caliper.engine.file_export.mlflow_backend import _load_json_file

        run_dirs = discover_run_dirs(artifact_tree)
        run_a = [d for d in run_dirs if "u16" in d.name][0]

        metrics = _load_json_file(run_a / METRICS_FILE)
        assert metrics["total_requests"] == 1000
        assert metrics["p95_ms"] == 120.0
        assert metrics["requests_per_second"] == 31.5

    def test_load_parameters_json(self, artifact_tree: Path):
        from projects.caliper.engine.file_export.mlflow_backend import _load_json_file

        run_dirs = discover_run_dirs(artifact_tree)
        run_a = [d for d in run_dirs if "u16" in d.name][0]

        params = _load_json_file(run_a / PARAMETERS_FILE)
        assert params["preset"] == "smoke"
        assert params["users"] == 16
        assert params["target"] == "gateway"

    def test_load_missing_file_returns_empty(self, tmp_path: Path):
        from projects.caliper.engine.file_export.mlflow_backend import _load_json_file

        result = _load_json_file(tmp_path / "nonexistent.json")
        assert result == {}

    def test_load_invalid_json_returns_empty(self, tmp_path: Path):
        from projects.caliper.engine.file_export.mlflow_backend import _load_json_file

        bad = tmp_path / "bad.json"
        bad.write_text("{invalid json")
        result = _load_json_file(bad)
        assert result == {}


# ---------------------------------------------------------------------------
# Test: MLflow multi-run logging (mocked)
# ---------------------------------------------------------------------------


class TestMultiRunMlflowLogging:
    """Test that log_multi_run_artifacts creates the correct MLflow structure."""

    @pytest.fixture()
    def mock_mlflow(self):
        mock_ml = MagicMock()

        mock_active = MagicMock()
        mock_active.info.run_id = "active-run-id"
        mock_ml.active_run.return_value = mock_active

        mock_parent_run = MagicMock()
        mock_parent_run.info.run_id = "parent-run-id"
        mock_ml.start_run.return_value.__enter__ = MagicMock(return_value=mock_parent_run)
        mock_ml.start_run.return_value.__exit__ = MagicMock(return_value=False)
        mock_ml.get_tracking_uri.return_value = "http://test-mlflow:5000"

        mock_client = MagicMock()
        mock_ml.tracking.MlflowClient.return_value = mock_client

        with _mock_mlflow_modules(mock_ml):
            yield mock_ml

    def test_creates_parent_and_child_runs(self, artifact_tree: Path, mock_mlflow):
        from projects.caliper.engine.file_export.mlflow_backend import (
            log_multi_run_artifacts,
        )

        run_dirs = discover_run_dirs(artifact_tree)
        all_files = [p for p in artifact_tree.rglob("*") if p.is_file()]

        log_multi_run_artifacts(
            all_artifact_paths=all_files,
            artifact_root=artifact_tree,
            run_dirs=run_dirs,
            metrics_file=METRICS_FILE,
            parameters_file=PARAMETERS_FILE,
            tracking_uri="http://test-mlflow:5000",
            experiment="test-experiment",
            parent_run_name="test-parent",
        )

        # Parent run should be created with run_name
        start_calls = mock_mlflow.start_run.call_args_list
        assert any(
            c.kwargs.get("run_name") == "test-parent" or (c.args and c.args[0] == "test-parent")
            for c in start_calls
        ), f"Parent run_name not found in calls: {start_calls}"

        # Nested child runs should be created
        nested_calls = [c for c in start_calls if c.kwargs.get("nested") is True]
        assert len(nested_calls) == 2

        child_names = sorted(c.kwargs.get("run_name") for c in nested_calls)
        assert child_names == ["mcp-smoke-s1-u16-gateway", "mcp-smoke-s1-u64-gateway"]

    def test_logs_metrics_from_json(self, artifact_tree: Path, mock_mlflow):
        from projects.caliper.engine.file_export.mlflow_backend import (
            log_multi_run_artifacts,
        )

        run_dirs = discover_run_dirs(artifact_tree)

        log_multi_run_artifacts(
            all_artifact_paths=[],
            artifact_root=artifact_tree,
            run_dirs=run_dirs,
            metrics_file=METRICS_FILE,
            parameters_file=PARAMETERS_FILE,
            tracking_uri="http://test-mlflow:5000",
            experiment="test-experiment",
        )

        metric_calls = mock_mlflow.log_metric.call_args_list
        metric_keys = {c.args[0] for c in metric_calls}
        assert "total_requests" in metric_keys
        assert "p95_ms" in metric_keys
        assert "requests_per_second" in metric_keys

    def test_logs_parameters_from_json(self, artifact_tree: Path, mock_mlflow):
        from projects.caliper.engine.file_export.mlflow_backend import (
            log_multi_run_artifacts,
        )

        run_dirs = discover_run_dirs(artifact_tree)

        log_multi_run_artifacts(
            all_artifact_paths=[],
            artifact_root=artifact_tree,
            run_dirs=run_dirs,
            metrics_file=METRICS_FILE,
            parameters_file=PARAMETERS_FILE,
            tracking_uri="http://test-mlflow:5000",
            experiment="test-experiment",
        )

        param_calls = mock_mlflow.log_param.call_args_list
        param_keys = {c.args[0] for c in param_calls}
        assert "preset" in param_keys
        assert "users" in param_keys
        assert "target" in param_keys

    def test_sets_experiment(self, artifact_tree: Path, mock_mlflow):
        from projects.caliper.engine.file_export.mlflow_backend import (
            log_multi_run_artifacts,
        )

        log_multi_run_artifacts(
            all_artifact_paths=[],
            artifact_root=artifact_tree,
            run_dirs=discover_run_dirs(artifact_tree),
            metrics_file=METRICS_FILE,
            parameters_file=PARAMETERS_FILE,
            tracking_uri="http://test-mlflow:5000",
            experiment="my-experiment",
        )

        mock_mlflow.set_experiment.assert_called_once_with("my-experiment")

    def test_only_parent_run_gets_workload_artifacts(self, artifact_tree: Path, mock_mlflow):
        from projects.caliper.engine.file_export.mlflow_backend import (
            log_multi_run_artifacts,
        )

        run_dirs = discover_run_dirs(artifact_tree)
        all_files = [p for p in artifact_tree.rglob("*") if p.is_file()]

        mock_client = mock_mlflow.tracking.MlflowClient.return_value

        log_multi_run_artifacts(
            all_artifact_paths=all_files,
            artifact_root=artifact_tree,
            run_dirs=run_dirs,
            metrics_file=METRICS_FILE,
            parameters_file=PARAMETERS_FILE,
            tracking_uri="http://test-mlflow:5000",
            experiment="test-experiment",
            parent_run_name="test-parent",
        )

        assert mock_client.log_artifact.call_count == len(all_files)
        for path in all_files:
            mock_client.log_artifact.assert_any_call(
                "parent-run-id",
                str(path),
                artifact_path=path.relative_to(artifact_tree).parent.as_posix(),
            )

    def test_sets_forge_tags_on_parent(self, artifact_tree: Path, mock_mlflow):
        from projects.caliper.engine.file_export.mlflow_backend import (
            log_multi_run_artifacts,
        )

        run_dirs = discover_run_dirs(artifact_tree)

        log_multi_run_artifacts(
            all_artifact_paths=[],
            artifact_root=artifact_tree,
            run_dirs=run_dirs,
            metrics_file=METRICS_FILE,
            parameters_file=PARAMETERS_FILE,
            tracking_uri="http://test-mlflow:5000",
            experiment="test",
        )

        tag_calls = mock_mlflow.set_tag.call_args_list
        tag_dict = {c.args[0]: c.args[1] for c in tag_calls}
        assert tag_dict.get("forge.multi_run") == "true"
        assert tag_dict.get("forge.child_count") == "2"

    def test_child_run_metadata_in_status(self, artifact_tree: Path, mock_mlflow):
        """Status links to child run pages, and each child points to its parent artifacts."""
        from projects.caliper.engine.file_export.mlflow_backend import (
            log_multi_run_artifacts,
        )

        run_dirs = discover_run_dirs(artifact_tree)

        child_run_ids = iter(["child-id-1", "child-id-2"])

        def _active_run_side_effect():
            mock = MagicMock()
            mock.info.run_id = next(child_run_ids, "parent-run-id")
            mock.info.experiment_id = "123"
            return mock

        mock_mlflow.active_run.side_effect = _active_run_side_effect
        mock_mlflow.start_run.return_value.__enter__.return_value.info.experiment_id = "123"

        mock_client = mock_mlflow.tracking.MlflowClient.return_value
        mock_get_run = MagicMock()
        mock_get_run.info.run_name = "test-parent"
        mock_get_run.data.tags = {}
        mock_client.get_run.return_value = mock_get_run

        mock_exp = MagicMock()
        mock_exp.name = "test-experiment"
        mock_client.get_experiment.return_value = mock_exp

        _, meta = log_multi_run_artifacts(
            all_artifact_paths=[],
            artifact_root=artifact_tree,
            run_dirs=run_dirs,
            metrics_file=METRICS_FILE,
            parameters_file=PARAMETERS_FILE,
            tracking_uri="http://test-mlflow:5000",
            experiment="test-experiment",
            parent_run_name="test-parent",
            connection={"tracking_uri": "http://test-mlflow:5000"},
            workspace="forge-sandbox",
        )

        assert meta is not None
        assert "child_runs" in meta

        child_runs = meta["child_runs"]
        assert len(child_runs) == 2

        child_names = sorted(c["run_name"] for c in child_runs)
        assert child_names == ["mcp-smoke-s1-u16-gateway", "mcp-smoke-s1-u64-gateway"]

        child_ids = {c["run_id"] for c in child_runs}
        assert child_ids == {"child-id-1", "child-id-2"}

        run_base = "http://test-mlflow:5000/#/experiments/123/runs"
        assert meta["run_url"] == f"{run_base}/parent-run-id/artifacts?workspace=forge-sandbox"
        for child in child_runs:
            assert child["run_url"] == f"{run_base}/{child['run_id']}?workspace=forge-sandbox"

        readmes = mock_client.log_text.call_args_list
        assert len(readmes) == len(child_runs)
        for child, run_dir, readme in zip(child_runs, sorted(run_dirs), readmes, strict=True):
            run_id, contents, artifact_file = readme.args
            workload_path = run_dir.relative_to(artifact_tree).as_posix()
            assert run_id == child["run_id"]
            assert artifact_file == "README.txt"
            assert f"Parent artifacts: {meta['run_url']}\n" in contents
            assert f"Workload directory: {workload_path}\n" in contents


# ---------------------------------------------------------------------------
# Test: workspace propagation
# ---------------------------------------------------------------------------


class TestWorkspacePropagation:
    @pytest.fixture()
    def _mock_mlflow(self):
        mock_ml = MagicMock()
        mock_run = MagicMock()
        mock_run.info.run_id = "test-id"
        mock_ml.start_run.return_value.__enter__ = MagicMock(return_value=mock_run)
        mock_ml.start_run.return_value.__exit__ = MagicMock(return_value=False)
        mock_ml.active_run.return_value = mock_run
        mock_ml.get_tracking_uri.return_value = "http://test:5000"
        mock_ml.tracking.MlflowClient.return_value = MagicMock()
        with _mock_mlflow_modules(mock_ml):
            yield mock_ml

    def test_workspace_set_in_env(self, artifact_tree: Path, _mock_mlflow):
        from projects.caliper.engine.file_export.mlflow_backend import (
            log_multi_run_artifacts,
        )

        os.environ.pop("MLFLOW_WORKSPACE", None)
        log_multi_run_artifacts(
            all_artifact_paths=[],
            artifact_root=artifact_tree,
            run_dirs=discover_run_dirs(artifact_tree),
            metrics_file=METRICS_FILE,
            parameters_file=PARAMETERS_FILE,
            tracking_uri="http://test:5000",
            experiment="test",
            workspace="ashtarkb",
        )
        assert os.environ.get("MLFLOW_WORKSPACE") == "ashtarkb"
        os.environ.pop("MLFLOW_WORKSPACE", None)

    def test_no_workspace_leaves_env_unchanged(self, artifact_tree: Path, _mock_mlflow):
        from projects.caliper.engine.file_export.mlflow_backend import (
            log_multi_run_artifacts,
        )

        os.environ.pop("MLFLOW_WORKSPACE", None)
        log_multi_run_artifacts(
            all_artifact_paths=[],
            artifact_root=artifact_tree,
            run_dirs=discover_run_dirs(artifact_tree),
            metrics_file=METRICS_FILE,
            parameters_file=PARAMETERS_FILE,
            tracking_uri="http://test:5000",
            experiment="test",
            workspace=None,
        )
        assert "MLFLOW_WORKSPACE" not in os.environ

    @pytest.mark.parametrize("use_vault", [True, False])
    def test_final_upload_uses_exported_workspace(
        self, artifact_tree: Path, _mock_mlflow, monkeypatch, use_vault: bool
    ):
        from projects.caliper.engine.file_export.artifacts_export_run import (
            write_artifacts_status_yaml,
        )
        from projects.caliper.engine.file_export.mlflow_backend import log_multi_run_artifacts
        from projects.caliper.engine.model import FileExportBackendResult
        from projects.core.library import env, export
        from projects.core.library.export_notifications import ExportStatus

        connection = {"tracking_uri": "http://test:5000", "token": "vault-token"}
        client = _mock_mlflow.tracking.MlflowClient.return_value
        client.get_run.return_value.info.run_name = "parent-run"
        client.get_experiment.return_value.name = "test"
        monkeypatch.setenv("MLFLOW_WORKSPACE", "default")
        monkeypatch.setenv("MLFLOW_TRACKING_TOKEN", "ambient-token")
        _, metadata = log_multi_run_artifacts(
            all_artifact_paths=[],
            artifact_root=artifact_tree,
            run_dirs=discover_run_dirs(artifact_tree),
            metrics_file=METRICS_FILE,
            parameters_file=PARAMETERS_FILE,
            experiment="test",
            tracking_uri=None,
            connection=connection,
            workspace="forge-sandbox",
        )
        assert os.environ["MLFLOW_WORKSPACE"] == "default"

        export_dir = artifact_tree / "003__export-artifacts"
        status_path = export_dir / "status.yaml"
        write_artifacts_status_yaml(
            status_path, [FileExportBackendResult("mlflow", "success", metadata=metadata)]
        )
        status = ExportStatus.from_dict(yaml.safe_load(status_path.read_text()))
        monkeypatch.setattr(export.config, "project", MagicMock())
        export.config.project.get_config.return_value = "vault-ref" if use_vault else None
        monkeypatch.setattr(export, "load_mlflow_connection", lambda *args: connection)

        def check_upload_workspace(*args, **kwargs):
            assert os.environ["MLFLOW_WORKSPACE"] == "forge-sandbox"
            assert os.environ["MLFLOW_TRACKING_TOKEN"] == (
                "vault-token" if use_vault else "ambient-token"
            )

        client.log_artifact.side_effect = check_upload_workspace
        # ARTIFACT_DIR is __getattr__-backed: monkeypatch.setattr on it would
        # materialize a real module attribute on undo and shadow __getattr__
        # for every later test in the session. TempArtifactDir restores cleanly.
        with env.TempArtifactDir(export_dir):
            export._update_final_artifacts(artifact_tree, status)

        client.log_artifact.assert_any_call(
            "test-id", str(export_dir / "run.log"), artifact_path=export_dir.name
        )
        assert client.log_artifact.call_count == 2
        assert os.environ["MLFLOW_WORKSPACE"] == "default"
        assert os.environ["MLFLOW_TRACKING_TOKEN"] == "ambient-token"


# ---------------------------------------------------------------------------
# Test: helpers/summary.py
# ---------------------------------------------------------------------------


class TestSummaryHelpers:
    def test_save_metrics(self, tmp_path: Path):
        from projects.agentic_tools.locust.helpers.parse_results import RunMetrics
        from projects.agentic_tools.locust.helpers.summary import save_metrics

        metrics = RunMetrics(
            total_requests=1000,
            total_failures=5,
            failure_rate=0.005,
            avg_response_time_ms=45.2,
            p50_ms=40.0,
            p90_ms=80.0,
            p95_ms=120.0,
            p99_ms=200.0,
            max_ms=500.0,
            requests_per_second=31.5,
        )

        path = save_metrics(metrics, tmp_path)

        assert path == tmp_path / "metrics.json"
        assert path.exists()

        data = json.loads(path.read_text())
        assert data["total_requests"] == 1000
        assert data["p95_ms"] == 120.0
        assert data["requests_per_second"] == 31.5
        assert data["failure_rate"] == 0.005

    def test_save_parameters(self, tmp_path: Path):
        from projects.agentic_tools.locust.helpers.summary import save_parameters

        path = save_parameters(
            tmp_path,
            preset="smoke",
            target="gateway",
            users=16,
            num_servers=1,
            version=None,
        )

        assert path == tmp_path / "parameters.json"
        assert path.exists()

        data = json.loads(path.read_text())
        assert data["preset"] == "smoke"
        assert data["users"] == 16
        assert data["version"] == ""  # None → empty string


# ---------------------------------------------------------------------------
# Test: helpers/parse_results.py
# ---------------------------------------------------------------------------


class TestParseResults:
    def test_parse_stats_csv(self):
        from projects.agentic_tools.locust.helpers.parse_results import parse_stats_csv

        csv_data = (
            "Type,Name,Request Count,Failure Count,Average Response Time,"
            "50%,90%,95%,99%,Max Response Time,Requests/s\n"
            "POST,/mcp/tools,800,2,42.5,35,70,90,150,300,25.0\n"
            "GET,/health,200,0,5.0,4,8,10,15,20,6.5\n"
            ",Aggregated,1000,2,33.5,30,60,80,120,300,31.5\n"
        )

        metrics = parse_stats_csv(csv_data)

        assert metrics.total_requests == 1000
        assert metrics.total_failures == 2
        assert metrics.avg_response_time_ms == 33.5
        assert metrics.p95_ms == 80.0
        assert metrics.requests_per_second == 31.5
        assert len(metrics.per_request_metrics) == 2
        assert "POST:/mcp/tools" in metrics.per_request_metrics


# ---------------------------------------------------------------------------
# Test: single marker triggers flat export
# ---------------------------------------------------------------------------


class TestSingleRunExport:
    def test_single_marker_uses_flat_export(self, tmp_path: Path):
        """When only one __test_labels__.yaml exists, export should use
        single-run (flat) mode instead of creating a parent with one nested child."""
        base = tmp_path / "artifacts"
        run_a = base / "000__mcp-smoke-s1-u16-gateway"
        run_a.mkdir(parents=True)
        _create_test_metadata(run_a, {"preset": "smoke"})
        (run_a / METRICS_FILE).write_text(json.dumps({"total_requests": 100}))
        (run_a / PARAMETERS_FILE).write_text(json.dumps({"preset": "smoke"}))

        run_dirs = discover_run_dirs(base)
        assert len(run_dirs) == 1
        assert not (len(run_dirs) > 1)
