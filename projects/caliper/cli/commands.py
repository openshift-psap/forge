"""Caliper CLI command definitions."""

from __future__ import annotations

import sys
from pathlib import Path

import click
import yaml

from projects.caliper.cli.s3_import import run_s3_import_with_explicit_params
from projects.caliper.engine.ai_eval import run_ai_eval_export
from projects.caliper.engine.file_export.artifacts_export_run import run_artifacts_export
from projects.caliper.engine.file_export.artifacts_import_run import run_artifacts_import
from projects.caliper.engine.file_export.mlflow_config import load_mlflow_config_yaml
from projects.caliper.engine.kpi.analyze import run_analyze
from projects.caliper.engine.kpi.generate import run_kpi_generate
from projects.caliper.engine.kpi.import_export import (
    import_kpis_snapshot,
)
from projects.caliper.engine.load_plugin import load_plugin
from projects.caliper.engine.parse import run_parse
from projects.caliper.engine.plugin_config import resolve_plugin_module_string
from projects.caliper.engine.visualize import run_visualize


def _exit_with_help(ctx: click.Context, msg: str, code: int = 1) -> None:
    """Print error message and help, then exit with error code."""
    click.echo(f"Error: {msg}", err=True)
    click.echo(ctx.get_help(), err=True)
    ctx.exit(code)


def _workspace_cli_options(cmd_func):
    """Common workspace CLI options decorator."""
    cmd_func = click.option(
        "--artifacts-dir",
        "artifacts_dir",
        type=click.Path(path_type=Path, exists=True),
        default=None,
    )(cmd_func)
    cmd_func = click.option(
        "--postprocess-config",
        type=click.Path(path_type=Path, dir_okay=False, exists=True),
        default=None,
    )(cmd_func)
    cmd_func = click.option(
        "--plugin",
        "plugin_module_override",
        metavar="MODULE",
        default=None,
    )(cmd_func)
    return cmd_func


def _apply_workspace_cli_overrides(
    ctx: click.Context,
    artifacts_dir: Path | None,
    postprocess_config: Path | None,
    plugin_module_override: str | None,
) -> None:
    """Apply CLI overrides to context object."""
    if artifacts_dir:
        ctx.obj["base_dir"] = artifacts_dir
    if postprocess_config:
        ctx.obj["postprocess_config"] = postprocess_config
    if plugin_module_override:
        ctx.obj["plugin_cli"] = plugin_module_override


def _root_obj(ctx: click.Context) -> dict:
    """Get root context object."""
    return ctx.find_root().obj


def _plugin_tuple(ctx: click.Context):
    """Get plugin module and plugin from context."""

    root = _root_obj(ctx)
    try:
        mod, _manifest_path = resolve_plugin_module_string(
            base_dir=root.get("base_dir"),
            postprocess_config=root.get("postprocess_config"),
            cli_plugin=root.get("plugin_cli"),
        )
        if not mod:
            raise RuntimeError("Plugin module not found")
        plugin = load_plugin(mod)
        if not plugin:
            raise RuntimeError(f"Unable to load plugin: {mod}")
    except RuntimeError as e:
        _exit_with_help(ctx, str(e), code=2)
    return mod, plugin


