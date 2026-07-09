"""
Config-driven Caliper artifact export for FORGE orchestration projects (e.g. skeleton).

Validates :class:`~projects.caliper.orchestration.export_config.CaliperOrchestrationExportConfig`
and calls
:func:`projects.caliper.engine.file_export.artifacts_export_run.run_artifacts_export`.
"""

from __future__ import annotations

import copy
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from projects.caliper.engine.file_export.artifacts_export_run import run_artifacts_export
from projects.caliper.engine.file_export.mlflow_config import (
    load_mlflow_config_yaml,
    project_metadata_fields,
)
from projects.caliper.orchestration.export_config import (
    CaliperOrchestrationExportConfig,
)
from projects.core.library import env
from projects.core.library import vault as vault_lib

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Run naming helpers
# ---------------------------------------------------------------------------

_TIMESTAMP_RE = re.compile(r"(\d{8}-\d{6})")
_DIR_INDEX_RE = re.compile(r"^(\d+__)")


def _extract_timestamp_from_fjob() -> str:
    """Extract YYYYMMDD-HHMMSS timestamp from FJOB_NAME env var."""
    fjob = os.environ.get("FJOB_NAME", "")
    m = _TIMESTAMP_RE.search(fjob)
    return m.group(1) if m else ""


def _read_test_labels(run_dir: Path) -> dict[str, str]:
    """Read labels from __test_labels__.yaml in a run directory."""
    marker = run_dir / "__test_labels__.yaml"
    if not marker.is_file():
        return {}
    try:
        data = yaml.safe_load(marker.read_text(encoding="utf-8"))
        labels = data.get("labels", {}) if isinstance(data, dict) else {}
        return labels if isinstance(labels, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def _format_run_name(template: str, labels: dict[str, str], *, prefix: str, timestamp: str) -> str:
    """Format a run name template using labels plus computed fields (prefix, timestamp)."""
    fields = {**labels, "prefix": prefix, "timestamp": timestamp}
    try:
        return template.format_map(fields)
    except (KeyError, ValueError) as e:
        logger.warning("Run naming template error (%s), falling back to raw template", e)
        return template


def format_child_run_name(
    run_dir: Path,
    *,
    template: str,
    prefix: str,
    timestamp: str,
) -> str:
    """Build a child run name: preserve the NNN__ index prefix, apply the template for the rest."""
    dir_name = run_dir.name
    m = _DIR_INDEX_RE.match(dir_name)
    num_prefix = m.group(1) if m else ""
    labels = _read_test_labels(run_dir)
    formatted = _format_run_name(template, labels, prefix=prefix, timestamp=timestamp)
    return f"{num_prefix}{formatted}"


def resolve_run_names(
    run_dirs: list[Path],
    mlflow_config_data: dict[str, Any] | None,
    *,
    fallback_run_name: str | None = None,
) -> dict[str, str | None | dict[Path, str]]:
    """Resolve parent and child run names from ``run_naming`` config + test labels.

    Returns a dict with keys:
        parent_run_name: str | None
        single_run_name: str | None
        child_run_names: dict[Path, str] (mapping run_dir -> formatted name)
    """
    naming_cfg = (mlflow_config_data or {}).get("run_naming")
    if not naming_cfg or not isinstance(naming_cfg, dict):
        return {
            "parent_run_name": fallback_run_name,
            "single_run_name": None,
            "child_run_names": {},
        }

    prefix = naming_cfg.get("prefix", "")
    timestamp = _extract_timestamp_from_fjob()
    single_tpl = naming_cfg.get("single_run")
    parent_tpl = naming_cfg.get("parent_run")
    child_tpl = naming_cfg.get("child_run")

    result: dict[str, Any] = {
        "parent_run_name": fallback_run_name,
        "single_run_name": None,
        "child_run_names": {},
    }

    if len(run_dirs) == 1 and single_tpl:
        labels = _read_test_labels(run_dirs[0])
        result["single_run_name"] = _format_run_name(
            single_tpl, labels, prefix=prefix, timestamp=timestamp
        )

    if len(run_dirs) > 1 and parent_tpl:
        labels = _read_test_labels(run_dirs[0])
        result["parent_run_name"] = _format_run_name(
            parent_tpl, labels, prefix=prefix, timestamp=timestamp
        )

    if child_tpl:
        child_names: dict[Path, str] = {}
        for rd in run_dirs:
            child_names[rd] = format_child_run_name(
                rd, template=child_tpl, prefix=prefix, timestamp=timestamp
            )
        result["child_run_names"] = child_names

    return result


def run_from_orchestration_config(
    caliper_cfg: dict[str, Any] | None,
) -> int:
    """
    Run Caliper file export from orchestration config.

    Pass:

    * ``caliper.export`` from :func:`get_config` (inner mapping only), or
    * The full ``caliper`` object with an ``export`` key.

    Backends are selected only via flags such as ``backend.mlflow.enabled`` (not a
    free-form backend name list).

    If ``backend.mlflow.secrets`` uses the ``vault: { name, key }`` form, the process must
    have called :func:`projects.core.library.vault.init` with that vault name (as in the
    top-level ``vaults:`` list in project config) so :func:`vault.get_vault_content_path`
    can return the secrets file path.
    """

    try:
        export_cfg = CaliperOrchestrationExportConfig.model_validate(caliper_cfg["export"])
    except (ValidationError, ValueError) as e:
        logger.error("Invalid caliper export config: %s", e)
        raise

    raw_from = export_cfg.from_path
    if raw_from is None or (isinstance(raw_from, str) and not raw_from.strip()):
        raise ValueError("caliper.export.from is not set")
    from_path = Path(raw_from)
    if not from_path.exists():
        raise FileNotFoundError(f"caliper.export.from does not exist: {from_path}")

    backends = export_cfg.backend_list
    mlflow_backend_cfg = export_cfg.backend.mlflow

    status_yaml = env.ARTIFACT_DIR / "status.yaml"

    if "mlflow" not in backends:
        raise ValueError(
            f"only 'mlflow' backend export is supported at the moment (got '{' '.join(backends)}')."
        )

    vault_name = export_cfg.backend.mlflow.secrets.vault.name
    vault_mlflow_secret = export_cfg.backend.mlflow.secrets.vault.mlflow_secret
    mlflow_secrets_path = vault_lib.get_vault_content_path(vault_name, vault_mlflow_secret)

    if mlflow_secrets_path is None:
        raise ValueError(f"Vault {vault_name}/{vault_mlflow_secret} missing :/")
    elif not mlflow_secrets_path.exists():
        raise FileNotFoundError(f"Vault {vault_name}/{vault_mlflow_secret} file missing :/")

    raw_cfg = mlflow_backend_cfg.config
    mlflow_config_data: dict[str, Any] | None = None
    if raw_cfg is None:
        pass
    elif isinstance(raw_cfg, dict):
        mlflow_config_data = copy.deepcopy(raw_cfg)
    else:
        mlflow_config_data = load_mlflow_config_yaml(Path(raw_cfg).expanduser().resolve())

    run_dirs = _discover_run_dirs(from_path)

    # Resolve descriptive run names from labels + run_naming config
    naming = resolve_run_names(
        run_dirs, mlflow_config_data, fallback_run_name=export_cfg.mlflow_run_name
    )

    if len(run_dirs) > 1:
        if export_cfg.dry_run:
            logger.info(
                "dry-run: would export %d run dirs from %s (skipping)", len(run_dirs), from_path
            )
            ret = 0
        else:
            ret = _run_multi_run_export(
                export_cfg=export_cfg,
                from_path=from_path,
                status_yaml=status_yaml,
                mlflow_secrets_path=mlflow_secrets_path,
                mlflow_config_data=mlflow_config_data,
                run_dirs=run_dirs,
                resolved_parent_name=naming.get("parent_run_name"),
                child_run_names=naming.get("child_run_names") or {},
            )
    else:
        effective_name = (
            naming.get("single_run_name") if len(run_dirs) == 1 else None
        ) or export_cfg.mlflow_run_name

        mlflow_kwargs: dict[str, Any] = {
            "mlflow_experiment": export_cfg.mlflow_experiment,
            "mlflow_run_id": export_cfg.mlflow_run_id,
            "mlflow_run_name": effective_name,
            "mlflow_secrets_path": mlflow_secrets_path,
        }
        if mlflow_config_data is not None:
            mlflow_kwargs["mlflow_config_data"] = mlflow_config_data

        ret = run_artifacts_export(
            from_path=from_path,
            status_yaml_path=status_yaml,
            dry_run=export_cfg.dry_run,
            verbose=export_cfg.verbose,
            upload_workers=export_cfg.upload_workers,
            backend=backends,
            **mlflow_kwargs,
        )

    if ret != 0:
        raise RuntimeError(f"Caliper export failed (ret code = {ret})")

    with open(status_yaml) as f:
        return yaml.safe_load(f.read())


METRICS_FILE = "metrics.json"
PARAMETERS_FILE = "parameters.json"
TEST_LABELS_MARKER = "__test_labels__.yaml"


def _discover_run_dirs(from_path: Path) -> list[Path]:
    """Auto-detect test run directories via ``__test_labels__.yaml`` markers."""
    run_dirs: list[Path] = []
    for marker in sorted(from_path.rglob(TEST_LABELS_MARKER)):
        if marker.is_file():
            run_dirs.append(marker.parent)

    if run_dirs:
        logger.info(
            "Auto-detected %d test run director%s via %s",
            len(run_dirs),
            "y" if len(run_dirs) == 1 else "ies",
            TEST_LABELS_MARKER,
        )
    return run_dirs


def _run_multi_run_export(
    *,
    export_cfg: CaliperOrchestrationExportConfig,
    from_path: Path,
    status_yaml: Path,
    mlflow_secrets_path: Path,
    mlflow_config_data: dict[str, Any] | None,
    run_dirs: list[Path],
    resolved_parent_name: str | None = None,
    child_run_names: dict[Path, str] | None = None,
) -> int:
    """Export as parent + nested child MLflow runs."""
    import sys
    import traceback

    import click

    from projects.caliper.engine.file_export import mlflow_backend
    from projects.caliper.engine.file_export.artifacts_export_run import (
        merge_mlflow_files_with_cli,
        write_artifacts_status_yaml,
    )
    from projects.caliper.engine.file_export.mlflow_secrets import (
        load_mlflow_secrets_yaml,
        project_secrets_fields,
        validate_mlflow_secrets,
    )
    from projects.caliper.engine.model import FileExportBackendResult

    logger.info("Multi-run export: %d test run(s) detected", len(run_dirs))

    all_artifact_paths = [p for p in from_path.rglob("*") if p.is_file()]

    secrets_data = None
    if mlflow_secrets_path is not None:
        secrets_data = load_mlflow_secrets_yaml(mlflow_secrets_path)
        validate_mlflow_secrets(secrets_data)

    merged_ml = merge_mlflow_files_with_cli(
        None,
        secrets_data=secrets_data,
        config_data=mlflow_config_data,
        cli_tracking_uri=None,
        cli_experiment=export_cfg.mlflow_experiment,
        cli_run_id=None,
        cli_run_name=export_cfg.mlflow_run_name,
    )

    secret_part = project_secrets_fields(merged_ml)
    mlflow_connection = secret_part if secret_part else None

    tracking_uri = merged_ml.get("tracking_uri")
    experiment = merged_ml.get("experiment")
    run_name = resolved_parent_name or merged_ml.get("run_name")
    workspace = merged_ml.get("workspace")
    if not workspace:
        raise ValueError("The export workspace must be specified")

    meta = project_metadata_fields(merged_ml)
    run_metadata = meta if meta else None

    insecure_tls = bool(mlflow_connection and mlflow_connection.get("insecure_tls"))

    if export_cfg.verbose:
        click.echo("caliper multi-run export (verbose)", err=True)
        click.echo(f"  Source: {from_path}", err=True)
        click.echo(f"  Total artifact files: {len(all_artifact_paths)}", err=True)
        click.echo(f"  Run directories: {len(run_dirs)}", err=True)
        click.echo(f"  Workspace: {workspace}", err=True)
        for rd in run_dirs:
            click.echo(f"    - {rd.name}", err=True)
        click.echo("", err=True)

    try:
        detail, ml_meta = mlflow_backend.log_multi_run_artifacts(
            all_artifact_paths=all_artifact_paths,
            artifact_root=from_path,
            run_dirs=run_dirs,
            metrics_file=METRICS_FILE,
            parameters_file=PARAMETERS_FILE,
            tracking_uri=tracking_uri,
            experiment=experiment,
            parent_run_name=run_name,
            insecure_tls=insecure_tls,
            connection=mlflow_connection,
            verbose=export_cfg.verbose,
            upload_workers=export_cfg.upload_workers,
            run_metadata=run_metadata,
            workspace=workspace,
            child_run_names=child_run_names or None,
        )
        results = [
            FileExportBackendResult(
                backend="mlflow",
                status="success",
                detail=detail,
                metadata=ml_meta,
            )
        ]
    except Exception as e:
        traceback.print_exception(e, file=sys.stderr)
        click.echo(f"multi-run export failed: {e}", err=True)
        results = [FileExportBackendResult(backend="mlflow", status="failure", detail=str(e))]

    for r in results:
        click.echo(f"{r.backend}: {r.status} {r.detail}")

    if status_yaml is not None:
        try:
            write_artifacts_status_yaml(status_yaml, results)
            click.echo(f"Wrote status YAML to {status_yaml}")
        except OSError as e:
            click.echo(f"Failed to write status YAML ({status_yaml}): {e}", err=True)
            return 4

    if any(r.status == "failure" for r in results):
        return 4
    return 0
