"""Resolve the llm-d benchmark tool before cluster work begins."""

from __future__ import annotations

from collections.abc import Callable

from .base import LlmDLoadGenerator


def _new_guidellm() -> LlmDLoadGenerator:
    from projects.guidellm.library.loadgenerator.llm_d import GuideLLMGenerator

    return GuideLLMGenerator()


def _new_aiperf() -> LlmDLoadGenerator:
    from projects.aiperf.library.loadgenerator.llm_d import AIPerfGenerator

    return AIPerfGenerator()


RUNNERS: dict[str, Callable[[], LlmDLoadGenerator]] = {
    "guidellm": _new_guidellm,
    "aiperf": _new_aiperf,
}


def get_load_generator(tool: str) -> LlmDLoadGenerator:
    """Return a registered runner or fail before deployment."""
    try:
        return RUNNERS[tool]()
    except KeyError as exc:
        raise ValueError(f"Benchmark tool {tool!r} has no runner") from exc
