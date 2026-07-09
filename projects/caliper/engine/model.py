"""Unified run model and plugin protocol for post-processing."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TestBaseNode:
    """Directory containing __test_labels__.yaml or MatrixBenchmarking settings.yaml."""

    directory: Path
    labels: dict[str, Any]
    artifact_paths: list[Path] = field(default_factory=list)
    test_path: Path = field(default_factory=lambda: Path(""))  # Path relative to base_dir


@dataclass
class UnifiedResultRecord:
    """One parsed facet / test result with distinguishing labels."""

    test_base_path: str
    distinguishing_labels: dict[str, Any]
    metrics: dict[str, Any]
    run_identity: dict[str, Any] = field(default_factory=dict)
    parse_notes: list[str] = field(default_factory=list)


@dataclass
class ParseResult:
    """Output of plugin parse pass."""

    records: list[UnifiedResultRecord]
    warnings: list[str] = field(default_factory=list)


@dataclass
class UnifiedRunModel:
    """Reloadable unified representation after parsing."""

    plugin_module: str
    base_directory: str
    test_nodes: list[TestBaseNode]
    unified_result_records: list[UnifiedResultRecord]
    parse_cache_ref: str | None = None
    schema_version: str = "1"


@dataclass
class FileExportManifest:
    """Files to upload to external backends."""

    source_paths: list[Path]
    run_identity: dict[str, Any]
    backends_enabled: frozenset[str]


@dataclass
class FileExportBackendResult:
    """Per-backend outcome."""

    backend: str
    status: str  # success | failure | skipped
    detail: str = ""
    metadata: dict[str, Any] | None = None


@dataclass
class RegressionFinding:
    """Single KPI regression outcome."""

    kpi_id: str
    current_value: Any
    baseline_value: Any
    direction: str
    status: str


class PostProcessingPlugin(ABC):
    """Project plugin: parse required; other hooks optional with defaults."""

    @abstractmethod
    def parse(self, nodes: list[TestBaseNode]) -> ParseResult:
        """Parse each labeled test base into unified records."""

    def visualize(
        self,
        model: UnifiedRunModel,
        output_dir: Path,
        report_ids: list[str] | None,
        group_id: str | None,
        visualize_config: dict[str, Any] | None,
    ) -> list[str]:
        """Write plots/HTML; return list of created file paths."""
        return []

    def kpi_catalog(self) -> list[dict[str, Any]]:
        """KPI definitions (id, name, unit, higher_is_better)."""
        return []

    def compute_kpis(self, model: UnifiedRunModel) -> list[dict[str, Any]]:
        """Return canonical-shaped KPI dicts (pre-validated)."""
        return []

    def build_ai_data_payload(self, model: UnifiedRunModel) -> dict[str, Any]:
        """Structured JSON for AI agent evaluation."""
        return {"schema_version": "1", "run_id": "", "metrics": {}}

    def get_ai_data_artifact_files_for_test(self, test_dir: Path) -> list[str]:
        """Return list of artifact files to copy for AI evaluation export from a specific test directory.

        This method is called once per test record and should only search within the provided test_dir,
        not across the entire base directory. This provides better security isolation.

        Args:
            test_dir: The specific test directory to search within (where __test_labels__.yaml is located)

        Returns:
            List of relative file paths from test_dir to copy for AI evaluation

        Example:
            return [
                "003__benchmark/000__run_benchmark/artifacts/results/results.json",
                "004__capture_service_state/artifacts/service_state.json"
            ]
        """
        return []
