from types import SimpleNamespace

import pytest

from projects.core.library import config
from projects.rhaiis.orchestration.test_phase import _standalone_only_analysis


@pytest.mark.parametrize(
    "run_benchmark,profiler_enabled,engine,expected",
    [
        (False, False, "vllm", True),
        (False, False, "sglang", True),
        (True, False, "vllm", False),
        (False, True, "vllm", False),
        (False, True, "sglang", True),
        (False, True, "trtllm", True),
        (True, True, "sglang", False),
    ],
)
def test_standalone_only_analysis(
    monkeypatch, run_benchmark: bool, profiler_enabled: bool, engine: str, expected: bool
) -> None:
    values = {
        "tests.rhaiis.run_benchmark": run_benchmark,
        "rhaiis.profiler": {"enabled": profiler_enabled},
        "rhaiis.engine": engine,
        "rhaiis.optional_phase.excluded_workloads": [],
    }
    monkeypatch.setattr(
        config,
        "project",
        SimpleNamespace(get_config=lambda path, default=None, **kwargs: values.get(path, default)),
    )

    assert _standalone_only_analysis(["profile1"]) is expected


def test_standalone_only_when_all_workloads_excluded_from_optional_phases(monkeypatch) -> None:
    values = {
        "tests.rhaiis.run_benchmark": False,
        "rhaiis.profiler": {"enabled": True},
        "rhaiis.engine": "vllm",
        "rhaiis.optional_phase.excluded_workloads": ["profile1"],
    }
    monkeypatch.setattr(
        config,
        "project",
        SimpleNamespace(get_config=lambda path, default=None, **kwargs: values.get(path, default)),
    )

    assert _standalone_only_analysis(["profile1"]) is True