# Main commands
@click.command("parse")
@_workspace_cli_options
@click.option("--no-cache", is_flag=True, help="Force full parse.")
@click.option(
    "--cache-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Override cache file path.",
)
@click.option(
    "--show-matrix/--no-show-matrix",
    default=True,
    help="Display parameter matrix summary after parsing (default: enabled).",
)
@click.option(
    "--status-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to write status YAML for orchestration (absolute path required).",
)
@click.pass_context
def parse_cmd(
    ctx: click.Context,
    no_cache: bool,
    cache_dir: Path | None,
    show_matrix: bool,
    status_file: Path | None,
    artifacts_dir: Path | None,
    postprocess_config: Path | None,
    plugin_module_override: str | None,
) -> None:
    """Parse test artifacts into a unified data model."""
    _apply_workspace_cli_overrides(
        ctx,
        artifacts_dir=artifacts_dir,
        postprocess_config=postprocess_config,
        plugin_module_override=plugin_module_override,
    )
    mod, plugin = _plugin_tuple(ctx)
    artifact_root: Path = _root_obj(ctx)["base_dir"]

    status = {"success": False}

    try:
        model = run_parse(
            base_dir=artifact_root,
            plugin_module=mod,
            plugin=plugin,
            use_cache=not no_cache,
            show_parameter_matrix=show_matrix,
        )

        # Extract test directories from test nodes
        test_directories = [str(node.directory) for node in model.test_nodes]

        status.update(
            {
                "success": True,
                "plugin_module": mod,
                "parsed_records": len(model.unified_result_records),
                "test_directories": test_directories,
                "test_directories_count": len(test_directories),
                "cache_ref": str(model.parse_cache_ref) if model.parse_cache_ref else None,
            }
        )

        click.echo(
            f"Parsed {len(model.unified_result_records)} record(s) from {len(test_directories)} test directories; cache={model.parse_cache_ref}"
        )

        # Show test directories for user reference
        if test_directories:
            click.echo("📁 Test directories found:")
            for test_dir in test_directories:
                click.echo(f"   • {test_dir}")
        else:
            click.echo("⚠️  No test directories found")

    except Exception as e:  # noqa: BLE001
        status.update(
            {
                "success": False,
                "error": str(e),
            }
        )
        click.echo(f"parse failed: {e}", err=True)
    finally:
        # Write status file if requested
        if status_file:
            try:
                status_file.parent.mkdir(parents=True, exist_ok=True)
                with open(status_file, "w", encoding="utf-8") as f:
                    yaml.dump(status, f, default_flow_style=False, sort_keys=False)
            except Exception as e:
                click.echo(f"Failed to write status file {status_file}: {e}", err=True)

    # Exit with non-zero code on failure
    if not status["success"]:
        sys.exit(2)


@click.command("visualize")
@_workspace_cli_options
@click.option("--reports", default=None, help="Comma-separated report ids.")
@click.option("--report-group", default=None)
@click.option("--visualize-config", type=click.Path(path_type=Path), default=None)
@click.option("--include-label", multiple=True)
@click.option("--exclude-label", multiple=True)
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    required=True,
)
@click.option(
    "--status-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to write status YAML for orchestration (absolute path required).",
)
@click.pass_context
def visualize_cmd(
    ctx: click.Context,
    reports: str | None,
    report_group: str | None,
    visualize_config: Path | None,
    include_label: tuple[str, ...],
    exclude_label: tuple[str, ...],
    output_dir: Path,
    status_file: Path | None,
    artifacts_dir: Path | None,
    postprocess_config: Path | None,
    plugin_module_override: str | None,
) -> None:
    """Generate visual reports and charts from parsed test data."""
    _apply_workspace_cli_overrides(
        ctx,
        artifacts_dir=artifacts_dir,
        postprocess_config=postprocess_config,
        plugin_module_override=plugin_module_override,
    )
    mod, plugin = _plugin_tuple(ctx)
    artifact_root: Path = _root_obj(ctx)["base_dir"]

    status = {"success": False}

    try:
        paths = run_visualize(
            base_dir=artifact_root,
            plugin_module=mod,
            plugin=plugin,
            output_dir=output_dir,
            reports_csv=reports,
            report_group=report_group,
            visualize_config_path=visualize_config,
            include_pairs=include_label,
            exclude_pairs=exclude_label,
            use_cache=True,
            cache_path=None,
        )

        status.update(
            {
                "success": True,
                "plugin_module": mod,
                "output_files": paths,
                "output_dir": str(output_dir),
                "generated_files": len(paths),
            }
        )

        click.echo("Wrote: " + ", ".join(paths))

    except Exception as e:  # noqa: BLE001
        import traceback

        error_details = {
            "error": str(e),
            "traceback": traceback.format_exc(),
        }

        status.update(
            {
                "success": False,
                **error_details,
            }
        )

        # Print full traceback to stderr for visibility
        click.echo(f"visualize failed: {e}", err=True)
        click.echo("Full traceback:", err=True)
        click.echo(traceback.format_exc(), err=True)
    finally:
        # Write status file if requested
        if status_file:
            try:
                status_file.parent.mkdir(parents=True, exist_ok=True)
                with open(status_file, "w", encoding="utf-8") as f:
                    yaml.dump(status, f, default_flow_style=False, sort_keys=False)
            except Exception as e:
                click.echo(f"Failed to write status file {status_file}: {e}", err=True)

    # Exit with non-zero code on failure
    if not status["success"]:
        sys.exit(2)


