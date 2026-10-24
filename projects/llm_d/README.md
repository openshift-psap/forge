# llm_d

`llm_d` is the Forge project for validating downstream llm-d on RHOAI.

The current implementation is intentionally narrow:

- target only downstream `LLMInferenceService`
- keep the public interface compatible with current Fournos phase execution
- use checked-in config chunks and manifests instead of a large mutable config surface

Configuration layout:

- project config chunk: [`orchestration/config.d/project.yaml`](./orchestration/config.d/project.yaml)
- config chunks: [`orchestration/config.d`](./orchestration/config.d)
- optional Thanos config: [`orchestration/config.d/observability.yaml`](./orchestration/config.d/observability.yaml)
- presets: [`orchestration/presets.d`](./orchestration/presets.d)
- manifests: [`orchestration/manifests`](./orchestration/manifests)

Main entrypoints:

- CI phase wrapper: [`orchestration/ci.py`](./orchestration/ci.py)
- CLI wrapper: [`orchestration/cli.py`](./orchestration/cli.py)
- Shared runtime/config loader: [`orchestration/runtime_config.py`](./orchestration/runtime_config.py)
- Prepare flow: [`orchestration/prepare_sequence.py`](./orchestration/prepare_sequence.py)
- Test flow: [`orchestration/test_phase.py`](./orchestration/test_phase.py)
- Cleanup flow: [`orchestration/cleanup_phase.py`](./orchestration/cleanup_phase.py)

## Optional Thanos metrics

`observability.enabled` is false by default. In a Fournos job, enable it in
`configOverrides`; Forge uses that job's `FJOB_NAME` as the run ID:

```yaml
observability.enabled: true
observability.cluster_label: psap-hera
```

For local runs, set `observability.run_id` explicitly. The run ID must match
`FJOB_NAME` in Fournos, and per-run Receiver teardown requires the `forge-full`
pipeline. Forge uses the configured `runtime.namespace` and claims it for the
duration of the run; two Forge runs sharing that namespace must run one at a
time. Presets that select `kpouget-dev` or `kevin-dev` still use those
namespaces, so any required image pull Secret must already be available there.
An observability-enabled run must use one workload namespace across all llm_d
run specifications. `monitoring.namespace_regex: null` selects only that
namespace.

Prepare creates a run-scoped Receiver and remote-write queues. The system
container filter is `main`. Keep `deployments.defaults.enable_monitors: true`:
the llm_d deployment creates its own HTTPS workload and HTTP scheduler
ServiceMonitors. GPU forwarding remains off by default because DCGM series
without a matching `exported_namespace` cannot be attributed safely. The
`psap-forge-thanos` vault must contain `objstore.yaml` with the S3 configuration. Post-cleanup
stops the run's workload, drains its queues, uploads remaining blocks, then
removes the Receiver and its PVC. If shipping cannot be confirmed, teardown
reports an error and preserves the data for a retry. The central Querier reads
uploaded blocks through its Store Gateway. If workload cleanup fails, Forge
skips teardown so the Receiver and queues remain available for a retry.

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
- Features such as pre-warmup and env passthrough are not modeled yet
