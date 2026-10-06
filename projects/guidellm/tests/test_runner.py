from __future__ import annotations

import inspect

import pytest

from projects.guidellm.library import runner


@pytest.mark.parametrize("settings", [{}, {"image": None, "timeout": None, "pvc_size": None}])
def test_job_uses_toolbox_defaults(monkeypatch: pytest.MonkeyPatch, settings: dict) -> None:
    captured = {}
    monkeypatch.setattr(runner.benchmark_command, "execute_tasks", captured.update)

    assert (
        runner.GuideLLMJob(
            endpoint_url="https://example.test/v1", name="benchmark", namespace="test", **settings
        ).run()
        == 0
    )

    signature = inspect.signature(runner.benchmark_command.run)
    for key in ("image", "timeout", "pvc_size"):
        assert captured[key] == signature.parameters[key].default


def test_job_preserves_explicit_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}
    monkeypatch.setattr(runner.benchmark_command, "execute_tasks", captured.update)

    runner.GuideLLMJob(
        endpoint_url="https://example.test/v1",
        name="benchmark",
        namespace="test",
        image="example.test/guidellm:custom",
        timeout=120,
        pvc_size="5Gi",
        use_pvc=False,
        fs_group=0,
    ).run()

    assert captured["image"] == "example.test/guidellm:custom"
    assert captured["timeout"] == 120
    assert captured["pvc_size"] == "5Gi"
    assert captured["use_pvc"] is False
    assert captured["fs_group"] == 0


def test_job_preserves_invalid_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runner.benchmark_command, "execute_tasks", lambda _: pytest.fail("ran tasks")
    )
    with pytest.raises(ValueError, match="timeout must be greater than zero"):
        runner.GuideLLMJob(
            endpoint_url="https://example.test/v1", name="benchmark", namespace="test", timeout=0
        ).run()