@click.command("list-reports")
@_workspace_cli_options
@click.pass_context
def list_reports_cmd(
    ctx: click.Context,
    artifacts_dir: Path | None,
    postprocess_config: Path | None,
    plugin_module_override: str | None,
):
    """List available reports supported by the plugin."""
    try:
        _apply_workspace_cli_overrides(
            ctx,
            artifacts_dir=artifacts_dir,
            postprocess_config=postprocess_config,
            plugin_module_override=plugin_module_override,
        )
        mod, plugin = _plugin_tuple(ctx)

        # Get plugin docstring to extract report information
        plugin_doc = plugin.__class__.__doc__ or ""

        # Extract available reports from docstring
        reports = []
        in_reports_section = False

        for line in plugin_doc.split("\n"):
            line = line.strip()
            if "Available visual reports:" in line:
                in_reports_section = True
                continue
            elif in_reports_section and line.startswith("*"):
                # Extract report ID from lines like "* ``report_id`` — description"
                if "``" in line:
                    report_parts = line.split("``")
                    if len(report_parts) >= 3:
                        report_id = report_parts[1]
                        description = report_parts[2].strip(" —").strip()
                        reports.append((report_id, description))
            elif in_reports_section and not line.startswith("*") and line:
                # End of reports section
                break

        if reports:
            click.echo("Available reports:")
            for report_id, description in reports:
                click.echo(f"  * {report_id} — {description}")
        else:
            click.echo("No reports found in plugin documentation.")

    except Exception as e:
        click.echo(f"Failed to list reports: {e}", err=True)
        sys.exit(1)


@click.command("ai-eval-export", hidden=True)
@_workspace_cli_options
@click.option("--output", type=click.Path(path_type=Path), required=True)
@click.option("--no-cache", is_flag=True, help="Disable parse cache")
@click.option(
    "--status-file", type=click.Path(path_type=Path), help="YAML file to write operation status"
)
@click.pass_context
def ai_eval_export(
    ctx: click.Context,
    output: Path,
    artifacts_dir: Path | None,
    postprocess_config: Path | None,
    plugin_module_override: str | None,
    no_cache: bool,
    status_file: Path | None,
) -> None:
    _apply_workspace_cli_overrides(
        ctx,
        artifacts_dir=artifacts_dir,
        postprocess_config=postprocess_config,
        plugin_module_override=plugin_module_override,
    )
    mod, plugin = _plugin_tuple(ctx)
    artifact_root: Path = _root_obj(ctx)["base_dir"]

    status_data = {"success": False}

    try:
        run_ai_eval_export(
            base_dir=artifact_root,
            plugin_module=mod,
            plugin=plugin,
            output=output,
            use_cache=not no_cache,
        )
        status_data = {"success": True, "output_file": str(output)}
        click.echo(f"Exported {output}")
    except Exception as e:  # noqa: BLE001
        import traceback

        full_traceback = traceback.format_exc()
        status_data = {"success": False, "error": str(e), "traceback": full_traceback}
        click.echo(f"ai-eval-export failed: {e}", err=True)
        click.echo(f"Full traceback:\n{full_traceback}", err=True)

        if not status_file:
            sys.exit(3)
    finally:
        # Write status file if requested
        if status_file:
            try:
                with open(status_file, "w", encoding="utf-8") as f:
                    yaml.dump(status_data, f, default_flow_style=False)
            except Exception as status_err:
                click.echo(f"Failed to write status file {status_file}: {status_err}", err=True)
                sys.exit(4)

    # Exit with error code if operation failed
    if not status_data.get("success", False):
        sys.exit(3)


