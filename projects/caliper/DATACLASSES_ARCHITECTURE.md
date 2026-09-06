# Caliper Core Dataclasses Architecture

This document outlines the new dataclass-based architecture for KPI handling across the Caliper ecosystem. The core engine now provides standardized dataclasses that all plugins must use for type safety and consistency.

## Design Principles

1. **Single Source of Truth**: Caliper core defines ALL dataclasses
2. **No Plugin Inheritance**: Plugins use core dataclasses directly, no custom inheritance
3. **Strict Typing**: `labels: dict[str, Any]`, `value: int | float` only
4. **Backward Compatibility**: Gradual migration path from dict-based APIs

## Core Dataclasses

### Available Imports

```python
from projects.caliper.engine.kpi import (
    # Core dataclasses - plugins should use these
    KpiRecord,  # Standard KPI record structure
    KpiCatalogEntry,  # KPI catalog metadata
    RegressionFinding,  # Individual regression result
    RegressionReport,  # Full regression analysis report
    # Convenience aliases
    KPI,  # Alias for KpiRecord
    Catalog,  # Alias for KpiCatalogEntry
    Report,  # Alias for RegressionReport
    # KPI function decorators and utilities
    Format,
    HigherBetter,
    KPIMetadata,
    LowerBetter,
    TwoDimensional,
    build_catalog_from_functions,
    create_label_extractor,
    get_kpi_functions,
    is_curve_kpi,
)
```

### 1. KpiRecord

Standard KPI record structure used by ALL plugins:

```python
@dataclass
class KpiRecord:
    # Core identification
    schema_version: str
    kpi_id: str

    # Value and measurement
    value: int | float  # ONLY numeric types allowed
    unit: str

    # Context and tracking
    run_id: str
    timestamp: str
    labels: dict[str, Any]  # Keys MUST be strings

    # Metadata and source tracking
    metadata: dict[str, Any] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)

    # Optional 2D KPI fields
    x_unit: str | None = None
    x_help: str | None = None
    y_unit: str | None = None
    y_help: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)
```

### 2. KpiCatalogEntry

KPI catalog metadata for ALL plugins:

```python
@dataclass
class KpiCatalogEntry:
    # Core identification
    kpi_id: str
    name: str

    # Measurement characteristics
    unit: str
    higher_is_better: bool
    is_curve: bool

    # Documentation and help
    help: str = ""
    description: str = ""

    # Optional 2D metadata
    x_unit: str = ""
    x_help: str = ""
    y_unit: str = ""
    y_help: str = ""

    # Categorization
    category: str = ""
    tags: list[str] = field(default_factory=list)
```

### 3. RegressionReport

Comprehensive regression analysis results:

```python
@dataclass
class RegressionReport:
    # Required fields
    status: str  # "success", "no_regression", "regression_detected", "error", "no_data"
    total_kpis: int
    regression_count: int
    analysis_timestamp: str
    
    # Optional fields
    improvement_count: int = 0
    baseline_version: str | None = None
    current_version: str | None = None
    findings: list[RegressionFinding] = field(default_factory=list)
    threshold_percent: float = 10.0
    comparison_labels: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
```

## Plugin Implementation Patterns

### 1. Basic Plugin Structure

```python
from projects.caliper.engine.kpi import (
    KpiRecord,
    KpiCatalogEntry,
    RegressionReport,
    Format,
    HigherBetter,
    KPIMetadata,
    LowerBetter,
    build_catalog_from_functions,
    create_label_extractor,
    get_kpi_functions,
)


# Define KPI functions with decorators (existing pattern)
@HigherBetter()
@Format("{:.2f}")
@KPIMetadata(help="Requests per second", unit="req/s")
def kpi_plugin_throughput_rps(unified_record) -> float:
    return float(unified_record.metrics.get("throughput", 0))


class PluginKpiHandler:
    def compute_kpis(self, model: UnifiedRunModel) -> list[dict[str, Any]]:
        """Compute KPIs using core dataclasses."""
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = []

        kpi_functions = get_kpi_functions(inspect.getmodule(self))

        for record in model.unified_result_records:
            for kpi_id, kpi_func in kpi_functions.items():
                try:
                    value = kpi_func(record)
                except (TypeError, ValueError, KeyError):
                    continue

                # Create KPI record using CORE dataclass
                kpi_record = KpiRecord(
                    schema_version="1",
                    kpi_id=kpi_id,
                    value=value,  # Must be int|float only
                    unit=kpi_func._kpi_unit,
                    run_id=record.test_base_path,
                    timestamp=ts,
                    labels={
                        **record.distinguishing_labels,
                        "higher_is_better": kpi_func._kpi_higher_is_better,
                    },
                    metadata={"test_config": record.run_identity},
                    source={"plugin_module": model.plugin_module},
                )

                out.append(kpi_record.to_dict())

        return out

    def get_catalog(self) -> list[dict[str, Any]]:
        """Generate KPI catalog using core dataclasses."""
        raw_catalog = build_catalog_from_functions(inspect.getmodule(self))

        catalog_entries = []
        for entry in raw_catalog:
            # Create catalog entry using CORE dataclass
            catalog_entry = KpiCatalogEntry(
                kpi_id=entry["kpi_id"],
                name=entry["name"],
                unit=entry["unit"],
                higher_is_better=entry["higher_is_better"],
                is_curve=entry["is_curve"],
                help=entry.get("help", ""),
                description=entry.get("description", ""),
            )
            catalog_entries.append(catalog_entry.to_dict())

        return catalog_entries
```

