"""Inputs shared by llm-d load generator adapters."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from projects.core.library.loadgenerator import LoadGenerator


@dataclass(frozen=True)
class BenchmarkContext:
    test_dir: Path
    endpoint_url: str
    benchmark_key: str
    benchmark: dict[str, Any]
    workload: dict[str, Any] | None
    model_name: str
    namespace: str


class LlmDLoadGenerator(LoadGenerator[BenchmarkContext]):
    """Tool adapter with llm-d profile resolution."""

    @abstractmethod
    def resolve_config(self, profile: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
        """Apply tool-specific defaults to a benchmark profile."""
