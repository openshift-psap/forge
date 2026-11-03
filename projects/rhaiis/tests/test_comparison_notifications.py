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
    assert captured["accelerator"] == "H200"
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


def _capture_notification_files(monkeypatch) -> list:
    from projects.core.library import ci

    recorded: list = []

    def capture(name, message):
        recorded.append((name, message))
        return True

    monkeypatch.setattr(ci, "add_notification_file", capture)
    return recorded


def test_comparison_skip_reports_undelivered_notification(monkeypatch) -> None:
    _mock_config(
        monkeypatch,
        {"tests.rhaiis.version": "v2", "tests.rhaiis.compare_version": "v1"},
    )
    monkeypatch.setattr(
        "projects.rhaiis.postprocess.regression.send_regression_notification",
        lambda *args, **kwargs: False,
    )
    recorded = _capture_notification_files(monkeypatch)

    analysis._notify_comparison_skipped("no data", model="m", accelerator="H200", job_id="job-1")

    assert len(recorded) == 1
    assert recorded[0][0] == "comparison-notification-failed"
    assert "Slack notification was not delivered" in recorded[0][1]


def test_comparison_skip_reports_notification_exception(monkeypatch) -> None:
    _mock_config(
        monkeypatch,
        {"tests.rhaiis.version": "v2", "tests.rhaiis.compare_version": "v1"},
    )

    def raise_error(*args, **kwargs):
        raise RuntimeError("vault unavailable")

    monkeypatch.setattr(
        "projects.rhaiis.postprocess.regression.send_regression_notification", raise_error
    )
    recorded = _capture_notification_files(monkeypatch)

    analysis._notify_comparison_skipped("no data", model="m", accelerator="H200", job_id="job-1")

    assert len(recorded) == 1
    assert "vault unavailable" in recorded[0][1]
