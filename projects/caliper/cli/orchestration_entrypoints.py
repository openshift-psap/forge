"""
Entrypoint functions for orchestration to call Caliper core functionality.

This module provides well-defined interfaces between the orchestration layer
and Caliper's engine/CLI components.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any


def parse_entrypoint(
    postprocess_config,
    tree_root: Path,
    manifest_path: Path | None,
    use_cache: bool = True,
) -> tuple[Any, str]:
    """Entrypoint for orchestration to call parse functionality."""
    from projects.caliper.engine.parse import run_parse

    mod_str, plugin = load_plugin_entrypoint(postprocess_config, tree_root, manifest_path)

    model = run_parse(
        base_dir=tree_root,
        plugin_module=mod_str,
        plugin=plugin,
        use_cache=use_cache,
    )

    return model, mod_str


def run_visualize_entrypoint(**kwargs) -> list[str]:
    """Entrypoint for orchestration to call visualize functionality."""
    from projects.caliper.engine.visualize import run_visualize

    return run_visualize(**kwargs)


def load_plugin_entrypoint(
    postprocess_config,
    tree_root: Path,
    manifest_path: Path | None,
) -> tuple[str, Any]:
    """Entrypoint for orchestration to load plugins."""
    from projects.caliper.engine.load_plugin import load_plugin
    from projects.caliper.engine.plugin_config import resolve_plugin_module_string

    mod_str, _manifest = resolve_plugin_module_string(
        base_dir=tree_root,
        postprocess_config=manifest_path,
        cli_plugin=postprocess_config.plugin_module,
    )

    return mod_str, load_plugin(mod_str)


def s3_import_entrypoint(
    postprocess_config,
    output_dir: Path,
) -> dict[str, Any]:
    """Entrypoint for orchestration to call S3 import functionality."""

    if not postprocess_config.s3.import_.enabled:
        return {"status": "disabled", "reason": "s3_import disabled", "completed_at": time.time()}

    try:
        from projects.caliper.cli.s3_import import run_s3_import_with_explicit_params
        from projects.caliper.orchestration.step_logging import log_s3_import_command

        # Log command to reproduce this step
        s3_parent_config = postprocess_config.s3
        s3_config = postprocess_config.s3.import_

        # Build import prefix using instance + s3.prefix + directory
        import_prefix_parts = []
        if s3_parent_config.instance:
            import_prefix_parts.append(s3_parent_config.instance)
        if s3_parent_config.prefix:
            import_prefix_parts.append(s3_parent_config.prefix.rstrip("/"))
        if s3_parent_config.directory:
            import_prefix_parts.append(s3_parent_config.directory)

        import_prefix = "/".join(import_prefix_parts) if import_prefix_parts else ""
        import_dir = output_dir / s3_config.output_dir

        log_s3_import_command(
            bucket=s3_parent_config.bucket,
            prefix=import_prefix,
            output_dir=import_dir,
            include_kpis_json=s3_config.include_kpis_json,
            include_kpis_csv=s3_config.include_kpis_csv,
            include_ai_data=s3_config.include_ai_data,
            max_downloads=s3_config.max_downloads,
        )

        # Use explicit parameters instead of config dict
        result = run_s3_import_with_explicit_params(
            bucket=s3_parent_config.bucket,
            prefix=import_prefix,
            output_dir=import_dir,
            vault=s3_parent_config.vault.name,
            aws_credentials_file=s3_parent_config.vault.aws_credentials_file,
            include_kpis_json=s3_config.include_kpis_json,
            include_kpis_csv=s3_config.include_kpis_csv,
            include_ai_data=s3_config.include_ai_data,
            max_downloads=s3_config.max_downloads,
        )
        return result

    except Exception as e:
        import logging

        logger = logging.getLogger(__name__)
        logger.exception("S3 import failed")
        return {"status": "failed", "error": str(e), "completed_at": time.time()}


def s3_export_entrypoint(
    postprocess_config,
    output_dir: Path,
    kpis_file: Path | None = None,
    csv_file: Path | None = None,
    ai_data_dir: Path | None = None,
    analysis_file: Path | None = None,
) -> dict[str, Any]:
    """Entrypoint for orchestration to call S3 export functionality with explicit paths."""
    from projects.caliper.cli.s3_export import run_s3_export_with_explicit_paths

    s3_parent_config = postprocess_config.s3
    s3_config = postprocess_config.s3.export

    return run_s3_export_with_explicit_paths(
        kpis_file=kpis_file,
        csv_file=csv_file,
        ai_data_dir=ai_data_dir,
        analysis_file=analysis_file,
        bucket=s3_parent_config.bucket,
        prefix=s3_parent_config.prefix,
        instance=s3_parent_config.instance,
        directory=s3_parent_config.directory,
        upload_id=s3_config.upload_id,
        vault=s3_parent_config.vault.name,
        aws_credentials_file=s3_parent_config.vault.aws_credentials_file,
        dry_run=s3_config.dry_run,
    )


def get_kpi_functions_entrypoint(plugin_module) -> dict:
    """Entrypoint for orchestration to get KPI function metadata."""
    from projects.caliper.engine.kpi.decorators import get_kpi_functions

    try:
        plugin_module_obj = __import__(plugin_module, fromlist=[""])
        return get_kpi_functions(plugin_module_obj)
    except (ImportError, AttributeError):
        return {}


def analyze_kpis_entrypoint(
    postprocess_config,
    plugin_module: str,
    base_dir: Path,
    output_dir: Path,
    current_kpis_file: Path,
) -> dict[str, Any]:
    """Entrypoint for orchestration to call KPI analysis."""
    from projects.caliper.engine.kpi.analyze import analyze_kpis

    return analyze_kpis(
        postprocess_config=postprocess_config,
        plugin_module=plugin_module,
        base_dir=base_dir,
        output_dir=output_dir,
        current_kpis_file=current_kpis_file,
    )
