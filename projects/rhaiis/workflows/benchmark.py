"""RHAIIS benchmark workflow.

Deploy vLLM -> Run GuideLLM -> Collect Artifacts
"""

from projects.core.steps import CleanupDeploymentStep, CollectArtifactsStep, RunGuideLLMStep
from projects.core.workflow import Workflow, WorkflowContext
from projects.rhaiis.workflows.steps import DeployVLLMStep, WaitForReadyStep


class BenchmarkWorkflow(Workflow):
    """
    RHAIIS benchmark workflow: deploy vLLM, run benchmark, cleanup.

    Steps:
    1. deploy: Deploy vLLM serving
    2. wait: Wait for deployment to be ready
    3. benchmark: Run GuideLLM benchmark

    Finally:
    1. collect_artifacts: Collect logs and events
    2. cleanup: Delete deployment
    """

    def __init__(
        self,
        ctx: WorkflowContext,
        model: str,
        workload: str = "balanced",
        vllm_image: str = "",
        runtime_args: dict | None = None,
        tensor_parallel: int = 1,
        max_requests: int = 100,
        namespace: str = "forge",
        env_vars: dict | None = None,
    ):
        """
        Initialize benchmark workflow.

        Args:
            ctx: Workflow context
            model: HuggingFace model ID
            workload: GuideLLM workload type
            vllm_image: vLLM container image (from config)
            runtime_args: vLLM runtime arguments (from config)
            tensor_parallel: Number of GPUs for tensor parallelism
            max_requests: Maximum requests for benchmark
            namespace: Kubernetes namespace
            env_vars: Environment variables for vLLM (from config)
        """
        super().__init__(ctx)
        self.model = model
        self.workload = workload
        self.vllm_image = vllm_image or ctx.get_env("VLLM_IMAGE", "")
        self.runtime_args = runtime_args or {}
        self.tensor_parallel = tensor_parallel
        self.max_requests = max_requests
        self.namespace = namespace
        self.env_vars = env_vars or {}

        # Generate deployment name from model
        self.deployment_name = self._sanitize_name(model)

    def define_steps(self):
        """Define workflow steps."""
        # Deploy vLLM
        self.add_step(
            DeployVLLMStep(
                model=self.model,
                deployment_name=self.deployment_name,
                vllm_image=self.vllm_image,
                runtime_args=self.runtime_args,
                tensor_parallel=self.tensor_parallel,
                namespace=self.namespace,
                env_vars=self.env_vars,
            )
        )

        # Wait for deployment to be ready (3600s = 1 hour for large models)
        self.add_step(
            WaitForReadyStep(
                deployment_name=self.deployment_name,
                namespace=self.namespace,
                timeout_seconds=3600,
            )
        )

        # Run GuideLLM benchmark (as a pod on cluster)
        # KServe RawDeployment mode creates service named {name}-predictor
        endpoint = f"http://{self.deployment_name}-predictor.{self.namespace}.svc.cluster.local:8080/v1"
        self.add_step(
            RunGuideLLMStep(
                endpoint=endpoint,
                model=self.model,
                namespace=self.namespace,
                workload=self.workload,
                max_requests=self.max_requests,
            )
        )

        # Finally: collect artifacts (always runs)
        self.add_finally(
            CollectArtifactsStep(
                app_label=self.deployment_name,
                namespace=self.namespace,
            )
        )

        # Finally: cleanup deployment (always runs)
        self.add_finally(
            CleanupDeploymentStep(
                deployment_name=self.deployment_name,
                namespace=self.namespace,
            )
        )

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """Sanitize model name for K8s resource naming."""
        name = name.split("/")[-1].lower()
        name = name.replace(".", "-").replace("_", "-")
        return name[:42]
