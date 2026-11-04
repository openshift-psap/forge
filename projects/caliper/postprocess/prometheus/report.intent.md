# Intent: report.py

## Core Intent
Assemble Prometheus metric figures into standalone group reports or embeddable plot components, using the shared Caliper visualization utilities for consistent file naming and output format.

## Public API Surface
- `generate_group_report(group_result, output_dir, report_number)` — given a `CaptureGroupResult`, an output directory, and an optional report number, generate one figure per metric using `create_metric_figure`, save each as both PNG and HTML via `save_figure()`, and produce a standalone HTML report page containing all plots with their descriptions and links; return the path to the report HTML file
- `render_embeddable(metric, output_dir)` — given a single `MetricTimeSeries` and an output directory, generate the figure, save it as both PNG and HTML via `save_figure()`, and return an `EmbeddableMetricPlot` containing the PNG and HTML file paths and a text summary (description + unit)
- `EmbeddableMetricPlot` — dataclass holding `png_path` (Path or None), `html_path` (Path or None), and `summary_text` (str) for embedding into another report

## Side Effects & State
- Writes PNG and HTML files to the provided `output_dir` via `save_figure()`
- Reads no external state; all data comes from the input arguments

## Constraints
- Do not import from any `projects.*` module except `projects.caliper.postprocess.prometheus.parser`, `projects.caliper.postprocess.prometheus.plotting`, and `projects.caliper.postprocess.helpers.visualization_utils`
- Use `save_figure()` from `visualization_utils` for all file output to ensure consistent naming and format across Caliper reports
- Use `create_report_filename()` and `create_report_title_display()` from `visualization_utils` for report naming consistency
- Report HTML must link to both the PNG (displayed inline) and the interactive HTML (as a clickable link) for each plot, following the guidellm report pattern
- Never skip a metric that has status `ok` in the group result; metrics with `no_data` or `error` status should appear as a placeholder with a message instead of a plot
- Report HTML must be functional as a standalone file (no external CSS/JS dependencies beyond the plotly CDN)

## Verification Hooks
- Write unit tests with synthetic `CaptureGroupResult` data that verify the generated report file exists and contains the expected metric titles
- Test that `render_embeddable` returns a valid result with non-empty summary text
- Test that metrics with `no_data` status produce a placeholder in the report rather than an error
