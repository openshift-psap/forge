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


def _url_params(url: str) -> dict:
    from urllib.parse import parse_qs

    return {k: v[0] for k, v in parse_qs(url.split("?", 1)[1]).items()}


def test_dashboard_url_splits_labeled_version() -> None:
    url = regression._build_dashboard_url(
        model="RedHatAI/Llama-3.3-70B-Instruct-FP8-dynamic",
        accelerator="H200",
        current_version="vLLM-0.24.0-benchmark",
        compare_version="vLLM-0.23.0",
        tp="2",
    )
    params = _url_params(url)

    assert params["versions"] == "vLLM-0.24.0,vLLM-0.23.0"
    assert params["version_labels"] == '[["vLLM-0.23.0","default"],["vLLM-0.24.0","benchmark"]]'


def test_dashboard_url_rhaiis_label_suffix() -> None:
    url = regression._build_dashboard_url(
        current_version="RHAIIS-3.6-fast1",
        compare_version="RHAIIS-3.6",
    )
    params = _url_params(url)

    assert params["versions"] == "RHAIIS-3.6"
    assert params["version_labels"] == '[["RHAIIS-3.6","default"],["RHAIIS-3.6","fast1"]]'


def test_dashboard_url_omits_version_labels_for_unlabeled_versions() -> None:
    url = regression._build_dashboard_url(
        current_version="1.0",
        compare_version="0.9",
    )
    params = _url_params(url)

    assert params["versions"] == "1.0,0.9"
    assert "version_labels" not in params


def test_dashboard_url_preserves_wrapped_release() -> None:
    release, label = regression.split_dashboard_version("AIC-0.10.0-vLLM-0.24.0-nightly")

    assert release == "AIC-0.10.0-vLLM-0.24.0"
    assert label == "nightly"
