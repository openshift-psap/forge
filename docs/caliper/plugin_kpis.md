# Plugin KPI Development Guide

This document explains how to build and expose KPIs (Key Performance Indicators) in Caliper plugins.

## Overview

Caliper plugins define KPIs as decorated Python functions that extract performance metrics from test results. The KPI system supports both scalar metrics and curve performance data, with rich metadata for visualization and analysis.

## KPI Function Structure

### Basic KPI Function

```python
from projects.caliper.engine.kpi import KPIMetadata, HigherBetter


@HigherBetter()
@KPIMetadata(help="Request throughput in requests per second", unit="req/s")
def request_rate(unified_record) -> float:
    """Request Rate KPI."""
    value = unified_record.metrics.get("request_rate")
    if value is None:
        raise ValueError("request_rate metric not found")
    return float(value)
```

### Function Requirements

1. **Function name**: Becomes the KPI ID in output
2. **Single parameter**: `unified_record` - the parsed test result
3. **Return type**: `float` for scalar KPIs, `list[tuple[float, float]]` for Curve KPIs
4. **Docstring**: First line becomes the display name (without " KPI.")
5. **Decorators**: Required for metadata and behavior

## KPI Decorators

### Required Decorators

**`@KPIMetadata(help, unit)`**
- `help`: Human-readable description of the metric
- `unit`: Unit of measurement (e.g., "req/s", "ms", "tokens")

**Comparison Direction**
- `@HigherBetter()`: Higher values indicate better performance
- `@LowerBetter()`: Lower values indicate better performance

### Optional Decorators

**`@Format(format_str)`**
- Specify number formatting (e.g., `"{:.1f}"`, `"{:.2%}"`)

**`@Curve(x_unit, x_help, y_unit=None, y_help=None)`**
- Marks KPI as returning curve data (performance curves)
- `x_unit`/`x_help`: X-axis unit and description
- `y_unit`/`y_help`: Y-axis unit and description (defaults to main unit/help)

## Curve KPIs (Performance Curves)

Curve KPIs return lists of (x, y) coordinate pairs representing performance curves:

```python
@HigherBetter()
@Curve(x_unit="req/s", x_help="Request rate", y_unit="tokens/s", y_help="Achieved throughput")
@KPIMetadata(help="Throughput achieved at different request rates", unit="tokens/s")
def throughput_curve(unified_record) -> list[tuple[float, float]]:
    """Throughput vs Request Rate Curve KPI."""
    request_rates = unified_record.metrics.get("request_rate", [])
    throughputs = unified_record.metrics.get("throughput", [])

    if len(request_rates) != len(throughputs):
        raise ValueError("Request rates and throughputs arrays must have same length")

    return [(float(x), float(y)) for x, y in zip(request_rates, throughputs)]
```

### Curve KPI Output Format

Curve KPIs generate structured JSON output:

```json
{
  "id": "throughput_curve",
  "value": {
    "data_points": [
      {"x": 1.0, "y": 150.2},
      {"x": 2.0, "y": 298.5}
    ],
    "count": 2
  },
  "higher_is_better": true,
  "is_curve": true,
  "name": "Throughput vs Request Rate Curve",
  "help": "Throughput achieved at different request rates",
  "x_unit": "req/s",
  "x_help": "Request rate",
  "y_unit": "tokens/s",
  "y_help": "Achieved throughput"
}
```

## Plugin Integration

### KPI Function Discovery

KPI functions are discovered through two mechanisms:

1. **During KPI Computation**: The KPI handler uses `get_kpi_functions(inspect.getmodule(KpiHandler))` to find functions in the handler's module
2. **During Format Transformation**: The hierarchical format transformer attempts to import the plugin module for metadata

```python
# In KPI handler module (e.g., projects/myplugin/postprocess/myplugin/parsing/kpis.py)
from projects.caliper.engine.kpi import KPIMetadata, HigherBetter


@HigherBetter()
@KPIMetadata(help="My metric", unit="units")
def my_kpi_function(unified_record) -> float:
    """My KPI."""
    return 42.0
```

### Plugin Module Structure