# KPI commands
@click.command("generate")
@_workspace_cli_options
@click.option("--output", type=click.Path(path_type=Path), required=True)
@click.option(
    "--format",
    "format_type",
    type=click.Choice(["hierarchical", "jsonl"], case_sensitive=False),
    default="hierarchical",
    help="Output format: hierarchical JSON (default) or JSONL",
)
@click.option(
    "--status-file", type=click.Path(path_type=Path), help="YAML file to write operation status"
)
@click.pass_context
def kpi_generate(
    ctx: click.Context,
    output: Path,
    format_type: str,
    artifacts_dir: Path | None,
    postprocess_config: Path | None,
    plugin_module_override: str | None,
    status_file: Path | None,
) -> None:
    _apply_workspace_cli_overrides(
        ctx,
        artifacts_dir=artifacts_dir,
        postprocess_config=postprocess_config,
        plugin_module_override=plugin_module_override,
    )
    mod, plugin = _plugin_tuple(ctx)
    artifact_root: Path = _root_obj(ctx)["base_dir"]

    status_data = {"success": False}

    try:
        run_kpi_generate(
            base_dir=artifact_root,
            plugin_module=mod,
            plugin=plugin,
            output=output,
            use_cache=True,
            cache_path=None,
            format_type=format_type,
        )
        status_data = {"success": True, "output_file": str(output)}
        click.echo(f"Generated {output}")
    except Exception as e:  # noqa: BLE001
        import traceback

        full_traceback = traceback.format_exc()
        status_data = {"success": False, "error": str(e), "traceback": full_traceback}
        click.echo(f"kpi generate failed: {e}", err=True)
        click.echo(f"Full traceback:\n{full_traceback}", err=True)

        if not status_file:
            sys.exit(3)
    finally:
        # Write status file if requested
        if status_file:
            try:
                with open(status_file, "w", encoding="utf-8") as f:
                    yaml.dump(status_data, f, default_flow_style=False)
            except Exception as status_err:
                click.echo(f"Failed to write status file {status_file}: {status_err}", err=True)
                sys.exit(4)

    # Exit with error code if operation failed
    if not status_data.get("success", False):
        sys.exit(3)


@click.command("csv-export")
@_workspace_cli_options
@click.option(
    "--input",
    "input_file",
    type=click.Path(path_type=Path),
    required=True,
    help="Input KPI JSON file",
)
@click.option("--output", type=click.Path(path_type=Path), required=True, help="Output CSV file")
@click.option(
    "--include-header-comments", is_flag=True, default=True, help="Include header comments in CSV"
)
@click.option(
    "--status-file", type=click.Path(path_type=Path), help="YAML file to write operation status"
)
@click.pass_context
def kpi_csv_export(
    ctx: click.Context,
    input_file: Path,
    output: Path,
    artifacts_dir: Path | None,
    postprocess_config: Path | None,
    plugin_module_override: str | None,
    include_header_comments: bool,
    status_file: Path | None,
) -> None:
    _apply_workspace_cli_overrides(
        ctx,
        artifacts_dir=artifacts_dir,
        postprocess_config=postprocess_config,
        plugin_module_override=plugin_module_override,
    )
    mod, plugin = _plugin_tuple(ctx)

    status_data = {"success": False}

    try:
        # Read KPI file (supports both hierarchical JSON and JSONL formats)
        from projects.caliper.engine.kpi.format import read_kpis_from_file

        kpi_records = read_kpis_from_file(input_file)

        # Export to CSV
        from projects.caliper.engine.kpi.csv_export import export_kpis_to_csv

        result_path = export_kpis_to_csv(
            plugin=plugin,
            kpi_records=kpi_records,
            output_path=output,
            include_header_comments=include_header_comments,
        )

        status_data = {
            "success": True,
            "output_file": str(result_path),
            "kpi_count": len(kpi_records),
        }
        click.echo(f"Exported {len(kpi_records)} KPI records to CSV: {result_path}")
    except Exception as e:  # noqa: BLE001
        import traceback

        full_traceback = traceback.format_exc()
        status_data = {"success": False, "error": str(e), "traceback": full_traceback}
        click.echo(f"kpi csv-export failed: {e}", err=True)
        click.echo(f"Full traceback:\n{full_traceback}", err=True)

        if not status_file:
            sys.exit(3)
    finally:
        # Write status file if requested
        if status_file:
            try:
                with open(status_file, "w", encoding="utf-8") as f:
                    yaml.dump(status_data, f, default_flow_style=False)
            except Exception as status_err:
                click.echo(f"Failed to write status file {status_file}: {status_err}", err=True)
                sys.exit(4)

    # Exit with error code if operation failed
    if not status_data.get("success", False):
        sys.exit(3)


