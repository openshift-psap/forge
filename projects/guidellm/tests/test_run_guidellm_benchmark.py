from __future__ import annotations

import pytest

from projects.guidellm.toolbox.run_guidellm_benchmark import build_guidellm_args
from projects.guidellm.toolbox.run_guidellm_benchmark.utils import (
    _build_multi_run_script,
    expand_guidellm_runs,
    render_guidellm_job_from_parts,
    render_guidellm_shared_volume_job_from_parts,
)


def test_expand_guidellm_runs_converts_rates_to_individual_runs() -> None:
    runs = expand_guidellm_runs(
        [
            "--backend-type=openai_http",
            "--rate-type=concurrent",
            "--rate=32,64",
            "--data=prompt_tokens=128,prefix_count={2*rate}",
            "--max-requests={10*rate}",
        ]
    )

    assert [run.rate for run in runs] == ["32", "64"]
    assert runs[0].args == [
        "--backend-type=openai_http",
        "--rate-type=concurrent",
        "--data=prompt_tokens=128,prefix_count=64",
        "--max-requests=320",
    ]
    assert runs[1].args == [
        "--backend-type=openai_http",
        "--rate-type=concurrent",
        "--data=prompt_tokens=128,prefix_count=128",
        "--max-requests=640",
    ]


def test_expand_guidellm_runs_expands_plain_rate_reference() -> None:
    runs = expand_guidellm_runs(
        [
            "--backend-type=openai_http",
            "--rate-type=concurrent",
            "--rate=32,64",
            "--max-requests={rate}",
        ]
    )

    assert [run.rate for run in runs] == ["32", "64"]
    assert runs[0].args == [
        "--backend-type=openai_http",
        "--rate-type=concurrent",
        "--max-requests=32",
    ]
    assert runs[1].args == [
        "--backend-type=openai_http",
        "--rate-type=concurrent",
        "--max-requests=64",
    ]


def test_expand_guidellm_runs_keeps_plain_multi_rate_benchmark_as_single_run() -> None:
    runs = expand_guidellm_runs(
        [
            "--backend-type=openai_http",
            "--rate-type=concurrent",
            "--rate=300,200,100",
            "--data=prompt_tokens=1000,output_tokens=1000",
            "--max-seconds=600",
        ]
    )

    assert len(runs) == 1
    assert runs[0].rate is None
    assert runs[0].args == [
        "--backend-type=openai_http",
        "--rate-type=concurrent",
        "--rate=300,200,100",
        "--data=prompt_tokens=1000,output_tokens=1000",
        "--max-seconds=600",
    ]


def test_expand_guidellm_runs_rejects_rate_expressions_without_rate_arg() -> None:
    with pytest.raises(ValueError, match="require a '--rate=' argument"):
        expand_guidellm_runs(
            [
                "--backend-type=openai_http",
                "--rate-type=concurrent",
                "--data=prompt_tokens=128,prefix_count={2*rate}",
                "--max-requests={10*rate}",
            ]
        )


def test_expand_guidellm_runs_rejects_empty_rate_arg_when_rate_expressions_are_used() -> None:
    with pytest.raises(ValueError, match="must include at least one non-empty value"):
        expand_guidellm_runs(
            [
                "--backend-type=openai_http",
                "--rate-type=concurrent",
                "--rate=",
                "--max-requests={rate}",
            ]
        )


def test_render_guidellm_job_from_parts_uses_shell_for_multi_run_benchmarks() -> None:
    manifest = render_guidellm_job_from_parts(
        namespace="forge-llm-d",
        name="guidellm-benchmark",
        image="example/guidellm:test",
        endpoint_url="https://example.test/llm-d",
        timeout_seconds=3600,
        guidellm_args=[
            "--backend-type=openai_http",
            "--rate-type=concurrent",
            "--rate=32,64",
            "--data=prompt_tokens=128,prefix_count={2*rate}",
            "--max-requests={10*rate}",
        ],
    )

    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert manifest["spec"]["activeDeadlineSeconds"] == 3600
    assert container["command"] == ["/bin/sh", "-lc"]
    script = container["args"][0]
    assert "guidellm run" in script
    assert "kind=openai_http" in script
    assert "prefix_count=64" in script
    assert "prefix_count=128" in script
    assert "max_requests" in script
    assert "benchmarks-rate-32.json" in script
    assert "benchmarks-rate-64.json" in script


