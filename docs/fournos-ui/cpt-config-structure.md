# CPT Pipeline Definitions (`cpt.d/cpt.yaml`)

This document describes how to write and understand CPT (Continuous Performance
Testing) pipeline definitions, and how the Fournos UI expands them into jobs.

## File Location

```
projects/rhaiis/orchestration/cpt.d/cpt.yaml
```

## Top-Level Marker

```yaml
__cpt: true
```

Signals that this file contains CPT pipeline definitions (not regular presets).

## Pipeline Structure

```yaml
<pipeline-name>:
  # ── Meta fields (prefixed with __) ──
  __description: "Human-readable description"
  __engine: vllm | sglang | trtllm
  __accelerator: nvidia | amd        # optional, defaults to nvidia
  __gpu_type: h200 | b200 | mi355x  # optional display metadata
  __clusters: [hera, zeus]           # optional allowed Fournos clusters
  __models:
    <preset-name>/tp<N>:             # model alias from presets.yaml + GPU count
    <preset-name>/tp<N>:
      __workloads: [profile1, profile4, profile5]  # optional per-model list
      <dotted.config.key>: value     # per-model overrides (optional)
  __workloads:
    - <workload-preset-name>         # from presets.yaml (maps to workload_key)
    - ...

  # ── Global overrides (applied to every job in this pipeline) ──
  rhaiis.profiler.enabled: true
  tests.rhaiis.run_benchmark: true
  caliper.postprocess.csv_dashboard.enabled: true
  tests.rhaiis.slack_notify_always: true
  rhaiis.agent_analysis.enabled: false
```

## Model Entries (`__models`)

Keys use the format `<preset>/tp<N>`:

- `<preset>` — an alias from `presets.d/presets.yaml` (e.g. `llama-70b` → `tests.rhaiis.model_key: llama-3-3-70b-fp8`)
- `tp<N>` — the tensor-parallel size (GPU count) for this job

If the value is `null` (or omitted), only the pipeline-level globals apply.
If the value is a map, those entries are additional config overrides for that
specific model (e.g. to override `tensor-parallel-size`):

```yaml
__models:
  llama-70b/tp4:           # uses model's default tp=4, no extra overrides
  llama-70b/tp2:           # override tp to 2 for this specific entry
      rhaiis.engines.vllm.args.tensor-parallel-size: 2
```

`__workloads` in a model entry is CPT metadata, not a Forge config override. It
replaces the pipeline-level `__workloads` list for that model. This allows a
matrix to run an optional profile, such as `profile5`, only for the models that
require it. The Fournos UI still creates one job per model and passes the
selected workload list in `tests.rhaiis.workload_keys`.

When `__clusters` is present, it limits the cluster choices shown by the
Fournos UI and is validated again by the submission API. The user still chooses
the actual cluster at submission time.

## Workload Entries (`__workloads`)

```yaml
__workloads:
  - ci-quick
  - profile1
  - profile2
```

Each entry is a preset name from `presets.d/presets.yaml` that maps to a
`tests.rhaiis.workload_key`.

## Global Overrides

Any non-`__` keys at the pipeline level are config overrides applied to every
job in that pipeline. Common ones:

| Key | Purpose |
|-----|---------|
| `rhaiis.profiler.enabled` | Enable PyTorch profiler |
| `tests.rhaiis.run_benchmark` | Actually run the GuideLLM benchmark |
| `caliper.postprocess.csv_dashboard.enabled` | Export results to dashboard CSV |
| `tests.rhaiis.slack_notify_always` | Send Slack notification regardless of result |
| `rhaiis.agent_analysis.enabled` | Run AI analysis on results |

## How the Fournos UI Expands a CPT Pipeline

Reference: [fournos PR #117](https://github.com/openshift-psap/fournos/pull/117)
(`fournos-ui/app/projects/rhaiis.py`)

### Job Granularity: One Job Per Model

The UI creates **one FournosJob per model entry** in `__models`. All workloads
are passed as a list in `tests.rhaiis.workload_keys` — the test phase iterates
over them internally.

A pipeline with 9 models × 5 workloads = **9 jobs** (not 45).

### Expansion Steps

For each model entry in `__models`:

1. Parse `<preset>/tp<N>` → extract preset name and TP size
2. Resolve preset → `tests.rhaiis.model_key` (via `presets.d/presets.yaml`)
3. Look up GPU count from TP suffix (or fall back to `models.yaml[model_key].vllm_args.tensor-parallel-size`)
4. Look up GPU type from cluster (e.g. hera → `h200`)
5. Merge overrides: pipeline globals → per-model overrides → `tests.rhaiis.workload_keys: __workloads`
6. Build FournosJob:

```yaml
spec:
  cluster: hera
  displayName: rhaiis-cpt-llama-70b-hera
  hardware:
    gpuType: h200
    gpuCount: 4
  executionEngine:
    forge:
      project: rhaiis
      args: [nvidia, vllm, hera, llama-70b]
      configOverrides:
        tests.rhaiis.workload_keys: [ci-quick, profile1, profile2, profile3, profile4]
        tests.rhaiis.version: "vLLM-0.24.0-CPT"
        rhaiis.profiler.enabled: true
        ...per-model overrides...
  secretRefs: [psap-forge-dashboard-s3, psap-forge-notifications]
```

## Full Example

```yaml
__cpt: true

cpt-vllm-release:
  __description: "vLLM Release CPT — Tier 1 models across all workload profiles"
  __engine: vllm
  __models:
    llama-70b/tp4:
    llama-70b/tp2:
      rhaiis.engines.vllm.args.tensor-parallel-size: 2
    granite-8b-bf16/tp1:
    mistral-24b/tp1:
    qwen25-7b/tp1:
  __workloads:
    - ci-quick
    - profile1
    - profile2
    - profile3
    - profile4
  rhaiis.profiler.enabled: true
  tests.rhaiis.run_benchmark: true
  caliper.postprocess.csv_dashboard.enabled: true
  tests.rhaiis.slack_notify_always: true
  rhaiis.agent_analysis.enabled: false
```

This produces **5 FournosJobs** (one per model), each running all 5 workloads.