### 2. Plugin Base Class Updates

Update the core plugin interface to support dataclass-based methods:

```python
# projects/caliper/engine/model.py
class PostProcessingPlugin(ABC):
    def kpi_catalog(self) -> list[dict[str, Any]]:
        """Return KPI catalog using core dataclasses.

        Plugins should use KpiCatalogEntry and return .to_dict() results.
        """
        return []

    def create_regression_report(
        self,
        baseline_kpis: list[dict[str, Any]],
        current_kpis: list[dict[str, Any]],
        threshold: float = 0.1,
    ) -> dict[str, Any]:
        """Optional: Create regression analysis using core dataclasses.

        Plugins should use RegressionReport and return .to_dict() result.
        """
        return {}
```

## Migration Guide

### For Existing Plugins

1. **Update imports** to include core dataclasses:
   ```python
   from projects.caliper.engine.kpi import KpiRecord, KpiCatalogEntry, RegressionReport
   ```

2. **Replace custom dataclasses** with core ones:
   ```python
   # OLD - Plugin-specific dataclass
   kpi_record = MyPluginKpiRecord(...)
   
   # NEW - Core dataclass
   kpi_record = KpiRecord(...)
   ```

3. **Update value types** to enforce `int | float`:
   ```python
   # OLD - Allowed lists/complex types
   value: float | list[Any]
   
   # NEW - Only numeric types
   value: int | float
   ```

4. **Update labels type** to enforce string keys:
   ```python
   # NEW - Enforced by core dataclass
   labels: dict[str, Any]  # Keys must be strings
   ```

### Plugin Priority Order

1. **Test/Stub Plugin** ✅ (Simple, good for validation)
2. **Skeleton Plugin** ✅ **DONE** (Reference implementation)
3. **GuideeLM Plugin** (Medium complexity)
4. **LLM-D Plugin** (Most complex, production workloads)

## Examples

### Creating KPI Records

```python
# Create KPI using core dataclass
kpi = KpiRecord(
    schema_version="1",
    kpi_id="throughput_rps",
    value=1000.5,  # Must be numeric
    unit="req/s",
    run_id="/path/to/test",
    timestamp="2024-08-29T10:00:00Z",
    labels={  # String keys only
        "version": "2024-08-29",
        "scenario": "load_test",
        "higher_is_better": True,
    },
    metadata={"environment": "production"},
    source={"plugin": "guidellm"},
)

# Serialize for JSON/YAML
kpi_dict = kpi.to_dict()
```

### Creating Catalog Entries

```python
# Create catalog entry using core dataclass
catalog = KpiCatalogEntry(
    kpi_id="throughput_rps",
    name="Throughput",
    unit="req/s",
    higher_is_better=True,
    is_curve=False,
    help="Requests processed per second",
    description="Measures system throughput under load",
    category="performance",
    tags=["throughput", "performance", "load"],
)

# Serialize
catalog_dict = catalog.to_dict()
```

### Creating Regression Reports

```python
# Create regression report using core dataclass
report = RegressionReport(
    status="regression_detected",
    total_kpis=5,
    regression_count=1,
    analysis_timestamp="2024-08-29T10:00:00Z",
    improvement_count=2,
    baseline_version="2024-08-28",
    current_version="2024-08-29",
    findings=[
        RegressionFinding(
            kpi_id="throughput_rps",
            baseline_value=1000.0,
            current_value=800.0,
            relative_change=-0.2,
            change_percent=-20.0,
            is_regression=True,
            higher_is_better=True,
            unit="req/s",
        )
    ],
    threshold_percent=10.0,
    comparison_labels=["version"],
)

# Serialize
report_dict = report.to_dict()
```

## Benefits

✅ **Type Safety**: Full mypy/IDE validation across ecosystem  
✅ **Consistency**: Same data structures for all plugins  
✅ **Maintainability**: Single source of truth for KPI schemas  
✅ **Serialization**: Clean JSON/YAML with `asdict()`  
✅ **Validation**: Dataclass field validation  
✅ **Extensibility**: Easy to add fields without breaking compatibility  
✅ **Testing**: Clear interfaces for unit testing  
✅ **Documentation**: Self-documenting data structures  

## Testing

The skeleton plugin demonstrates the complete pattern:

```bash
python projects/skeleton/test_dataclasses_kpi.py
```

This validates:
- Core dataclass imports
- KPI record creation and serialization  
- Catalog generation
- Regression analysis
- JSON serialization compatibility

## Next Steps

1. **Core Engine**: ✅ **DONE** - Dataclasses implemented
2. **Skeleton Plugin**: ✅ **DONE** - Reference implementation  
3. **Test Plugin**: Update stub plugin for testing
4. **GuideeLM Plugin**: Migrate to core dataclasses
5. **LLM-D Plugin**: Migrate to core dataclasses
6. **Analysis Pipeline**: Update to leverage structured data

The foundation is now in place for type-safe, consistent KPI handling across the entire Caliper ecosystem.