def test_render_guidellm_job_from_parts_keeps_plain_rates_as_single_guidellm_run() -> None:
    manifest = render_guidellm_job_from_parts(
        namespace="forge-llm-d",
        name="guidellm-benchmark",
        image="example/guidellm:test",
        endpoint_url="https://example.test/llm-d",
        timeout_seconds=3600,
        guidellm_args=[
            "--backend-type=openai_http",
            "--rate-type=concurrent",
            "--rate=300,200,100",
            "--data=prompt_tokens=1000,output_tokens=1000",
            "--max-seconds=600",
        ],
    )

    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["/opt/app-root/bin/guidellm"]
    args = container["args"]
    assert args[0] == "run"
    assert "--backend=kind=openai_http,target=https://example.test/llm-d" in args
    assert "--data=kind=synthetic_text,prompt_tokens=1000,output_tokens=1000" in args
    assert "--profile=kind=concurrent" in args
    override_index = args.index("--override")
    assert args[override_index : override_index + 3] == [
        "--override",
        "profile.streams",
        "300,200,100",
    ]
    assert "--constraint=kind=max_duration,seconds=600" in args


def test_build_guidellm_args_renders_list_values() -> None:
    benchmark = {
        "args": {
            "backend_type": "openai_http",
            "rate_type": "concurrent",
            "rate": [300, 200, 100, 50, 1],
            "max_seconds": 600,
        },
    }

    assert build_guidellm_args(benchmark) == [
        "--backend-type=openai_http",
        "--rate-type=concurrent",
        "--rate=300,200,100,50,1",
        "--max-seconds=600",
    ]


def test_build_guidellm_args_renders_top_level_rate_list() -> None:
    benchmark = {
        "args": {
            "profile": "kind=concurrent,streams={rate}",
            "data": "kind=synthetic_text,prompt_tokens=256,output_tokens=128",
            "constraint": "kind=max_duration,seconds=120",
        },
        "rate": [1, 4, 8],
    }

    args = build_guidellm_args(benchmark)
    assert "--rate=1,4,8" in args


class TestConvertDataSpec:
    def _convert(self, spec: str) -> str:
        from projects.guidellm.toolbox.run_guidellm_benchmark.utils import _convert_data_spec

        return _convert_data_spec(spec)

    def test_synthetic_text_from_token_spec(self) -> None:
        assert (
            self._convert("prompt_tokens=1000,output_tokens=100")
            == "kind=synthetic_text,prompt_tokens=1000,output_tokens=100"
        )

    def test_json_file_from_path(self) -> None:
        assert self._convert("/data/test.json") == "kind=json_file,path=/data/test.json"

    def test_csv_file_from_path(self) -> None:
        assert self._convert("dataset.csv") == "kind=csv_file,path=dataset.csv"

    def test_huggingface_from_slash_source(self) -> None:
        assert (
            self._convert("abisee/cnn_dailymail") == "kind=huggingface,source=abisee/cnn_dailymail"
        )

    def test_passthrough_already_converted(self) -> None:
        spec = "kind=synthetic_text,prompt_tokens=256"
        assert self._convert(spec) == spec


