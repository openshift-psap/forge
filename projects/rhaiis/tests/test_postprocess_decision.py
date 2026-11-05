from types import SimpleNamespace

import pytest

from projects.core.library import config
from projects.rhaiis.orchestration.test_phase import (
    _effective_profiler_enabled,
    _postprocess_enabled,
)


def _mock_config(monkeypatch, values: dict) -> None:
    monkeypatch.setattr(
        config,
        "project",
        SimpleNamespace(get_config=lambda path, default=None, **kwargs: values.get(path, default)),
    )


@pytest.mark.parametrize("run_benchmark,expected", [(True, True), (False, False)])
def test_postprocess_enabled_follows_benchmark(monkeypatch, run_benchmark, expected) -> None:
    _mock_config(monkeypatch, {"tests.rhaiis.run_benchmark": run_benchmark})

    assert _postprocess_enabled() is expected


@pytest.mark.parametrize(
    "profiler_enabled,engine,expected",
    [
        (False, "vllm", False),
        (True, "vllm", True),
        (True, "sglang", False),
        (True, "trtllm", False),
    ],
)
def test_effective_profiler_enabled_requires_vllm(
    monkeypatch, profiler_enabled: bool, engine: str, expected: bool
) -> None:
    _mock_config(monkeypatch, {"rhaiis.optional_phase.excluded_workloads": []})

    assert (
        _effective_profiler_enabled({"enabled": profiler_enabled}, ["profile1"], engine) is expected
    )


def test_effective_profiler_disabled_when_no_eligible_workloads(monkeypatch) -> None:
    _mock_config(monkeypatch, {"rhaiis.optional_phase.excluded_workloads": ["profile1"]})

    assert _effective_profiler_enabled({"enabled": True}, ["profile1"], "vllm") is False
