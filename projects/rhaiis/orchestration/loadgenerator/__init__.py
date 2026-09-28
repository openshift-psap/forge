"""RHAIIS benchmark tool registry."""

from .base import BenchmarkContext, RhaiisLoadGenerator
from .guidellm import GuideLLMGenerator

RUNNERS: dict[str, RhaiisLoadGenerator] = {"guidellm": GuideLLMGenerator()}


def get_load_generator(tool: str) -> RhaiisLoadGenerator:
    try:
        return RUNNERS[tool]
    except KeyError as exc:
        raise ValueError(f"Benchmark tool {tool!r} has no runner") from exc


__all__ = ["BenchmarkContext", "get_load_generator"]
