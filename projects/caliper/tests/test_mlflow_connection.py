import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from projects.caliper.public.mlflow import download_mlflow_artifacts, resolve_mlflow_tracking_uri


def test_connection_rejects_conflicting_tracking_uri():
    connection = {"tracking_uri": "https://staging.example", "token": "fixture"}
    assert (
        resolve_mlflow_tracking_uri(connection, "https://staging.example/")
        == "https://staging.example"
    )
    with pytest.raises(ValueError, match="does not match the selected connection"):
        resolve_mlflow_tracking_uri(connection, "https://prod.example")


@pytest.mark.parametrize("fail", [False, True])
def test_direct_download_restores_tracking_state(monkeypatch, tmp_path, fail):
    prod_uri = "https://prod.example"
    staging_uri = "https://staging.example"
    tracking_uri = prod_uri
    monkeypatch.setenv("MLFLOW_TRACKING_URI", prod_uri)
    monkeypatch.setenv("MLFLOW_TRACKING_TOKEN", "fixture-prod")
    monkeypatch.setenv("MLFLOW_WORKSPACE", "prod-workspace")

    def set_tracking_uri(uri):
        nonlocal tracking_uri
        tracking_uri = uri
        os.environ["MLFLOW_TRACKING_URI"] = uri

    class FakeClient:
        def __init__(self, *, tracking_uri):
            assert tracking_uri == staging_uri

        def get_run(self, run_id):
            assert run_id == "run-123"

        def download_artifacts(self, *, run_id, path, dst_path):
            assert run_id == "run-123"
            assert path == "reports"
            assert tracking_uri == staging_uri
            assert os.environ["MLFLOW_TRACKING_TOKEN"] == "fixture-staging"
            assert os.environ["MLFLOW_WORKSPACE"] == "staging-workspace"
            if fail:
                raise RuntimeError("test download failure")
            report = Path(dst_path) / "report.txt"
            report.write_text("result")
            return str(report)

    monkeypatch.setitem(
        sys.modules,
        "mlflow",
        SimpleNamespace(
            get_tracking_uri=lambda: tracking_uri,
            set_tracking_uri=set_tracking_uri,
            tracking=SimpleNamespace(MlflowClient=FakeClient),
        ),
    )
    download = {
        "connection": {"tracking_uri": staging_uri, "token": "fixture-staging"},
        "run_id": "run-123",
        "workspace": "staging-workspace",
        "artifact_path": "reports",
        "output_dir": tmp_path,
    }
    if fail:
        with pytest.raises(RuntimeError, match="test download failure"):
            download_mlflow_artifacts(**download)
    else:
        assert download_mlflow_artifacts(**download) == 1
        assert (tmp_path / "report.txt").read_text() == "result"

    assert tracking_uri == prod_uri
    assert os.environ["MLFLOW_TRACKING_URI"] == prod_uri
    assert os.environ["MLFLOW_TRACKING_TOKEN"] == "fixture-prod"
    assert os.environ["MLFLOW_WORKSPACE"] == "prod-workspace"
