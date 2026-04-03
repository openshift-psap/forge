"""Unit tests for ConfigLoader."""

import tempfile
from pathlib import Path

import pytest
import yaml

from projects.core.scenarios import ConfigLoader, ResolvedModelConfig, ResolvedWorkloadConfig


class TestConfigLoader:
    """Tests for ConfigLoader inheritance and resolution."""

    @pytest.fixture
    def config_dir(self, tmp_path):
        """Create a config directory with defaults, models, and workloads."""
        # defaults.yaml
        defaults = {
            "defaults": {
                "deploy": {
                    "namespace": "forge",
                    "replicas": 1,
                    "cpu_request": "4",
                    "memory_request": "16Gi",
                },
                "vllm_args": {
                    "gpu-memory-utilization": 0.9,
                    "trust-remote-code": True,
                    "tensor-parallel-size": 1,  # Also determines num_gpus
                },
                "guidellm": {
                    "max_requests": 100,
                    "rate_type": "concurrent",
                },
            },
            "accelerators": {
                "nvidia": {
                    "image": "quay.io/rhaiis/cuda:latest",
                    "vllm_args": {},
                    "env_vars": {},
                },
                "amd": {
                    "image": "quay.io/rhaiis/rocm:latest",
                    "vllm_args": {
                        "num-scheduler-steps": 8,
                    },
                    "env_vars": {
                        "VLLM_ROCM_USE_AITER": "1",
                    },
                },
            },
        }
        (tmp_path / "defaults.yaml").write_text(yaml.safe_dump(defaults))

        # models.yaml
        models = {
            "models": {
                "qwen-0.6b": {
                    "name": "Qwen3-0.6B",
                    "hf_model_id": "Qwen/Qwen3-0.6B",
                    "vllm_args": {
                        "max-model-len": 8192,
                    },
                    "supported_workloads": ["balanced", "short"],
                },
                "llama-70b-fp8": {
                    "name": "Llama-3.3-70B-FP8",
                    "hf_model_id": "RedHatAI/Llama-3.3-70B-Instruct-FP8-dynamic",
                    "aliases": ["llama-70b", "llama-fp8"],
                    "vllm_args": {
                        "tensor-parallel-size": 4,
                        "max-model-len": 32768,
                        "kv-cache-dtype": "fp8",
                    },
                    "supported_workloads": ["balanced", "short", "long-prompt"],
                },
                "deepseek-r1": {
                    "name": "DeepSeek-R1",
                    "hf_model_id": "deepseek-ai/DeepSeek-R1-0528",
                    "vllm_args": {
                        "tensor-parallel-size": 8,
                    },
                    "accelerator_overrides": {
                        "amd": {
                            "env_vars": {
                                "VLLM_ROCM_USE_AITER": "0",
                            },
                        },
                    },
                },
                # Model with env_vars that apply to all accelerators
                "model-with-env": {
                    "name": "Model With Env",
                    "hf_model_id": "test/model-with-env",
                    "env_vars": {
                        "VLLM_MXFP4_USE_MARLIN": "1",
                        "CUSTOM_VAR": "model-value",
                    },
                },
                # Model with both model-level and accelerator-specific env_vars
                "model-with-overrides": {
                    "name": "Model With Overrides",
                    "hf_model_id": "test/model-with-overrides",
                    "env_vars": {
                        "SHARED_VAR": "model-default",
                        "MODEL_ONLY_VAR": "from-model",
                    },
                    "accelerator_overrides": {
                        "nvidia": {
                            "env_vars": {
                                "TORCH_CUDA_ARCH_LIST": "9.0",
                                "SHARED_VAR": "nvidia-override",
                            },
                        },
                        "amd": {
                            "env_vars": {
                                "SHARED_VAR": "amd-override",
                            },
                        },
                    },
                },
            },
        }
        (tmp_path / "models.yaml").write_text(yaml.safe_dump(models))

        # workloads.yaml
        workloads = {
            "workloads": {
                "balanced": {
                    "name": "Balanced",
                    "description": "Balanced prompt and output (1k/1k)",
                    "guidellm": {
                        "data": "prompt_tokens=1000,output_tokens=1000",
                        "rates": [1, 50, 100],
                    },
                    "max_seconds": 180,
                },
                "short": {
                    "name": "Short",
                    "description": "Short prompt and output (256/256)",
                    "guidellm": {
                        "data": "prompt_tokens=256,output_tokens=256",
                    },
                    "max_seconds": 120,
                },
                "long-prompt": {
                    "name": "Long Prompt",
                    "description": "Long prompt (8k/1k) - requires larger context",
                    "guidellm": {
                        "data": "prompt_tokens=8000,output_tokens=1000",
                    },
                    "max_seconds": 300,
                    "vllm_args": {
                        "max-model-len": 10000,
                    },
                },
                "very-long-prompt": {
                    "name": "Very Long Prompt",
                    "description": "Very long prompt (16k/1k)",
                    "guidellm": {
                        "data": "prompt_tokens=16000,output_tokens=1000",
                    },
                    "max_seconds": 600,
                    "vllm_args": {
                        "max-model-len": 20000,
                    },
                },
            },
        }
        (tmp_path / "workloads.yaml").write_text(yaml.safe_dump(workloads))

        return tmp_path

    def test_load_model_basic(self, config_dir):
        """ConfigLoader loads model with defaults applied."""
        loader = ConfigLoader(config_dir, accelerator="nvidia")
        model = loader.load_model("qwen-0.6b")

        assert isinstance(model, ResolvedModelConfig)
        assert model.key == "qwen-0.6b"
        assert model.name == "Qwen3-0.6B"
        assert model.hf_model_id == "Qwen/Qwen3-0.6B"

    def test_defaults_inheritance(self, config_dir):
        """Model inherits from global defaults."""
        loader = ConfigLoader(config_dir, accelerator="nvidia")
        model = loader.load_model("qwen-0.6b")

        # From defaults
        assert model.vllm_args["gpu-memory-utilization"] == 0.9
        assert model.vllm_args["trust-remote-code"] is True

        # From model config (overrides default)
        assert model.vllm_args["max-model-len"] == 8192

    def test_accelerator_nvidia_defaults(self, config_dir):
        """NVIDIA accelerator uses correct settings."""
        loader = ConfigLoader(config_dir, accelerator="nvidia")
        model = loader.load_model("qwen-0.6b")

        # NVIDIA has no special vllm_args or env_vars
        assert "num-scheduler-steps" not in model.vllm_args
        assert model.env_vars == {}

    def test_accelerator_amd_defaults(self, config_dir):
        """AMD accelerator applies accelerator-specific settings."""
        loader = ConfigLoader(config_dir, accelerator="amd")
        model = loader.load_model("qwen-0.6b")

        # AMD accelerator defaults
        assert model.vllm_args["num-scheduler-steps"] == 8
        assert model.env_vars["VLLM_ROCM_USE_AITER"] == "1"

    def test_accelerator_overrides_in_model(self, config_dir):
        """Model-specific accelerator overrides take precedence."""
        # DeepSeek needs AITER disabled on AMD
        loader = ConfigLoader(config_dir, accelerator="amd")
        model = loader.load_model("deepseek-r1")

        # Model accelerator_override takes precedence over accelerator defaults
        assert model.env_vars["VLLM_ROCM_USE_AITER"] == "0"

    def test_model_level_env_vars(self, config_dir):
        """Model-level env_vars apply to all accelerators."""
        # Test on NVIDIA
        nvidia_loader = ConfigLoader(config_dir, accelerator="nvidia")
        model_nvidia = nvidia_loader.load_model("model-with-env")

        assert model_nvidia.env_vars["VLLM_MXFP4_USE_MARLIN"] == "1"
        assert model_nvidia.env_vars["CUSTOM_VAR"] == "model-value"

        # Test on AMD - same model env_vars plus AMD accelerator defaults
        amd_loader = ConfigLoader(config_dir, accelerator="amd")
        model_amd = amd_loader.load_model("model-with-env")

        assert model_amd.env_vars["VLLM_MXFP4_USE_MARLIN"] == "1"
        assert model_amd.env_vars["CUSTOM_VAR"] == "model-value"
        # Also gets AMD accelerator defaults
        assert model_amd.env_vars["VLLM_ROCM_USE_AITER"] == "1"

    def test_env_vars_inheritance_chain(self, config_dir):
        """Env vars follow inheritance: accelerator → model → model.accelerator_overrides."""
        # NVIDIA: accelerator has no env_vars, model has some, model.accelerator_overrides adds CUDA arch
        nvidia_loader = ConfigLoader(config_dir, accelerator="nvidia")
        model_nvidia = nvidia_loader.load_model("model-with-overrides")

        assert model_nvidia.env_vars["MODEL_ONLY_VAR"] == "from-model"
        assert model_nvidia.env_vars["TORCH_CUDA_ARCH_LIST"] == "9.0"
        # SHARED_VAR: nvidia override wins over model default
        assert model_nvidia.env_vars["SHARED_VAR"] == "nvidia-override"

        # AMD: accelerator has AITER, model has its vars, model.accelerator_overrides overrides SHARED_VAR
        amd_loader = ConfigLoader(config_dir, accelerator="amd")
        model_amd = amd_loader.load_model("model-with-overrides")

        assert model_amd.env_vars["MODEL_ONLY_VAR"] == "from-model"
        assert model_amd.env_vars["VLLM_ROCM_USE_AITER"] == "1"  # From AMD accelerator defaults
        # SHARED_VAR: amd override wins over model default
        assert model_amd.env_vars["SHARED_VAR"] == "amd-override"
        # No CUDA arch on AMD
        assert "TORCH_CUDA_ARCH_LIST" not in model_amd.env_vars

    def test_model_alias_lookup(self, config_dir):
        """ConfigLoader finds model by alias."""
        loader = ConfigLoader(config_dir, accelerator="nvidia")
        model = loader.load_model("llama-70b")

        assert model.key == "llama-70b-fp8"
        assert model.hf_model_id == "RedHatAI/Llama-3.3-70B-Instruct-FP8-dynamic"

    def test_model_hf_id_lookup(self, config_dir):
        """ConfigLoader finds model by HuggingFace ID."""
        loader = ConfigLoader(config_dir, accelerator="nvidia")
        model = loader.load_model("Qwen/Qwen3-0.6B")

        assert model.key == "qwen-0.6b"

    def test_model_not_found(self, config_dir):
        """ConfigLoader raises KeyError for unknown model."""
        loader = ConfigLoader(config_dir, accelerator="nvidia")

        with pytest.raises(KeyError, match="not found"):
            loader.load_model("nonexistent-model")

    def test_num_gpus_property(self, config_dir):
        """ResolvedModelConfig.num_gpus returns correct value."""
        loader = ConfigLoader(config_dir, accelerator="nvidia")

        # Small model - 1 GPU
        small = loader.load_model("qwen-0.6b")
        assert small.num_gpus == 1

        # Large model - 4 GPUs
        large = loader.load_model("llama-70b-fp8")
        assert large.num_gpus == 4

    def test_tensor_parallel_property(self, config_dir):
        """ResolvedModelConfig.tensor_parallel returns correct value."""
        loader = ConfigLoader(config_dir, accelerator="nvidia")

        # Default TP=1
        small = loader.load_model("qwen-0.6b")
        assert small.tensor_parallel == 1

        # TP=4 from model config
        large = loader.load_model("llama-70b-fp8")
        assert large.tensor_parallel == 4

    def test_load_workload(self, config_dir):
        """ConfigLoader loads workload with guidellm defaults merged."""
        loader = ConfigLoader(config_dir, accelerator="nvidia")
        workload = loader.load_workload("balanced")

        assert isinstance(workload, ResolvedWorkloadConfig)
        assert workload.key == "balanced"
        assert workload.name == "Balanced"
        assert workload.max_seconds == 180

        # Guidellm config merged with defaults
        assert workload.guidellm["data"] == "prompt_tokens=1000,output_tokens=1000"
        assert workload.guidellm["rate_type"] == "concurrent"  # From defaults
        assert workload.guidellm["rates"] == [1, 50, 100]  # From workload

    def test_load_workload_without_vllm_args(self, config_dir):
        """Workload without vllm_args has empty dict."""
        loader = ConfigLoader(config_dir, accelerator="nvidia")
        workload = loader.load_workload("balanced")

        assert workload.vllm_args == {}

    def test_load_workload_with_vllm_args(self, config_dir):
        """Workload with vllm_args returns the override."""
        loader = ConfigLoader(config_dir, accelerator="nvidia")
        workload = loader.load_workload("long-prompt")

        assert workload.key == "long-prompt"
        assert workload.vllm_args == {"max-model-len": 10000}

    def test_workload_not_found(self, config_dir):
        """ConfigLoader raises KeyError for unknown workload."""
        loader = ConfigLoader(config_dir, accelerator="nvidia")

        with pytest.raises(KeyError, match="not found"):
            loader.load_workload("nonexistent")

    def test_get_image(self, config_dir):
        """ConfigLoader returns correct image for accelerator."""
        nvidia_loader = ConfigLoader(config_dir, accelerator="nvidia")
        assert nvidia_loader.get_image() == "quay.io/rhaiis/cuda:latest"

        amd_loader = ConfigLoader(config_dir, accelerator="amd")
        assert amd_loader.get_image() == "quay.io/rhaiis/rocm:latest"

    def test_list_models(self, config_dir):
        """ConfigLoader.list_models returns all model keys."""
        loader = ConfigLoader(config_dir, accelerator="nvidia")
        models = loader.list_models()

        assert "qwen-0.6b" in models
        assert "llama-70b-fp8" in models
        assert "deepseek-r1" in models
        assert "model-with-env" in models
        assert "model-with-overrides" in models
        assert len(models) == 5

    def test_list_workloads(self, config_dir):
        """ConfigLoader.list_workloads returns all workload keys."""
        loader = ConfigLoader(config_dir, accelerator="nvidia")
        workloads = loader.list_workloads()

        assert "balanced" in workloads
        assert "short" in workloads
        assert "long-prompt" in workloads
        assert "very-long-prompt" in workloads
        assert len(workloads) == 4

    def test_caching(self, config_dir):
        """ConfigLoader caches loaded configs."""
        loader = ConfigLoader(config_dir, accelerator="nvidia")

        # Access defaults twice - should be same object
        defaults1 = loader.defaults
        defaults2 = loader.defaults
        assert defaults1 is defaults2

        # Same for models
        models1 = loader.models
        models2 = loader.models
        assert models1 is models2