class TestBuildRunArgs:
    def _build(self, endpoint: str, old_args: list[str]) -> list[str]:
        from projects.guidellm.toolbox.run_guidellm_benchmark.utils import _build_run_args

        return _build_run_args(endpoint, old_args)

    def test_single_rate_concurrent(self) -> None:
        args = self._build(
            "http://model:8000",
            [
                "--backend-type=openai_http",
                "--rate-type=concurrent",
                "--rate=16",
                "--data=prompt_tokens=256,output_tokens=128",
                "--max-seconds=60",
            ],
        )
        assert "--backend=kind=openai_http,target=http://model:8000" in args
        assert "--data=kind=synthetic_text,prompt_tokens=256,output_tokens=128" in args
        assert "--profile=kind=concurrent,streams=16" in args
        assert "--constraint=kind=max_duration,seconds=60" in args
        assert "--output=kind=json,path=/results/benchmarks-default.json" in args

    def test_warmup_injected_into_profile(self) -> None:
        args = self._build(
            "http://model:8000",
            ["--rate-type=concurrent", "--rate=8", "--warmup=75"],
        )
        profile_arg = next(a for a in args if a.startswith("--profile="))
        assert "warmup=75" in profile_arg
        assert not any(a.startswith("--warmup") for a in args)

    def test_rampup_injected_into_profile(self) -> None:
        args = self._build(
            "http://model:8000",
            ["--rate-type=concurrent", "--rate=8", "--rampup=35"],
        )
        profile_arg = next(a for a in args if a.startswith("--profile="))
        assert "rampup_duration=35" in profile_arg

    def test_decimal_rates_preserved(self) -> None:
        args = self._build(
            "http://model:8000",
            ["--rate-type=concurrent", "--rate=0.5,1.5"],
        )
        override_index = args.index("--override")
        assert args[override_index : override_index + 3] == [
            "--override",
            "profile.streams",
            "0.5,1.5",
        ]

    def test_model_included_in_backend(self) -> None:
        args = self._build(
            "http://model:8000",
            ["--model=meta-llama/Llama-3.1-8B-Instruct"],
        )
        backend_arg = next(a for a in args if a.startswith("--backend="))
        assert "model=meta-llama/Llama-3.1-8B-Instruct" in backend_arg

    def test_max_requests_constraint(self) -> None:
        args = self._build(
            "http://model:8000",
            ["--max-requests=500"],
        )
        assert "--constraint=kind=max_requests,count=500" in args

    def test_outputs_stripped(self) -> None:
        args = self._build(
            "http://model:8000",
            ["--outputs=json", "--output-dir=/tmp"],
        )
        assert not any("--outputs" in a for a in args)
        assert not any("--output-dir" in a for a in args)
        assert "--output=kind=json,path=/results/benchmarks-default.json" in args

    def test_processor_converted_to_tokenizer(self) -> None:
        args = self._build(
            "http://model:8000",
            ["--processor=meta-llama/Llama-3.1-8B-Instruct"],
        )
        assert "--tokenizer=kind=huggingface_auto,model=meta-llama/Llama-3.1-8B-Instruct" in args
        assert not any("--processor" in a for a in args)

    def test_processor_args_preserved_as_load_kwargs(self) -> None:
        args = self._build(
            "http://model:8000",
            ["--processor=gpt2", '--processor-args={"use_fast": false}'],
        )
        import json

        expected = json.dumps(
            {"kind": "huggingface_auto", "model": "gpt2", "load_kwargs": {"use_fast": False}}
        )
        assert f"--tokenizer={expected}" in args
        assert not any("--processor-args" in a for a in args)

    def test_request_type_converted_to_backend_request_format(self) -> None:
        args = self._build(
            "http://model:8000",
            ["--request-type=text_completions"],
        )
        backend_arg = next(a for a in args if a.startswith("--backend="))
        assert "request_format=text_completions" in backend_arg
        assert not any("--request-type" in a for a in args)

    def test_backend_arg_merged_into_backend_spec(self) -> None:
        args = self._build(
            "http://model:8000",
            ["--backend=request_format=/v1/completions"],
        )
        backend_arg = next(a for a in args if a.startswith("--backend="))
        assert "request_format=/v1/completions" in backend_arg
        assert "target=http://model:8000" in backend_arg
        # Only one --backend= arg should exist
        assert sum(1 for a in args if a.startswith("--backend=")) == 1

    def test_rampup_in_no_rate_branch(self) -> None:
        args = self._build(
            "http://model:8000",
            ["--rate-type=throughput", "--rampup=30"],
        )
        profile_arg = next(a for a in args if a.startswith("--profile="))
        assert "rampup_duration=30" in profile_arg


