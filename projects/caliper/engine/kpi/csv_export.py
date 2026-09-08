"""Export KPIs to CSV format using plugin interface."""

from __future__ import annotations

from pathlib import Path


def export_dashboard_csv(
    *,
    plugin: object,
    model: object,
    output_path: Path,
) -> tuple[str, int]:
    """
    Export dashboard CSV using the plugin's new export_dashboard_csv method.

    Args:
        plugin: PostProcessingPlugin instance with export_dashboard_csv method
        model: UnifiedRunModel for generating dashboard KPIs independently
        output_path: Path where to write the CSV file

    Returns:
        Tuple of (path to generated CSV file as string, number of KPI rows written)

    Raises:
        AttributeError: If plugin doesn't have export_dashboard_csv method
        NotImplementedError: If plugin doesn't support dashboard CSV export
    """
    if not hasattr(plugin, "export_dashboard_csv"):
        raise AttributeError(
            f"Plugin {plugin.__class__.__name__} does not implement export_dashboard_csv method"
        )

    try:
        # Delegate to plugin-specific dashboard CSV export implementation
        result_path = plugin.export_dashboard_csv(
            model=model,
            output_path=output_path,
        )

        # Count actual KPI rows written (excluding header and comments)
        kpi_count = _count_csv_data_rows(output_path)

        return result_path, kpi_count

    except NotImplementedError:
        # Plugin explicitly doesn't support dashboard CSV export
        raise NotImplementedError(
            f"Plugin {plugin.__class__.__name__} does not support dashboard CSV export. "
            f"This plugin cannot generate dashboard KPIs."
        ) from None


def _count_csv_data_rows(csv_path: Path) -> int:
    """Count actual data rows in CSV file (excluding header and comment lines)."""
    if not csv_path.exists():
        return 0

    import csv

    try:
        with open(csv_path, encoding="utf-8") as csvfile:
            # Skip comment lines that start with #
            lines = [line for line in csvfile if not line.strip().startswith("#")]

        if not lines:
            return 0

        # Parse CSV and count data rows (excluding header)
        csv_content = "".join(lines)
        rows = list(csv.reader(csv_content.splitlines()))

        # Return count of data rows (total rows - 1 header row)
        return max(0, len(rows) - 1)

    except Exception:
        # If we can't parse the CSV, assume no data rows
        return 0