class TestConfigLoaderScenarios:
    """Tests for ConfigLoader scenario loading."""

    @pytest.fixture
    def full_config_dir(self, tmp_path):
        """Create full config directory with scenarios."""
        # Create base configs
        defaults = {
            "defaults": {
                "deploy": {"namespace": "forge"},
                "vllm_args": {"gpu-memory-utilization": 0.9},
                "guidellm": {"max_requests": 100},
            },
            "accelerators": {
                "nvidia": {"image": "cuda:latest"},
            },
        }
        (tmp_path / "defaults.yaml").write_text(yaml.safe_dump(defaults))

        models = {
            "models": {
                "test-model": {
                    "hf_model_id": "test/model",
                    "vllm_args": {"max-model-len": 4096},
                },
            },
        }
        (tmp_path / "models.yaml").write_text(yaml.safe_dump(models))

        workloads = {
            "workloads": {
                "balanced": {"guidellm": {"data": "1k"}},
            },
        }
        (tmp_path / "workloads.yaml").write_text(yaml.safe_dump(workloads))

        # Create scenarios directory
        scenarios_dir = tmp_path / "scenarios"
        scenarios_dir.mkdir()

        scenario = {
            "name": "test-scenario",
            "defaults": {
                "deploy": {
                    "namespace": "test-ns",
                },
            },
            "scenarios": [
                {
                    "model": "test-model",
                    "workloads": ["balanced"],
                },
            ],
        }
        (scenarios_dir / "test.yaml").write_text(yaml.safe_dump(scenario))

        return tmp_path

    def test_load_scenario(self, full_config_dir):
        """ConfigLoader loads scenario with resolved defaults."""
        loader = ConfigLoader(full_config_dir, accelerator="nvidia")
        scenario = loader.load_scenario(full_config_dir / "scenarios" / "test.yaml")

        assert scenario["name"] == "test-scenario"
        assert scenario["_accelerator"] == "nvidia"

        # Resolved defaults merge global + scenario
        resolved = scenario["_resolved_defaults"]
        assert resolved["deploy"]["namespace"] == "test-ns"  # From scenario
        assert resolved["vllm_args"]["gpu-memory-utilization"] == 0.9  # From global