**For KPI Computation**: Functions are discovered in the KPI handler's module using `inspect.getmodule()`.

**For Metadata Extraction**: The hierarchical format transformer attempts to import the main plugin module to extract decorator metadata. If this fails (e.g., due to heavy dependencies), the transformer preserves metadata from the original v1 KPI records, ensuring curve KPI information (`x_unit`, `y_unit`, `x_help`, `y_help`) is not lost during format conversion.

### Integration with PostProcessingPlugin

Your plugin class should implement the `compute_kpis` method to generate KPI records:

```python
class MyPlugin(PostProcessingPlugin):
    def compute_kpis(self, model: UnifiedRunModel) -> list[dict[str, Any]]:
        """Compute KPI values from the unified model."""
        return self.kpi_handler.compute_kpis(model)
```

## KPI Handler Pattern

### Standard KPI Handler Implementation

```python
import inspect
from projects.caliper.engine.kpi.decorators import get_kpi_functions


class MyKpiHandler:
    def compute_kpis(self, model: UnifiedRunModel) -> list[dict[str, Any]]:
        """Generate KPI records from unified model."""
        kpi_functions = get_kpi_functions(inspect.getmodule(MyKpiHandler))
        records = []

        for record in model.unified_result_records:
            for kpi_name, kpi_func in kpi_functions.items():
                try:
                    value = kpi_func(record)

                    # Base KPI record
                    kpi_record = {
                        "schema_version": "1",
                        "kpi_id": kpi_name,
                        "value": value,
                        "unit": kpi_func._kpi_unit,
                        "run_id": record.test_base_path,
                        "timestamp": "2024-01-01T00:00:00Z",
                        "labels": {
                            "higher_is_better": kpi_func._kpi_higher_is_better,
                            # Add other labels from record
                        },
                        "source": {"test_base_path": record.test_base_path},
                    }

                    # Add curve-specific metadata if this is a curve KPI
                    if getattr(kpi_func, "_kpi_is_curve", False):
                        kpi_record.update(
                            {
                                "is_curve": True,
                                "x_unit": kpi_func._kpi_x_unit,
                                "x_help": kpi_func._kpi_x_help,
                                "y_unit": kpi_func._kpi_y_unit
                                "y_help": kpi_func._kpi_y_help,
                            }
                        )

                    records.append(kpi_record)
                except ValueError as e:
                    # Handle missing metrics gracefully - KPI functions raise ValueError for absent data
                    logger.debug(f"Skipping KPI {kpi_name} for {record.test_base_path}: {e}")
                    continue
                except Exception as e:
                    # Log and re-raise programming errors and conversion failures to make KPI failures visible
                    logger.warning(f"KPI {kpi_name} failed for {record.test_base_path}: {e}")
                    raise

        return records
```

## Best Practices

### 1. Error Handling

```python
@HigherBetter()
@KPIMetadata(help="Robust metric", unit="units")
def robust_kpi(unified_record) -> float:
    """Robust KPI."""
    value = unified_record.metrics.get("my_metric")
    if value is None:
        raise ValueError("my_metric not found in record")

    # Validate data type
    if not isinstance(value, (int, float)):
        raise ValueError(f"Expected numeric value, got {type(value)}")

    return float(value)
```

### 2. Label Extraction

```python
def extract_test_labels(record) -> dict[str, Any]:
    """Extract labels for KPI records."""
    return {
        "platform": record.distinguishing_labels.get("platform", "unknown"),
        "model": record.metrics.get("model_name", "unknown"),
        "version": record.metrics.get("product_version", "unknown"),
    }
```

### 3. Curve Data Validation

```python
@Curve(x_unit="x", x_help="X values", y_unit="y", y_help="Y values")
@KPIMetadata(help="Performance curve", unit="y")
def performance_curve(unified_record) -> list[tuple[float, float]]:
    """Performance Curve KPI."""
    x_values = unified_record.metrics.get("x_data", [])
    y_values = unified_record.metrics.get("y_data", [])

    if len(x_values) != len(y_values):
        raise ValueError("X and Y data arrays must have same length")

    if not x_values:
        return []  # Return empty list for missing data

    return [(float(x), float(y)) for x, y in zip(x_values, y_values)]
```

