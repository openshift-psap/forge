# rhaiis

`rhaiis` is the Forge project for benchmarking AI inference engines on OpenShift
using KServe InferenceService.

The current implementation focuses on single-replica vLLM deployments benchmarked
with GuideLLM. The workflow deploys an InferenceService, waits for readiness,
runs a GuideLLM benchmark job, extracts results, and cleans up.

## Workflow sequence

```
deploy_kserve_isvc      Deploy ServingRuntime + InferenceService
        |
wait_isvc_ready         Poll InferenceService status + health check
        |
run_guidellm_benchmark  Create PVC, run GuideLLM job, copy results, cleanup
        |
capture_isvc_state      Capture ISVC YAML, pod logs, events
        |
cleanup_isvc            Delete InferenceService + ServingRuntime
```

On failure at any step, `capture_isvc_state` and `cleanup_isvc` still run
(try/finally in the orchestration layer).

After the benchmark, `run_and_postprocess()` automatically runs:
- KPI computation (22 metrics via rhaiis postprocess plugin)
- PSAP payload generation (model-furnace compatible JSON)
- MLflow metadata tagging

The benchmark step uses the canonical `projects.guidellm.toolbox.run_guidellm_benchmark`
(shared with llm_d). rhaiis builds `guidellm_args` from its config and passes them
to the canonical runner.

## Configuration

Config is split into `config.yaml` (base) and `config.d/` (per-domain):

- [`orchestration/config.yaml`](./orchestration/config.yaml) — vaults, benchmarks, tests, caliper
- [`orchestration/config.d/rhaiis.yaml`](./orchestration/config.d/rhaiis.yaml) — deploy defaults, vLLM args, images, gpu_types
- [`orchestration/config.d/models.yaml`](./orchestration/config.d/models.yaml) — 52 model definitions
- [`orchestration/config.d/workloads.yaml`](./orchestration/config.d/workloads.yaml) — workload profiles

Key sections:

| Section | Purpose |
|---------|---------|
| `rhaiis` | Namespace, accelerator, vLLM images, deploy settings, vLLM args |
| `models` | Model definitions (hf_model_id, per-model vLLM overrides) |
| `workloads` | Benchmark profiles (data shape, rates, max_seconds) |
| `benchmarks.guidellm` | GuideLLM image, backend, timeout, PVC size |
| `tests` | CI test mapping (model_key, workload_key) |

## Fournos integration

rhaiis supports Fournos-driven execution via `ci.py`:

```
bin/run_ci rhaiis ci resolve-fournos-config   # Populate spec.secretRefs + hardware
bin/run_ci rhaiis ci pre-cleanup              # Delete leftover jobs/pods
bin/run_ci rhaiis ci prepare                  # Verify cluster, ensure namespace/SA
bin/run_ci rhaiis ci test                     # Deploy, benchmark, capture, cleanup
bin/run_ci rhaiis ci export-artifacts         # Caliper export to MLflow
```

FournosJob example:
```yaml
apiVersion: fournos.dev/v1
kind: FournosJob
metadata:
  generateName: rhaiis-benchmark-
spec:
  owner: mehul
  displayName: rhaiis-qwen3-benchmark
  pipeline: forge-prepare-test
  cluster: fournos-fg
  priority: manual
  hardware:
    gpuType: l40s
    gpuCount: 1
  executionEngine:
    forge:
      project: rhaiis
      args: [nvidia, profile1]
      configOverrides:
        tests.rhaiis.model_key: qwen3-0_6b
        rhaiis.namespace: kserve-e2e-perf
        rhaiis.deploy.image_pull_secret: npalaska-image-pull
        rhaiis.deploy.storage_pvc: model-pvc
```

Presets from `args` are applied via `project.args` → `presets.d/presets.yaml`.
Config overrides use dot-notation to set any nested config value.

Available configOverrides:

| Key | Description |
|-----|-------------|
| `tests.rhaiis.model_key` | Model key from config.d/models.yaml |
| `rhaiis.namespace` | Kubernetes namespace |
| `rhaiis.deploy.image_pull_secret` | Image pull secret name |
| `rhaiis.deploy.storage_pvc` | PVC name for model storage |
| `rhaiis.deploy.replicas` | Number of predictor replicas |
| `rhaiis.images.nvidia` | vLLM container image override |
| `rhaiis.vllm_args.tensor-parallel-size` | Tensor parallel size |
| `benchmarks.guidellm.timeout` | Benchmark timeout in seconds |

Monitoring Fournos jobs:
```bash
export KUBECONFIG=~/kubeconfigs/psap-automation-kubeconfig
oc get fournosjobs -n psap-automation              # Job status
oc get workloads -n psap-automation                # Queue status
oc get pipelineruns -n psap-automation | grep <name>  # Pipeline progress
oc logs -f <pod-name> -n psap-automation -c step-main # Live logs
oc patch fournosjob <name> -n psap-automation \
  --type merge -p '{"spec":{"shutdown":"Stop"}}'   # Stop a job
```