class TestDeepMerge:
    """Tests for deep_merge utility function."""

    def test_basic_merge(self):
        """Basic dictionary merge."""
        from projects.core.scenarios.config_loader import deep_merge

        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = deep_merge(base, override)

        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self):
        """Nested dictionaries are merged recursively."""
        from projects.core.scenarios.config_loader import deep_merge

        base = {"outer": {"a": 1, "b": 2}}
        override = {"outer": {"b": 3, "c": 4}}
        result = deep_merge(base, override)

        assert result == {"outer": {"a": 1, "b": 3, "c": 4}}

    def test_lists_replaced(self):
        """Lists are replaced, not merged."""
        from projects.core.scenarios.config_loader import deep_merge

        base = {"items": [1, 2, 3]}
        override = {"items": [4, 5]}
        result = deep_merge(base, override)

        assert result == {"items": [4, 5]}

    def test_no_mutation(self):
        """Original dicts are not mutated."""
        from projects.core.scenarios.config_loader import deep_merge

        base = {"a": {"b": 1}}
        override = {"a": {"c": 2}}

        result = deep_merge(base, override)

        # Original unchanged
        assert base == {"a": {"b": 1}}
        assert override == {"a": {"c": 2}}
        # Result has both
        assert result == {"a": {"b": 1, "c": 2}}


