from __future__ import annotations

import base64
import json
import logging
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from projects.cluster.toolbox.cluster_deploy_operator import main as cluster_deploy_operator
from projects.cluster.toolbox.deploy_custom_catalog import main as deploy_custom_catalog
from projects.cluster.toolbox.wait_for_crds import main as wait_for_crds_command
from projects.core.dsl.utils.k8s import oc, oc_get_json
from projects.core.library import vault
from projects.core.library.config import requires

logger = logging.getLogger(__name__)

RHOAI_CUSTOM_CATALOG_VAULTS = [
    "psap-rhoai-rc",
    "psap-forge-staging-image-pull",
]


@requires(custom_catalog_enabled="platform.rhoai.custom_catalog.enabled")
def list_mandatory_vaults(_cfg) -> list[str]:
    """Return extra mandatory vaults needed when custom catalog is enabled."""

    if _cfg.custom_catalog_enabled:
        return list(RHOAI_CUSTOM_CATALOG_VAULTS)

    return []


RHOAI_PULL_SECRET_NAMESPACE = "openshift-config"
RHOAI_PULL_SECRET_NAME = "pull-secret"
RHOAI_CATALOG_REGISTRIES = ("quay.io/rhoai",)
RHOAI_REGISTRIES = (
    "registry.stage.redhat.io/rhaii",
    "registry.stage.redhat.io/rhaii-early-access",
)


class _RhoaiCustomCatalogPullSecretVaultConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    content: str


class _RhoaiCustomCatalogPullSecretConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    vault: _RhoaiCustomCatalogPullSecretVaultConfig


class _RhoaiCustomCatalogPullSecretInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pull_secret: _RhoaiCustomCatalogPullSecretConfig


class RhoaiCustomCatalogConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    name: str
    namespace: str
    image: str | None = None
    display_name: str | None = None
    publisher: str | None = None
    pull_secret: _RhoaiCustomCatalogPullSecretConfig
    staging_pull_secret: _RhoaiCustomCatalogPullSecretConfig | None = None


class RhoaiOperatorConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    custom_catalog: RhoaiCustomCatalogConfig
    namespace: str
    datasciencecluster_name: str
    components: list[str] = Field(default_factory=list)
    required_crds_before_dsc: list[str] = Field(default_factory=list)
    required_crds_after_dsc: list[str] = Field(default_factory=list)


def custom_catalog_pull_secret_path(custom_catalog: dict[str, Any]) -> Path:
    catalog = _RhoaiCustomCatalogPullSecretInput.model_validate(custom_catalog)
    secret_path = vault.get_vault_content_path(
        catalog.pull_secret.vault.name,
        catalog.pull_secret.vault.content,
    )
    if secret_path is None:
        raise RuntimeError(
            f"RHOAI custom catalog pull secret is required but not available. "
            f"Vault '{catalog.pull_secret.vault.name}' with content "
            f"'{catalog.pull_secret.vault.content}' is MANDATORY when using custom catalog. "
            f"Please ensure the vault is properly configured and accessible."
        )
    return secret_path


def operator_spec_by_package(platform: dict[str, Any], package: str) -> dict[str, Any]:
    operators = platform["operators"]
    if isinstance(operators, dict):
        if package in operators:
            return {"package": package, **operators[package]}
        raise KeyError(f"Unknown operator package: {package}")

    for operator_spec in operators:
        if operator_spec["package"] == package:
            return operator_spec
    raise KeyError(f"Unknown operator package: {package}")


def ensure_operator_subscription(operator_spec: dict[str, str]) -> dict[str, object]:
    return cluster_deploy_operator.run(
        package_name=operator_spec["package"],
        target_namespace=operator_spec["namespace"],
        source_name=operator_spec["source"],
        channel=operator_spec["channel"],
        source_namespace=operator_spec.get("source_namespace", "openshift-marketplace"),
        display_name=operator_spec.get("display_name", operator_spec["package"]),
        artifact_dirname_suffix=f"_{operator_spec['package']}",
    )


def deploy_rhoai_custom_catalog(*, custom_catalog: RhoaiCustomCatalogConfig) -> int:
    if not custom_catalog.enabled:
        logger.info("RHOAI custom catalog disabled; using default catalog source")
        return 0

    if not custom_catalog.image:
        raise RuntimeError("RHOAI custom catalog is enabled but no image was configured")

    return deploy_custom_catalog.run(
        catalog_source_name=custom_catalog.name,
        catalog_namespace=custom_catalog.namespace,
        catalog_image=custom_catalog.image,
        display_name=custom_catalog.display_name or custom_catalog.name,
        publisher=custom_catalog.publisher or "",
        artifact_dirname_suffix="rhoai",
    )


