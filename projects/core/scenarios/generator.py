"""Declarative scenario generation with split configuration and inheritance.

NOTE: This module is not currently wired to any CLI. The RHAIIS CLI uses
ConfigLoader directly with --model and --workloads flags. This generator
is available for future batch/matrix scenario execution if needed.

Supports:
1. Split config files: defaults.yaml, models.yaml, workloads.yaml
2. Deploy-once pattern: Deploy vLLM once, run multiple workloads
3. Config inheritance: defaults → accelerator → model → scenario
4. Matrix expansion: model × workloads × tensor_parallel

Example scenario format (if wired to CLI):
```yaml
scenarios:
  - model: qwen-0.6b        # Key from models.yaml
    workloads: [balanced, short]  # Keys from workloads.yaml
    tensor_parallel: [1]
```
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .config import ScenarioConfig

if TYPE_CHECKING:
    from .config_loader import ConfigLoader


@dataclass
class ModelConfig:
    """Model configuration from models.yaml."""

    key: str  # qwen-0.6b
    name: str  # Qwen3-0.6B
    hf_model_id: str  # Qwen/Qwen3-0.6B
    aliases: list[str] = field(default_factory=list)
    vllm_args: dict[str, Any] = field(default_factory=dict)
    env_vars: dict[str, str] = field(default_factory=dict)
    supported_workloads: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, key: str, data: dict[str, Any]) -> "ModelConfig":
        return cls(
            key=key,
            name=data.get("name", key),
            hf_model_id=data.get("hf_model_id", key),
            aliases=data.get("aliases", []),
            vllm_args=data.get("vllm_args", {}),
            env_vars=data.get("env_vars", {}),
            supported_workloads=data.get("supported_workloads", []),
        )


@dataclass
class WorkloadConfig:
    """Workload configuration from workloads.yaml."""

    key: str  # balanced
    name: str  # Balanced
    description: str = ""
    guidellm: dict[str, Any] = field(default_factory=dict)
    max_seconds: int = 300
    vllm_args: dict[str, Any] = field(default_factory=dict)  # Workload-specific overrides

    @classmethod
    def from_dict(cls, key: str, data: dict[str, Any]) -> "WorkloadConfig":
        return cls(
            key=key,
            name=data.get("name", key),
            description=data.get("description", ""),
            guidellm=data.get("guidellm", {}),
            max_seconds=data.get("max_seconds", 300),
            vllm_args=data.get("vllm_args", {}),
        )


@dataclass
class DeploymentGroup:
    """
    A group of workloads to run on a single vLLM deployment.

    Deploy vLLM once -> Run all workloads -> Cleanup

    Workloads with different vllm_args get separate deployment groups.
    """

    model: ModelConfig
    tensor_parallel: int
    routing: str
    workloads: list[WorkloadConfig]
    routing_config: dict[str, Any] = field(default_factory=dict)
    namespace: str = "forge"
    vllm_args_override: dict[str, Any] = field(default_factory=dict)  # From workload

    @property
    def deployment_id(self) -> str:
        """Unique ID for this deployment."""
        base = f"{self.model.key}_{self.routing}_tp{self.tensor_parallel}"
        if self.vllm_args_override:
            # Add hash suffix for workload-specific vllm_args
            override_hash = hash(frozenset(self.vllm_args_override.items())) % 10000
            return f"{base}_wl{override_hash}"
        return base

    @property
    def deployment_name(self) -> str:
        """K8s resource name."""
        return ScenarioConfig.sanitize_name(self.model.key)

    @property
    def merged_vllm_args(self) -> dict[str, Any]:
        """Model vllm_args merged with workload overrides."""
        merged = dict(self.model.vllm_args)
        merged.update(self.vllm_args_override)
        return merged


@dataclass
class ExpandedScenario:
    """A single expanded scenario from matrix."""

    model_id: str  # HuggingFace ID
    model_key: str  # Key from models.yaml
    model_short: str  # Short name for display
    workload: str  # balanced, short, etc.
    routing: str  # direct, prefix-estimation, etc.
    tensor_parallel: int  # TP size
    runtime_args: dict[str, Any]  # Merged runtime args
    workload_config: dict[str, Any]  # Workload settings
    routing_config: dict[str, Any]  # Routing settings
    deploy_config: dict[str, Any]  # Deployment settings

    @property
    def scenario_id(self) -> str:
        """Generate deterministic scenario ID."""
        return f"{self.model_short}_{self.workload}_{self.routing}_tp{self.tensor_parallel}"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "model_id": self.model_id,
            "model_key": self.model_key,
            "model_short": self.model_short,
            "workload": self.workload,
            "routing": self.routing,
            "tensor_parallel": self.tensor_parallel,
            "scenario_id": self.scenario_id,
            "runtime_args": self.runtime_args,
            "workload_config": self.workload_config,
            "routing_config": self.routing_config,
            "deploy_config": self.deploy_config,
        }

    def to_scenario_config(self, namespace: str = "forge") -> ScenarioConfig:
        """Convert to ScenarioConfig for workflow execution."""
        return ScenarioConfig(
            scenario_id=self.scenario_id,
            model_id=self.model_id,
            model_short=self.model_short,
            workload=self.workload,
            routing=self.routing,
            tensor_parallel=self.tensor_parallel,
            deployment_name=ScenarioConfig.sanitize_name(self.model_short),
            namespace=namespace,
            replicas=self.deploy_config.get("replicas", 1),
            runtime_args=self.runtime_args,
            workload_config=self.workload_config,
            routing_config=self.routing_config,
        )


@dataclass
class ParsedConfig:
    """Parsed scenario YAML configuration."""

    name: str
    description: str
    target_cluster: str = ""
    # Common defaults
    common: dict[str, Any] = field(default_factory=dict)
    # Workload definitions (inline or from workloads.yaml)
    workloads: dict[str, WorkloadConfig] = field(default_factory=dict)
    # Routing definitions
    routing: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Model registry (from models.yaml)
    models: dict[str, ModelConfig] = field(default_factory=dict)
    # New: Scenario list (references model keys)
    scenarios: list[dict[str, Any]] = field(default_factory=list)
    # Legacy: Explicit run list
    runs: list[dict[str, Any]] = field(default_factory=list)


class ScenarioGenerator:
    """
    Generate scenarios from declarative configuration.

    Supports:
    - Split config: defaults.yaml + models.yaml + workloads.yaml + scenarios/*.yaml
    - Deploy-once pattern: Group workloads under single deployment
    - Config inheritance via ConfigLoader: defaults → accelerator → model → scenario
    - Legacy inline: All config in single file
    """

    def __init__(
        self,
        scenarios_path: str | Path | None = None,
        models_path: str | Path | None = None,
        workloads_path: str | Path | None = None,
        config_dir: str | Path | None = None,
        config_loader: ConfigLoader | None = None,
        accelerator: str = "nvidia",
    ):
        """
        Initialize generator.

        Args:
            scenarios_path: Path to scenarios/*.yaml file
            models_path: Path to models.yaml (optional, auto-detected from config_dir)
            workloads_path: Path to workloads.yaml (optional, auto-detected)
            config_dir: Config directory (auto-detects models.yaml, workloads.yaml)
            config_loader: Optional ConfigLoader for inheritance-based resolution
            accelerator: Accelerator type ('nvidia', 'amd') for inheritance
        """
        self.scenarios_path = Path(scenarios_path) if scenarios_path else None
        self.models_path = Path(models_path) if models_path else None
        self.workloads_path = Path(workloads_path) if workloads_path else None
        self.accelerator = accelerator

        # Auto-detect config paths from config_dir or scenarios_path parent
        if config_dir:
            config_dir = Path(config_dir)
        elif scenarios_path:
            # Handle scenarios in subdirectory: config/projects/rhaiis.yaml -> config/
            scenarios_parent = Path(scenarios_path).parent
            if scenarios_parent.name == "projects":
                config_dir = scenarios_parent.parent
            else:
                config_dir = scenarios_parent

        self.config_dir = config_dir

        if config_dir:
            if not self.models_path and (config_dir / "models.yaml").exists():
                self.models_path = config_dir / "models.yaml"
            if not self.workloads_path and (config_dir / "workloads.yaml").exists():
                self.workloads_path = config_dir / "workloads.yaml"

        # Use provided ConfigLoader or create one if config_dir is available
        self.config_loader = config_loader
        if not self.config_loader and config_dir:
            from .config_loader import ConfigLoader
            self.config_loader = ConfigLoader(config_dir, accelerator=accelerator)

        self.config: ParsedConfig | None = None

    def load(self, path: str | Path | None = None) -> ParsedConfig:
        """
        Load and parse scenario configuration.

        Loads from:
        1. models.yaml (if exists) -> model registry
        2. workloads.yaml (if exists) -> workload profiles
        3. scenarios-*.yaml -> scenario definitions

        Args:
            path: Optional path override for scenarios file

        Returns:
            Parsed configuration
        """
        scenarios_path = Path(path) if path else self.scenarios_path
        if not scenarios_path:
            raise ValueError("No scenarios config path provided")

        # Load models registry
        models: dict[str, ModelConfig] = {}
        if self.models_path and self.models_path.exists():
            with open(self.models_path) as f:
                models_data = yaml.safe_load(f)
            for key, data in models_data.get("models", {}).items():
                models[key] = ModelConfig.from_dict(key, data)

        # Load workloads
        workloads: dict[str, WorkloadConfig] = {}
        if self.workloads_path and self.workloads_path.exists():
            with open(self.workloads_path) as f:
                workloads_data = yaml.safe_load(f)
            for key, data in workloads_data.get("workloads", {}).items():
                workloads[key] = WorkloadConfig.from_dict(key, data)

        # Load scenarios
        with open(scenarios_path) as f:
            data = yaml.safe_load(f)

        # Merge inline workloads (if any) with loaded workloads
        for key, wl_data in data.get("workloads", {}).items():
            if key not in workloads:
                workloads[key] = WorkloadConfig.from_dict(key, wl_data)

        # Merge inline models with loaded models
        # Supports both new format (hf_model_id, vllm_args) and legacy (runtime_args)
        for model_key, model_data in data.get("models", {}).items():
            if model_key not in models:
                # Check for new format fields
                hf_model_id = model_data.get("hf_model_id", model_key)
                vllm_args = model_data.get("vllm_args") or model_data.get("runtime_args", {})
                name = model_data.get("name") or model_data.get("deploy", {}).get("name", model_key)

                models[model_key] = ModelConfig.from_dict(
                    key=model_key,
                    data={
                        "hf_model_id": hf_model_id,
                        "name": name,
                        "vllm_args": vllm_args,
                        "env_vars": model_data.get("env_vars", {}),
                    },
                )

        self.config = ParsedConfig(
            name=data.get("name", scenarios_path.stem),
            description=data.get("description", ""),
            target_cluster=data.get("target_cluster", ""),
            common=data.get("common", {}),
            workloads=workloads,
            routing=data.get("routing", {}),
            models=models,
            scenarios=data.get("scenarios", []),
            runs=data.get("runs", []),
        )

        return self.config

    def expand(self) -> list[ExpandedScenario]:
        """
        Expand all scenarios into individual benchmark runs.

        Returns:
            List of ExpandedScenario objects
        """
        if not self.config:
            raise RuntimeError("Must call load() first")

        expanded = []

        # New format: scenarios list with model key references
        for scenario_def in self.config.scenarios:
            scenarios = self._expand_scenario_def(scenario_def)
            expanded.extend(scenarios)

        # Legacy format: models with inline matrix
        for model_id, model_config in self.config.models.items():
            if isinstance(model_config, ModelConfig):
                continue  # Skip, already processed via scenarios
            # Legacy dict format
            matrix = model_config.get("matrix", {}) if isinstance(model_config, dict) else {}
            if matrix:
                scenarios = self._expand_legacy_model_matrix(model_id, model_config)
                expanded.extend(scenarios)

        # Explicit runs (no matrix expansion)
        for run in self.config.runs:
            scenario = self._create_from_run(run)
            if scenario:
                expanded.append(scenario)

        return expanded

    def expand_grouped(self) -> list[DeploymentGroup]:
        """
        Expand scenarios grouped by deployment.

        Returns groups where each group shares a single vLLM deployment.
        Deploy once -> Run all workloads in group -> Cleanup

        Uses ConfigLoader when available for full inheritance chain.

        Returns:
            List of DeploymentGroup objects
        """
        if not self.config:
            raise RuntimeError("Must call load() first")

        groups: dict[str, DeploymentGroup] = {}

        for scenario_def in self.config.scenarios:
            model_key = scenario_def.get("model")
            if not model_key or model_key not in self.config.models:
                continue

            workload_keys = scenario_def.get("workloads", ["balanced"])
            routings = scenario_def.get("routing", ["direct"])
            tp_values = scenario_def.get("tensor_parallel", [1])
            namespace = self.config.common.get("namespace", "forge")

            # Use ConfigLoader for resolved model config if available
            if self.config_loader:
                try:
                    resolved_model = self.config_loader.load_model(model_key)
                    # Create a ModelConfig-compatible object with resolved values
                    model = ModelConfig(
                        key=resolved_model.key,
                        name=resolved_model.name,
                        hf_model_id=resolved_model.hf_model_id,
                        aliases=resolved_model.aliases,
                        vllm_args=resolved_model.vllm_args,
                        env_vars=resolved_model.env_vars,
                        supported_workloads=resolved_model.supported_workloads,
                    )
                except KeyError:
                    model = self.config.models[model_key]
            else:
                model = self.config.models[model_key]

            # Create groups for each (model, routing, tp, vllm_args) combination
            # Workloads with different vllm_args get separate deployment groups
            for routing, tp in product(routings, tp_values):
                # Group workloads by their vllm_args
                workloads_by_vllm_args: dict[tuple, list[WorkloadConfig]] = {}

                for wl_key in workload_keys:
                    if wl_key not in self.config.workloads:
                        continue
                    wl = self.config.workloads[wl_key]
                    # Create hashable key from vllm_args
                    vllm_args_key = tuple(sorted(wl.vllm_args.items())) if wl.vllm_args else ()
                    if vllm_args_key not in workloads_by_vllm_args:
                        workloads_by_vllm_args[vllm_args_key] = []
                    workloads_by_vllm_args[vllm_args_key].append(wl)

                # Create a deployment group for each unique vllm_args
                for vllm_args_key, workloads in workloads_by_vllm_args.items():
                    vllm_args_override = dict(vllm_args_key) if vllm_args_key else {}

                    # Include vllm_args hash in group_id for uniqueness
                    if vllm_args_override:
                        override_hash = hash(vllm_args_key) % 10000
                        group_id = f"{model_key}_{routing}_tp{tp}_wl{override_hash}"
                    else:
                        group_id = f"{model_key}_{routing}_tp{tp}"

                    if group_id not in groups:
                        groups[group_id] = DeploymentGroup(
                            model=model,
                            tensor_parallel=tp,
                            routing=routing,
                            workloads=workloads,
                            routing_config=self.config.routing.get(routing, {}),
                            namespace=namespace,
                            vllm_args_override=vllm_args_override,
                        )
                    else:
                        # Add more workloads to existing group
                        for wl in workloads:
                            if wl not in groups[group_id].workloads:
                                groups[group_id].workloads.append(wl)

        return list(groups.values())

    def _expand_scenario_def(self, scenario_def: dict[str, Any]) -> list[ExpandedScenario]:
        """Expand a scenario definition from the new format.

        Uses ConfigLoader when available for full inheritance chain:
        defaults → accelerator → model → model.accelerator_overrides → scenario
        """
        model_key = scenario_def.get("model")
        if not model_key or model_key not in self.config.models:
            return []

        workload_keys = scenario_def.get("workloads", ["balanced"])
        routings = scenario_def.get("routing", ["direct"])
        tp_values = scenario_def.get("tensor_parallel", [1])
        vllm_args_override = scenario_def.get("vllm_args_override", {})

        scenarios = []
        namespace = self.config.common.get("namespace", "forge")

        # Use ConfigLoader for full inheritance if available
        if self.config_loader:
            try:
                resolved_model = self.config_loader.load_model(model_key)
                model_id = resolved_model.hf_model_id
                base_vllm_args = dict(resolved_model.vllm_args)
                env_vars = dict(resolved_model.env_vars)
                deploy_config_base = dict(resolved_model.deploy)
            except KeyError:
                # Fall back to basic model config
                model = self.config.models[model_key]
                model_id = model.hf_model_id
                base_vllm_args = dict(model.vllm_args)
                env_vars = dict(model.env_vars)
                deploy_config_base = {}
        else:
            model = self.config.models[model_key]
            model_id = model.hf_model_id
            base_vllm_args = dict(model.vllm_args)
            env_vars = dict(model.env_vars)
            deploy_config_base = {}

        for workload_key, routing, tp in product(workload_keys, routings, tp_values):
            # Resolve workload config
            if self.config_loader:
                try:
                    resolved_workload = self.config_loader.load_workload(workload_key)
                    workload_guidellm = resolved_workload.guidellm
                except KeyError:
                    workload_config = self.config.workloads.get(workload_key)
                    if not workload_config:
                        continue
                    workload_guidellm = workload_config.guidellm
            else:
                workload_config = self.config.workloads.get(workload_key)
                if not workload_config:
                    continue
                workload_guidellm = workload_config.guidellm

            # Build runtime args with inheritance
            runtime_args = dict(base_vllm_args)
            runtime_args["tensor-parallel-size"] = tp
            runtime_args.update(vllm_args_override)

            scenario = ExpandedScenario(
                model_id=model_id,
                model_key=model_key,
                model_short=self._shorten_model_name(model_key),
                workload=workload_key,
                routing=routing,
                tensor_parallel=tp,
                runtime_args=runtime_args,
                workload_config=workload_guidellm,
                routing_config=self.config.routing.get(routing, {}),
                deploy_config={
                    "namespace": namespace,
                    "replicas": self.config.common.get("replicas", 1),
                    "num_gpus": deploy_config_base.get("num_gpus", tp),
                    "env_vars": env_vars,
                },
            )
            scenarios.append(scenario)

        return scenarios

    def _expand_legacy_model_matrix(
        self,
        model_id: str,
        model_config: dict[str, Any],
    ) -> list[ExpandedScenario]:
        """Expand a model's matrix (legacy inline format)."""
        matrix = model_config.get("matrix", {})
        deploy_config = model_config.get("deploy", {})

        workloads = matrix.get("workloads", ["balanced"])
        routings = matrix.get("routing", ["direct"])
        tp_values = matrix.get("tensor-parallel-size", [1])

        common_runtime = self.config.common.get("runtime_args", {})
        model_runtime = model_config.get("runtime_args", {})

        scenarios = []

        for workload, routing, tp in product(workloads, routings, tp_values):
            runtime_args = dict(common_runtime)
            runtime_args.update(model_runtime)
            runtime_args["tensor-parallel-size"] = tp

            workload_config = self.config.workloads.get(workload)
            wl_dict = workload_config.guidellm if workload_config else {}

            routing_config = self.config.routing.get(routing, {})

            model_short = self._shorten_model_name(model_id)

            scenario_deploy_config = dict(deploy_config)
            scenario_deploy_config["num_gpus"] = tp

            scenario = ExpandedScenario(
                model_id=model_id,
                model_key=model_id,
                model_short=model_short,
                workload=workload,
                routing=routing,
                tensor_parallel=tp,
                runtime_args=runtime_args,
                workload_config=wl_dict,
                routing_config=routing_config,
                deploy_config=scenario_deploy_config,
            )
            scenarios.append(scenario)

        return scenarios

    def _create_from_run(self, run: dict[str, Any]) -> ExpandedScenario | None:
        """Create scenario from explicit run definition."""
        model_key = run.get("model")
        if not model_key:
            return None

        model = self.config.models.get(model_key)
        if not model:
            return None

        workload = run.get("workload", "balanced")
        routing = run.get("routing", "direct")
        tp = run.get("tensor_parallel", 1 if isinstance(model, ModelConfig) else 1)

        runtime_args = dict(model.vllm_args) if isinstance(model, ModelConfig) else {}
        runtime_args.update(run.get("runtime_args_override", {}))
        runtime_args["tensor-parallel-size"] = tp

        workload_config = self.config.workloads.get(workload)

        return ExpandedScenario(
            model_id=model.hf_model_id if isinstance(model, ModelConfig) else model_key,
            model_key=model_key,
            model_short=self._shorten_model_name(model_key),
            workload=workload,
            routing=routing,
            tensor_parallel=tp,
            runtime_args=runtime_args,
            workload_config=workload_config.guidellm if workload_config else {},
            routing_config=self.config.routing.get(routing, {}),
            deploy_config={
                "namespace": self.config.common.get("namespace", "forge"),
                "replicas": 1,
            },
        )

    @staticmethod
    def _shorten_model_name(model_id: str) -> str:
        """Create short model name from model key or HuggingFace ID."""
        name = model_id.split("/")[-1].lower()
        name = re.sub(r"-instruct.*", "", name)
        name = re.sub(r"-dynamic$", "", name)
        name = re.sub(r"-a\d+b", "", name)
        name = re.sub(r"[^a-z0-9]+", "-", name)
        name = name.strip("-")
        if len(name) > 40:
            name = name[:40].rstrip("-")
        return name

    def summary(self) -> str:
        """Generate summary of scenarios."""
        if not self.config:
            return "No config loaded"

        expanded = self.expand()
        groups = self.expand_grouped()

        lines = [
            f"Scenario Config: {self.config.name}",
            f"Description: {self.config.description}",
            f"Target Cluster: {self.config.target_cluster or '(not set)'}",
            f"Models: {len(self.config.models)}",
            f"Workloads: {len(self.config.workloads)}",
            f"Deployment Groups: {len(groups)}",
            f"Total Benchmark Runs: {len(expanded)}",
            "",
        ]

        # Show deployment groups
        lines.append("Deployment Groups (deploy once, run N workloads):")
        for group in groups:
            wl_names = ", ".join(wl.key for wl in group.workloads)
            lines.append(f"  {group.deployment_id}:")
            lines.append(f"    Model: {group.model.hf_model_id}")
            lines.append(f"    Workloads: [{wl_names}]")

        return "\n".join(lines)
