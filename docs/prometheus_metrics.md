# Prometheus Metrics Capture

The Prometheus capture system collects metrics from OpenShift cluster
Prometheus during CI test runs. It supports two complementary capture
modes and a config-driven metric definition DSL for targeted PromQL
queries.

## Architecture overview

```
orchestration (test_phase.py)
  |
  |  capture_prometheus(start, end, runtime_variables)
  v
collection.py  -----> capture_prometheus_metrics()   [PromQL queries]
  |                        |
  |                        |  for each group:
  |                        |    resolve_files -> load_definitions -> interpolate_params -> resolve -> write_capture_input
  |                        |    call toolbox capture_prometheus_metrics
  |                        |    build_index
  |                        v
  |                   toolbox/capture_prometheus_metrics/main.py
  |                        creates a curl pod in openshift-monitoring
  |                        executes each PromQL range query via curl
  |                        writes raw JSON responses to output dir
  |
  +-----> capture_prometheus()                       [TSDB snapshots]
               toolbox/capture_prometheus_db/main.py
               exports compressed OpenMetrics archive from Prometheus TSDB
```

There are three independent capture backends, each controlled by its own
config flag:

| Backend | Config key | What it captures |
|---------|-----------|-----------------|
| **Metrics (PromQL)** | `prom.capture.metrics.enabled` | Targeted PromQL range queries, saved as per-metric JSON files |
| **DB (TSDB)** | `prom.capture.db.enabled` | Top-level gate for both TSDB snapshot backends below |
| **System TSDB** | `prom.capture.db.system_metrics.enabled` | Full TSDB snapshot from `openshift-monitoring` Prometheus |
| **User Workload TSDB** | `prom.capture.db.user_workload.enabled` | Full TSDB snapshot from `openshift-user-workload-monitoring` Prometheus |

## Metric definition files

Metrics are defined in YAML files under `projects/cluster/metrics/` and
`projects/kserve/metrics/`. Each file is a flat mapping of metric key to
definition:

```yaml
# projects/cluster/metrics/resource_cpu.yaml

# File-level params apply to all metrics in the file unless overridden
params:
  namespace:
    description: Target namespace
  pod_name:
    description: Pod name for the resource capture

avg_cpu_usage_percent:
  category: cpu
  description: Avg cpu usage percent
  unit: ratio
  promql: >-
    avg(rate(container_cpu_usage_seconds_total{namespace="{namespace}", pod=~"{pod_name}"}[5m])) * 100

cpu_usage_percent_by_pod:
  category: cpu
  tags: [per-pod]
  description: Cpu usage percent by pod
  unit: ratio
  promql: >-
    rate(container_cpu_usage_seconds_total{namespace="{namespace}", pod=~"{pod_name}"}[5m]) * 100
```

### Metric fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `category` | yes | | Grouping label (e.g. `cpu`, `memory`, `latency`) |
| `description` | yes | | Human-readable description |
| `unit` | yes | | Measurement unit (e.g. `ratio`, `bytes`, `seconds`) |
| `promql` | yes | | PromQL query template; `{param_name}` placeholders are substituted at resolve time |
| `tags` | no | `[]` | List of tags for filtering (e.g. `per-pod`, `per-node`) |
| `on_error` | no | `ignore` | `ignore` or `fail` -- whether a query error is fatal |
| `params` | no | `{}` | Parameter declarations with `description` and optional `default` |

### File-level params

A top-level `params:` key in the YAML file defines parameters that apply
to all metrics in the file. Individual metrics can override by declaring
their own `params:`.

## Project configuration

Each project configures the capture system via its `config.d/prom.yaml`:

```yaml
# projects/llm_d/orchestration/config.d/prom.yaml

prepare:
  user_workload:
    during_prepare: true
    during_test: true

capture:
  enabled: true
  db:
    enabled: true
    system_metrics:
      enabled: true
    user_workload:
      enabled: true
      fail_if_not_enabled: false
  metrics:
    enabled: true
    include_dirs:
      - projects/cluster/metrics
      - projects/kserve/metrics
    config:
      llmisvc_namespace: "@runtime.namespace"
      llmisvc_name: SET_AT_RUNTIME
    groups:
      resources:
        enabled: true
        files: [resource_cpu, resource_memory, resource_network, workload]
        step_seconds: 15
        params:
          namespace: "{llmisvc_namespace}"
          pod_name: "{llmisvc_name}-.*"
      vllm:
        enabled: true
        files: [vllm_latency, vllm_throughput]
        step_seconds: 15
        params:
          llmisvc_namespace: "{llmisvc_namespace}"
          llmisvc_name: "{llmisvc_name}"
```

### Config structure

- **`include_dirs`** -- directories to search when resolving metric file
  names (bare names like `resource_cpu` are resolved to
  `<dir>/resource_cpu.yaml`)

- **`config`** -- variables available during parameter interpolation.
  Values can use the framework's `@key` reference syntax (e.g.
  `@runtime.namespace` resolves to a value from the runtime config).
  The special value `SET_AT_RUNTIME` marks variables that must be
  provided by the calling code via `runtime_variables`.

- **`groups`** -- named metric groups, each with:
  - `enabled` (default `true`) -- toggle the group
  - `files` -- list of metric definition file names (resolved from `include_dirs`)
  - `step_seconds` (default `15`) -- PromQL range query step
  - `params` -- parameter values passed to PromQL templates

## Two-level parameter interpolation

Parameters go through two interpolation stages:

### Stage 1: Config variable interpolation

