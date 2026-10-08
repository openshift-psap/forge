from __future__ import annotations

from types import SimpleNamespace

from projects.core.library import config
from projects.rhaiis.postprocess import regression


def _capture_slack_messages(monkeypatch) -> list[str]:
    messages: list[str] = []

    def capture(message: str, **_kwargs) -> bool:
        messages.append(message)
        return True

    monkeypatch.setattr(regression, "_send_via_topsail_bot", capture)
    monkeypatch.setattr(regression, "_build_mlflow_run_url", lambda: "")
    monkeypatch.setattr(regression, "_build_dashboard_url", lambda **_kwargs: "")
    return messages


def test_failure_notification_includes_owner(monkeypatch) -> None:
    messages = _capture_slack_messages(monkeypatch)

    assert regression.send_failure_notification(
        error="test failure",
        job_id="rhaiis-run-123",
        slack_user="U01234567",
        owner="nmiriyal",
    )

    assert len(messages) == 1
    assert "*Triggered by:* <@U01234567>\n*Owner:* nmiriyal\n" in messages[0]


def test_pipeline_failure_uses_raw_accelerator_label(monkeypatch) -> None:
    from projects.rhaiis.orchestration import notifications, runtime_config

    captured: dict = {}
    project_values = {"rhaiis.cluster_tag": "mi355x"}
    monkeypatch.setattr(
        config,
        "project",
        SimpleNamespace(get_config=lambda key, default=None: project_values.get(key, default)),
    )
    monkeypatch.setattr(runtime_config, "get_model", lambda _key: {"hf_model_id": "org/model"})
    monkeypatch.setattr(runtime_config, "get_accelerator", lambda: "amd")
    monkeypatch.setattr(runtime_config, "get_engine", lambda: "vllm")
    monkeypatch.setattr(runtime_config, "get_engine_args", lambda _engine: {})
    monkeypatch.setattr(runtime_config, "get_workload", lambda _key: {})
    monkeypatch.setattr(runtime_config, "merge_engine_args", lambda *args: {})
    monkeypatch.setattr(
        regression,
        "send_failure_notification",
        lambda **kwargs: captured.update(kwargs),
    )

    notifications._send_alert("test failure", model_key="model-key", workload_keys=["profile1"])

    assert captured["accelerator"] == "amd"
    assert captured["cluster"] == "mi355x"


def test_success_notification_includes_owner(monkeypatch) -> None:
    messages = _capture_slack_messages(monkeypatch)

    class Project:
        def get_config(self, *_args, **_kwargs):
            return False

    monkeypatch.setattr(config, "project", Project())

    assert regression.send_success_notification(
        job_id="rhaiis-run-123",
        owner="nmiriyal",
        workload_keys=["profile1"],
    )

    assert len(messages) == 1
    assert "*Owner:* nmiriyal\n" in messages[0]


def test_regression_notification_includes_owner(monkeypatch) -> None:
    messages = _capture_slack_messages(monkeypatch)
    analysis_result = {
        "status": "completed",
        "regressions": [{"profile": "profile1"}],
        "improvements": [],
        "current_version": "1.0",
        "compare_version": "0.9",
        "all_results": [
            {
                "profile": "profile1",
                "is_regression": True,
                "is_improvement": False,
                "pct_diff": -12.0,
                "metric": "throughput",
                "baseline": 100.0,
                "current": 88.0,
            }
        ],
    }

    assert regression.send_regression_notification(
        analysis_result,
        job_id="rhaiis-run-123",
        owner="nmiriyal",
    )

    assert len(messages) == 1
    assert "*Owner:* nmiriyal\n" in messages[0]
