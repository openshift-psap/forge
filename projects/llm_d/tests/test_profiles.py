from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from projects.core.library import config as core_config
from projects.core.library import env
from projects.llm_d.orchestration import ci as llmd_ci
from projects.llm_d.orchestration import runtime_config, test_phase
from projects.llm_d.orchestration.render_inference_service import (
    render_inference_service_from_parts,
)

PROJECT_ORCHESTRATION_DIR = Path(__file__).resolve().parents[1] / "orchestration"


@pytest.fixture(autouse=True)
def _reset_project_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path / "artifacts"))
    env.init()
    core_config.project = None
    yield
    core_config.project = None


def _init_project_config() -> None:
    core_config.init(PROJECT_ORCHESTRATION_DIR)


def test_deployment_presets_resolve_deployments() -> None:
    _init_project_config()

    core_config.project.apply_preset("deployment-approximate-prefix-cache")
    assert runtime_config.get_deployment_profile_name() == "approximate-prefix-cache"

    core_config.project.apply_preset("deployment-precise-prefix-cache")
    assert runtime_config.get_deployment_profile_name() == "precise-prefix-cache"

    core_config.project.apply_preset("deployment-distributed-default")
    assert runtime_config.get_deployment_profile_name() == "distributed-default"


def test_release_deployment_profiles_have_expected_shape() -> None:
    _init_project_config()

    core_config.project.set_config("runtime.deployment_profile", "approximate-prefix-cache")
    approximate = runtime_config.get_deployment_profile()
    core_config.project.set_config("runtime.deployment_profile", "precise-prefix-cache")
    precise = runtime_config.get_deployment_profile()
    core_config.project.set_config("runtime.deployment_profile", "distributed-default")
    distributed = runtime_config.get_deployment_profile()

    for profile in (approximate, precise, distributed):
        assert profile["replicas"] == 1
        assert profile["tensor_parallelism"] == 1
        assert profile["vllm_args"] == [
            "--max-model-len=4096",
            "--gpu-memory-utilization=0.92",
            "--trust-remote-code",
            "--no-enable-log-requests",
            "--enable-prefix-caching",
        ]

    assert isinstance(approximate["scheduler"], dict)
    assert isinstance(precise["scheduler"], dict)
    assert distributed["scheduler"] == {}


@pytest.mark.parametrize(
    ("preset", "expected_deployment"),
    [
        ("smoke", "approximate-prefix-cache"),
        ("smoke-precise", "precise-prefix-cache"),
        ("smoke-default-scheduler", "distributed-default"),
    ],
)
def test_smoke_presets_inherit_deployment_modes(preset: str, expected_deployment: str) -> None:
    _init_project_config()
    core_config.project.apply_preset(preset)
    assert runtime_config.get_deployment_profile_name() == expected_deployment
    assert runtime_config.get_model_name() == "Qwen/Qwen3-0.6B"
    # Only the base "smoke" preset enables benchmarking via runtime.benchmark_key: short
    if preset == "smoke":
        assert runtime_config.get_benchmark_config() is not None
    else:
        assert runtime_config.get_benchmark_config() is None


def test_benchmark_workloads_are_available() -> None:
    _init_project_config()

    concurrent = core_config.project.get_config(
        "workloads.benchmarks.concurrent-1k-1k", print=False
    )
    heavy = core_config.project.get_config("workloads.benchmarks.heavy-heterogeneous", print=False)
    multi_turn = core_config.project.get_config("workloads.benchmarks.multi-turn", print=False)

    assert concurrent["args"]["rate"] == [300, 200, 100, 50, 1]
    assert heavy["args"]["max_seconds"] == 600
    assert "prompt_tokens_stdev=8500" in heavy["args"]["data"]
    assert "output_tokens_max=8000" in heavy["args"]["data"]
    assert multi_turn["args"]["rate"] == [32, 64, 128, 256, 512]
    assert "turns=5" in multi_turn["args"]["data"]
    assert "prefix_count={2*rate}" in multi_turn["args"]["data"]
    assert multi_turn["args"]["max_requests"] == "{10*rate}"


def test_benchmark_resolution_applies_workload_defaults_and_per_benchmark_overrides() -> None:
    _init_project_config()

    core_config.project.set_config("runtime.benchmark_key", "concurrent-1k-1k")
    concurrent = runtime_config.get_benchmark_config()
    assert concurrent is not None
    assert concurrent["job_name"] == "guidellm-benchmark"
    assert concurrent["image"] == "ghcr.io/vllm-project/guidellm:v0.5.4"
    assert concurrent["pvc_size"] == "1Gi"
    assert concurrent["timeout_seconds"] == 3600

    core_config.project.set_config("runtime.benchmark_key", "multi-turn")
    multi_turn = runtime_config.get_benchmark_config()
    assert multi_turn is not None
    assert multi_turn["job_name"] == "guidellm-benchmark"
    assert multi_turn["image"] == "ghcr.io/vllm-project/guidellm:v0.5.4"
    assert multi_turn["pvc_size"] == "1Gi"
    assert multi_turn["timeout_seconds"] == 3600


