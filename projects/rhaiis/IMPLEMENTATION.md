# RHAIIS Benchmark Implementation

RHAIIS benchmarking system built on Forge's workflow engine (projects/core/workflow/).

## Architecture

```
CLI/CI Entry Points
       │
       ▼
┌─────────────────────────────────────┐
│        test_rhaiis.py               │  ← Orchestration layer
│  (run_test, run_prepare, run_cleanup)│
└─────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────┐
│      ConfigLoader                   │  ← Config inheritance
│  defaults → accelerator → model    │
└─────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────┐
│      BenchmarkWorkflow              │  ← Workflow definition
│  (deploy → wait → benchmark)        │
└─────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────┐
│      WorkflowStep implementations   │  ← Step execution
│  DeployVLLM, WaitForReady,         │
│  RunGuideLLM, CollectArtifacts     │
└─────────────────────────────────────┘
```

## Entry Points

### CLI (`cli.py`)
```bash
# Single model + workload
PYTHONPATH=. python3 projects/rhaiis/orchestration/cli.py test \
  --model llama-3.3-70b-fp8 --workload balanced --accelerator nvidia

# Deploy-once: multiple workloads without restarting vLLM
cli.py test --model qwen-0.6b --workloads balanced,short,long-prompt
```

### CI (`ci.py`)
```bash
# Env var driven (for FOURNOS jobs)
FORGE_MODEL=qwen-0.6b FORGE_WORKLOADS=balanced,short \
  python3 projects/rhaiis/orchestration/ci.py test
```

## Config Structure

Project-specific configs allow different projects (rhaiis, llm-d) to have their own settings:

```
config/
├── rhaiis/
│   ├── defaults.yaml     # Global defaults + accelerator settings
│   ├── models.yaml       # Model registry (HF IDs, vllm_args, env_vars)
│   └── workloads.yaml    # GuideLLM profiles (rates, max_seconds)
└── llm-d/                # (future) llm-d specific configs
    └── ...
```

### Inheritance Chain
```
defaults.yaml (base vllm_args, deploy settings)
    ↓ merge
accelerators[nvidia|amd] (image, vllm_args, env_vars)
    ↓ merge
models[model] (hf_model_id, vllm_args, env_vars)
    ↓ merge
models[model].accelerator_overrides[accelerator] (vllm_args, env_vars)
```

## Environment Variables

Env vars are passed to the vLLM pod and follow the same inheritance chain:

### Accelerator-level (`defaults.yaml`)
```yaml
accelerators:
  nvidia:
    env_vars:
      TORCH_CUDA_ARCH_LIST: "9.0"  # All NVIDIA models
  amd:
    env_vars:
      VLLM_ROCM_USE_AITER: "1"     # All AMD models
```

### Model-level (`models.yaml`)
```yaml
models:
  my-model:
    env_vars:
      VLLM_MXFP4_USE_MARLIN: "1"  # This model on all accelerators
```

### Model + Accelerator specific (`models.yaml`)
```yaml
models:
  deepseek-r1:
    accelerator_overrides:
      amd:
        env_vars:
          VLLM_ROCM_USE_AITER: "0"  # Override AMD default for this model
      nvidia:
        env_vars:
          TORCH_CUDA_ARCH_LIST: "9.0"
```

## Core Interfaces

### WorkflowStep
```python
class WorkflowStep(ABC):
    """Base class for all workflow steps."""

    def __init__(self, name: str | None = None):
        self._name = name  # Defaults to class name if not provided

    @abstractmethod
    def execute(self, ctx: WorkflowContext) -> StepResult:
        """Execute step, return success/failure with data."""

@dataclass
class StepResult:
    success: bool
    message: str = ""
    error: Exception | None = None
    artifacts: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float = 0.0
```

### Workflow
```python
class Workflow(ABC):
    def add_step(self, step: WorkflowStep): ...
    def add_finally(self, step: WorkflowStep): ...  # Always runs

    @abstractmethod
    def define_steps(self) -> None:
        """Register steps via add_step() and add_finally()."""
```

### WorkflowContext
```python
@dataclass
class WorkflowContext:
    run_uuid: str
    artifact_dir: Path
    config: dict
    env_vars: dict  # FORGE_* env vars
```

### SequentialExecutor
The executor runs steps with these guarantees:
```python
class SequentialExecutor:
    """
    Execution flow:
    1. Run normal steps in order until completion or failure
    2. On failure, skip remaining normal steps
    3. Always run finally steps, even if normal steps failed
    4. Finally steps continue even if previous finally steps fail
    5. Collect all results and return WorkflowResult
    """
```