@click.command("import")
@click.option("--snapshot", type=click.Path(path_type=Path), required=True)
@click.pass_context
def kpi_import(ctx: click.Context, snapshot: Path) -> None:
    try:
        import_kpis_snapshot(snapshot_path=snapshot)
    except Exception as e:  # noqa: BLE001
        click.echo(f"kpi import failed: {e}", err=True)
        sys.exit(3)
    click.echo(f"Imported {snapshot}")


@click.command("analyze")
@click.option(
    "--current", type=click.Path(path_type=Path), required=True, help="Current KPI file to analyze"
)
@click.option(
    "--baseline-dir",
    type=click.Path(path_type=Path),
    required=True,
    help="Directory containing historical KPI files (will use most recent)",
)
@click.option(
    "--output",
    type=click.Path(path_type=Path),
    required=True,
    help="Output file for analysis results",
)
@click.option(
    "--plugin",
    "plugin_module",
    metavar="MODULE",
    required=True,
    help="Plugin module for KPI definitions and analysis rules",
)
@click.pass_context
def kpi_analyze(
    ctx: click.Context,
    current: Path,
    baseline_dir: Path,
    output: Path,
    plugin_module: str,
) -> None:
    """Analyze KPIs for regression."""
    try:
        from projects.caliper.engine.kpi.analyze import find_baseline_kpis
        from projects.caliper.engine.load_plugin import load_plugin

        # Load plugin for KPI definitions and analysis rules
        plugin = load_plugin(plugin_module)
        if not plugin:
            click.echo(f"❌ Failed to load plugin: {plugin_module}", err=True)
            sys.exit(1)

        click.echo(f"🔌 Using plugin: {plugin_module}")

        baseline_kpis = find_baseline_kpis(baseline_dir)
        if not baseline_kpis:
            click.echo(
                f"❌ No kpis.json files found in baseline directory: {baseline_dir}", err=True
            )
            sys.exit(1)

        click.echo(f"📊 Found {len(baseline_kpis)} baseline files to process")

        # Run analysis with ALL baseline files (not just the most recent)
        result = run_analyze(
            current_path=current, baseline_kpis=baseline_kpis, output_path=output, plugin=plugin
        )

        # Check result status at CLI level
        if result["status"] == "success":
            click.echo(f"✅ Analysis complete. Results written to: {output}")
        elif result["status"] == "skipped":
            click.echo(f"⚠️  Analysis skipped: {result.get('reason', 'Unknown reason')}")
            click.echo(f"📝 Output written to: {output}")
        else:
            click.echo(f"❌ Analysis failed: {result.get('error', 'Unknown error')}", err=True)
            sys.exit(1)

        # Show the analysis results on screen
        if output.exists():
            try:
                import json

                with open(output) as f:
                    result_data = json.load(f)

                # Convert to YAML for better readability
                yaml_output = yaml.dump(result_data, default_flow_style=False, indent=2)
                click.echo("\n📊 Analysis Results:")
                click.echo("=" * 50)
                click.echo(yaml_output)
            except Exception as e:
                click.echo(f"⚠️  Could not display results: {e}")
        else:
            click.echo("⚠️  Output file not found")
    except Exception as e:  # noqa: BLE001
        raise e
        click.echo(f"❌ kpi analyze failed: {e}", err=True)
        sys.exit(3)


