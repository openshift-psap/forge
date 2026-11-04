# Intent: plotting.py

## Core Intent
Generate a single plotly figure from a `MetricTimeSeries`, with unit-aware axis formatting, deduplicated short legend names, and a companion legend mapping for full label details.

## Public API Surface
- `create_metric_figure(metric)` — given a `MetricTimeSeries`, return a `plotly.graph_objects.Figure` with one trace per `LabeledSeries`; labels common to all series are removed from trace names to keep them short; axis labels derived from `description` and `unit`, formatting dispatched by unit family
- `build_legend_info(metric)` — given a `MetricTimeSeries`, return a `LegendInfo` containing `common_labels` (dict of labels shared by all series) and `entries` (list of `LegendEntry` with `short_name` and `full_labels` dict); the short names match the trace names used by `create_metric_figure`
- `LegendInfo` — dataclass holding `common_labels` (dict) and `entries` (list of `LegendEntry`)
- `LegendEntry` — dataclass holding `short_name` (str) and `full_labels` (dict)
- `UNIT_FORMATTERS` — dict mapping unit family strings to formatting config (axis label suffix, value scaling, tick format); built-in families: `ratio`, `percent`, `bytes`, `MiB`, `bytes/s`, `count`; unknown units fall back to raw numeric display

## Side Effects & State
- Stateless: no disk I/O, no global state, no side effects
- Imports only from `plotly` and standard library

## Constraints
- Do not save figures to disk; the caller decides output format and path
- Do not import from any `projects.*` module
- Do not aggregate, resample, or transform time-series values; plot them as received
- Do not hardcode colors; rely on plotly's default color cycle
- Never drop a `LabeledSeries` from the figure; every series in the input appears as a trace
- Trace names must exclude labels that have the same value across all series in the metric; if only one distinguishing label remains, use just its value (not key=value) for brevity
- Noisy labels are suppressed from short trace names: keys in `SUPPRESSED_LABEL_KEYS` (`id`, `image`, `name`) and values longer than `MAX_LABEL_VALUE_LEN` (64 chars) are excluded from the display labels used to build short names; they remain in `full_labels` for the legend table
- If labels are empty or all labels are common, use the metric name as the trace name
- Keep the figure layout minimal: title from `description`, x-axis as time, y-axis with unit-appropriate formatting

## Verification Hooks
- Write unit tests that construct synthetic `MetricTimeSeries` with known values and verify the returned figure has the correct number of traces, trace names, and axis title
- Test that common labels are excluded from trace names
- Test that `build_legend_info` returns matching common labels and entries
- Test each built-in unit family produces the expected y-axis format
- Test fallback behavior for an unknown unit string
