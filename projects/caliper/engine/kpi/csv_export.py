"""Export KPIs to CSV format using plugin interface."""

from __future__ import annotations

from pathlib import Path


def export_dashboard_csv(
    *,
    plugin: object,
    model: object,
    output_path: Path,
    include_header_comments: bool = True,
) -> str:
    """
    Export dashboard CSV using the plugin's new export_dashboard_csv method.

    Args:
        plugin: PostProcessingPlugin instance with export_dashboard_csv method
        model: UnifiedRunModel for generating dashboard KPIs independently
        output_path: Path where to write the CSV file
        include_header_comments: Whether to include descriptive header comments

    Returns:
        Path to the generated CSV file as string

    Raises:
        AttributeError: If plugin doesn't have export_dashboard_csv method
    """
    if not hasattr(plugin, "export_dashboard_csv"):
        raise AttributeError(
            f"Plugin {plugin.__class__.__name__} does not implement export_dashboard_csv method"
        )

    # Delegate to plugin-specific dashboard CSV export implementation
    result_path = plugin.export_dashboard_csv(
        model=model,
        output_path=output_path,
    )

    return result_path
