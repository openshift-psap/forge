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
@click.option(
    "--html",
    is_flag=True,
    default=False,
    help="Generate HTML report in addition to JSON output.",
)
def analyze_cli(
    current: Path, historical_dir: Path, output: Path, plugin_module: str, html: bool
) -> None:
    """CLI entrypoint for KPI analysis."""
    from projects.caliper.engine.kpi.analyze import analyze_kpis

    try:
        # Call the core analysis function from engine (returns status dict)
        result = analyze_kpis(
            current_kpis_file=current,
            historical_kpis_dir=historical_dir,
            output_file=output,
            plugin_module=plugin_module,
        )

        # Use exit code directly from the status dict
        exit_code = result.get("exit_code", 1)

        if result.get("success"):
            click.echo(f"✅ Analysis completed successfully. Results written to: {output}")
            if result.get("regressions_detected"):
                click.echo("⚠️  Regressions detected in analysis results")
            elif result.get("message"):
                click.echo(f"ℹ️  {result.get('message')}")

            # Generate HTML report if requested
            if html:
                try:
                    from projects.caliper.engine.kpi.html_generator import (
                        generate_regression_html_from_file,
                    )

                    html_output = output.with_suffix(".html")
                    generate_regression_html_from_file(output, html_output)
                    click.echo(f"📄 Generated HTML regression report: {html_output}")
                except Exception as e:
                    click.echo(f"⚠️  Failed to generate HTML report: {e}", err=True)
        else:
            error_msg = result.get("error", "Unknown error")
            click.echo(f"❌ Analysis failed: {error_msg}", err=True)

        if exit_code != 0:
            sys.exit(exit_code)

    except Exception as e:
        click.echo(f"❌ Analysis failed: {e}", err=True)
        sys.exit(1)
