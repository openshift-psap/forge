#!/usr/bin/env python3
"""MLflow Tracing Performance Testing — CI Operations"""

from pathlib import Path

from projects.agentic_tools.ci_base import CIApp, PhaseSpec
from projects.core.library import vault
from projects.rhoai.library.deploy import list_mandatory_vaults as rhoai_list_mandatory_vaults


class MLflowCIApp(CIApp):
    """Add conditional RHOAI catalog credentials to prepare-phase vaults."""

    def init_vaults(self, phase: str) -> None:
        global_mandatory = self._get_vaults_for_key("all")
        phase_mandatory = self._get_vaults_for_key(phase)
        if phase == "prepare":
            phase_mandatory = phase_mandatory + rhoai_list_mandatory_vaults()

        global_optional = self._get_vaults_for_key("all-optional")
        phase_optional = self._get_vaults_for_key(f"{phase}-optional")
        mandatory_vaults = list(dict.fromkeys(global_mandatory + phase_mandatory))
        optional_vaults = list(dict.fromkeys(global_optional + phase_optional))
        if mandatory_vaults or optional_vaults:
            vault.init(
                mandatory_vaults=mandatory_vaults,
                optional_vaults=optional_vaults,
            )

    def list_vaults(self) -> list[str]:
        return list(dict.fromkeys(super().list_vaults() + rhoai_list_mandatory_vaults()))


main = MLflowCIApp(
    project_name="mlflow",
    description="MLflow Tracing Performance Testing CI Operations for FORGE.",
    config_dir=Path(__file__).parent,
    phases={
        "prepare": PhaseSpec(
            func="projects.mlflow.orchestration.prepare_phase:run",
            help="Prepare phase - Enable MLflow operator and deploy test infrastructure.",
        ),
        "test": PhaseSpec(
            func="projects.mlflow.orchestration.test_phase:run",
            help="Test phase - Execute tracing performance tests.",
        ),
        "pre-cleanup": PhaseSpec(
            func="projects.mlflow.orchestration.cleanup_phase:run",
            help="Pre-cleanup phase - Remove per-test resources (MinIO, PostgreSQL, MLflow CR).",
        ),
        "post-cleanup": PhaseSpec(
            func="projects.mlflow.orchestration.cleanup_phase:run_platform_cleanup",
            help="Post-cleanup phase - Disable MLflow operator and delete namespace.",
        ),
    },
).build()

if __name__ == "__main__":
    main()