@click.command("analyse-kpis")
@click.option(
    "--output",
    type=click.Path(path_type=Path),
    required=True,
    help="Output file for analysis results",
)
@click.option(
    "--current-kpis-file",
    type=click.Path(path_type=Path, exists=True),
    required=True,
    help="Path to current KPIs JSON file",
)
@click.option(
    "--historical-kpis-dir",
    type=click.Path(path_type=Path, exists=True),
    required=True,
    help="Directory containing historical KPI files",
)
@click.option(
    "--plugin",
    "plugin_module",
    metavar="MODULE",
    required=True,
    help="Plugin module for KPI definitions and analysis rules",
)
@click.option(
    "--status-file", type=click.Path(path_type=Path), help="YAML file to write operation status"
)
def analyse_kpis_cmd(
    output: Path,
    current_kpis_file: Path,
    historical_kpis_dir: Path,
    plugin_module: str,
    status_file: Path | None,
) -> None:
    """Analyze KPIs for orchestration (fork/exec)."""
    import yaml

    status_data = {"success": False}

    try:
        # Create a minimal postprocess config for analysis
        from projects.caliper.orchestration.postprocess_config import (
            CaliperOrchestrationPostprocessConfig,
        )

        # Create config with analyze enabled and using provided directories
        postprocess_config_dict = {
            "analyze": {
                "enabled": True,
                "output": output.name,  # Use the provided output filename
                "historical_kpis": str(historical_kpis_dir),  # Use provided historical dir
            }
        }

        postprocess_config_obj = CaliperOrchestrationPostprocessConfig.model_validate(
            postprocess_config_dict
        )

        output_dir = output.parent

        # Run analysis
        from projects.caliper.engine.kpi.analyze import analyze_kpis

        try:
            result = analyze_kpis(
                postprocess_config=postprocess_config_obj,
                plugin_module=plugin_module,
                base_dir=output_dir,  # Use output dir as base since we have absolute paths
                output_dir=output_dir,
                current_kpis_file=current_kpis_file,
            )
        except Exception as e:
            # Don't suppress the exception - let it bubble up with full traceback
            import sys
            import traceback

            print(f"❌ Exception in analyze_kpis: {e}", file=sys.stderr)
            print(f"Full traceback:\n{traceback.format_exc()}", file=sys.stderr)
            raise

        # Check if the analysis function returned a proper result
        if not isinstance(result, dict) or "status" not in result:
            raise ValueError(f"Invalid result from analyze_kpis: {result}")

        status_data = {
            "success": result.get("status") == "success",
            "output_file": result.get("output_file"),
        }

        if result.get("status") == "success":
            click.echo(f"✅ KPI analysis completed: {result.get('output_file')}")
        else:
            # Show the full result for debugging if no clear error message
            if "error" in result:
                error_msg = result["error"]
            else:
                error_msg = f"Analysis failed with result: {result}"

            status_data["error"] = error_msg
            status_data.update(result)  # Include all result data for debugging
            click.echo(f"❌ KPI analysis failed: {error_msg}", err=True)
            if not status_file:
                sys.exit(1)

    except Exception as e:  # noqa: BLE001
        import traceback

        full_traceback = traceback.format_exc()
        status_data = {"success": False, "error": str(e), "traceback": full_traceback}
        click.echo(f"❌ analyse-kpis failed: {e}", err=True)
        click.echo(f"Full traceback:\n{full_traceback}", err=True)
        if not status_file:
            sys.exit(2)
    finally:
        # Write status file if requested
        if status_file:
            try:
                with open(status_file, "w", encoding="utf-8") as f:
                    yaml.dump(status_data, f, default_flow_style=False)
            except Exception as status_err:
                click.echo(f"Failed to write status file {status_file}: {status_err}", err=True)
                sys.exit(4)

    # Exit with error code if operation failed
    if not status_data.get("success", False):
        sys.exit(3)


