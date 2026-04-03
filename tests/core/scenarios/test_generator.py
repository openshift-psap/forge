"""Unit tests for ScenarioGenerator."""

import tempfile
from pathlib import Path

import pytest
import yaml

from projects.core.scenarios import ExpandedScenario, ScenarioConfig, ScenarioGenerator


class TestScenarioGenerator:
    """Tests for ScenarioGenerator."""

    @pytest.fixture
    def sample_config_path(self):
        """Create a sample scenarios.yaml file using new format."""
        config = {
            "name": "test-scenarios",
            "description": "Test scenario configuration",
            "common": {
                "namespace": "forge",
                "runtime_args": {
                    "dtype": "auto",
                    "gpu-memory-utilization": 0.9,
                },
            },
            "workloads": {
                "balanced": {
                    "description": "Balanced workload",
                    "guidellm": {"max_requests": 100},
                },
                "short": {
                    "description": "Short workload",
                    "guidellm": {"max_requests": 50},
                },
            },
            "routing": {
                "direct": {"mode": "direct"},
            },
            # New format: models section with model definitions
            "models": {
                "qwen-0.6b": {
                    "hf_model_id": "Qwen/Qwen3-0.6B",
                    "name": "qwen3-0-6b",
                    "vllm_args": {"max-model-len": 4096},
                },
            },
            # New format: scenarios list references model keys
            "scenarios": [
                {
                    "model": "qwen-0.6b",
                    "workloads": ["balanced", "short"],
                    "routing": ["direct"],
                    "tensor_parallel": [1, 2],
                },
            ],
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            yaml.safe_dump(config, f)
            yield Path(f.name)

    def test_load_config(self, sample_config_path):
        """Generator loads and parses YAML config."""
        gen = ScenarioGenerator(sample_config_path)
        config = gen.load()

        assert config.name == "test-scenarios"
        assert config.description == "Test scenario configuration"
        assert "qwen-0.6b" in config.models

    def test_matrix_expansion(self, sample_config_path):
        """Matrix expansion produces correct number of scenarios."""
        gen = ScenarioGenerator(sample_config_path)
        gen.load()

        scenarios = gen.expand()

        # 2 workloads × 1 routing × 2 TP = 4 scenarios
        assert len(scenarios) == 4

    def test_expanded_scenario_ids(self, sample_config_path):
        """Expanded scenarios have deterministic IDs."""
        gen = ScenarioGenerator(sample_config_path)
        gen.load()

        scenarios = gen.expand()
        scenario_ids = [s.scenario_id for s in scenarios]

        # Check expected scenario IDs (model_short derived from model key)
        assert "qwen-0-6b_balanced_direct_tp1" in scenario_ids
        assert "qwen-0-6b_balanced_direct_tp2" in scenario_ids
        assert "qwen-0-6b_short_direct_tp1" in scenario_ids
        assert "qwen-0-6b_short_direct_tp2" in scenario_ids

    def test_runtime_args_merging(self, sample_config_path):
        """Runtime args come from model vllm_args + tensor_parallel."""
        gen = ScenarioGenerator(sample_config_path)
        gen.load()

        scenarios = gen.expand()

        for s in scenarios:
            # Model-specific vllm_args
            assert s.runtime_args["max-model-len"] == 4096
            # TP from matrix
            assert s.runtime_args["tensor-parallel-size"] == s.tensor_parallel

    def test_workload_config_applied(self, sample_config_path):
        """Workload config is available in workload_config."""
        gen = ScenarioGenerator(sample_config_path)
        gen.load()

        scenarios = gen.expand()

        for s in scenarios:
            # Workload guidellm config is in workload_config
            assert "max_requests" in s.workload_config

    def test_deploy_config_num_gpus(self, sample_config_path):
        """Deploy config num_gpus matches tensor-parallel-size."""
        gen = ScenarioGenerator(sample_config_path)
        gen.load()

        scenarios = gen.expand()

        for s in scenarios:
            assert s.deploy_config["num_gpus"] == s.tensor_parallel

    def test_summary(self, sample_config_path):
        """Summary produces readable output."""
        gen = ScenarioGenerator(sample_config_path)
        gen.load()

        summary = gen.summary()

        assert "test-scenarios" in summary
        assert "qwen-0.6b" in summary  # Model key appears in deployment groups
        assert "Total Benchmark Runs: 4" in summary

    def test_to_scenario_config(self, sample_config_path):
        """ExpandedScenario converts to ScenarioConfig."""
        gen = ScenarioGenerator(sample_config_path)
        gen.load()

        scenarios = gen.expand()
        scenario_config = scenarios[0].to_scenario_config(namespace="test-ns")

        assert isinstance(scenario_config, ScenarioConfig)
        assert scenario_config.namespace == "test-ns"
        assert scenario_config.model_id == scenarios[0].model_id

    def test_to_dict(self, sample_config_path):
        """ExpandedScenario serializes to dict."""
        gen = ScenarioGenerator(sample_config_path)
        gen.load()

        scenarios = gen.expand()
        d = scenarios[0].to_dict()

        assert "model_id" in d
        assert "scenario_id" in d
        assert "runtime_args" in d
        assert d["model_id"] == scenarios[0].model_id


class TestScenarioConfig:
    """Tests for ScenarioConfig utilities."""

    def test_sanitize_name(self):
        """sanitize_name produces K8s-compatible names."""
        # Dots are removed for K8s compatibility
        assert ScenarioConfig.sanitize_name("Qwen/Qwen3-0.6B") == "qwen-qwen3-06b"
        assert ScenarioConfig.sanitize_name("test_name") == "test-name"
        assert (
            ScenarioConfig.sanitize_name("very-long-name" * 10, max_len=20)
            == "very-long-namevery-l"
        )

    def test_shorten_model_name(self):
        """shorten_model_name extracts short name."""
        assert ScenarioConfig.shorten_model_name("Qwen/Qwen3-0.6B") == "qwen3-0-6b"
        assert (
            ScenarioConfig.shorten_model_name("openai/gpt-oss-120b") == "gpt-oss-120b"
        )
        assert (
            ScenarioConfig.shorten_model_name("RedHatAI/model-instruct")
            == "model"
        )
        assert (
            ScenarioConfig.shorten_model_name("org/model-dynamic") == "model"
        )


class TestExplicitRuns:
    """Tests for explicit run definitions (no matrix)."""

    @pytest.fixture
    def explicit_runs_config(self):
        """Config with explicit runs instead of matrix."""
        config = {
            "name": "explicit-runs",
            "common": {"namespace": "forge"},
            "workloads": {"balanced": {"guidellm": {"max_requests": 100}}},
            "routing": {"direct": {"mode": "direct"}},
            "models": {
                "test-model": {
                    "hf_model_id": "test/model",
                    "name": "test-model",
                    "vllm_args": {"extra": "value"},
                },
            },
            "runs": [
                {
                    "model": "test-model",
                    "workload": "balanced",
                    "routing": "direct",
                    "tensor_parallel": 4,
                },
            ],
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            yaml.safe_dump(config, f)
            yield Path(f.name)

    def test_explicit_run_expansion(self, explicit_runs_config):
        """Explicit runs expand without matrix."""
        gen = ScenarioGenerator(explicit_runs_config)
        gen.load()

        scenarios = gen.expand()

        assert len(scenarios) == 1
        s = scenarios[0]
        assert s.model_id == "test/model"
        assert s.workload == "balanced"
        assert s.tensor_parallel == 4
        assert s.runtime_args["tensor-parallel-size"] == 4
        assert s.runtime_args["extra"] == "value"
