# llm_d

`llm_d` is the Forge project for validating downstream llm-d on RHOAI.

The current implementation is intentionally narrow:

- target only downstream `LLMInferenceService`
- keep the public interface compatible with current Fournos phase execution
- use checked-in config chunks and manifests instead of a large mutable config surface

Configuration layout:

- project config chunk: [`orchestration/config.d/project.yaml`](./orchestration/config.d/project.yaml)
- config chunks: [`orchestration/config.d`](./orchestration/config.d)
- presets: [`orchestration/presets.d`](./orchestration/presets.d)
- manifests: [`orchestration/manifests`](./orchestration/manifests)

Main entrypoints:

- CI phase wrapper: [`orchestration/ci.py`](./orchestration/ci.py)
- CLI wrapper: [`orchestration/cli.py`](./orchestration/cli.py)
- Shared runtime/config loader: [`orchestration/runtime_config.py`](./orchestration/runtime_config.py)
- Prepare flow: [`orchestration/prepare_sequence.py`](./orchestration/prepare_sequence.py)
- Test flow: [`orchestration/test_phase.py`](./orchestration/test_phase.py)
- Cleanup flow: [`orchestration/cleanup_phase.py`](./orchestration/cleanup_phase.py)

Profile model:

- deployment profiles are defined in
  [`orchestration/config.d/deployments.yaml`](./orchestration/config.d/deployments.yaml)
  and reference scheduler/router manifest fragments under
  [`orchestration/manifests/deployments`](./orchestration/manifests/deployments)
- benchmark profiles are represented as named entries under
  [`orchestration/config.d/workloads.yaml`](./orchestration/config.d/workloads.yaml)

Current deployment presets:

- `deployment-approximate-prefix-cache`
- `deployment-precise-prefix-cache`
- `deployment-distributed-default`

Existing smoke presets extend those deployment presets and keep the model and smoke request
selection.

Model and deployment selection:

- local default behavior comes from `project.args: [smoke]`
- select a deployment preset on the `/test` line when you want preset defaults
- select one or more literal Hugging Face model names with `/var runtime.model_name: ...`
- select one or more deployment profiles with `/var runtime.deployment_profile: ...`
- select a benchmark workload with `/var runtime.benchmark_key: ...`

Example:

```text
/test fournos llm_d deployment-precise-prefix-cache
/cluster athena-fire
/var runtime.model_name: meta-llama/Llama-3.1-8B-Instruct
/var runtime.benchmark_key: heavy-heterogeneous
```

Matrix example:

```text
/test fournos llm_d smoke
/cluster athena-fire
/var runtime.model_name: [openai/gpt-oss-120b, Qwen/Qwen3-0.6B]
/var runtime.deployment_profile: [distributed-default, precise-prefix-cache]
/var runtime.benchmark_key: multi-turn
```

Benchmark adaptation notes:

- deployment profiles resolve from lightweight config plus manifest fragments
- benchmark workloads are adapted to the existing GuideLLM execution model
- rate-dependent benchmarks (args containing `{rate}` / `{N*rate}` / `{rate*N}`) expand into one GuideLLM run per rate
- plain multi-rate benchmarks (no `{rate}` expressions) stay a single GuideLLM invocation
- expressions such as `{2*rate}` and `{10*rate}` are resolved in those expanded runs

## Burn-in Phase

Before performance benchmarks, a **burn-in phase** runs to prime the KV cache and CUDA kernels.
This ensures stable, representative benchmark results by warming up the inference engine.

Configuration is in [`orchestration/config.d/burnin.yaml`](./orchestration/config.d/burnin.yaml):

| Setting | Default | Description |
|---------|---------|-------------|
| `burnin.enabled` | `true` | Enable/disable burn-in phase |
| `burnin.rate` | `200` | Concurrent streams during burn-in |
| `burnin.max_seconds` | `200` | Duration of burn-in in seconds |

Burn-in automatically skips for `smoke` tests (functional checks don't need warming).

To disable burn-in for a specific run:
```text
/var burnin.enabled: false
```
