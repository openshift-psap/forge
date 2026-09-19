from __future__ import annotations

import pytest

from projects.guidellm.toolbox.run_guidellm_benchmark import build_guidellm_args
from projects.guidellm.toolbox.run_guidellm_benchmark.utils import (
    expand_guidellm_runs,
    render_guidellm_job_from_parts,
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
        "--rate=32",
        "--data=prompt_tokens=128,prefix_count=64",
        "--max-requests=320",
    ]
    assert runs[1].args == [
        "--backend-type=openai_http",
        "--rate-type=concurrent",
        "--rate=64",
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
        "--rate=32",
        "--max-requests=32",
    ]
    assert runs[1].args == [
        "--backend-type=openai_http",
        "--rate-type=concurrent",
        "--rate=64",
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
        image="ghcr.io/vllm-project/guidellm:v0.7.4",
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
        image="ghcr.io/vllm-project/guidellm:v0.7.4",
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
    profile_arg = next(a for a in args if a.startswith("--profile="))
    assert '"kind": "concurrent"' in profile_arg
    assert "[300, 200, 100]" in profile_arg
    assert "--constraint=kind=max_duration,seconds=600" in args


def test_build_guidellm_args_renders_list_values() -> None:
    benchmark = {
        "outputs": "json",
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
        "--outputs=json",
    ]


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
        assert "--output=kind=json,path=/results/benchmarks.json" in args

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
        profile_arg = next(a for a in args if a.startswith("--profile="))
        assert "0.5" in profile_arg
        assert "1.5" in profile_arg

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
        assert "--output=kind=json,path=/results/benchmarks.json" in args

    def test_processor_converted_to_tokenizer(self) -> None:
        args = self._build(
            "http://model:8000",
            ["--processor=meta-llama/Llama-3.1-8B-Instruct"],
        )
        assert "--tokenizer=kind=huggingface_auto,model=meta-llama/Llama-3.1-8B-Instruct" in args
        assert not any("--processor" in a for a in args)

    def test_processor_args_stripped(self) -> None:
        args = self._build(
            "http://model:8000",
            ["--processor=gpt2", '--processor-args={"use_fast": false}'],
        )
        assert "--tokenizer=kind=huggingface_auto,model=gpt2" in args
        assert not any("--processor-args" in a for a in args)

    def test_rampup_in_no_rate_branch(self) -> None:
        args = self._build(
            "http://model:8000",
            ["--rate-type=throughput", "--rampup=30"],
        )
        profile_arg = next(a for a in args if a.startswith("--profile="))
        assert "rampup_duration=30" in profile_arg
