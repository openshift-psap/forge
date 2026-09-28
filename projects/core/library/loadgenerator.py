"""Contract for project orchestration adapters that launch benchmark tools."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Generic, TypeVar

ContextT = TypeVar("ContextT")


class LoadGenerator(ABC, Generic[ContextT]):  # noqa: UP046 - Python 3.11 support
    """Run one benchmark tool with a project-specific, typed context."""

    tool: ClassVar[str]

    @abstractmethod
    def run(self, context: ContextT) -> None:
        """Run the benchmark and write its artifacts before returning."""
