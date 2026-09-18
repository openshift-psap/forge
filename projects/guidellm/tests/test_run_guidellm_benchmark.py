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
        image="ghcr.io/vllm-project/guidellm:v0.5.4",
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
    assert "--rate=" not in script
    assert "kind=openai_http,target=https://example.test/llm-d" in script
    assert "prefix_count=64" in script
    assert "prefix_count=128" in script
    assert "max-requests=320" in script
    assert "max-requests=640" in script
    assert "benchmarks-rate-32.json" in script
    assert "benchmarks-rate-64.json" in script


def test_render_guidellm_job_from_parts_keeps_plain_rates_as_single_guidellm_run() -> None:
    manifest = render_guidellm_job_from_parts(
        namespace="forge-llm-d",
        name="guidellm-benchmark",
        image="ghcr.io/vllm-project/guidellm:v0.5.4",
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
    assert container["args"] == [
        "run",
        "--backend=kind=openai_http,target=https://example.test/llm-d",
        "--backend-type=openai_http",
        "--rate-type=concurrent",
        "--rate=300,200,100",
        "--data=prompt_tokens=1000,output_tokens=1000",
        "--max-seconds=600",
    ]


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