def test_guidellm_benchmark_uses_original_model_name_as_processor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_project_config()
    core_config.project.set_config("runtime.model_name", "openai/gpt-oss-120b")
    core_config.project.set_config("runtime.deployment_profile", "distributed-default")
    core_config.project.set_config("runtime.benchmark_key", "concurrent-1k-1k")

    captured: dict[str, object] = {}

    def _fake_run(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(test_phase.run_guidellm_benchmark_command, "run", _fake_run)
    test_phase.run_guidellm_benchmark(endpoint_url="https://example.test/llm-d")

    guidellm_args = captured["guidellm_args"]
    assert isinstance(guidellm_args, list)
    assert "--processor=openai/gpt-oss-120b" in guidellm_args


def test_release_preset_expands_benchmark_list_and_merges_workload_args() -> None:
    _init_project_config()

    core_config.project.apply_preset("gpt-oss-120b-inference-scheduling-release")

    assert runtime_config.get_deployment_profile_name() == "distributed-default"
    assert runtime_config.get_model_cache_config()["pvc"]["size"] == "300Gi"
    assert runtime_config.get_benchmark_keys() == [
        "concurrent-1k-1k",
        "heavy-heterogeneous",
        "multi-turn",
    ]

    benchmark_configs = dict(runtime_config.get_benchmark_configs())
    assert benchmark_configs["concurrent-1k-1k"]["args"]["request_type"] == "text_completions"
    assert benchmark_configs["heavy-heterogeneous"]["args"]["request_type"] == "text_completions"
    assert benchmark_configs["multi-turn"]["args"]["request_type"] == "text_completions"


def test_ci_init_uses_framework_project_args_preset_and_keeps_var_overrides() -> None:
    variable_overrides_path = env.ARTIFACT_DIR / "000__ci_metadata" / "variable_overrides.yaml"
    variable_overrides_path.parent.mkdir(parents=True, exist_ok=True)
    variable_overrides_path.write_text(
        yaml.safe_dump(
            {
                "project.args": ["gpt-oss-120b-inference-scheduling-release"],
                "runtime.benchmark_key": "multi-turn",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    llmd_ci.init()

    assert runtime_config.get_model_name() == "openai/gpt-oss-120b"
    assert runtime_config.get_deployment_profile_name() == "distributed-default"
    assert runtime_config.get_benchmark_keys() == ["multi-turn"]


def test_ci_init_uses_project_default_preset_when_no_explicit_preset_is_provided() -> None:
    llmd_ci.init()

    assert runtime_config.get_model_name() == "Qwen/Qwen3-0.6B"
    assert runtime_config.get_deployment_profile_name() == "approximate-prefix-cache"
    # Default preset "smoke" enables the short benchmark
    assert runtime_config.get_benchmark_keys() == ["short"]


def test_model_and_deployment_profile_accept_yaml_list_strings() -> None:
    _init_project_config()

    core_config.project.set_config(
        "runtime.model_name",
        "[openai/gpt-oss-120b, Qwen/Qwen3-0.6B]",
    )
    core_config.project.set_config(
        "runtime.deployment_profile",
        "[distributed-default, precise-prefix-cache]",
    )

    run_specs = runtime_config.get_run_specs()

    assert [(spec.model_name, spec.deployment_profile_name) for spec in run_specs] == [
        ("openai/gpt-oss-120b", "distributed-default"),
        ("openai/gpt-oss-120b", "precise-prefix-cache"),
        ("Qwen/Qwen3-0.6B", "distributed-default"),
        ("Qwen/Qwen3-0.6B", "precise-prefix-cache"),
    ]
    assert run_specs[0].model_slug == "openai-gpt-oss-120b"
    assert run_specs[2].model_slug == "qwen-qwen3-0-6b"


def test_runtime_rejects_legacy_model_key_path() -> None:
    _init_project_config()

    core_config.project.config.setdefault("runtime", {})["model_key"] = "qwen3-0-6b"
    with pytest.raises(ValueError, match="runtime.model_key"):
        runtime_config.get_run_specs()


def test_render_uses_sanitized_model_name_and_profile_resources() -> None:
    _init_project_config()
    core_config.project.set_config("model_cache.enabled", False)
    core_config.project.set_config("runtime.model_name", "openai/gpt-oss-120b")
    core_config.project.set_config("runtime.deployment_profile", "distributed-default")

    manifest = render_inference_service_from_parts(
        config_dir=str(PROJECT_ORCHESTRATION_DIR),
        namespace="forge-llm-d",
        inference_service=runtime_config.get_platform_config()["inference_service"],
        model_name=runtime_config.get_model_name(),
        model_slug=runtime_config.get_model_slug(),
        deployment_profile=runtime_config.get_deployment_profile(),
        model_cache=runtime_config.get_model_cache_config(),
    )

    assert manifest["spec"]["replicas"] == 1
    assert manifest["spec"]["model"]["uri"] == "hf://openai/gpt-oss-120b"
    assert manifest["spec"]["model"]["name"] == "openai-gpt-oss-120b"
    assert manifest["spec"]["template"]["containers"][0]["resources"] == {
        "requests": {"nvidia.com/gpu": "1"},
        "limits": {"nvidia.com/gpu": "1"},
    }
    assert manifest["spec"]["template"]["containers"][0]["args"] == [
        "--max-model-len=4096",
        "--gpu-memory-utilization=0.92",
        "--trust-remote-code",
        "--no-enable-log-requests",
        "--enable-prefix-caching",
        "--tensor-parallel-size=1",
    ]
    assert manifest["spec"]["router"]["scheduler"] == {}


def test_render_preserves_explicit_tensor_parallel_arg() -> None:
    _init_project_config()
    core_config.project.set_config("model_cache.enabled", False)
    core_config.project.config["deployments"]["explicit-tp"] = {
        "replicas": 1,
        "tensor_parallelism": 2,
        "scheduler": {},
        "vllm_args": [
            "--tensor-parallel-size=4",
            "--gpu-memory-utilization=0.85",
        ],
    }
    core_config.project.set_config("runtime.model_name", "openai/gpt-oss-120b")
    core_config.project.set_config("runtime.deployment_profile", "explicit-tp")

    manifest = render_inference_service_from_parts(
        config_dir=str(PROJECT_ORCHESTRATION_DIR),
        namespace="forge-llm-d",
        inference_service=runtime_config.get_platform_config()["inference_service"],
        model_name=runtime_config.get_model_name(),
        model_slug=runtime_config.get_model_slug(),
        deployment_profile=runtime_config.get_deployment_profile(),
        model_cache=runtime_config.get_model_cache_config(),
    )

    assert manifest["spec"]["template"]["containers"][0]["args"] == [
        "--tensor-parallel-size=4",
        "--gpu-memory-utilization=0.85",
    ]


def test_render_uses_embedded_scheduler_config() -> None:
    _init_project_config()
    core_config.project.set_config("model_cache.enabled", False)
    core_config.project.set_config("runtime.model_name", "Qwen/Qwen3-0.6B")
    core_config.project.set_config("runtime.deployment_profile", "approximate-prefix-cache")

    manifest = render_inference_service_from_parts(
        config_dir=str(PROJECT_ORCHESTRATION_DIR),
        namespace="forge-llm-d",
        inference_service=runtime_config.get_platform_config()["inference_service"],
        model_name=runtime_config.get_model_name(),
        model_slug=runtime_config.get_model_slug(),
        deployment_profile=runtime_config.get_deployment_profile(),
        model_cache=runtime_config.get_model_cache_config(),
    )

    scheduler = manifest["spec"]["router"]["scheduler"]
    assert scheduler["template"]["containers"][0]["args"][-2] == "--config-text"
    assert "EndpointPickerConfig" in scheduler["template"]["containers"][0]["args"][-1]


def test_render_removes_scheduler_when_deployment_requests_null_scheduler() -> None:
    _init_project_config()
    core_config.project.set_config("model_cache.enabled", False)
    core_config.project.config["deployments"]["no-scheduler"] = {
        "replicas": 1,
        "tensor_parallelism": 1,
        "scheduler": None,
        "vllm_args": [],
    }
    core_config.project.set_config("runtime.model_name", "Qwen/Qwen3-0.6B")
    core_config.project.set_config("runtime.deployment_profile", "no-scheduler")

    manifest = render_inference_service_from_parts(
        config_dir=str(PROJECT_ORCHESTRATION_DIR),
        namespace="forge-llm-d",
        inference_service=runtime_config.get_platform_config()["inference_service"],
        model_name=runtime_config.get_model_name(),
        model_slug=runtime_config.get_model_slug(),
        deployment_profile=runtime_config.get_deployment_profile(),
        model_cache=runtime_config.get_model_cache_config(),
    )

    assert "scheduler" not in manifest["spec"]["router"]


def test_benchmark_job_names_collapse_shared_default() -> None:
    _init_project_config()

    core_config.project.apply_preset("gpt-oss-120b-inference-scheduling-release")
    # Three benchmark_keys, all sharing the workload default job_name -> one entry.
    assert runtime_config.get_benchmark_job_names() == ["guidellm-benchmark"]


def test_smoke_preset_benchmark_behavior() -> None:
    _init_project_config()

    core_config.project.apply_preset("smoke")
    # The smoke preset enables the short benchmark
    assert runtime_config.get_benchmark_keys() == ["short"]
    assert runtime_config.get_benchmark_job_names() == ["guidellm-benchmark"]


def test_run_spec_preserves_namespace_managed_flag() -> None:
    _init_project_config()

    core_config.project.apply_preset("smoke")
    base_managed = runtime_config.get_namespace_is_managed()

    run_spec = runtime_config.get_run_specs()[0]
    assert run_spec.namespace_is_managed == base_managed

    with runtime_config.activate_run_spec(run_spec):
        # Inside the active spec the override is applied, but managed-ness must
        # still reflect the base value, not flip to False.
        assert runtime_config.get_namespace_is_managed() == base_managed
    # And it is restored on exit.
    assert runtime_config.get_namespace_is_managed() == base_managed


def test_run_spec_preserves_managed_flag_when_namespace_is_auto_derived() -> None:
    """Verify managed flag stays True even when activate_run_spec sets namespace_override."""
    _init_project_config()

    # Force auto-derived namespace by clearing the hardcoded name
    core_config.project.config["platform"]["cluster"]["namespace"]["name"] = None

    core_config.project.apply_preset("smoke")
    assert runtime_config.get_namespace_is_managed() is True

    run_spec = runtime_config.get_run_specs()[0]
    assert run_spec.namespace_is_managed is True

    with runtime_config.activate_run_spec(run_spec):
        # activate_run_spec sets namespace_override, but managed flag must stay True
        assert runtime_config.get_namespace_is_managed() is True
    assert runtime_config.get_namespace_is_managed() is True


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("no", ["no"]),
        ("yes", ["yes"]),
        ("null", ["null"]),
        ("123", ["123"]),
        ("1.5", ["1.5"]),
        ("meta-llama/Llama-3.1-8B-Instruct", ["meta-llama/Llama-3.1-8B-Instruct"]),
        # Properly quoted list strings (valid Python literals):
        (
            "['openai/gpt-oss-120b', \"Qwen/Qwen3-0.6B\"]",
            ["openai/gpt-oss-120b", "Qwen/Qwen3-0.6B"],
        ),
        # Edge cases now handled by ast.literal_eval:
        ("['a', 'b,c']", ["a", "b,c"]),  # Comma inside quotes
        ("[1, 2, 3]", ["1", "2", "3"]),  # Numbers in brackets
        # Actual Python lists (from YAML parsing):
        (["a", "b"], ["a", "b"]),
        ([1, 2, 3], ["1", "2", "3"]),
    ],
)
def test_normalize_string_or_list_treats_scalars_as_literals(raw: str, expected: list[str]) -> None:
    assert runtime_config._normalize_string_or_list(raw, "runtime.test") == expected


def test_render_supports_oci_model_uri() -> None:
    _init_project_config()
    core_config.project.set_config("model_cache.enabled", True)
    core_config.project.set_config(
        "runtime.model_name",
        "oci://registry.redhat.io/rhelai1/modelcar-llama-3-1-8b-instruct-fp8-dynamic:1.5",
    )
    core_config.project.set_config("runtime.deployment_profile", "distributed-default")

    manifest = render_inference_service_from_parts(
        config_dir=str(PROJECT_ORCHESTRATION_DIR),
        namespace="forge-llm-d",
        inference_service=runtime_config.get_platform_config()["inference_service"],
        model_name=runtime_config.get_model_name(),
        model_slug=runtime_config.get_model_slug(),
        deployment_profile=runtime_config.get_deployment_profile(),
        model_cache=runtime_config.get_model_cache_config(),
    )

    # Should use PVC-cached URI when model cache is enabled
    assert manifest["spec"]["model"]["uri"].startswith("pvc://")
    # Model slug should sanitize the full OCI path (truncated to 32 chars)
    assert manifest["spec"]["model"]["name"] == "oci-registry-redhat-io-rhelai1-m"


def test_get_model_uri_detects_scheme() -> None:
    _init_project_config()

    # Plain name → hf:// prefix
    core_config.project.set_config("runtime.model_name", "meta-llama/Llama-3.1-8B")
    assert runtime_config.get_model_uri() == "hf://meta-llama/Llama-3.1-8B"

    # OCI URI → passed through
    core_config.project.set_config("runtime.model_name", "oci://registry.example.com/model:tag")
    assert runtime_config.get_model_uri() == "oci://registry.example.com/model:tag"

    # HF URI → passed through
    core_config.project.set_config("runtime.model_name", "hf://Qwen/Qwen3-0.6B")
    assert runtime_config.get_model_uri() == "hf://Qwen/Qwen3-0.6B"