def rhoai_operator_spec(
    *,
    custom_catalog: RhoaiCustomCatalogConfig,
    operator_spec: dict[str, str],
) -> dict[str, str]:
    if not custom_catalog.enabled:
        return operator_spec

    updated_spec = dict(operator_spec)
    updated_spec["source"] = custom_catalog.name
    updated_spec["source_namespace"] = custom_catalog.namespace
    return updated_spec


def prepare_rhcl_operator(platform: dict[str, Any]) -> None:
    operator_spec = operator_spec_by_package(platform, "rhcl-operator")
    ensure_operator_subscription(operator_spec)


def _decode_pull_secret(secret_data: dict[str, Any]) -> str:
    encoded = secret_data.get("data", {}).get(".dockerconfigjson")
    if not encoded:
        raise RuntimeError("openshift-config/pull-secret is missing .dockerconfigjson data")

    return base64.b64decode(encoded).decode("utf-8")


def _read_vault_pull_secret(pull_secret_path: Path) -> str | dict[str, Any]:
    raw_content = pull_secret_path.read_text(encoding="utf-8").strip()
    if not raw_content:
        raise RuntimeError(f"RHOAI pull secret file is empty: {pull_secret_path}")

    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError:
        return raw_content

    if isinstance(parsed, dict) and "auths" in parsed:
        return parsed

    return raw_content


def _apply_vault_pull_secret(
    *,
    current_secret: dict[str, Any],
    pull_secret_path: Path,
    registries: tuple[str, ...],
    temp_path: Path,
) -> dict[str, Any]:
    pull_secret_payload = _read_vault_pull_secret(pull_secret_path)

    if isinstance(pull_secret_payload, dict):
        merged_secret = _merge_pull_secret_auths(current_secret, pull_secret_payload)
        temp_path.write_text(json.dumps(merged_secret, indent=2, sort_keys=True), encoding="utf-8")
        return merged_secret

    for registry in registries:
        oc(
            "registry",
            "login",
            f"--registry={registry}",
            f"--auth-basic={pull_secret_payload}",
            "--to",
            str(temp_path),
        )
    return json.loads(temp_path.read_text(encoding="utf-8"))


def _current_pull_secret_json() -> dict[str, Any]:
    current_secret = oc_get_json(
        "secret",
        name=RHOAI_PULL_SECRET_NAME,
        namespace=RHOAI_PULL_SECRET_NAMESPACE,
    )
    decoded_secret = _decode_pull_secret(current_secret)
    try:
        return json.loads(decoded_secret)
    except json.JSONDecodeError as exc:
        raise RuntimeError("openshift-config/pull-secret is not valid dockerconfigjson") from exc


def _normalize_registry_key(registry: str) -> str:
    return registry.removeprefix("https://").removeprefix("http://").rstrip("/")


def _auth_entry_covers_registry(auth_key: str, required_registry: str) -> bool:
    auth_key = _normalize_registry_key(auth_key)
    required_registry = _normalize_registry_key(required_registry)
    return auth_key == required_registry or required_registry.startswith(f"{auth_key}/")


def _registries_present(
    pull_secret: dict[str, Any], registries: tuple[str, ...] = RHOAI_REGISTRIES
) -> bool:
    auth_keys = tuple(pull_secret.get("auths", {}).keys())
    return all(
        any(_auth_entry_covers_registry(auth_key, registry) for auth_key in auth_keys)
        for registry in registries
    )


def _merge_pull_secret_auths(
    current_secret: dict[str, Any], pull_secret_payload: dict[str, Any]
) -> dict[str, Any]:
    merged_secret = dict(current_secret)
    merged_auths = dict(current_secret.get("auths", {}))
    merged_auths.update(pull_secret_payload.get("auths", {}))
    merged_secret["auths"] = merged_auths
    return merged_secret


