"""RHAIIS benchmark tool registry."""

from collections.abc import Callable

from .base import BenchmarkContext, RhaiisLoadGenerator


def _new_guidellm() -> RhaiisLoadGenerator:
    from projects.guidellm.library.loadgenerator.rhaiis import GuideLLMGenerator

    return GuideLLMGenerator()


RUNNERS: dict[str, Callable[[], RhaiisLoadGenerator]] = {"guidellm": _new_guidellm}


def get_load_generator(tool: str) -> RhaiisLoadGenerator:
    try:
        return RUNNERS[tool]()
    except KeyError as exc:
        raise ValueError(f"Benchmark tool {tool!r} has no runner") from exc


__all__ = ["BenchmarkContext", "get_load_generator"]
