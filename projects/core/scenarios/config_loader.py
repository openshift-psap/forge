"""Config loading with inheritance and accelerator support.

Resolution order:
    defaults.yaml (base)
      ↓ merge
    defaults.yaml.accelerators[accelerator]
      ↓ merge
    models.yaml[model]
      ↓ merge
    models.yaml[model].accelerator_overrides[accelerator]
      ↓ merge
    scenarios/*.yaml.defaults
      ↓ merge
    scenarios/*.yaml.runs[].overrides
"""

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def deep_merge(base: dict, override: dict) -> dict:
    """
    Deep merge two dictionaries.

    Values in override take precedence. Nested dicts are merged recursively.
    Lists are replaced (not merged).
    """
    result = deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


@dataclass
class ResolvedModelConfig:
    """Fully resolved model configuration after inheritance."""

    key: str
    name: str
    hf_model_id: str
    deploy: dict[str, Any] = field(default_factory=dict)
    vllm_args: dict[str, Any] = field(default_factory=dict)
    env_vars: dict[str, str] = field(default_factory=dict)
    supported_workloads: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)

    @property
    def num_gpus(self) -> int:
        """Number of GPUs from deploy config or tensor_parallel."""
        return self.deploy.get("num_gpus", self.vllm_args.get("tensor-parallel-size", 1))

    @property
    def tensor_parallel(self) -> int:
        """Tensor parallel size from vllm_args."""
        return self.vllm_args.get("tensor-parallel-size", 1)


@dataclass
class ResolvedWorkloadConfig:
    """Resolved workload configuration."""

    key: str
    name: str
    description: str = ""
    guidellm: dict[str, Any] = field(default_factory=dict)
    max_seconds: int = 300
    vllm_args: dict[str, Any] = field(default_factory=dict)  # Workload-specific overrides