class TestWorkloadVllmArgsGrouping:
    """Tests for workload-specific vllm_args and deployment grouping."""

    @pytest.fixture
    def config_dir_with_vllm_args(self, tmp_path):
        """Create config directory with workloads that have vllm_args."""
        # defaults.yaml
        defaults = {
            "defaults": {
                "deploy": {"namespace": "forge"},
                "vllm_args": {"gpu-memory-utilization": 0.9, "max-model-len": 4096},
                "guidellm": {"max_requests": 100},
            },
            "accelerators": {
                "nvidia": {"image": "cuda:latest"},
            },
        }
        (tmp_path / "defaults.yaml").write_text(yaml.safe_dump(defaults))

        # models.yaml
        models = {
            "models": {
                "test-model": {
                    "hf_model_id": "test/model",
                    "vllm_args": {"trust-remote-code": True},
                },
            },
        }
        (tmp_path / "models.yaml").write_text(yaml.safe_dump(models))

        # workloads.yaml - some with vllm_args, some without
        workloads = {
            "workloads": {
                "balanced": {
                    "name": "Balanced",
                    "guidellm": {"data": "1k/1k"},
                },
                "short": {
                    "name": "Short",
                    "guidellm": {"data": "256/256"},
                },
                "long-prompt": {
                    "name": "Long Prompt",
                    "guidellm": {"data": "8k/1k"},
                    "vllm_args": {"max-model-len": 10000},
                },
                "very-long-prompt": {
                    "name": "Very Long Prompt",
                    "guidellm": {"data": "16k/1k"},
                    "vllm_args": {"max-model-len": 20000},
                },
            },
        }
        (tmp_path / "workloads.yaml").write_text(yaml.safe_dump(workloads))

        # scenarios/test.yaml
        scenarios_dir = tmp_path / "scenarios"
        scenarios_dir.mkdir()
        scenario = {
            "name": "test-scenario",
            "scenarios": [
                {
                    "model": "test-model",
                    "workloads": ["balanced", "short", "long-prompt", "very-long-prompt"],
                },
            ],
        }
        (scenarios_dir / "test.yaml").write_text(yaml.safe_dump(scenario))

        return tmp_path

    def test_workload_config_has_vllm_args(self, config_dir_with_vllm_args):
        """WorkloadConfig parses vllm_args from config."""
        from projects.core.scenarios.generator import WorkloadConfig

        wl = WorkloadConfig.from_dict("long-prompt", {
            "name": "Long Prompt",
            "guidellm": {"data": "8k/1k"},
            "vllm_args": {"max-model-len": 10000},
        })

        assert wl.vllm_args == {"max-model-len": 10000}

    def test_workload_config_empty_vllm_args(self, config_dir_with_vllm_args):
        """WorkloadConfig without vllm_args has empty dict."""
        from projects.core.scenarios.generator import WorkloadConfig

        wl = WorkloadConfig.from_dict("balanced", {
            "name": "Balanced",
            "guidellm": {"data": "1k/1k"},
        })

        assert wl.vllm_args == {}

    def test_deployment_group_merged_vllm_args(self, config_dir_with_vllm_args):
        """DeploymentGroup.merged_vllm_args combines model and workload args."""
        from projects.core.scenarios.generator import DeploymentGroup, ModelConfig

        model = ModelConfig(
            key="test-model",
            name="Test Model",
            hf_model_id="test/model",
            vllm_args={"gpu-memory-utilization": 0.9, "max-model-len": 4096},
        )
        group = DeploymentGroup(
            model=model,
            tensor_parallel=1,
            routing="direct",
            workloads=[],
            vllm_args_override={"max-model-len": 10000},
        )

        merged = group.merged_vllm_args

        # Model args preserved
        assert merged["gpu-memory-utilization"] == 0.9
        # Workload override wins
        assert merged["max-model-len"] == 10000

    def test_expand_grouped_separates_by_vllm_args(self, config_dir_with_vllm_args):
        """ScenarioGenerator groups workloads by vllm_args."""
        from projects.core.scenarios.generator import ScenarioGenerator

        gen = ScenarioGenerator(
            scenarios_path=config_dir_with_vllm_args / "scenarios" / "test.yaml",
            config_dir=config_dir_with_vllm_args,
            accelerator="nvidia",
        )
        gen.load()
        groups = gen.expand_grouped()

        # Should have 3 groups:
        # 1. balanced + short (no vllm_args)
        # 2. long-prompt (max-model-len: 10000)
        # 3. very-long-prompt (max-model-len: 20000)
        assert len(groups) == 3

        # Find each group by its vllm_args
        no_override_group = None
        long_prompt_group = None
        very_long_group = None

        for g in groups:
            if not g.vllm_args_override:
                no_override_group = g
            elif g.vllm_args_override.get("max-model-len") == 10000:
                long_prompt_group = g
            elif g.vllm_args_override.get("max-model-len") == 20000:
                very_long_group = g

        # Group without override has balanced + short
        assert no_override_group is not None
        assert len(no_override_group.workloads) == 2
        assert {w.key for w in no_override_group.workloads} == {"balanced", "short"}

        # long-prompt group
        assert long_prompt_group is not None
        assert len(long_prompt_group.workloads) == 1
        assert long_prompt_group.workloads[0].key == "long-prompt"
        assert long_prompt_group.vllm_args_override == {"max-model-len": 10000}

        # very-long-prompt group
        assert very_long_group is not None
        assert len(very_long_group.workloads) == 1
        assert very_long_group.workloads[0].key == "very-long-prompt"
        assert very_long_group.vllm_args_override == {"max-model-len": 20000}

    def test_same_vllm_args_same_group(self, tmp_path):
        """Workloads with identical vllm_args share a deployment group."""
        # Create config where two workloads have same vllm_args
        defaults = {
            "defaults": {"vllm_args": {}},
            "accelerators": {"nvidia": {"image": "cuda:latest"}},
        }
        (tmp_path / "defaults.yaml").write_text(yaml.safe_dump(defaults))

        models = {
            "models": {
                "test-model": {"hf_model_id": "test/model"},
            },
        }
        (tmp_path / "models.yaml").write_text(yaml.safe_dump(models))

        workloads = {
            "workloads": {
                "long-a": {
                    "guidellm": {"data": "a"},
                    "vllm_args": {"max-model-len": 10000},
                },
                "long-b": {
                    "guidellm": {"data": "b"},
                    "vllm_args": {"max-model-len": 10000},  # Same as long-a
                },
            },
        }
        (tmp_path / "workloads.yaml").write_text(yaml.safe_dump(workloads))

        scenarios_dir = tmp_path / "scenarios"
        scenarios_dir.mkdir()
        scenario = {
            "name": "test",
            "scenarios": [{"model": "test-model", "workloads": ["long-a", "long-b"]}],
        }
        (scenarios_dir / "test.yaml").write_text(yaml.safe_dump(scenario))

        from projects.core.scenarios.generator import ScenarioGenerator

        gen = ScenarioGenerator(
            scenarios_path=tmp_path / "scenarios" / "test.yaml",
            config_dir=tmp_path,
        )
        gen.load()
        groups = gen.expand_grouped()

        # Both workloads have same vllm_args -> 1 group
        assert len(groups) == 1
        assert len(groups[0].workloads) == 2
        assert {w.key for w in groups[0].workloads} == {"long-a", "long-b"}