@click.command("s3-import")
@click.option("--bucket", required=True, help="S3 bucket name")
@click.option("--prefix", default="", help="S3 object prefix/path")
@click.option(
    "--output-dir", type=click.Path(path_type=Path), required=True, help="Local output directory"
)
@click.option("--include-kpis-json", is_flag=True, default=False, help="Download kpis.json files")
@click.option("--include-kpis-csv", is_flag=True, default=False, help="Download kpis.csv files")
@click.option("--include-ai-data", is_flag=True, default=False, help="Download ai_data directories")
@click.option("--max-downloads", type=int, default=50, help="Maximum number of files to download")
@click.option(
    "--vault", default="psap-forge-aws-s3-export", help="Vault containing AWS credentials"
)
@click.option(
    "--aws-credentials-file", default="aws.credentials", help="Credentials file name within vault"
)
@click.option("-v", "--verbose", is_flag=True, help="Show detailed progress information")
@click.option(
    "--status-file", type=click.Path(path_type=Path), help="YAML file to write operation status"
)
@click.pass_context
def kpi_s3_import(
    ctx: click.Context,
    bucket: str,
    prefix: str,
    output_dir: Path,
    include_kpis_json: bool,
    include_kpis_csv: bool,
    include_ai_data: bool,
    max_downloads: int,
    vault: str,
    aws_credentials_file: str,
    verbose: bool,
    status_file: Path | None,
) -> None:
    """Download historical KPI and analysis data from S3."""
    from projects.core.library import vault as vault_lib

    status_data = {"success": False}

    try:
        # Ensure output directory exists
        output_dir.mkdir(parents=True, exist_ok=True)

        if verbose:
            click.echo("🔍 S3 Import Configuration:")
            click.echo(f"   Bucket: {bucket}")
            click.echo(f"   Prefix: {prefix}")
            click.echo(f"   Output Directory: {output_dir}")

        # Initialize vault system
        vault_lib.init(vaults=[vault] if vault else [])

        # Run S3 import
        result = run_s3_import_with_explicit_params(
            bucket=bucket,
            prefix=prefix,
            output_dir=output_dir,
            vault=vault,
            aws_credentials_file=aws_credentials_file,
            include_kpis_json=include_kpis_json,
            include_kpis_csv=include_kpis_csv,
            include_ai_data=include_ai_data,
            max_downloads=max_downloads,
        )

        # Construct S3 path for status
        s3_path = f"s3://{bucket}"
        if prefix:
            s3_path += f"/{prefix}"

        if result["status"] == "success":
            downloaded_count = result.get("downloaded_files", 0)
            status_data = {
                "success": True,
                "output_dir": str(output_dir),
                "downloaded_files": downloaded_count,
                "file_count": downloaded_count,
                "imported_path": s3_path,
            }
            click.echo("✅ S3 import completed successfully")
            click.echo(f"📄 Downloaded {downloaded_count} files to {output_dir}")
        elif result["status"] == "warning":
            status_data = {
                "success": True,
                "output_dir": str(output_dir),
                "downloaded_files": 0,
                "file_count": 0,
                "warning": result.get("message", "No objects found matching filters"),
                "imported_path": s3_path,
            }
            click.echo(f"⚠️  {result.get('message', 'No objects found matching filters')}")
        else:
            error_msg = result.get("error", "unknown error")
            status_data = {"success": False, "error": error_msg, "imported_path": s3_path}
            click.echo(f"❌ S3 import failed: {error_msg}", err=True)
            if not status_file:
                sys.exit(1)

    except Exception as e:  # noqa: BLE001
        import traceback

        # Construct S3 path for error cases too
        s3_path = f"s3://{bucket}"
        if prefix:
            s3_path += f"/{prefix}"

        full_traceback = traceback.format_exc()
        status_data = {
            "success": False,
            "error": str(e),
            "traceback": full_traceback,
            "imported_path": s3_path,
        }
        click.echo(f"S3 import failed: {e}", err=True)
        click.echo(f"Full traceback:\n{full_traceback}", err=True)

        if not status_file:
            sys.exit(3)
    finally:
        # Write status file if requested
        if status_file:
            try:
                with open(status_file, "w", encoding="utf-8") as f:
                    yaml.dump(status_data, f, default_flow_style=False)
            except Exception as status_err:
                click.echo(f"Failed to write status file {status_file}: {status_err}", err=True)
                sys.exit(4)

    # Exit with error code if operation failed
    if not status_data.get("success", False):
        sys.exit(3)