class TestApiKeySecret:
    def test_render_shared_volume_job_with_api_key_secret(self) -> None:
        manifest = render_guidellm_shared_volume_job_from_parts(
            namespace="forge-llm-d",
            name="guidellm-benchmark",
            image="ghcr.io/vllm-project/guidellm:v0.8.1",
            endpoint_url="https://example.test/llm-d",
            timeout_seconds=3600,
            guidellm_args=["--backend-type=openai_http"],
            api_key_secret="my-api-key-secret",
        )

        pod_spec = manifest["spec"]["template"]["spec"]
        containers = pod_spec["containers"]
        main_container = containers[0]

        # Secret volume should be present
        volume_names = [v["name"] for v in pod_spec["volumes"]]
        assert "auth-token" in volume_names

        api_key_volume = next(v for v in pod_spec["volumes"] if v["name"] == "auth-token")
        assert api_key_volume["secret"]["secretName"] == "my-api-key-secret"

        # Volume mount should be on the main container
        mount_names = [m["name"] for m in main_container["volumeMounts"]]
        assert "auth-token" in mount_names

        api_key_mount = next(m for m in main_container["volumeMounts"] if m["name"] == "auth-token")
        assert api_key_mount["mountPath"] == "/secrets/auth-token"
        assert api_key_mount["readOnly"] is True

    def test_render_job_with_api_key_secret(self) -> None:
        manifest = render_guidellm_job_from_parts(
            namespace="forge-llm-d",
            name="guidellm-benchmark",
            image="ghcr.io/vllm-project/guidellm:v0.8.1",
            endpoint_url="https://example.test/llm-d",
            timeout_seconds=3600,
            guidellm_args=["--backend-type=openai_http"],
            api_key_secret="my-api-key-secret",
        )

        pod_spec = manifest["spec"]["template"]["spec"]
        volume_names = [v["name"] for v in pod_spec["volumes"]]
        assert "auth-token" in volume_names

    def test_api_key_forces_shell_mode(self) -> None:
        manifest = render_guidellm_shared_volume_job_from_parts(
            namespace="forge-llm-d",
            name="guidellm-benchmark",
            image="ghcr.io/vllm-project/guidellm:v0.8.1",
            endpoint_url="https://example.test/llm-d",
            timeout_seconds=3600,
            guidellm_args=["--backend-type=openai_http"],
            api_key_secret="my-api-key-secret",
        )

        main_container = manifest["spec"]["template"]["spec"]["containers"][0]
        assert main_container["command"] == ["/bin/sh", "-c"]
        script = main_container["args"][0]
        assert "GUIDELLM_API_KEY" in script
        assert "set +x" in script

    def test_build_multi_run_script_with_api_key(self) -> None:
        script = _build_multi_run_script(
            endpoint_url="https://example.test/llm-d",
            runs=[
                expand_guidellm_runs(["--backend-type=openai_http"])[0],
            ],
            api_key_file="/secrets/auth-token/auth_token",
        )

        lines = script.split("\n")
        assert any("GUIDELLM_API_KEY=$(cat" in line for line in lines)
        assert any("api_key=$GUIDELLM_API_KEY" in line for line in lines)

        # Tracing must be disabled before reading the key and re-enabled only after the command
        set_plus_x_idx = lines.index("set +x")
        cat_idx = next(i for i, line in enumerate(lines) if "GUIDELLM_API_KEY=$(cat" in line)
        cmd_idx = next(i for i, line in enumerate(lines) if "guidellm" in line)
        set_minus_x_idx = next(i for i in range(len(lines) - 1, -1, -1) if lines[i] == "set -x")
        assert set_plus_x_idx < cat_idx < cmd_idx < set_minus_x_idx

    def test_no_api_key_secret_omits_volume(self) -> None:
        manifest = render_guidellm_shared_volume_job_from_parts(
            namespace="forge-llm-d",
            name="guidellm-benchmark",
            image="ghcr.io/vllm-project/guidellm:v0.8.1",
            endpoint_url="https://example.test/llm-d",
            timeout_seconds=3600,
            guidellm_args=["--backend-type=openai_http"],
        )

        pod_spec = manifest["spec"]["template"]["spec"]
        volume_names = [v["name"] for v in pod_spec["volumes"]]
        assert "auth-token" not in volume_names


