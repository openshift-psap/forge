"""HTML generation for Caliper KPI reports and regression analysis."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import jinja2

from projects.caliper.engine.kpi.format import read_kpis_from_file
from projects.caliper.engine.kpi.report_dataclasses import RegressionReport

logger = logging.getLogger(__name__)


class HTMLGenerator:
    """Generate HTML reports from Caliper KPI data and regression analysis."""

    def __init__(self, template_dir: Path | None = None):
        """Initialize HTML generator with template directory."""
        if template_dir is None:
            template_dir = Path(__file__).parent.parent.parent / "templates"

        self.template_dir = template_dir
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(template_dir),
            autoescape=jinja2.select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

        # Add custom filters
        self.env.filters["tojson"] = json.dumps
        self.env.filters["round"] = round

    def generate_kpi_html(self, kpi_data: dict | list, output_file: Path) -> None:
        """Generate HTML report from KPI data."""
        try:
            template = self.env.get_template("kpi_listing.html")

            # Handle both hierarchical (dict) and flat (list) formats
            if isinstance(kpi_data, dict):
                tests = kpi_data.get("tests", [])
                schema_version = kpi_data.get("schema_version", "2")
            else:
                # Convert flat list to grouped format for template
                tests = self._group_flat_kpis(kpi_data)
                schema_version = "1"

            # Calculate KPI counts and prepare chart data for curve KPIs
            total_kpis = 0
            curve_kpis_count = 0
            scalar_kpis_count = 0
            curve_kpi_ids = set()  # Track unique curve KPI IDs

            for test in tests:
                for kpi in test.get("kpis", []):
                    total_kpis += 1
                    if kpi.get("is_curve", False):
                        curve_kpis_count += 1
                        # Add values count and prepare chart data
                        values = kpi.get("values", [])
                        kpi["values_count"] = len(values) if values else 0
                        # Store clean values list for template iteration
                        kpi["values_list"] = list(values) if values else []
                        # Pre-serialize the values as JSON string for safe chart usage
                        if values:
                            kpi["values_json"] = json.dumps(values)
                            # Add to curve KPI IDs set if it has data
                            curve_kpi_ids.add(kpi.get("kpi_id"))
                        else:
                            kpi["values_json"] = "[]"
                    else:
                        scalar_kpis_count += 1

            html_content = template.render(
                tests=tests,
                schema_version=schema_version,
                total_tests=len(tests),
                total_kpis=total_kpis,
                curve_kpis_count=curve_kpis_count,
                scalar_kpis_count=scalar_kpis_count,
                curve_kpi_ids=sorted(curve_kpi_ids),  # Pass sorted list of curve KPI IDs
            )

            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_text(html_content, encoding="utf-8")
            logger.info(f"Generated KPI HTML report: {output_file}")

        except Exception as e:
            logger.error(f"Failed to generate KPI HTML report: {e}")
            raise

    def generate_regression_html(
        self, report_data: dict | RegressionReport, output_file: Path
    ) -> None:
        """Generate HTML report from regression analysis data."""
        try:
            template = self.env.get_template("regression_report.html")

            # Handle both dict and RegressionReport dataclass
            if isinstance(report_data, RegressionReport):
                data = report_data.to_dict()
            else:
                data = report_data

            # Filter regression results with relative change for visualization
            results = data.get("results", [])
            regression_data = [
                result
                for result in results
                if result.get("verdict") == "REGRESSION"
                and result.get("details", {}).get("relative_change") is not None
            ]

            html_content = template.render(
                report=data,
                overall=data.get("overall", {}),
                tested=data.get("tested", {}),
                results=results,
                analysis=data.get("analysis", {}),
                config=data.get("config", {}),
                input_data=data.get("input_data", {}),
                regression_data=regression_data,
            )

            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_text(html_content, encoding="utf-8")
            logger.info(f"Generated regression HTML report: {output_file}")

        except Exception as e:
            logger.error(f"Failed to generate regression HTML report: {e}")
            raise

    def convert_json_to_html(self, json_file: Path, output_file: Path | None = None) -> Path:
        """Convert a JSON file to HTML format."""
        if output_file is None:
            output_file = json_file.with_suffix(".html")

        try:
            # Try to determine file type and generate appropriate HTML
            with open(json_file, encoding="utf-8") as f:
                data = json.load(f)

            if self._is_regression_report(data):
                self.generate_regression_html(data, output_file)
            else:
                # Assume it's KPI data
                self.generate_kpi_html(data, output_file)

            return output_file

        except Exception as e:
            logger.error(f"Failed to convert {json_file} to HTML: {e}")
            raise

    def _is_regression_report(self, data: dict) -> bool:
        """Check if JSON data is a regression report."""
        # Check for regression report structure
        required_fields = ["analysis", "overall", "tested", "results"]
        return all(field in data for field in required_fields)

    def _group_flat_kpis(self, flat_kpis: list[dict]) -> list[dict]:
        """Group flat KPI list by run_id for template rendering."""
        grouped = {}

        for kpi in flat_kpis:
            run_id = kpi.get("run_id", "unknown")
            if run_id not in grouped:
                grouped[run_id] = {
                    "run_id": run_id,
                    "labels": kpi.get("labels", {}),
                    "metadata": {"timestamp": kpi.get("timestamp", "")},
                    "kpis": [],
                }

            # Add KPI to test group
            kpi_data = {
                "kpi_id": kpi.get("kpi_id"),
                "name": kpi.get("name", kpi.get("kpi_id")),
                "value": kpi.get("value"),
                "values": kpi.get("values"),
                "unit": kpi.get("unit", ""),
                "higher_is_better": kpi.get("higher_is_better", True),
                "is_curve": kpi.get("is_curve", False),
                "help": kpi.get("help", ""),
            }

            # Add values count and JSON data for curve KPIs
            if kpi_data["is_curve"]:
                values = kpi_data.get("values", [])
                kpi_data["values_count"] = len(values) if values else 0
                # Store clean values list for template iteration
                kpi_data["values_list"] = list(values) if values else []
                # Pre-serialize the values as JSON string for safe chart usage
                if values:
                    kpi_data["values_json"] = json.dumps(values)
                else:
                    kpi_data["values_json"] = "[]"

            grouped[run_id]["kpis"].append(kpi_data)

        return list(grouped.values())


def generate_kpi_html_from_file(input_file: Path, output_file: Path | None = None) -> Path:
    """Generate HTML report from KPI file."""
    generator = HTMLGenerator()

    if output_file is None:
        output_file = input_file.with_suffix(".html")

    # Read KPI data
    if input_file.suffix.lower() == ".json":
        with open(input_file, encoding="utf-8") as f:
            data = json.load(f)
    else:
        # Assume JSONL format
        data = read_kpis_from_file(input_file)

    generator.generate_kpi_html(data, output_file)
    return output_file


def generate_regression_html_from_file(input_file: Path, output_file: Path | None = None) -> Path:
    """Generate HTML report from regression analysis file."""
    generator = HTMLGenerator()

    if output_file is None:
        output_file = input_file.with_suffix(".html")

    with open(input_file, encoding="utf-8") as f:
        data = json.load(f)

    generator.generate_regression_html(data, output_file)
    return output_file