## Main entrypoints

- CLI: [`orchestration/cli.py`](./orchestration/cli.py)
- CI: [`orchestration/ci.py`](./orchestration/ci.py) (Fournos pipeline)
- CI test: [`orchestration/test_rhaiis.py`](./orchestration/test_rhaiis.py)

## Toolbox commands

| Command | Source | Purpose |
|---------|--------|---------|
| `deploy_kserve_isvc` | [rhaiis](./toolbox/deploy_kserve_isvc/) | Render and apply KServe InferenceService + ServingRuntime |
| `wait_isvc_ready` | [rhaiis](./toolbox/wait_isvc_ready/) | Poll InferenceService readiness with health check |
| `run_guidellm_benchmark` | [canonical](../guidellm/toolbox/run_guidellm_benchmark/) | Run GuideLLM benchmark (shared with llm_d) |
| `capture_isvc_state` | [rhaiis](./toolbox/capture_isvc_state/) | Capture InferenceService YAML, pod logs, events, describe output |
| `cleanup_isvc` | [rhaiis](./toolbox/cleanup_isvc/) | Delete InferenceService, ServingRuntime, wait for deletion |

## Usage

```bash
# Activate the virtualenv
source ~/test_foo/python3_virt/bin/activate

# Dry run (prints config without deploying)
python3 -m projects.rhaiis.orchestration.cli test \
  --model qwen3-0_6b --workload profile1 --dry-run

# Dry run with a specific model
python3 -m projects.rhaiis.orchestration.cli test \
  --model llama-4-scout-fp8 --workload profile2 --dry-run

# Full E2E test
python3 -m projects.rhaiis.orchestration.cli test \
  --model qwen3-0_6b \
  --workload profile1 \
  --namespace kserve-e2e-perf \
  --image-pull-secret npalaska-image-pull

# Cleanup only
python3 -m projects.rhaiis.orchestration.cli cleanup \
  --deployment-name qwen3-0-6b --namespace kserve-e2e-perf

# CI resolve dry-run (shows what Fournos would resolve)
PYTHONPATH=$PWD python3 projects/rhaiis/orchestration/ci.py \
  resolve-fournos-config --dry-run
```

## CLI overrides

The CLI accepts flags that override workload profile defaults. This is useful for
quick validation runs without changing `config.yaml`.

```bash
# Override rates and max-seconds for a quick test (2 rates, 60s each)
python3 -m projects.rhaiis.orchestration.cli test \
  --model qwen3-0_6b \
  --workload profile1 \
  --namespace kserve-e2e-perf \
  --image-pull-secret npalaska-image-pull \
  --rates 1,5 --max-seconds 60

# Override tensor-parallel size
python3 -m projects.rhaiis.orchestration.cli test \
  --model llama-3-1-8b-fp8 \
  --tensor-parallel 2 \
  --namespace kserve-e2e-perf

# Override vLLM image
python3 -m projects.rhaiis.orchestration.cli test \
  --model qwen3-0_6b \
  --vllm-image quay.io/custom/vllm:latest \
  --namespace kserve-e2e-perf
```

Available overrides:

| Flag | Default source | Description |
|------|---------------|-------------|
| `--rates` | `workloads.<key>.rates` | Comma-separated concurrency levels (e.g. `1,5,50`) |
| `--max-seconds` | `workloads.<key>.max_seconds` | Max benchmark duration per rate |
| `--tensor-parallel` | `rhaiis.vllm_args.tensor-parallel-size` | Tensor parallel size |
| `--vllm-image` | `rhaiis.images.<accelerator>` | vLLM container image |
| `--accelerator` | `rhaiis.accelerator` | `nvidia` or `amd` |
| `--replicas` | `rhaiis.deploy.replicas` | Number of predictor replicas |
| `--storage-source` | `rhaiis.deploy.storage_source` | `hf` (HuggingFace download) or `pvc` |
| `--storage-pvc` | `rhaiis.deploy.storage_pvc` | PVC name for model storage |
| `--image-pull-secret` | `rhaiis.deploy.image_pull_secret` | Image pull secret name |
| `--service-account-name` | `rhaiis.deploy.service_account_name` | Service account for predictor |
| `--deployment-name` | derived from model HF ID | InferenceService name |

## Result extraction

GuideLLM results are extracted using the copy-pod pattern (same as llm_d):

1. GuideLLM job writes `benchmarks.json` to a PVC mounted at `/results`
2. A copy pod is created on the same node (required for ReadWriteOnce PVC)
3. Results are extracted via `oc exec cat /results/benchmarks.json`
4. Written to local `artifacts/results/benchmarks.json`
5. PVC, job, and copy pod are deleted

