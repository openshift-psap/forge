"""Contract for project orchestration adapters that launch benchmark tools."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Generic, TypeVar

ContextT = TypeVar("ContextT")


class LoadGenerator(ABC, Generic[ContextT]):  # noqa: UP046 - Python 3.11 support
    """Run one benchmark tool with a project-specific, typed context."""

    tool: ClassVar[str]

    def expand(self, benchmark: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        """Expand a benchmark config into a list of ``(label, config)`` runs.

        Override this to turn a single config entry into multiple sequential
        runs — for example, expanding a concurrency list into one run per
        value.  The default returns the input as a single run.
        """
        return [("default", benchmark)]

    @abstractmethod
    def run(self, context: ContextT) -> None:
        """Run the benchmark and write its artifacts before returning."""
