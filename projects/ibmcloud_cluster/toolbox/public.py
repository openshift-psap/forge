"""Public configuration and credential-directory contracts for IBM cluster tools."""

from pathlib import Path
from typing import Annotated, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

MachineType = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]+$")]


class ContractModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)


class Workers(ContractModel):
    count: int = Field(ge=0)
    type: MachineType


class ControlPlane(Workers):
    count: int = Field(ge=1)


class Release(ContractModel):
    image: str = Field(
        pattern=r"^[^\s]+@sha256:[0-9a-f]{64}$",
        description="OpenShift payload image pinned by SHA256 digest.",
    )
    commit: str = Field(
        pattern=r"^[0-9a-f]{40}$",
        description="Full openshift/release scripts commit SHA.",
    )


class ClusterConfig(ContractModel):
    """Content of the create settings YAML file; no outer ibmcloud key or secrets."""

    region: MachineType
    zones: list[str] = Field(min_length=1)
    control_plane: ControlPlane
    workers: Workers
    base_domain: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$")
    resource_group: str = Field(pattern=r"^[^\r\n\x00]+$")
    release: Release

    @model_validator(mode="after")
    def validate_zones(self) -> Self:
        allowed_zones = {f"{self.region}-1", f"{self.region}-2", f"{self.region}-3"}
        for zone in self.zones:
            if zone not in allowed_zones:
                raise ValueError("zones must belong to the selected region")
        return self

    @classmethod
    def from_yaml(cls, settings_file_path: str | Path) -> Self:
        """Read and validate a standalone configuration file without logging its input."""
        try:
            content = yaml.safe_load(Path(settings_file_path).read_text())
        except yaml.YAMLError:
            raise ValueError("Invalid cluster settings YAML") from None
        return cls.model_validate(content)


CREATE_VAULT_FILES = (
    "ibmcloud-api-key",
    "ibmcloud-cloud-controller-manager-api-key",
    "ibmcloud-cluster-csi-drivers-api-key",
    "ibmcloud-image-registry-api-key",
    "ibmcloud-ingress-operator-api-key",
    "ibmcloud-machine-api-api-key",
    "pull-secret",
    "ssh-publickey",
    "ssh-privatekey",
)
DESTROY_VAULT_FILES = ("ibmcloud-api-key", "pull-secret")


def validate_vault_directory(vault_dir: str | Path, *, for_create: bool) -> Path:
    """Require readable, nonempty credential files; never log or return their contents.

    Create requires CREATE_VAULT_FILES. Destroy requires DESTROY_VAULT_FILES.
    Extra files are allowed. The directory is read-only from the toolbox's perspective.
    """
    if not vault_dir:
        raise ValueError("The ibmcloud-ipi vault directory is required")
    path = Path(vault_dir).resolve()
    required = CREATE_VAULT_FILES if for_create else DESTROY_VAULT_FILES
    for name in required:
        if not (path / name).is_file():
            raise ValueError(f"Vault requires a regular {name} file")
        try:
            with (path / name).open("rb") as credential:
                if credential.read(1):
                    continue
        except OSError:
            pass
        raise ValueError(f"Vault requires a readable, nonempty {name} file") from None
    return path
