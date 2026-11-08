from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from projects.core.library import config, env
from projects.core.notifications.provider import NotificationContext
from projects.rhaiis.orchestration import slack_provider
from projects.rhaiis.postprocess import regression


def _capture_slack_messages(monkeypatch) -> list[str]:
    messages: list[str] = []

    def capture(message: str, **_kwargs) -> bool:
        messages.append(message)
        return True

    monkeypatch.setattr(regression, "_send_via_topsail_bot", capture)
    monkeypatch.setattr(regression, "_build_mlflow_run_url", lambda: "")
    monkeypatch.setattr(regression, "_build_dashboard_url", lambda **_kwargs: "")
    monkeypatch.setattr(regression, "_get_slack_channel_id", lambda: "C_TEST_CHANNEL")
    monkeypatch.setattr(regression, "_get_bot_mention", lambda: "")
    return messages


def test_completion_dashboard_link_selects_custom_shape(monkeypatch, tmp_path) -> None:
    values = {
        "tests.rhaiis.model_key": "custom",
        "models.custom": {
            "hf_model_id": "google/gemma-4-26B-A4B-it",
            "vllm_args": {"tensor-parallel-size": 1},
        },
        "rhaiis.accelerator": "nvidia",
        "tests.rhaiis.version": "vLLM-0.31.0-recipe-latency-oriented",
        "tests.rhaiis.workload_keys": ["custom"],
        "workloads.custom": {"data": "prompt_tokens=128,output_tokens=16"},
        "caliper.postprocess.csv_dashboard.enabled": True,
    }
    monkeypatch.setattr(
        config,
        "project",
        SimpleNamespace(get_config=lambda key, default=None: values.get(key, default)),
    )
    monkeypatch.setattr(env, "BASE_ARTIFACT_DIR", tmp_path)
    monkeypatch.setattr(slack_provider, "collect_notification_files", lambda _: ("", [], []))
    monkeypatch.setattr(slack_provider, "collect_step_failure_summaries", lambda _: [])
    monkeypatch.setattr(regression, "_build_mlflow_run_url", lambda: "")

    message = slack_provider.RhaiisSlackProvider().format_message(
        NotificationContext(
            status={"success": True}, finish_reason="success", project_name="rhaiis"
        )
    )
    url = message.split("*Dashboard:* <", 1)[1].split("|", 1)[0]
    query = parse_qs(urlsplit(url).query)

    assert query["profile"] == ["Custom ISL/OSL"]
    assert query["custom_isl_osl"] == ["128/16"]
    assert query["accelerators"] == ["nvidia"]
    assert query["models"] == ["google/gemma-4-26B-A4B-it"]
    assert query["versions"] == ["vLLM-0.31.0-recipe-latency-oriented"]
    assert query["tp_sizes"] == ["1.0"]


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


def test_regression_notification_skipped(monkeypatch) -> None:
    messages = _capture_slack_messages(monkeypatch)
    analysis_result = {
        "status": "skipped",
        "reason": "No baseline data for 0.9 with model=m, accelerator=nvidia, TP=1",
        "current_version": "1.0",
        "compare_version": "0.9",
    }

    assert regression.send_regression_notification(
        analysis_result,
        job_id="rhaiis-run-123",
    )

    assert len(messages) == 1
    assert "*Performance comparison skipped*" in messages[0]
    assert (
        "*Reason:* No baseline data for 0.9 with model=m, accelerator=nvidia, TP=1\n" in messages[0]
    )
    assert "*Versions:* 1.0 vs 0.9 (baseline)\n" in messages[0]


def test_regression_notification_no_changes(monkeypatch) -> None:
    messages = _capture_slack_messages(monkeypatch)
    analysis_result = {
        "status": "completed",
        "regressions": [],
        "improvements": [],
        "total_comparisons": 5,
        "current_version": "1.0",
        "compare_version": "0.9",
    }

    assert regression.send_regression_notification(
        analysis_result,
        job_id="rhaiis-run-123",
    )

    assert len(messages) == 1
    assert "*No performance changes detected*" in messages[0]
    assert "*Versions:* 1.0 vs 0.9 (baseline)\n" in messages[0]


def test_regression_notification_zero_comparisons_is_skip(monkeypatch) -> None:
    messages = _capture_slack_messages(monkeypatch)
    analysis_result = {
        "status": "completed",
        "regressions": [],
        "improvements": [],
        "total_comparisons": 0,
        "current_version": "1.0",
        "compare_version": "0.9",
    }

    assert regression.send_regression_notification(
        analysis_result,
        job_id="rhaiis-run-123",
    )

    assert len(messages) == 1
    assert "*No performance changes detected*" not in messages[0]
    assert "*Performance comparison skipped*" in messages[0]
    assert "*Reason:* No common concurrency levels between current and baseline\n" in messages[0]


def test_regression_notification_notes_same_version(monkeypatch) -> None:
    messages = _capture_slack_messages(monkeypatch)
    analysis_result = {
        "status": "completed",
        "regressions": [],
        "improvements": [],
        "total_comparisons": 3,
        "current_version": "vLLM-0.28.0",
        "compare_version": "vLLM-0.28.0",
    }

    assert regression.send_regression_notification(
        analysis_result,
        job_id="rhaiis-run-123",
    )

    assert len(messages) == 1
    assert "*Note:* the same version is used on both sides." in messages[0]


def test_regression_notification_reports_missing_profiles(monkeypatch) -> None:
    messages = _capture_slack_messages(monkeypatch)
    analysis_result = {
        "status": "completed",
        "regressions": [],
        "improvements": [],
        "total_comparisons": 2,
        "missing_profiles": ["profile6"],
        "current_version": "1.0",
        "compare_version": "0.9",
    }

    assert regression.send_regression_notification(
        analysis_result,
        job_id="rhaiis-run-123",
    )

    assert len(messages) == 1
    assert "*Incomplete:* profile(s) profile6 could not be compared" in messages[0]


def test_regression_notification_reports_failed_analysis(monkeypatch) -> None:
    messages = _capture_slack_messages(monkeypatch)
    analysis_result = {
        "status": "failed",
        "error": "boom",
        "current_version": "1.0",
        "compare_version": "0.9",
    }

    assert regression.send_regression_notification(
        analysis_result,
        job_id="rhaiis-run-123",
    )

    assert len(messages) == 1
    assert "*Performance comparison failed*" in messages[0]
    assert "*Reason:* boom\n" in messages[0]
