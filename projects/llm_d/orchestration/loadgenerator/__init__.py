"""Resolve the llm-d benchmark tool before cluster work begins."""

from __future__ import annotations

from .base import LlmDLoadGenerator
from .guidellm import GuideLLMGenerator

RUNNERS: dict[str, LlmDLoadGenerator] = {GuideLLMGenerator.tool: GuideLLMGenerator()}


def get_load_generator(tool: str) -> LlmDLoadGenerator:
    """Return a registered runner or fail before deployment."""
    try:
        return RUNNERS[tool]
    except KeyError as exc:
        raise ValueError(f"Benchmark tool {tool!r} has no runner") from exc
