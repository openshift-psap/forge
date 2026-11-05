# Intent: parser.py

## Core Intent
Parse a Prometheus capture artifact directory into normalized time-series dataclasses, using the saved metric profile YAMLs as the source of truth for metric metadata.

## Public API Surface
- `MetricTimeSeries` — dataclass holding a single metric's parsed data: metric_name, description, unit, source_file, and a list of `LabeledSeries` (one per unique label combination)
- `LabeledSeries` — dataclass holding labels (dict) and parallel lists of timestamps (datetime) and values (float)
- `CaptureGroupResult` — dataclass holding all parsed metrics for one capture group: group_name, variables (dict), and a list of `MetricTimeSeries`
- `parse_capture_group(capture_dir)` — given a capture group artifact directory (the one containing `prometheus_metrics/` and `src/`), parse everything and return a `CaptureGroupResult`
- `filter_by_source_file(result, source_file_names)` — filter a `CaptureGroupResult` to only include metrics originating from the given profile file names (e.g., `["resource_cpu"]`)

## Side Effects & State
- Reads from disk: metric profile YAMLs under `src/metric_profiles/`, JSON files under `prometheus_metrics/`, and `metrics_metadata.yaml`
- Stateless: no caching, no mutation, no global state

## Constraints
- Do not import from any `projects.*` module; this is standalone Caliper postprocess code that reads files directly
- Do not modify any files on disk
- Do not assume a specific parent directory structure above the capture group directory
- Never silently skip a metric JSON file that exists but fails to parse; raise a clear error
- Do not parse TSDB dump files; only handle kube-burner JSON format (array of `{timestamp, labels, value, metricName}` objects)
- Do not apply any aggregation or transformation to the raw values; preserve them as captured
- Filter functions return new objects; never mutate the input

## Verification Hooks
- Write unit tests with synthetic fixture data (a minimal directory tree with metric profile YAMLs and JSON files) to verify round-trip parsing
- Test that `filter_by_source_file` correctly narrows results and does not mutate the original
- Test error handling: missing `metrics_metadata.yaml`, malformed JSON, empty JSON arrays
