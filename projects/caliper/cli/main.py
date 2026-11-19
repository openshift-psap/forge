"""Caliper CLI."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

from projects.caliper.cli.commands import (
    ai_eval_export,
    analyse_kpis_cmd,
    artifacts_censor,
    artifacts_export,
    artifacts_import,
    html_export_cmd,
    kpi_csv_export,
    kpi_generate,
    kpi_import,
    kpi_s3_import,
    kpis_to_mlflow_cmd,
    list_reports_cmd,
    parse_cmd,
    visualize_cmd,
)
from projects.caliper.cli.s3_export_cli import s3_export_cmd

_ARTIFACTS_DIR_HELP = (
    "Root directory of the test artifact tree (directories containing "
    "__caliper_test_metadata__.yaml). Optional manifest files (e.g. caliper.yaml) are searched here "
    "unless --postprocess-config is set."
)
_PLUGIN_MODULE_HELP = (
    "Python import path of the Caliper plugin module (must expose get_plugin()). "
    "Names the plugin implementation; overrides plugin_module in the manifest when both "
    "are set."
)
_POSTPROCESS_CONFIG_HELP = (
    "Path to the post-processing manifest (YAML). If omitted, conventional filenames "
    "are searched under the artifact tree (--artifacts-dir)."
)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--artifacts-dir",
    "artifacts_dir",
    type=click.Path(path_type=Path, exists=True),
    default=None,
    help=_ARTIFACTS_DIR_HELP,
)
@click.option(
    "--postprocess-config",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    default=None,
    help=_POSTPROCESS_CONFIG_HELP,
)
@click.option(
    "--plugin",
    "plugin_module",
    metavar="MODULE",
    default=None,
    help=_PLUGIN_MODULE_HELP,
)
@click.pass_context
def main(
    ctx: click.Context,
    artifacts_dir: Path | None,
    postprocess_config: Path | None,
    plugin_module: str | None,
) -> None:
    """Caliper — artifact post-processing."""
    ctx.ensure_object(dict)
    ctx.obj["base_dir"] = artifacts_dir
    ctx.obj["postprocess_config"] = postprocess_config
    ctx.obj["plugin_cli"] = plugin_module


@main.group("kpi")
@click.pass_context
def kpi_group(ctx: click.Context) -> None:
    """KPI generate/import/export/."""


@main.group("artifacts")
@click.pass_context
def artifacts_group(ctx: click.Context) -> None:
    """File artifact export and import."""


def run_cli() -> None:
    """Invoke CLI; on missing required options, print subcommand help."""
    # Configure logging to capture INFO level messages for detailed output
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        force=True,  # Override any existing configuration
    )

    try:
        # standalone_mode=False returns exit codes instead of calling sys.exit;
        # propagate them so failures are non-zero (e.g. ctx.exit(1) from _exit_with_help).
        rv = main.main(standalone_mode=False, prog_name="caliper")
        if isinstance(rv, int) and rv != 0:
            sys.exit(rv)
    except click.ClickException as exc:
        # Handle click exceptions including NoArgsIsHelpError and MissingParameter
        if isinstance(exc, click.UsageError):
            # Check if this is an invalid command error
            if "No such command" in str(exc):
                click.echo(f"❌ Error: {exc}", err=True)
                click.echo("", err=True)
            else:
                click.echo(f"❌ Usage Error: {exc}", err=True)
                click.echo("", err=True)
        elif isinstance(exc, click.MissingParameter):
            click.echo(f"❌ Error: Missing required parameter: {exc.param.name}", err=True)
            if exc.param.name == "output_dir":
                click.echo("The --output-dir parameter is mandatory for artifact import.", err=True)
            click.echo("", err=True)

        # Show help context if available
        if hasattr(exc, "ctx") and exc.ctx:
            click.echo(exc.ctx.get_help(), err=True)
        else:
            exc.show(sys.stderr)
        sys.exit(2)
    except SystemExit:
        raise


# Register main commands
main.add_command(parse_cmd)
main.add_command(visualize_cmd)
main.add_command(list_reports_cmd)
main.add_command(ai_eval_export)

# Register KPI commands
kpi_group.add_command(kpi_generate)
kpi_group.add_command(kpi_csv_export)
kpi_group.add_command(kpi_import)
kpi_group.add_command(analyse_kpis_cmd)
kpi_group.add_command(kpi_s3_import)
kpi_group.add_command(kpis_to_mlflow_cmd)
kpi_group.add_command(html_export_cmd)

# Register s3-export command under kpi group
kpi_group.add_command(s3_export_cmd)

# Register artifacts commands
artifacts_group.add_command(artifacts_censor)
artifacts_group.add_command(artifacts_export)
artifacts_group.add_command(artifacts_import)


if __name__ == "__main__":
    run_cli()