class ConfigLoader:
    """
    Load and resolve configurations with inheritance.

    Usage:
        loader = ConfigLoader('config/', accelerator='nvidia')
        model_config = loader.load_model('llama-3.3-70b-fp8')
        workload_config = loader.load_workload('balanced')
    """

    def __init__(
        self,
        config_dir: str | Path,
        accelerator: str = "nvidia",
    ):
        """
        Initialize config loader.

        Args:
            config_dir: Directory containing defaults.yaml, models.yaml, workloads.yaml
            accelerator: Accelerator type ('nvidia', 'amd')
        """
        self.config_dir = Path(config_dir)
        self.accelerator = accelerator

        # Cache loaded configs
        self._defaults: dict[str, Any] | None = None
        self._models: dict[str, Any] | None = None
        self._workloads: dict[str, Any] | None = None

    @property
    def defaults(self) -> dict[str, Any]:
        """Load and cache defaults.yaml."""
        if self._defaults is None:
            defaults_path = self.config_dir / "defaults.yaml"
            if defaults_path.exists():
                with open(defaults_path) as f:
                    self._defaults = yaml.safe_load(f) or {}
            else:
                self._defaults = {}
        return self._defaults

    @property
    def models(self) -> dict[str, Any]:
        """Load and cache models.yaml."""
        if self._models is None:
            models_path = self.config_dir / "models.yaml"
            if models_path.exists():
                with open(models_path) as f:
                    data = yaml.safe_load(f) or {}
                    self._models = data.get("models", {})
            else:
                self._models = {}
        return self._models

    @property
    def workloads(self) -> dict[str, Any]:
        """Load and cache workloads.yaml."""
        if self._workloads is None:
            workloads_path = self.config_dir / "workloads.yaml"
            if workloads_path.exists():
                with open(workloads_path) as f:
                    data = yaml.safe_load(f) or {}
                    self._workloads = data.get("workloads", {})
            else:
                self._workloads = {}
        return self._workloads

    def get_accelerator_defaults(self) -> dict[str, Any]:
        """Get accelerator-specific defaults."""
        accelerators = self.defaults.get("accelerators", {})
        return accelerators.get(self.accelerator, {})

    def get_global_defaults(self) -> dict[str, Any]:
        """Get global defaults (deploy, vllm_args, guidellm)."""
        return self.defaults.get("defaults", {})

    def load_model(self, model_key: str) -> ResolvedModelConfig:
        """
        Load and resolve a model configuration.

        Applies inheritance:
            defaults → accelerator_defaults → model → model.accelerator_overrides

        Args:
            model_key: Model key from models.yaml, alias, or HuggingFace ID

        Returns:
            Fully resolved model configuration

        Raises:
            KeyError: If model not found
        """
        model_data = self._find_model(model_key)
        if model_data is None:
            raise KeyError(f"Model '{model_key}' not found in registry")

        actual_key, raw_config = model_data

        # Start with global defaults
        global_defaults = self.get_global_defaults()
        base_deploy = global_defaults.get("deploy", {})
        base_vllm_args = global_defaults.get("vllm_args", {})

        # Merge accelerator defaults
        accel_defaults = self.get_accelerator_defaults()
        accel_vllm_args = accel_defaults.get("vllm_args", {})
        accel_env_vars = accel_defaults.get("env_vars", {})

        # Merge model config
        model_deploy = raw_config.get("deploy", {})
        model_vllm_args = raw_config.get("vllm_args", {})
        model_env_vars = raw_config.get("env_vars", {})

        # Merge accelerator overrides from model
        accel_overrides = raw_config.get("accelerator_overrides", {}).get(self.accelerator, {})
        override_vllm_args = accel_overrides.get("vllm_args", {})
        override_env_vars = accel_overrides.get("env_vars", {})

        # Build final config through inheritance chain
        final_deploy = deep_merge(base_deploy, model_deploy)
        final_vllm_args = deep_merge(
            deep_merge(deep_merge(base_vllm_args, accel_vllm_args), model_vllm_args),
            override_vllm_args,
        )
        final_env_vars = deep_merge(
            deep_merge(accel_env_vars, model_env_vars),
            override_env_vars,
        )

        return ResolvedModelConfig(
            key=actual_key,
            name=raw_config.get("name", actual_key),
            hf_model_id=raw_config.get("hf_model_id", actual_key),
            deploy=final_deploy,
            vllm_args=final_vllm_args,
            env_vars=final_env_vars,
            supported_workloads=raw_config.get("supported_workloads", []),
            aliases=raw_config.get("aliases", []),
        )

    def load_workload(self, workload_key: str) -> ResolvedWorkloadConfig:
        """
        Load and resolve a workload configuration.

        Args:
            workload_key: Workload key from workloads.yaml

        Returns:
            Resolved workload configuration

        Raises:
            KeyError: If workload not found
        """
        if workload_key not in self.workloads:
            raise KeyError(f"Workload '{workload_key}' not found")

        raw_config = self.workloads[workload_key]

        # Merge with guidellm defaults
        global_defaults = self.get_global_defaults()
        base_guidellm = global_defaults.get("guidellm", {})
        workload_guidellm = raw_config.get("guidellm", {})
        final_guidellm = deep_merge(base_guidellm, workload_guidellm)

        return ResolvedWorkloadConfig(
            key=workload_key,
            name=raw_config.get("name", workload_key),
            description=raw_config.get("description", ""),
            guidellm=final_guidellm,
            max_seconds=raw_config.get("max_seconds", base_guidellm.get("max_seconds", 300)),
            vllm_args=raw_config.get("vllm_args", {}),
        )

    def load_scenario(self, scenario_path: str | Path) -> dict[str, Any]:
        """
        Load a scenario file and resolve its defaults.

        Args:
            scenario_path: Path to scenario YAML file

        Returns:
            Parsed scenario data with resolved defaults
        """
        scenario_path = Path(scenario_path)

        with open(scenario_path) as f:
            data = yaml.safe_load(f) or {}

        # Merge scenario defaults with global defaults
        global_defaults = self.get_global_defaults()
        scenario_defaults = data.get("defaults", {})
        data["_resolved_defaults"] = deep_merge(global_defaults, scenario_defaults)

        # Add accelerator info
        data["_accelerator"] = self.accelerator
        data["_accelerator_config"] = self.get_accelerator_defaults()

        return data

    def _find_model(self, model_key: str) -> tuple[str, dict[str, Any]] | None:
        """
        Find model by key, alias, or HuggingFace ID.

        Returns:
            Tuple of (actual_key, config) or None if not found
        """
        # Try exact key match
        if model_key in self.models:
            return (model_key, self.models[model_key])

        # Try alias match
        for key, config in self.models.items():
            aliases = config.get("aliases", [])
            if model_key in aliases:
                return (key, config)

        # Try HuggingFace ID match
        for key, config in self.models.items():
            if config.get("hf_model_id") == model_key:
                return (key, config)

        return None

    def list_models(self) -> list[str]:
        """List all model keys."""
        return list(self.models.keys())

    def list_workloads(self) -> list[str]:
        """List all workload keys."""
        return list(self.workloads.keys())

    def get_image(self) -> str:
        """Get container image for current accelerator."""
        accel_config = self.get_accelerator_defaults()
        return accel_config.get("image", "")