class TestReplicaCount:
    def test_render_shared_volume_job_with_replicas(self) -> None:
        manifest = render_guidellm_shared_volume_job_from_parts(
            namespace="forge-llm-d",
            name="guidellm-benchmark",
            image="ghcr.io/vllm-project/guidellm:v0.8.1",
            endpoint_url="https://example.test/llm-d",
            timeout_seconds=3600,
            guidellm_args=["--backend-type=openai_http"],
            replica_count=4,
        )

        job_spec = manifest["spec"]
        assert job_spec["completionMode"] == "Indexed"
        assert job_spec["completions"] == 4
        assert job_spec["parallelism"] == 4

    def test_render_job_with_replicas(self) -> None:
        manifest = render_guidellm_job_from_parts(
            namespace="forge-llm-d",
            name="guidellm-benchmark",
            image="ghcr.io/vllm-project/guidellm:v0.8.1",
            endpoint_url="https://example.test/llm-d",
            timeout_seconds=3600,
            guidellm_args=["--backend-type=openai_http"],
            replica_count=3,
        )

        job_spec = manifest["spec"]
        assert job_spec["completionMode"] == "Indexed"
        assert job_spec["completions"] == 3
        assert job_spec["parallelism"] == 3

    def test_single_replica_omits_indexed_mode(self) -> None:
        manifest = render_guidellm_shared_volume_job_from_parts(
            namespace="forge-llm-d",
            name="guidellm-benchmark",
            image="ghcr.io/vllm-project/guidellm:v0.8.1",
            endpoint_url="https://example.test/llm-d",
            timeout_seconds=3600,
            guidellm_args=["--backend-type=openai_http"],
            replica_count=1,
        )

        job_spec = manifest["spec"]
        assert "completionMode" not in job_spec
        assert "completions" not in job_spec
        assert "parallelism" not in job_spec

    def test_build_multi_run_script_with_replicas(self) -> None:
        script = _build_multi_run_script(
            endpoint_url="https://example.test/llm-d",
            runs=[
                expand_guidellm_runs(["--backend-type=openai_http"])[0],
            ],
            replica_count=4,
        )

        assert "$JOB_COMPLETION_INDEX" in script
        assert "benchmarks-default-$JOB_COMPLETION_INDEX.json" in script

    def test_build_multi_run_script_single_replica_no_index(self) -> None:
        script = _build_multi_run_script(
            endpoint_url="https://example.test/llm-d",
            runs=[
                expand_guidellm_runs(["--backend-type=openai_http"])[0],
            ],
            replica_count=1,
        )

        assert "JOB_COMPLETION_INDEX" not in script
        assert "benchmarks-default.json" in script

    def test_replicas_forces_shell_mode(self) -> None:
        manifest = render_guidellm_shared_volume_job_from_parts(
            namespace="forge-llm-d",
            name="guidellm-benchmark",
            image="ghcr.io/vllm-project/guidellm:v0.8.1",
            endpoint_url="https://example.test/llm-d",
            timeout_seconds=3600,
            guidellm_args=["--backend-type=openai_http"],
            replica_count=2,
        )

        main_container = manifest["spec"]["template"]["spec"]["containers"][0]
        assert main_container["command"] == ["/bin/sh", "-c"]
        script = main_container["args"][0]
        assert "$JOB_COMPLETION_INDEX" in script


class TestApiKeyAndReplicas:
    def test_combined_api_key_and_replicas(self) -> None:
        script = _build_multi_run_script(
            endpoint_url="https://example.test/llm-d",
            runs=[
                expand_guidellm_runs(["--backend-type=openai_http"])[0],
            ],
            api_key_file="/secrets/auth-token/auth_token",
            replica_count=4,
        )

        assert "GUIDELLM_API_KEY" in script
        assert "api_key=$GUIDELLM_API_KEY" in script
        assert "$JOB_COMPLETION_INDEX" in script
        assert "set +x" in script
