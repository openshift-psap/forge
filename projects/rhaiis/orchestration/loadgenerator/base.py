"""Inputs and contract for RHAIIS benchmark tools."""

from __future__ import annotations

from dataclasses import dataclass

from projects.core.library.loadgenerator import LoadGenerator


@dataclass(frozen=True)
class BenchmarkContext:
    deployment_name: str
    namespace: str
    endpoint_url: str
    benchmark_cfg: dict
    model_cfg: dict
    workload: dict
    workload_key: str
    benchmark_timeout: int


class RhaiisLoadGenerator(LoadGenerator[BenchmarkContext]):
    """Run a benchmark; optional preparation phases are explicit capabilities."""

    def configure_timeout(self, timeout: int) -> None:
        """Configure any tool-specific wait task before deployment."""

    def warmup(self, context: BenchmarkContext) -> None:
        raise ValueError(f"Benchmark tool {self.tool!r} does not support warmup")

    def profile(self, context: BenchmarkContext) -> None:
        raise ValueError(f"Benchmark tool {self.tool!r} does not support profiling")
