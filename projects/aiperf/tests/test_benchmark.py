"""AI Perf execution contract and Caliper mapping."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from projects.aiperf.library.loadgenerator.rhaiis import AIPerfGenerator
from projects.aiperf.postprocess.results import AIPerfParser, compute_aiperf_kpis
from projects.aiperf.toolbox.run_aiperf_benchmark import main as aiperf_command
from projects.aiperf.toolbox.run_aiperf_benchmark.main import (
    DEFAULT_IMAGE,
    build_job_manifest,
    build_profile_command,
)
from projects.caliper.engine.model import BaseTestNode, UnifiedRunModel
from projects.llm_d.postprocess.llm_d.plugin import LlmDGuideLLMPlugin
from projects.rhaiis.orchestration import runtime_config as rhaiis_config
from projects.rhaiis.orchestration.loadgenerator.base import BenchmarkContext


def _profile(**overrides):
    values = dict(
        endpoint_url="http://model.example:8000",
        model_name="Qwen/Qwen3-0.6B",
        tokenizer=None,
        endpoint_type="chat",
        endpoint_path="/v1/chat/completions",
        streaming=True,
        concurrency=1,
        request_rate=None,
        request_count=10,
        input_tokens=128,
        output_tokens=64,
        dataset_type=None,
        fixed_schedule=False,
        fixed_schedule_auto_offset=False,
    )
    values.update(overrides)
    return build_profile_command(**values)


def test_profile_command_uses_summary_only_and_no_shell() -> None:
    command = _profile()
    assert command[:2] == ["aiperf", "profile"]
    assert command[command.index("--export-level") + 1] == "summary"
    assert "--synthetic-input-tokens-mean" in command
    assert "--concurrency" in command
    assert "sh" not in command


@pytest.mark.parametrize(
    "overrides",
    [
        {"concurrency": 1, "request_rate": 2.0},
        {"concurrency": None},
        {"request_count": 0},
        {"endpoint_url": "https://user:secret@model.example"},
        {"fixed_schedule_auto_offset": True},
    ],
)
def test_profile_command_rejects_invalid_inputs(overrides: dict) -> None:
    with pytest.raises(ValueError):
        _profile(**overrides)


def test_job_uses_prebuilt_quay_image_and_deadline() -> None:
    manifest = build_job_manifest(
        name="aiperf-smoke",
        namespace="bench",
        image=DEFAULT_IMAGE,
        pvc_name="aiperf-smoke-results",
        command=_profile(),
        timeout=300,
        image_pull_secret=None,
        dataset_url=None,
        dataset_sha256=None,
    )
    assert manifest["spec"]["activeDeadlineSeconds"] == 300
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "quay.io/rh-ee-thibrahi/aiperf:0.12.0"
    assert container["command"][0] == "aiperf"
    assert "initContainers" not in manifest["spec"]["template"]["spec"]


def test_rhaiis_adapter_uses_aiperf_workload_and_tool_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(aiperf_command, "run", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(
        rhaiis_config,
        "get_aiperf_config",
        lambda: {"image": DEFAULT_IMAGE, "timeout": 300, "pvc_size": "5Gi"},
    )
    context = BenchmarkContext(
        deployment_name="qwen-server",
        namespace="test-ns",
        endpoint_url="http://qwen-server.test-ns:8080",
        benchmark_cfg={},
        model_cfg={"hf_model_id": "Qwen/Qwen3-0.6B"},
        workload={"request_count": 10, "concurrency": 1, "timeout_seconds": 600},
        workload_key="aiperf-smoke",
        benchmark_timeout=14400,
    )

    generator = AIPerfGenerator()
    assert not generator.supports_warmup
    generator.run(context)

    assert calls[0]["name"] == "aiperf-bench-aiperf-smoke-qwen-server"
    assert calls[0]["image"] == DEFAULT_IMAGE
    assert calls[0]["timeout"] == 600
    assert calls[0]["request_count"] == 10
    assert calls[0]["concurrency"] == 1


def test_aiperf_summary_routes_to_tool_specific_kpis(tmp_path: Path) -> None:
    summary = tmp_path / "profile_export_aiperf.json"
    summary.write_text(
        json.dumps(
            {
                "request_count": {"unit": "requests", "avg": 10},
                "request_throughput": {"unit": "requests/sec", "avg": 2.5},
                "time_to_first_token": {"unit": "ms", "avg": 20, "p95": 35},
            }
        ),
        encoding="utf-8",
    )
    node = BaseTestNode(
        directory=tmp_path,
        test_path=Path("benchmark_aiperf-smoke"),
        artifact_paths=[summary],
        test_labels={"labels": {"benchmark_tool": "aiperf", "benchmark_key": "aiperf-smoke"}},
    )
    parsed = LlmDGuideLLMPlugin().parse([node])
    assert len(parsed.records) == 1
    assert parsed.records[0].run_identity == {"aiperf": True}
    model = UnifiedRunModel(
        plugin_module="projects.llm_d.postprocess.llm_d.plugin",
        base_directory=str(tmp_path),
        test_nodes=[node],
        unified_result_records=parsed.records,
    )
    rows, status = LlmDGuideLLMPlugin().compute_kpis(model)
    assert status.success
    by_id = {row.kpi_id: row for row in rows}
    assert by_id["aiperf_request_throughput_avg"].value == 2.5
    assert by_id["aiperf_time_to_first_token_p95"].unit == "ms"


def test_missing_summary_and_wrong_units_fail(tmp_path: Path) -> None:
    node = BaseTestNode(
        directory=tmp_path,
        test_path=Path("benchmark_aiperf"),
        artifact_paths=[],
        test_labels={"labels": {"benchmark_tool": "aiperf"}},
    )
    with pytest.raises(ValueError, match="Expected one AI Perf summary"):
        AIPerfParser().parse([node])
    summary = tmp_path / "profile_export_aiperf.json"
    summary.write_text(
        json.dumps(
            {
                "request_count": {"unit": "requests", "avg": 1},
                "request_throughput": {"unit": "ms", "avg": 2},
            }
        ),
        encoding="utf-8",
    )
    node.artifact_paths.append(summary)
    parsed = AIPerfParser().parse([node])
    model = UnifiedRunModel("aiperf", str(tmp_path), [node], parsed.records)
    with pytest.raises(ValueError, match="unexpected unit"):
        compute_aiperf_kpis(model)
