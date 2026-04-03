# llm-d Integration Extensibility Report

Analysis of how the current RHAIIS workflow implementation can be extended to support llm-d benchmarking use cases.

## Executive Summary

The current workflow engine (`projects/core/workflow/`) is **highly extensible** for llm-d. The abstract `WorkflowStep` and `Workflow` interfaces allow llm-d to define its own deployment steps while reusing shared steps (GuideLLM, artifact collection). Key differences are in the **deployment layer**, not the workflow engine itself.

## llm-d vs RHAIIS Comparison

| Aspect | RHAIIS | llm-d |
|--------|--------|-------|
| **Deployment** | KServe (ServingRuntime + InferenceService) | Helm + Helmfile (model-service, gaie-scheduler, infra) |
| **Networking** | KServe service (`{name}-predictor.{ns}`) | Gateway API + HTTPRoute |
| **Routing** | Direct to vLLM pod | EPP router with scheduling strategies |
| **Scheduling** | None (direct inference) | GAIE scheduler (prefix-aware, disaggregated) |
| **Config** | YAML (models.yaml, defaults.yaml) | Helmfile values + routing configs |

## Reusable Components

### Fully Reusable (No Changes)
```
projects/core/
├── workflow/
│   ├── step.py          # WorkflowStep abstract class
│   ├── workflow.py      # Workflow abstract class
│   ├── context.py       # WorkflowContext
│   └── executor.py      # SequentialExecutor
├── steps/
│   ├── guidellm.py      # RunGuideLLMStep (endpoint agnostic)
│   └── artifacts.py     # CollectArtifactsStep, CleanupDeploymentStep
└── scenarios/
    └── config_loader.py # ConfigLoader (model + workload resolution)
```

### Requires llm-d-Specific Implementation
```
projects/llm_d/
├── workflows/
│   ├── steps/
│   │   ├── deploy_helm.py     # DeployHelmStep (model-service, gaie, infra)
│   │   ├── configure_epp.py   # ConfigureEPPStep (routing strategy)
│   │   └── wait_gateway.py    # WaitForGatewayStep
│   ├── benchmark.py           # LlmdBenchmarkWorkflow
│   └── prepare.py             # LlmdPrepareWorkflow (install operators)
└── orchestration/
    ├── cli.py                 # llm-d CLI (similar structure to RHAIIS)
    └── test_llmd.py           # llm-d orchestration (run_test, etc.)
```

## Proposed llm-d Step Implementations

### 1. DeployHelmStep
```python
class DeployHelmStep(WorkflowStep):
    """Deploy llm-d components via Helm/Helmfile."""

    def __init__(
        self,
        model: str,
        routing_mode: str,  # direct, prefix-estimation, pd-disaggregation
        helmfile_path: str,
        namespace: str,
    ):
        ...

    def execute(self, ctx: WorkflowContext) -> StepResult:
        # helmfile apply -f {helmfile_path} --state-values-set model={model}
        # Returns: gateway_url, epp_endpoint
```

### 2. ConfigureEPPStep
```python
class ConfigureEPPStep(WorkflowStep):
    """Configure EPP routing strategy."""

    def __init__(
        self,
        routing_mode: str,
        epp_namespace: str,
    ):
        ...

    def execute(self, ctx: WorkflowContext) -> StepResult:
        # Patch EPP ConfigMap with routing config
        # Wait for EPP pods to reload
```

### 3. WaitForGatewayStep
```python
class WaitForGatewayStep(WorkflowStep):
    """Wait for K8s Gateway + HTTPRoute to be ready."""

    def __init__(
        self,
        gateway_name: str,
        namespace: str,
    ):
        ...

    def execute(self, ctx: WorkflowContext) -> StepResult:
        # Check Gateway status
        # Verify HTTPRoute attached
        # Health check endpoint
```

## Proposed LlmdBenchmarkWorkflow

