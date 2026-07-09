"""KPI analysis CLI entrypoint for Caliper."""

import sys
from pathlib import Path

import click


@click.command("analyze")
@click.option(
    "--current",
    type=click.Path(path_type=Path, exists=True),
    required=True,
    help="Path to current KPI JSON file",
)
@click.option(
    "--historical-dir",
    type=click.Path(path_type=Path, exists=True),
    required=True,
    help="Directory containing historical KPI files",
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
    required=True,
    help="Plugin module name for analysis",
)
def analyze_cli(current: Path, historical_dir: Path, output: Path, plugin_module: str) -> None:
    """CLI entrypoint for KPI analysis."""
    from projects.caliper.engine.kpi.analyze import run_kpi_analysis

    try:
        # Call the core analysis function from engine
        exit_code = run_kpi_analysis(
            current_kpi_file=current,
            historical_data_dir=historical_dir,
            output_file=output,
            plugin_module=plugin_module,
        )

        if exit_code == 0:
            click.echo(f"✅ Analysis completed successfully. Results written to: {output}")
        else:
            click.echo(f"❌ Analysis failed with exit code {exit_code}", err=True)
            sys.exit(exit_code)

    except Exception as e:
        click.echo(f"❌ Analysis failed: {e}", err=True)
        sys.exit(1)