### 4. Conditional KPIs

```python
@HigherBetter()
@KPIMetadata(help="Optional metric", unit="units")
def optional_kpi(unified_record) -> float:
    """Optional KPI."""
    # Only compute for certain test types
    if not unified_record.metrics.get("enable_optional_metrics", False):
        raise ValueError("Optional metrics disabled for this test")

    return unified_record.metrics.get("optional_value", 0.0)
```

### 5. Curve Metadata Preservation

Always include curve metadata in your KPI handler implementation to ensure proper display:

```python
# In your KPI handler's compute_kpis method
if getattr(kpi_func, "_kpi_is_curve", False):
    kpi_record.update(
        {
            "is_curve": True,
            "x_unit": kpi_func._kpi_x_unit,
            "x_help": kpi_func._kpi_x_help,
            "y_unit": kpi_func._kpi_y_unit,
            "y_help": kpi_func._kpi_y_help,
        }
    )
```

This metadata is preserved during v1→v2 format transformation even if the plugin module cannot be imported due to dependencies.

## Output Formats

### Hierarchical JSON (Schema v2)

The default output format groups KPIs by test with metadata:

```json
{
  "schema_version": "2",
  "tests": [
    {
      "run_id": "test_001",
      "labels": {"platform": "gpu", "model": "llama"},
      "metadata": {"timestamp": "2024-01-01T00:00:00Z"},
      "kpis": [
        {
          "id": "throughput",
          "value": 150.5,
          "higher_is_better": true,
          "is_curve": false,
          "unit": "tokens/s",
          "name": "Throughput",
          "help": "Token generation rate"
        }
      ]
    }
  ]
}
```

### JSONL Format (Schema v1)

Legacy flat format with one KPI per line:

```json
{"schema_version": "1", "kpi_id": "throughput", "value": 150.5, "unit": "tokens/s", "run_id": "test_001", "timestamp": "2024-01-15T10:30:00Z", "labels": {"version": "v1.0", "platform": "gpu"}, "source": {"test_base_path": "/path/to/test", "plugin_module": "example_plugin"}}
```

## Testing KPIs

### Unit Testing KPI Functions

```python
import pytest
from unittest.mock import Mock


def test_request_rate_kpi():
    # Create mock record
    mock_record = Mock()
    mock_record.metrics = {"request_rate": 150.5}

    # Test KPI function
    result = request_rate(mock_record)

    assert result == 150.5
    assert isinstance(result, float)


def test_request_rate_kpi_missing_data():
    mock_record = Mock()
    mock_record.metrics = {}

    with pytest.raises(ValueError, match="request_rate metric not found"):
        request_rate(mock_record)
```

### Integration Testing

```python
def test_kpi_generation(sample_unified_model):
    handler = MyKpiHandler()
    kpis = handler.compute_kpis(sample_unified_model)

    assert len(kpis) > 0
    assert all("kpi_id" in kpi for kpi in kpis)
    assert all("value" in kpi for kpi in kpis)
```

## Common Patterns

### Aggregation KPIs

```python
@HigherBetter()
@KPIMetadata(help="Average performance across tests", unit="req/s")
def average_performance(unified_record) -> float:
    """Average Performance KPI."""
    values = unified_record.metrics.get("performance_samples", [])
    if not values:
        raise ValueError("No performance samples found")

    return sum(values) / len(values)
```

### Derived Metrics

```python
@LowerBetter()
@KPIMetadata(help="Efficiency ratio", unit="ratio")
def efficiency_ratio(unified_record) -> float:
    """Efficiency Ratio KPI."""
    throughput = unified_record.metrics.get("throughput")
    latency = unified_record.metrics.get("latency")

    if throughput is None or latency is None:
        raise ValueError("Both throughput and latency required")

    if latency == 0:
        raise ValueError("Latency cannot be zero")

    return throughput / latency
```

This guide provides the foundation for implementing robust, well-documented KPIs in Caliper plugins.
