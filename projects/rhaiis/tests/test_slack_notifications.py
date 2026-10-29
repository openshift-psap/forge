from __future__ import annotations

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
    assert "*Note:* same version on both sides" in messages[0]