Group `params` values can reference config variables with `{variable}`
syntax. The variables come from the `config` dict (resolved via the
framework's `@key` references) merged with `runtime_variables` passed
by the orchestration code.

```yaml
config:
  llmisvc_namespace: "@runtime.namespace"   # resolved from runtime config
  llmisvc_name: SET_AT_RUNTIME              # must be provided at runtime

groups:
  vllm:
    params:
      llmisvc_namespace: "{llmisvc_namespace}"   # -> actual namespace value
      llmisvc_name: "{llmisvc_name}"             # -> actual service name
```

The orchestration code provides runtime values:

```python
runtime_variables = {"llmisvc_name": "my-llm-service"}
capture_prometheus(start_time, end_time, runtime_variables=runtime_variables)
```

If any variable still has the value `SET_AT_RUNTIME` after merging, the
system raises a `ValueError`.

### Stage 2: PromQL template substitution

The interpolated params are then substituted into each metric's PromQL
template:

```
promql: rate(container_cpu_usage_seconds_total{namespace="{namespace}"}[5m])
params: {namespace: "llm-serving"}
result: rate(container_cpu_usage_seconds_total{namespace="llm-serving"}[5m])
```

Metrics with missing mandatory parameters (no default, not in params)
are skipped with a log warning.

## Pipeline flow

When the orchestration calls `capture_prometheus()`, the following
happens for each enabled metric group:

1. **Resolve files** -- file names from the group's `files` list are
   resolved to actual paths using `include_dirs`
2. **Load definitions** -- YAML files are parsed into `MetricDefinition`
   objects; duplicate keys across files raise an error
3. **Interpolate params** -- group params are interpolated with config
   variables and runtime variables
4. **Resolve queries** -- PromQL templates are filled with the
   interpolated params, producing a `{name: promql}` dict
5. **Write capture input** -- the query dict is written to a temporary
   YAML file
6. **Execute queries** -- the `capture_prometheus_metrics` toolbox
   command creates a curl pod in `openshift-monitoring`, runs each
   query against Thanos, and saves raw JSON responses
7. **Build index** -- an `index.yaml` is written to the output dir
   summarizing each metric's status (`ok`, `no_data`, `error`),
   resolved query, and metadata

### Output structure

```
prometheus_metrics/
  resources/
    avg_cpu_usage_percent.json      # raw Prometheus JSON response
    cpu_usage_percent_by_pod.json
    ...
    index.yaml                      # summary with status per metric
  vllm/
    ...
    index.yaml
  scheduler/
    ...
    index.yaml
```

The `index.yaml` contains:

```yaml
timestamp: "2026-09-25T12:00:00+00:00"
results:
  avg_cpu_usage_percent:
    status: ok          # ok | no_data | error
    description: Avg cpu usage percent
    unit: ratio
    category: cpu
    on_error: ignore
    promql: avg(rate(...{namespace="llm-serving", pod=~"my-svc-.*"}[5m])) * 100
```

## Orchestration integration

The capture runs as a **finalizer** in the test phase, after the
benchmark completes but before resource cleanup. This ensures metrics
are captured even if the test fails:

```python
# projects/llm_d/orchestration/test_phase.py

def run_finalizers(..., benchmark_times=None):
    ...
    if benchmark_times:
        start_time, end_time = benchmark_times
        runtime_variables = {"llmisvc_name": llmisvc_name or "..."}

        def _capture_prom():
            with env.NextArtifactDir("prometheus_metrics"):
                capture_prometheus(start_time, end_time, runtime_variables=runtime_variables)

        finalizer_exc = _run_finalizer("capturing Prometheus metrics", _capture_prom)
    ...
```

The benchmark function records its start and end times and returns them
as a `tuple[datetime, datetime]` for the finalizer to use as the query
window.

## CLI usage

The `capture-metrics` Click command provides standalone access for
debugging and ad-hoc queries:

```bash
# Run all configured groups
./bin/run_toolbox cluster capture-metrics \
    --start-time "2026-09-25T10:00:00+00:00" \
    --end-time "2026-09-25T10:20:00+00:00"

# Run a single configured group
./bin/run_toolbox cluster capture-metrics \
    --group resources \
    --start-time "2026-09-25T10:00:00+00:00" \
    --end-time "2026-09-25T10:20:00+00:00"

# Ad-hoc file selection with custom params
./bin/run_toolbox cluster capture-metrics \
    --files resource_cpu --files resource_memory \
    --param namespace=my-namespace \
    --param pod_name="my-pod-.*" \
    --start-time "2026-09-25T10:00:00+00:00" \
    --end-time "2026-09-25T10:20:00+00:00" \
    --output-dir /tmp/metrics_output

# Filter by tags
./bin/run_toolbox cluster capture-metrics \
    --group resources \
    --exclude-tags per-pod \
    --start-time "2026-09-25T10:00:00+00:00" \
    --end-time "2026-09-25T10:20:00+00:00"
```

Without `--start-time`/`--end-time`, defaults to the last hour.

## Key files

| File | Role |
|------|------|
| `projects/cluster/library/prom/metrics.py` | Core library: definition loading, file resolution, param interpolation, query resolution, index building, CLI command |
| `projects/cluster/library/prom/collection.py` | Orchestration glue: reads project config, resolves variables, dispatches to toolbox commands |
| `projects/cluster/toolbox/capture_prometheus_metrics/main.py` | Toolbox command: creates curl pod, executes PromQL queries, writes JSON results |
| `projects/cluster/toolbox/capture_prometheus_db/main.py` | Toolbox command: TSDB snapshot export as compressed OpenMetrics archive |
| `projects/cluster/metrics/*.yaml` | Cluster-level metric definitions (CPU, memory, network, workload) |
| `projects/kserve/metrics/*.yaml` | KServe/vLLM metric definitions (latency, throughput, scheduler) |
| `projects/cluster/tests/test_prom_metrics.py` | Unit tests for the metrics library |

## Adding metrics to an existing project

1. Create or edit a YAML file in the appropriate `metrics/` directory
2. Add entries with `category`, `description`, `unit`, and `promql`
3. Use `{param_name}` placeholders for values that vary per deployment
4. Add the file name to the relevant group's `files` list in your
   project's `config.d/prom.yaml`
5. Ensure the group's `params` provide values for all non-default
   parameters

## Adding metrics to a new project

1. Create `projects/<name>/orchestration/config.d/prom.yaml` with the
   structure shown above
2. Set `include_dirs` to point at the directories containing your
   metric definition files
3. Define groups with the files and params appropriate to your workload
4. Call `capture_prometheus(start, end, runtime_variables=...)` from
   your test finalizers
