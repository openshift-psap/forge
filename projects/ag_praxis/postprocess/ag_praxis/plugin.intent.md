# Intent: plugin.py

## Core Intent
Provide an ag_praxis Caliper postprocess plugin that extends GuideLLMPlugin with Prometheus metrics visualization integrated into the plot registry.

## Public API Surface
- `AgPraxisPlugin` — subclass of `GuideLLMPlugin` that registers a `report_prometheus_metrics` entry in the plot registry; the Prometheus report generator discovers capture directories across all test nodes and produces per-node, per-group reports
- `get_plugin()` — factory function returning an `AgPraxisPlugin` instance (Caliper plugin discovery convention)
- `find_prometheus_capture_dirs(test_node)` — given a `BaseTestNode`, scan its artifact paths for Prometheus capture group directories (directories containing both `prometheus_metrics/metrics_metadata.yaml` and `src/metric_profiles/`); return a list of Paths

## Side Effects & State
- Reads from disk: scans test artifact directories for Prometheus capture data
- Writes to disk: generates Prometheus HTML report files in the output directory (via `generate_group_report`)
- Inherits all side effects from `GuideLLMPlugin` (parsing, visualization, KPI computation)

## Constraints
- Do not override `parse()`, `compute_kpis()`, `export_dashboard_csv()`, or `kpi_catalog()`; inherit them from `GuideLLMPlugin`
- Do not duplicate any GuideLLM visualization logic; the parent `visualize()` handles registry dispatch including the registered Prometheus entry
- Do not import from `projects.cluster.*` or `projects.core.*`; use only `projects.caliper.*` and `projects.guidellm.postprocess.guidellm.plugin`
- Do not fail the visualization if no Prometheus capture directories are found; log a message and return normally
- Do not fail the visualization if a single Prometheus capture group fails to parse; log a warning and continue with the remaining groups
- Output paths must include the test node directory name to avoid collisions when multiple test nodes contain Prometheus data with the same group names

## Verification Hooks
- Write unit tests that verify `find_prometheus_capture_dirs` returns correct paths for a synthetic artifact tree
- Test that `find_prometheus_capture_dirs` returns an empty list when no Prometheus data exists
