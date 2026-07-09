"""KPI CSV export model and schema definition."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class KPICsvRow:
    """Model representing a single row in the KPI CSV export."""

    # KPI Identity
    kpi_id: str
    kpi_name: str
    kpi_unit: str
    kpi_help: str
    higher_is_better: bool

    # KPI Value
    value: float | str

    # Test Identification & System Info
    run_id: str
    timestamp: str
    schema_version: str = "1"

    # Test Conditions (Labels) - These will become individual columns
    # Test identification
    run: str = ""

    # Model configuration
    model: str = ""
    version: str = ""
    image_tag: str = ""
    guidellm_version: str = ""

    # Hardware setup
    accelerator: str = ""

    # Workload configuration
    prompt_toks: str = ""
    output_toks: str = ""
    intended_concurrency: str = ""

    # Parallelism settings
    TP: str = ""  # Tensor Parallelism
    DP: str = ""  # Data Parallelism
    EP: str = ""  # Expert Parallelism
    replicas: str = ""

    # Infrastructure setup
    prefill_pod_count: str = ""
    decode_pod_count: str = ""
    router_config: str = ""

    # Runtime configuration
    runtime_args: str = ""

    # Derived performance tier
    performance_tier: str = ""

    # Metadata
    uuid: str = ""
    notes: str = ""

    # Source information
    test_base_path: str = ""
    plugin_module: str = ""

    # 2D KPI specific fields (for curve/dimensional data)
    is_2d: bool = False
    point_index: int | str = ""  # Index of this point in 2D series
    x_value: float | str = ""  # X coordinate value
    y_value: float | str = ""  # Y coordinate value (same as value for 2D KPIs)
    x_unit: str = ""  # Unit for X axis
    y_unit: str = ""  # Unit for Y axis


@dataclass
class KPICsvSchema:
    """Schema definition for KPI CSV exports."""

    # Define column order and metadata
    columns: list[str] = field(
        default_factory=lambda: [
            # Core KPI information (always first)
            "kpi_id",
            "kpi_name",
            "kpi_unit",
            "kpi_help",
            "higher_is_better",
            "value",
            # System information
            "run_id",
            "timestamp",
            "schema_version",
            # Test conditions (labels) - grouped logically
            "run",
            "model",
            "version",
            "image_tag",
            "guidellm_version",
            "accelerator",
            "prompt_toks",
            "output_toks",
            "intended_concurrency",
            "TP",
            "DP",
            "EP",
            "replicas",
            "prefill_pod_count",
            "decode_pod_count",
            "router_config",
            "runtime_args",
            "performance_tier",
            # Metadata
            "uuid",
            "notes",
            # Source tracking
            "test_base_path",
            "plugin_module",
            # 2D KPI fields (for curve/dimensional data)
            "is_2d",
            "point_index",
            "x_value",
            "y_value",
            "x_unit",
            "y_unit",
        ]
    )

    # Column descriptions for documentation
    column_descriptions: dict[str, str] = field(
        default_factory=lambda: {
            # Core KPI fields
            "kpi_id": "Unique identifier for the KPI",
            "kpi_name": "Human-readable name of the KPI",
            "kpi_unit": "Unit of measurement (e.g., req/s, tokens, ms)",
            "kpi_help": "Description of what this KPI measures",
            "higher_is_better": "Whether higher values indicate better performance",
            "value": "The measured KPI value",
            # System fields
            "run_id": "Test run identifier (usually test directory path)",
            "timestamp": "When the KPI was computed (ISO format)",
            "schema_version": "CSV schema version for compatibility",
            # Test condition labels
            "run": "Test run name/identifier",
            "model": "Model name (e.g., llama-2-7b)",
            "version": "Model version",
            "image_tag": "Container image tag used",
            "guidellm_version": "GuideLLM version used for testing",
            "accelerator": "Hardware accelerator type (e.g., A100-80GB)",
            "prompt_toks": "Number of prompt tokens in test",
            "output_toks": "Number of output tokens in test",
            "intended_concurrency": "Target concurrent connections",
            "TP": "Tensor parallelism setting",
            "DP": "Data parallelism setting",
            "EP": "Expert parallelism setting",
            "replicas": "Number of model replicas",
            "prefill_pod_count": "Number of prefill pods",
            "decode_pod_count": "Number of decode pods",
            "router_config": "Router configuration used",
            "runtime_args": "Runtime arguments passed to the model",
            "performance_tier": "Derived performance classification (high/medium/low)",
            # Metadata
            "uuid": "Unique test identifier for tracking",
            "notes": "Human-added notes about the test",
            # Source tracking
            "test_base_path": "File path where test artifacts are stored",
            "plugin_module": "Caliper plugin that processed this test",
            # 2D KPI fields
            "is_2d": "Whether this is a 2D KPI data point (true/false)",
            "point_index": "Index of this point in 2D series (0, 1, 2, ...)",
            "x_value": "X coordinate value for 2D KPIs",
            "y_value": "Y coordinate value for 2D KPIs (same as value)",
            "x_unit": "Unit of measurement for X axis",
            "y_unit": "Unit of measurement for Y axis",
        }
    )

    # Data types for validation
    column_types: dict[str, type] = field(
        default_factory=lambda: {
            "kpi_id": str,
            "kpi_name": str,
            "kpi_unit": str,
            "kpi_help": str,
            "higher_is_better": bool,
            "value": float,  # Primary type, but can also be str for non-numeric
            "run_id": str,
            "timestamp": str,
            "schema_version": str,
            # All other fields are strings (labels, metadata, etc.)
            # 2D KPI field types
            "is_2d": bool,
            "point_index": int,
            "x_value": float,
            "y_value": float,
            "x_unit": str,
            "y_unit": str,
        }
    )

    def get_header_row(self) -> list[str]:
        """Get the CSV header row."""
        return self.columns.copy()

    def validate_row_data(self, row_dict: dict[str, Any]) -> dict[str, Any]:
        """Validate and clean row data according to schema."""
        cleaned = {}

        for col in self.columns:
            value = row_dict.get(col, "")

            # Handle special type conversions
            if col in ["higher_is_better", "is_2d"] and isinstance(value, bool):
                cleaned[col] = str(value).lower()  # Convert bool to string
            elif col in ["value", "x_value", "y_value"]:
                # Keep numeric values as-is, convert others to string
                if isinstance(value, int | float):
                    cleaned[col] = value
                else:
                    cleaned[col] = str(value)
            elif col == "point_index":
                # Convert to int if possible, otherwise string
                if isinstance(value, int):
                    cleaned[col] = value
                elif isinstance(value, str) and value.isdigit():
                    cleaned[col] = int(value)
                else:
                    cleaned[col] = str(value)
            else:
                # Everything else becomes a string
                cleaned[col] = str(value) if value is not None else ""

        return cleaned

    def to_csv_header_with_descriptions(self) -> list[str]:
        """Generate a commented header with column descriptions."""
        header_lines = []
        header_lines.append("# GuideLLM KPI Export")
        header_lines.append(
            f"# Schema Version: {self.columns[self.columns.index('schema_version')]}"
        )
        header_lines.append("# Column Descriptions:")

        for col in self.columns:
            desc = self.column_descriptions.get(col, "No description available")
            header_lines.append(f"#   {col}: {desc}")

        header_lines.append("#")
        return header_lines


def create_csv_rows_from_kpi_record(kpi_record: dict[str, Any]) -> list[KPICsvRow]:
    """
    Convert a KPI record (from compute_kpis) to CSV row model(s).

    For scalar KPIs, returns a single row.
    For 2D KPIs, returns multiple rows (one per data point).

    Args:
        kpi_record: KPI record dictionary from GuideLLMKpiHandler.compute_kpis()

    Returns:
        List of KPICsvRow instances
    """
    is_2d = kpi_record.get("is_2d", False)

    if not is_2d:
        # Scalar KPI - single row
        return [create_csv_row_from_kpi_record(kpi_record)]

    # 2D KPI - expand into multiple rows
    value = kpi_record.get("value", [])
    if not isinstance(value, list) or not value:
        # Invalid 2D data - treat as scalar with empty value
        return [create_csv_row_from_kpi_record(kpi_record)]

    rows = []
    x_unit = kpi_record.get("x_unit", "")
    y_unit = kpi_record.get("y_unit", kpi_record.get("unit", ""))

    for idx, point in enumerate(value):
        if not isinstance(point, list | tuple) or len(point) != 2:
            continue  # Skip invalid data points

        x_val, y_val = point

        # Create a modified record for this specific point
        point_record = kpi_record.copy()
        point_record["value"] = y_val  # Y value becomes the main value
        point_record["is_2d"] = True
        point_record["point_index"] = idx
        point_record["x_value"] = x_val
        point_record["y_value"] = y_val
        point_record["x_unit"] = x_unit
        point_record["y_unit"] = y_unit

        rows.append(create_csv_row_from_kpi_record(point_record))

    return rows


def create_csv_row_from_kpi_record(kpi_record: dict[str, Any]) -> KPICsvRow:
    """
    Convert a KPI record (from compute_kpis) to a CSV row model.

    Args:
        kpi_record: KPI record dictionary from GuideLLMKpiHandler.compute_kpis()

    Returns:
        KPICsvRow instance
    """
    # Extract labels and metadata
    labels = kpi_record.get("labels", {})
    metadata = kpi_record.get("metadata", {})
    source = kpi_record.get("source", {})

    # Generate KPI name from ID if not provided
    kpi_name = kpi_record.get("name", "")
    if not kpi_name and kpi_record.get("kpi_id"):
        # Convert kpi_id to readable name: "guidellm_measured_rps" -> "Measured Rps"
        kpi_name = kpi_record["kpi_id"].replace("guidellm_", "").replace("_", " ").title()

    return KPICsvRow(
        # Core KPI info
        kpi_id=kpi_record.get("kpi_id", ""),
        kpi_name=kpi_name,
        kpi_unit=kpi_record.get("unit", ""),
        kpi_help=kpi_record.get("help", ""),
        higher_is_better=labels.get("higher_is_better", False),
        value=kpi_record.get("value"),
        # 2D KPI info
        is_2d=kpi_record.get("is_2d", False),
        point_index=kpi_record.get("point_index", ""),
        x_value=kpi_record.get("x_value", ""),
        y_value=kpi_record.get("y_value", ""),
        x_unit=kpi_record.get("x_unit", ""),
        y_unit=kpi_record.get("y_unit", ""),
        # System info
        run_id=kpi_record.get("run_id", ""),
        timestamp=kpi_record.get("timestamp", ""),
        schema_version=kpi_record.get("schema_version", "1"),
        # Test conditions from labels
        run=labels.get("run", ""),
        model=labels.get("model", ""),
        version=labels.get("version", ""),
        image_tag=labels.get("image_tag", ""),
        guidellm_version=labels.get("guidellm_version", ""),
        accelerator=labels.get("accelerator", ""),
        prompt_toks=labels.get("prompt_toks", ""),
        output_toks=labels.get("output_toks", ""),
        intended_concurrency=labels.get("intended_concurrency", ""),
        TP=labels.get("TP", ""),
        DP=labels.get("DP", ""),
        EP=labels.get("EP", ""),
        replicas=labels.get("replicas", ""),
        prefill_pod_count=labels.get("prefill_pod_count", ""),
        decode_pod_count=labels.get("decode_pod_count", ""),
        router_config=labels.get("router_config", ""),
        runtime_args=labels.get("runtime_args", ""),
        performance_tier=labels.get("performance_tier", ""),
        # Metadata
        uuid=metadata.get("uuid", ""),
        notes=metadata.get("notes", ""),
        # Source info
        test_base_path=source.get("test_base_path", ""),
        plugin_module=source.get("plugin_module", ""),
    )