# Artifacts commands
@click.command("export")
@click.option(
    "--from",
    "from_path",
    type=click.Path(path_type=Path, exists=True),
    required=True,
    help="Source path containing artifacts to export.",
)
@click.option("--to", default=None, help="Target URL for upload.")
@click.option("--backend", multiple=True, type=str, help="Repeat: mlflow.")
@click.option(
    "--mlflow-tracking-uri",
    help="MLflow tracking server URI.",
    envvar="MLFLOW_TRACKING_URI",
)
@click.option("--mlflow-experiment", default=None, envvar="MLFLOW_EXPERIMENT_NAME")
@click.option("--mlflow-run-id", default=None, envvar="MLFLOW_RUN_ID")
@click.option("--mlflow-run-name", default=None, envvar="CALIPER_MLFLOW_RUN_NAME")
@click.option("--mlflow-insecure-tls", is_flag=True)
@click.option(
    "--mlflow-secrets",
    "mlflow_secrets_path",
    type=click.Path(path_type=Path, exists=True),
    default=None,
)
@click.option(
    "--mlflow-config",
    "mlflow_config_path",
    type=click.Path(path_type=Path, exists=True),
    default=None,
)
@click.option("--dry-run", is_flag=True)
@click.option("-v", "--verbose", is_flag=True)
@click.option(
    "--status-yaml",
    "status_yaml_path",
    type=click.Path(path_type=Path),
    default=None,
)
@click.option("--upload-workers", type=click.IntRange(min=1, max=64), default=10)
@click.pass_context
def artifacts_export(
    ctx: click.Context,
    from_path: Path,
    to: str | None,
    backend: tuple[str, ...],
    mlflow_tracking_uri: str | None,
    mlflow_experiment: str | None,
    mlflow_run_id: str | None,
    mlflow_run_name: str | None,
    mlflow_insecure_tls: bool,
    mlflow_secrets_path: Path | None,
    mlflow_config_path: Path | None,
    dry_run: bool,
    verbose: bool,
    status_yaml_path: Path | None,
    upload_workers: int,
) -> None:
    # Load config from file if provided, CLI args override
    config = {}
    if mlflow_config_path:
        try:
            config = load_mlflow_config_yaml(mlflow_config_path)
        except Exception as e:
            click.echo(f"Failed to load MLflow config: {e}", err=True)
            sys.exit(1)

    # CLI args override config file values
    final_config = {
        "tracking_uri": mlflow_tracking_uri or config.get("tracking_uri"),
        "experiment": mlflow_experiment or config.get("experiment"),
        "run_id": mlflow_run_id or config.get("run_id"),
        "run_name": mlflow_run_name or config.get("run_name"),
        "insecure_tls": mlflow_insecure_tls or config.get("insecure_tls", False),
        "secrets_path": mlflow_secrets_path or config.get("secrets_path"),
        "upload_workers": upload_workers,
    }

    try:
        run_artifacts_export(
            from_path=from_path,
            backend=list(backend) if backend else ["mlflow"],
            dry_run=dry_run,
            verbose=verbose,
            status_yaml_path=status_yaml_path,
            mlflow_config=final_config,
        )
    except Exception as e:  # noqa: BLE001
        click.echo(f"artifacts export failed: {e}", err=True)
        sys.exit(3)


@click.command("import")
@click.option("--from-mlflow", "mlflow_run_id", help="MLflow run ID to download artifacts from.")
@click.option("--from-mlflow-url", "mlflow_url", help="MLflow web UI URL.")
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    required=True,
    help="Directory where downloaded artifacts will be saved.",
)
@click.option("--mlflow-tracking-uri", envvar="MLFLOW_TRACKING_URI")
@click.option("--artifact-path", default="")
@click.option("--timeout", default=300, type=click.IntRange(min=1))
@click.option("--mlflow-insecure-tls", is_flag=True)
@click.option("--mlflow-experiment", default=None)
@click.option("--mlflow-workspace", default=None)
@click.option(
    "--mlflow-secrets",
    "mlflow_secrets_path",
    type=click.Path(path_type=Path, exists=True),
    default=None,
)
@click.pass_context
def artifacts_import(
    ctx: click.Context,
    mlflow_run_id: str | None,
    mlflow_url: str | None,
    output_dir: Path,
    mlflow_tracking_uri: str | None,
    artifact_path: str,
    timeout: int,
    mlflow_insecure_tls: bool,
    mlflow_experiment: str | None,
    mlflow_workspace: str | None,
    mlflow_secrets_path: Path | None,
) -> None:
    """Download artifacts from MLflow."""

    try:
        run_artifacts_import(
            mlflow_run_id=mlflow_run_id,
            mlflow_url=mlflow_url,
            output_dir=output_dir,
            mlflow_tracking_uri=mlflow_tracking_uri,
            artifact_path=artifact_path,
            timeout=timeout,
            mlflow_insecure_tls=mlflow_insecure_tls,
            mlflow_experiment=mlflow_experiment,
            mlflow_workspace=mlflow_workspace,
            mlflow_secrets_path=mlflow_secrets_path,
        )
    except Exception as e:  # noqa: BLE001
        click.echo(f"❌ artifacts import failed: {e}", err=True)
        sys.exit(3)