def wait_for_rhoai_pull_secret_ready(
    *, timeout_seconds: int = 600, poll_interval_seconds: int = 15
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None

    while time.monotonic() < deadline:
        try:
            pull_secret = _current_pull_secret_json()
            if not _registries_present(pull_secret, (*RHOAI_CATALOG_REGISTRIES, *RHOAI_REGISTRIES)):
                raise RuntimeError("required RHOAI registries are not yet present in pull secret")
            mcp_status = oc(
                "get",
                "mcp",
                "-o",
                r'jsonpath={.items[*].status.conditions[?(@.type=="Updated")].status}',
                check=False,
                log_stdout=False,
                log_stderr=False,
            ).stdout.strip()
            if not mcp_status or "False" in mcp_status:
                raise RuntimeError("machine config pools are still updating")

            logger.info(
                "RHOAI pull secret now contains %s and machine config pools are updated",
                ", ".join(RHOAI_REGISTRIES),
            )
            return
        except RuntimeError as exc:
            last_error = str(exc)
            logger.info("Waiting for RHOAI pull secret propagation: %s", last_error)
            time.sleep(poll_interval_seconds)

    raise RuntimeError(
        f"Timed out waiting for RHOAI registries pull secret propagation: {last_error}"
    )


def prepare_rhoai_pull_secret(custom_catalog: RhoaiCustomCatalogConfig) -> None:
    pull_secret_path = vault.get_vault_content_path(
        custom_catalog.pull_secret.vault.name,
        custom_catalog.pull_secret.vault.content,
    )
    if pull_secret_path is None:
        raise RuntimeError(
            f"RHOAI custom catalog is enabled, but the required pull secret is not available. "
            f"Vault '{custom_catalog.pull_secret.vault.name}' with content "
            f"'{custom_catalog.pull_secret.vault.content}' is MANDATORY when using custom catalog. "
            f"Please ensure the vault is properly configured and accessible."
        )

    current_secret = _current_pull_secret_json()
    required_registries = (*RHOAI_CATALOG_REGISTRIES, *RHOAI_REGISTRIES)

    if _registries_present(current_secret, required_registries):
        logger.info(
            "RHOAI registries already present in cluster pull secret: %s",
            ", ".join(required_registries),
        )
        return

    logger.info(
        "Adding %s to cluster pull secret from %s",
        ", ".join(required_registries),
        pull_secret_path,
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as temp_file:
        temp_file.write(json.dumps(current_secret, indent=2, sort_keys=True))
        temp_path = Path(temp_file.name)

    try:
        current_secret = _apply_vault_pull_secret(
            current_secret=current_secret,
            pull_secret_path=pull_secret_path,
            registries=RHOAI_CATALOG_REGISTRIES,
            temp_path=temp_path,
        )

        if custom_catalog.staging_pull_secret is not None:
            staging_pull_secret_path = vault.get_vault_content_path(
                custom_catalog.staging_pull_secret.vault.name,
                custom_catalog.staging_pull_secret.vault.content,
            )
            if staging_pull_secret_path is None:
                raise RuntimeError(
                    f"RHOAI custom catalog is enabled with staging pull secret configured, but the "
                    f"required staging pull secret is not available. "
                    f"Vault '{custom_catalog.staging_pull_secret.vault.name}' with content "
                    f"'{custom_catalog.staging_pull_secret.vault.content}' is MANDATORY when staging pull secret is configured. "
                    f"Please ensure the vault is properly configured and accessible."
                )

            current_secret = _apply_vault_pull_secret(
                current_secret=current_secret,
                pull_secret_path=staging_pull_secret_path,
                registries=RHOAI_REGISTRIES,
                temp_path=temp_path,
            )
        oc(
            "set",
            "data",
            f"secret/{RHOAI_PULL_SECRET_NAME}",
            "-n",
            RHOAI_PULL_SECRET_NAMESPACE,
            f"--from-file=.dockerconfigjson={temp_path}",
        )
    finally:
        temp_path.unlink(missing_ok=True)

    wait_for_rhoai_pull_secret_ready()


def ensure_required_crds_before_dsc(rhoai: RhoaiOperatorConfig) -> None:
    wait_for_crds_command.run(
        crd_names=rhoai.required_crds_before_dsc,
        display_name="RHOAI pre-DSC CRDs",
    )


def prepare_rhoai_operator(
    *,
    platform: dict[str, Any],
    rhoai: dict[str, Any],
    icsp_applier: Callable[[], None],
) -> None:
    rhoai_config = RhoaiOperatorConfig.model_validate(rhoai)

    prepare_rhcl_operator(platform)
    if rhoai_config.custom_catalog.enabled:
        prepare_rhoai_pull_secret(rhoai_config.custom_catalog)
        icsp_applier()
    deploy_rhoai_custom_catalog(custom_catalog=rhoai_config.custom_catalog)
    operator_spec = operator_spec_by_package(platform, "rhods-operator")
    operator_spec = rhoai_operator_spec(
        custom_catalog=rhoai_config.custom_catalog,
        operator_spec=operator_spec,
    )
    ensure_operator_subscription(operator_spec)
    ensure_required_crds_before_dsc(rhoai_config)