Artifacts are stored under `/tmp/forge_<timestamp>/002__run_guidellm_benchmark/artifacts/`.

## Available models

52 models from model_furnace are defined in `config.yaml`. Key families:

| Family | Key examples | TP size |
|--------|-------------|---------|
| Llama-4 Scout | `llama-4-scout`, `llama-4-scout-fp8`, `llama-4-scout-int4` | 2-4 |
| Llama-4 Maverick | `llama-4-maverick`, `llama-4-maverick-fp8` | 8 |
| Llama-3.3-70B | `llama-3-3-70b`, `llama-3-3-70b-fp8`, `-w8a8`, `-w4a16` | 4 |
| Llama-3.1-8B | `llama-3-1-8b`, `llama-3-1-8b-fp8`, `-w8a8`, `-w4a16` | 1 |
| Llama-3.1-405B | `llama-3-1-405b`, `llama-3-1-405b-fp8`, `-w8a8` | 8 |
| Granite 3.1 8B | `granite-3-1-8b-instruct`, `-fp8`, `-w4a16`, `-w8a8` | 1 |
| Mistral Small 3.1 | `mistral-2503`, `-fp8`, `-w4a16`, `-w8a8` | 1 |
| Qwen3 235B | `qwen3-235b-instruct`, `-fp8` | 4 |
| DeepSeek | `deepseek-r1-0528`, `deepseek-v3-2`, `deepseek-v4-pro` | 8 |
| Phi-4 | `phi-4`, `phi-4-fp8`, `-w4a16`, `-w8a8` | 1 |
| Validation | `qwen3-0_6b` | 1 |

Full list: `grep "^[a-z]" orchestration/config.d/models.yaml`

## Workload profiles

From model_furnace `guidellm_profiles.iterations`:

| Key | Prompt tokens | Output tokens | Rates | Max seconds |
|-----|--------------|---------------|-------|-------------|
| `profile1` | 1000 | 1000 | 1, 50, 100, 200, 300 | 450 |
| `profile2` | 512 (stdev 128) | 2048 (stdev 512) | 1, 50, 100, 200, 300 | 450 |
| `profile3` | 2048 | 128 | 1, 50, 100, 200, 300 | 450 |
| `profile4` | 8000 | 1000 | 1, 25, 50, 75, 100 | 450 |

## Postprocessing and KPIs

After each benchmark run, the rhaiis postprocess plugin (`projects/rhaiis/postprocess/`)
automatically computes 22 KPIs with model-furnace parity:

| Category | KPIs |
|----------|------|
| Throughput | request_rate, tokens/s, input_tokens/s, output_tokens/s |
| Latency | ttft (median, p95, p99), itl (median, p95, p99), tpot (median, p95, p99) |
| Request latency | request_latency (median, p95) |
| Token efficiency | input_tokens/request, output_tokens/request |
| Request stats | concurrency, completed_requests, failed_requests |
| Benchmark | duration, prompt_token_count_mean |

KPIs are written to `kpis.json` in the postprocess output directory.

## PSAP payload

A model-furnace compatible `PSAP_perf_<profile>_<accelerator>_<model>_<date>.json`
is generated after each benchmark. It wraps the raw `benchmarks.json` with experiment
metadata (model, vLLM image/args, accelerator, timestamps) for downstream compatibility.

## MLflow export

Results are exported to MLflow via the caliper pipeline. The export step runs as
a separate CI command (`export-artifacts`) in the Fournos pipeline.

MLflow tags set on each run:

| Tag | Example |
|-----|---------|
| `project` | rhaiis |
| `model_key` | qwen3-0_6b |
| `hf_model_id` | Qwen/Qwen3-0.6B |
| `accelerator` | nvidia |
| `tensor_parallel_size` | 1 |
| `vllm_image` | quay.io/aipcc/rhaiis/cuda-ubi9:3.4.0-ea.2 |
| `vllm_version` | 3.4.0-ea.2-1773886296 |
| `workload_key` | profile1 |
| `rates` | 1,5,10 |
| `guidellm_backend_type` | openai_http |

MLflow experiment: `forge-rhaiis`

## Presets

Presets in `presets.d/presets.yaml` provide shortcuts for common configurations:

```bash
# Use presets instead of specifying model/workload/accelerator separately
python3 -m projects.rhaiis.orchestration.cli test \
  --preset llama-8b --preset profile1 \
  --namespace kserve-e2e-perf

# Available model presets: llama-8b, llama-70b, llama-405b, llama-4-scout,
#   llama-4-maverick, granite-8b, mistral-24b, qwen25-7b, qwen3-235b,
#   deepseek-r1, deepseek-v3, gpt-oss
# Workload presets: profile1, profile2, profile3, profile4
# Accelerator presets: nvidia, amd
```

Per-cluster presets (e.g. `presets.d/mehulvalidation.yaml`) set cluster-specific
defaults like namespace, image pull secret, and service account.
