from types import SimpleNamespace

from projects.core.library import config
from projects.rhaiis.orchestration import analysis


def _mock_config(monkeypatch, values: dict) -> None:
    monkeypatch.setattr(
        config,
        "project",
        SimpleNamespace(get_config=lambda path, default=None, **kwargs: values.get(path, default)),
    )


def _capture_skip(monkeypatch) -> dict:
    captured: dict = {}

    def capture(reason, **kwargs):
        captured["reason"] = reason
        captured.update(kwargs)

    monkeypatch.setattr(analysis, "_notify_comparison_skipped", capture)
    return captured


def test_standalone_analysis_reports_missing_credentials(monkeypatch) -> None:
    _mock_config(
        monkeypatch,
        {
            "tests.rhaiis.version": "v2",
            "tests.rhaiis.compare_version": "v1",
            "rhaiis.s3": {},
            "caliper.postprocess.csv_dashboard": {},
            "rhaiis.accelerator": "nvidia",
        },
    )
    monkeypatch.setattr("projects.caliper.cli.s3_export.get_aws_credentials", lambda *a, **k: None)
    captured = _capture_skip(monkeypatch)

    analysis.run_standalone_analysis(
        {"hf_model_id": "org/model"},
        "h200_hera2",
        {"tensor-parallel-size": 2},
        run_uuid="job-1",
    )

    assert "credentials" in captured["reason"]
    assert captured["model"] == "org/model"
    assert captured["accelerator"] == "nvidia"
    assert captured["job_id"] == "job-1"
    assert captured["tp"] == "2"


def test_regression_check_reports_missing_credentials(monkeypatch) -> None:
    _mock_config(
        monkeypatch,
        {
            "ci_job.owner": "owner",
            "rhaiis.s3": {},
            "caliper.postprocess.csv_dashboard": {},
        },
    )
    monkeypatch.setattr("projects.caliper.cli.s3_export.get_aws_credentials", lambda *a, **k: None)
    captured = _capture_skip(monkeypatch)

    analysis.run_regression_check(
        "/nonexistent/current.csv",
        "v1",
        "v2",
        {"hf_model_id": "org/model"},
        "H200",
        run_uuid="job-1",
        engine_args={"tensor-parallel-size": 2},
    )

    assert "credentials" in captured["reason"]
    assert captured["model"] == "org/model"
    assert captured["accelerator"] == "H200"
    assert captured["job_id"] == "job-1"
    assert captured["tp"] == "2"