## Reliability and Safety

### Current Reliability Features

| Feature | Status | Description |
|---------|--------|-------------|
| Finally steps | ✅ | Cleanup always runs, even on failure |
| Exception handling | ✅ | Unhandled exceptions caught, logged, step marked failed |
| Artifact collection | ✅ | Each step gets its own artifact directory |
| Duration tracking | ✅ | Execution time recorded per step |
| Transient retry | ✅ | OC wrapper retries network errors with backoff |

### Execution Guarantees

```
Step 1 (deploy) ──success──► Step 2 (wait) ──success──► Step 3 (benchmark)
      │                            │                           │
      │ failure                    │ failure                   │ failure
      ▼                            ▼                           ▼
   Finally 1 (collect) ─────► Finally 2 (cleanup) ─────► Return Result
   (always runs)              (always runs)
```

### Transient Errors Handled by OC Wrapper

- Connection refused / reset / timed out
- Service unavailable
- API server not ready
- etcd timeout
- Rate limiting (too many requests)
- TLS handshake timeout

### Safety Considerations

| Aspect | Implementation |
|--------|---------------|
| Resource cleanup | Finally steps delete InferenceService/ServingRuntime |
| Namespace isolation | All resources created in specified namespace |
| Resource labeling | Resources labeled with `app={deployment_name}` for easy identification |
| Idempotent apply | Uses `oc apply` (not `create`) for idempotency |
| Orphan prevention | Cleanup step uses `--ignore-not-found` |

### Known Limitations

| Limitation | Mitigation |
|------------|------------|
| No checkpointing | Re-run from beginning on failure |
| No step timeout enforcement | Use subprocess timeout in OC wrapper |
| No parallel step execution | Use deploy-once pattern to minimize overhead |
| No circuit breaker | Relies on retry exhaustion |

## RHAIIS Steps

| Step | Location | Purpose |
|------|----------|---------|
| `DeployVLLMStep` | `rhaiis/workflows/steps/deploy.py` | Create KServe ServingRuntime + InferenceService |
| `WaitForReadyStep` | `rhaiis/workflows/steps/deploy.py` | Wait for ISVC ready + health check |
| `RunGuideLLMStep` | `core/steps/guidellm.py` | Run GuideLLM as pod, collect results |
| `CollectArtifactsStep` | `core/steps/artifacts.py` | Gather logs, events, pod status |
| `CleanupDeploymentStep` | `core/steps/artifacts.py` | Delete ISVC/ServingRuntime |

## BenchmarkWorkflow

```python
class BenchmarkWorkflow(Workflow):
    def define_steps(self):
        self.add_step(DeployVLLMStep(...))
        self.add_step(WaitForReadyStep(...))
        self.add_step(RunGuideLLMStep(...))
        self.add_finally(CollectArtifactsStep(...))  # Always runs
        self.add_finally(CleanupDeploymentStep(...)) # Always runs
```

## Deploy-Once Pattern

For multiple workloads with same vLLM config, deploys once and runs GuideLLM multiple times:

```python
# test_rhaiis.py::_run_multi_workload()
for workload in workloads:
    # Group by vllm_args (workloads with different vllm_args get separate deployments)
    # Run GuideLLM for each workload without restarting vLLM
```

## Artifact Structure

```
artifacts/{run_uuid}/
├── _meta/
│   └── metadata.yaml
├── 001__deploy/
│   └── kserve.yaml
├── 002__wait/
├── 003__benchmark_balanced/
│   ├── guidellm_logs.txt
│   └── results/
│       └── benchmark_results.json
├── 004__collect_artifacts/
│   ├── app_logs.txt
│   ├── pod_describe.txt
│   └── events.txt
└── 005__cleanup/
```

## Key Design Decisions

1. **KServe RawDeployment**: Uses ServingRuntime + InferenceService for RHOAI compatibility
2. **Pod-based GuideLLM**: Runs benchmark as pod inside cluster (not local)
3. **Finally steps**: CollectArtifacts and Cleanup always run, even on failure
4. **Config inheritance**: Minimizes duplication, accelerator-specific overrides where needed
5. **num_gpus = tensor-parallel-size**: Single source of truth for GPU count
6. **Project-specific configs**: Each project (rhaiis, llm-d) has its own config directory
7. **Env vars inheritance**: Supports accelerator → model → model.accelerator_overrides chain
