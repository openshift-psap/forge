"""
Pydantic models for :func:`run_from_orchestration_config` (``caliper.export`` in project YAML).
"""

from __future__ import annotations

from typing import Any, Self

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)


class CaliperExportMlflowVaultContentRef(BaseModel):
    """``secrets.vault`` — which vault and which content entry holds the secrets file."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(..., description="Vault name (see ``$FORGE_HOME/vaults/<name>.yaml``).")
    mlflow_secret: str = Field(
        ..., description="Content key in that vault (filename / logical key)."
    )


class CaliperExportMlflowSecretsSpec(BaseModel):
    """
    ``secrets`` when not a path string — e.g. ``vault: { name, key }`` after ``secrets:``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    vault: CaliperExportMlflowVaultContentRef


class CaliperExportBackendMlflow(BaseModel):
    """
    ``backend.mlflow`` — whether to use the MLflow export path and which YAMLs to load.

    Only :attr:`enabled` controls inclusion of the ``mlflow`` backend; ``secrets`` and
    ``config`` are used when it is true. ``secrets`` is either a path to the secrets YAML
    (as for ``--mlflow-secrets``) or a :class:`CaliperExportMlflowSecretsSpec` that names
    a vault and content key, looked up with :func:`vault.get_vault_content_path` at export time.
    ``config`` may be a path to a settings file or an inline mapping (same keys as the file
    consumed by ``--mlflow-config``).
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    enabled: bool = False
    secrets: str | CaliperExportMlflowSecretsSpec | None = None
    config: str | dict[str, Any] | None = Field(
        default=None,
        description="Path to MLflow settings YAML, or the same data inline.",
    )


class CaliperExportTraces(BaseModel):
    """``caliper.export.traces`` — native OTLP trace ingestion switch.

    ``enabled`` selects native ingestion of discovered trace bundles when the
    MLflow backend is active. Disabling it does not remove trace artifacts.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    enabled: bool = False


class CaliperExportBackends(BaseModel):
    """``backend`` mapping (per-backend switch and options)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    mlflow: CaliperExportBackendMlflow | None = None


class CaliperOrchestrationExportConfig(BaseModel):
    """``caliper.export`` — mirrors ``caliper artifacts export`` where applicable."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    from_path: str | None = Field(
        default=None,
        validation_alias=AliasChoices("from", "from_path"),
        description="Artifact file or directory to upload; may be null in YAML if set elsewhere.",
    )
    backend: CaliperExportBackends

    verbose: bool = False
    dry_run: bool = False
    upload_workers: int = Field(10, ge=1, le=64)

    traces: CaliperExportTraces = Field(default_factory=CaliperExportTraces)

    mlflow_experiment: str | None = None
    mlflow_run_id: str | None = None
    mlflow_run_name: str | None = None

    @model_validator(mode="after")
    def _at_least_one_backend(self) -> Self:
        b = self.backend
        if b.mlflow is not None and b.mlflow.enabled:
            return self
        # Future: other backends, etc.
        raise ValueError(
            "caliper.export must enable at least one backend (e.g. backend.mlflow.enabled: true)"
        )

    @property
    def backend_list(self) -> list[str]:
        b = self.backend
        out: list[str] = []
        if b.mlflow is not None and b.mlflow.enabled:
            out.append("mlflow")
        return out