```python
class LlmdBenchmarkWorkflow(Workflow):
    """llm-d benchmark: deploy via Helm → configure EPP → run GuideLLM → cleanup."""

    def __init__(
        self,
        ctx: WorkflowContext,
        model: str,
        routing_mode: str,  # direct, prefix-estimation, prefix-precise, pd-disaggregation
        workload: str,
        namespace: str,
    ):
        ...

    def define_steps(self):
        # Deploy model-service + GAIE scheduler + infra via Helm
        self.add_step(DeployHelmStep(
            model=self.model,
            routing_mode=self.routing_mode,
            helmfile_path=self._get_helmfile(),
            namespace=self.namespace,
        ))

        # Configure EPP routing strategy
        self.add_step(ConfigureEPPStep(
            routing_mode=self.routing_mode,
            epp_namespace=self.namespace,
        ))

        # Wait for Gateway API + HTTPRoute
        self.add_step(WaitForGatewayStep(
            gateway_name=f"{self.model}-gateway",
            namespace=self.namespace,
        ))

        # Run GuideLLM (reused from core)
        gateway_endpoint = f"http://{self.model}-gateway.{self.namespace}.svc:8080/v1"
        self.add_step(RunGuideLLMStep(
            endpoint=gateway_endpoint,
            model=self.model,
            namespace=self.namespace,
            workload=self.workload,
        ))

        # Cleanup (reused from core, modified for Helm)
        self.add_finally(CollectArtifactsStep(
            app_label="llm-d",
            namespace=self.namespace,
        ))
        self.add_finally(HelmCleanupStep(
            namespace=self.namespace,
        ))
```

## Config Structure for llm-d

```yaml
# config/llm-d/defaults.yaml
defaults:
  deploy:
    namespace: llm-d
    helmfile_path: deploy/llm-d/helmfile.yaml

  routing:
    default_mode: direct
    modes:
      direct: {}
      prefix-estimation:
        scheduler: gaie
        prefix_cache: redis
      pd-disaggregation:
        prefill_replicas: 2
        decode_replicas: 4

# config/llm-d/models.yaml
models:
  llama-3.1-8b:
    hf_model_id: meta-llama/Llama-3.1-8B-Instruct
    supported_routing: [direct, prefix-estimation]
    helm_values:
      vllm:
        tensor_parallel: 1
```

## Implementation Roadmap

### Phase 1: Step Implementations (2-3 days)
- [ ] `DeployHelmStep` - Helm/Helmfile deployment
- [ ] `WaitForGatewayStep` - Gateway API readiness
- [ ] `HelmCleanupStep` - Helm uninstall

### Phase 2: EPP Integration (2-3 days)
- [ ] `ConfigureEPPStep` - Routing strategy configuration
- [ ] EPP config templates (prefix-estimation, pd-disaggregation)

### Phase 3: Workflow + CLI (1-2 days)
- [ ] `LlmdBenchmarkWorkflow`
- [ ] `projects/llm_d/orchestration/cli.py`
- [ ] `projects/llm_d/orchestration/test_llmd.py`

### Phase 4: Config + Testing (1-2 days)
- [ ] llm-d config files (defaults, models, routing modes)
- [ ] Integration tests

## Gaps in Current Implementation

| Gap | Impact | Resolution |
|-----|--------|------------|
| No Helm support | High | Create `DeployHelmStep` |
| No Gateway API support | High | Create `WaitForGatewayStep` |
| No EPP routing config | High | Create `ConfigureEPPStep` |
| RHAIIS-specific in deploy.py | Low | Already isolated in `rhaiis/workflows/steps/` |
| GuideLLM assumes OpenAI endpoint | None | Already generic (`--target` flag) |

## Conclusion

The workflow engine architecture is **well-suited** for llm-d extension:

1. **Clean separation**: Core workflow engine (`projects/core/`) is deployment-agnostic
2. **Step abstraction**: New steps (Helm, Gateway) implement same `WorkflowStep` interface
3. **Reusable components**: GuideLLM, artifact collection work unchanged
4. **Config system**: `ConfigLoader` can be extended with llm-d-specific configs

Estimated effort: **6-10 developer days** for full llm-d integration with routing modes.
