"""KPI CSV exporter implementation."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from projects.caliper.engine.model import UnifiedRunModel

from .kpi_csv_model import (
    KPICsvSchema,
    create_csv_rows_from_kpi_record,
)


class KPICsvExporter:
    """Export KPI data to CSV format."""

    def __init__(self, include_2d_kpis: bool = True):
        """
        Initialize the CSV exporter.

        Args:
            include_2d_kpis: Whether to include 2D KPIs in export (expanded as multiple rows)
        """
        self.schema = KPICsvSchema()
        self.include_2d_kpis = include_2d_kpis

    def export_kpis_to_csv(
        self,
        kpi_records: list[dict[str, Any]],
        output_path: Path,
        include_header_comments: bool = True,
    ) -> str:
        """
        Export KPI records to CSV file.

        Args:
            kpi_records: List of KPI records from GuideLLMKpiHandler.compute_kpis()
            output_path: Path where to write the CSV file
            include_header_comments: Whether to include descriptive header comments

        Returns:
            Path to the generated CSV file
        """
        # Ensure output directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Convert KPI records to CSV rows (handling 2D KPI expansion)
        csv_rows = []
        skipped_2d_count = 0

        for record in kpi_records:
            try:
                if record.get("is_2d", False) and not self.include_2d_kpis:
                    # Skip 2D KPIs if not including them
                    skipped_2d_count += 1
                    continue

                # Use new function that handles both scalar and 2D KPIs
                rows = create_csv_rows_from_kpi_record(record)
                csv_rows.extend(rows)
            except Exception as e:
                print(f"Warning: Failed to convert KPI record to CSV row: {e}")
                continue

        # Write CSV file
        with open(output_path, "w", newline="", encoding="utf-8") as csvfile:
            # Write header comments if requested
            if include_header_comments:
                for comment_line in self.schema.to_csv_header_with_descriptions():
                    csvfile.write(f"{comment_line}\n")

            # Write CSV data
            if csv_rows:
                # Get field names from the first row (dataclass fields)
                fieldnames = self.schema.get_header_row()
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

                # Write header
                writer.writeheader()

                # Write data rows
                for csv_row in csv_rows:
                    # Convert dataclass to dict and validate
                    row_dict = {field: getattr(csv_row, field, "") for field in fieldnames}
                    validated_row = self.schema.validate_row_data(row_dict)
                    writer.writerow(validated_row)

        # Summary info
        total_written = len(csv_rows)
        print(f"Exported {total_written} KPI records to {output_path}")
        if skipped_2d_count > 0:
            print(f"Skipped {skipped_2d_count} 2D KPIs (include_2d_kpis=False)")

        # Count 2D KPI expansion
        total_2d_rows = sum(1 for row in csv_rows if getattr(row, "is_2d", False))
        if total_2d_rows > 0:
            print(f"Expanded 2D KPIs into {total_2d_rows} individual data point rows")

        return str(output_path)

    def export_from_model(
        self,
        model: UnifiedRunModel,
        output_path: Path,
        include_header_comments: bool = True,
    ) -> str:
        """
        Export KPIs from a unified model to CSV.

        Args:
            model: UnifiedRunModel containing test results
            output_path: Path where to write the CSV file
            include_header_comments: Whether to include descriptive header comments

        Returns:
            Path to the generated CSV file
        """
        from projects.guidellm.postprocess.guidellm.parsing.kpis import GuideLLMKpiHandler

        # Compute KPIs from the model
        kpi_handler = GuideLLMKpiHandler()
        kpi_records = kpi_handler.compute_kpis(model)

        # Export to CSV
        return self.export_kpis_to_csv(kpi_records, output_path, include_header_comments)

    def get_csv_schema_info(self) -> dict[str, Any]:
        """Get information about the CSV schema."""
        return {
            "total_columns": len(self.schema.columns),
            "column_names": self.schema.columns,
            "column_descriptions": self.schema.column_descriptions,
            "schema_version": "1",
            "supports_2d_kpis": self.include_2d_kpis,
        }

    def export_records_from_list(
        self,
        records: list[Any],
        output_path: Path,
        include_header_comments: bool = True,
    ) -> str:
        """
        Export KPIs from a list of unified result records.

        Args:
            records: List of UnifiedResultRecord objects
            output_path: Path where to write the CSV file
            include_header_comments: Whether to include descriptive header comments

        Returns:
            Path to the generated CSV file
        """
        from projects.caliper.engine.model import UnifiedRunModel

        # Create a minimal model for KPI computation
        model = UnifiedRunModel(
            plugin_module="guidellm",
            base_directory="",
            test_nodes=[],
            unified_result_records=records,
            parse_cache_ref=None,
        )

        return self.export_from_model(model, output_path, include_header_comments)


def quick_export_kpis_to_csv(
    records: list[Any],
    output_path: Path | str,
    include_header_comments: bool = True,
) -> str:
    """
    Quick utility function to export KPIs to CSV.

    Args:
        records: List of UnifiedResultRecord objects or KPI records
        output_path: Path where to write the CSV file
        include_header_comments: Whether to include descriptive header comments

    Returns:
        Path to the generated CSV file
    """
    exporter = KPICsvExporter()
    output_path = Path(output_path)

    # Determine if we have KPI records or UnifiedResultRecord objects
    if records and isinstance(records[0], dict) and "kpi_id" in records[0]:
        # These are KPI records
        return exporter.export_kpis_to_csv(records, output_path, include_header_comments)
    else:
        # These are UnifiedResultRecord objects
        return exporter.export_records_from_list(records, output_path, include_header_comments)
