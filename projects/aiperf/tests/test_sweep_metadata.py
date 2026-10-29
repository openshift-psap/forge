"""Exercise persisted sweep membership from each orchestration path to Caliper."""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from projects.aiperf.library.loadgenerator.llm_d import AIPerfGenerator as LlmDGenerator
from projects.aiperf.library.loadgenerator.rhaiis import AIPerfGenerator as RhaiisGenerator
from projects.aiperf.postprocess.results import AIPerfParser, compute_aiperf_kpis
from projects.aiperf.toolbox.run_aiperf_benchmark import main as aiperf_command
from projects.caliper.engine.kpi.format import write_kpis_in_format
from projects.caliper.engine.kpi.html_generator import generate_kpi_html_from_file
from projects.caliper.engine.kpi.kpis_to_mlflow import generate_metrics_from_kpis
from projects.caliper.engine.model import UnifiedRunModel
from projects.caliper.engine.traverse import discover_test_bases
from projects.core.library import config, env
from projects.inference_playbooks.orchestration import test_phase as playbooks
from projects.llm_d.orchestration import test_phase as llmd
from projects.rhaiis.orchestration import test_phase as rhaiis


@pytest.mark.parametrize("project", ["inference-playbooks", "llm-d", "rhaiis"])
def test_persisted_sweep_membership_survives_discovery_and_parsing(tmp_path, monkeypatch, project):
    monkeypatch.setattr(env, "ARTIFACT_DIR", tmp_path)
    counter = 0

    @contextmanager
    def next_artifact_dir(name):
        nonlocal counter
        counter += 1
        parent = env.ARTIFACT_DIR
        directory = parent / f"{counter:03d}__{name}"
        directory.mkdir()
        monkeypatch.setattr(env, "ARTIFACT_DIR", directory)
        try:
            yield
        finally:
            monkeypatch.setattr(env, "ARTIFACT_DIR", parent)

    monkeypatch.setattr(env, "NextArtifactDir", next_artifact_dir)
    monkeypatch.setattr(
        config,
        "project",
        SimpleNamespace(
            get_config=lambda _key, default=None: default, save_config=lambda **_: None
        ),
    )

    def write_summary(**kwargs):
        summary = {
            "request_count": {"avg": 10, "unit": "requests"},
            "request_throughput": {"avg": kwargs["concurrency"] * 2, "unit": "requests/sec"},
        }
        (env.ARTIFACT_DIR / "profile_export_aiperf.json").write_text(json.dumps(summary))

    monkeypatch.setattr(aiperf_command, "run", write_summary)
    workload = {"tool": "aiperf", "concurrency": [8, 4], "request_count": 10}
    tool_config = {"image": "test/aiperf", "timeout": 60, "pvc_size": "1Gi"}

    if project == "inference-playbooks":
        monkeypatch.setattr(
            config, "Config", lambda _: SimpleNamespace(get_config=lambda _: tool_config)
        )

        def invoke():
            playbooks._run_aiperf_workload(
                "http://serving:8000",
                "test-ns",
                "deployment-a",
                "test/model",
                "test/model",
                "agentx",
                workload,
                {"model_name": "test/model", "deployment": "deployment-a"},
            )

    elif project == "llm-d":
        monkeypatch.setattr(llmd, "update_test_labels_with_timing", lambda *_: None)
        monkeypatch.setattr(
            llmd, "extract_kpi_labels_from_config", lambda: {"model_name": "test/model"}
        )
        monkeypatch.setattr(llmd.runtime_config, "get_benchmark_keys", lambda: ["agentx"])
        monkeypatch.setattr(llmd.runtime_config, "get_workload_config", lambda: {})
        monkeypatch.setattr(llmd.runtime_config, "get_model_name", lambda: "test/model")
        monkeypatch.setattr(llmd.runtime_config, "get_served_model_name", lambda: "test/model")
        monkeypatch.setattr(llmd.runtime_config, "get_namespace", lambda: "test-ns")
        benchmark = {
            **workload,
            **tool_config,
            "job_name": "aiperf-test",
            "timeout_seconds": 60,
        }

        def invoke():
            llmd.run_benchmark(
                tmp_path,
                endpoint_url="http://serving:8000",
                benchmark=benchmark,
                generator=LlmDGenerator(),
            )

    else:
        monkeypatch.setattr(rhaiis.runtime_config, "get_workload", lambda _: workload)
        monkeypatch.setattr(rhaiis.runtime_config, "get_aiperf_config", lambda: tool_config)

        def invoke():
            rhaiis._run_workload_benchmark(
                model_key="test-model",
                workload_key="agentx",
                model_cfg={"hf_model_id": "test/model"},
                accelerator="h200",
                accelerator_key="h200",
                gpu_type="h200",
                serving_image="test/vllm:v1",
                engine_args={},
                benchmark_cfg={},
                deployment_name="deployment-a",
                namespace="test-ns",
                endpoint_url="http://serving:8000",
                benchmark_timeout=60,
                generator=RhaiisGenerator(),
                run_uuid="deployment-a",
                version="v1",
                cluster_tag="test",
            )

    # Same workload/deployment twice must never become one curve, even when
    # every other label and concurrency point is identical.
    invoke()
    invoke()
    nodes, _ = discover_test_bases(tmp_path)
    assert len(nodes) == 4
    sweep_ids = {node.test_labels["labels"]["sweep_id"] for node in nodes}
    assert len(sweep_ids) == 2
    for sweep_id in sweep_ids:
        members = [node for node in nodes if node.test_labels["labels"]["sweep_id"] == sweep_id]
        assert {node.test_labels["labels"]["concurrency"] for node in members} == {"4", "8"}

    records = AIPerfParser().parse(nodes).records
    model = UnifiedRunModel(
        "projects.inference_playbooks.postprocess.plugin", str(tmp_path), nodes, records
    )
    rows, status = compute_aiperf_kpis(model)
    assert status.success
    assert all(not row.is_curve for row in rows)
    output = tmp_path / "kpis.json"
    write_kpis_in_format([row.to_dict() for row in rows], output, model=model)
    original_json = output.read_bytes()
    original_directories = {path for path in tmp_path.rglob("*") if path.is_dir()}
    html = generate_kpi_html_from_file(output).read_text()
    assert output.read_bytes() == original_json
    assert {path for path in tmp_path.rglob("*") if path.is_dir()} == original_directories
    for sweep_id in sweep_ids:
        assert f"name: 'sweep/{sweep_id}'" in html
    assert html.count("const rawData = [[4.0, 8.0], [8.0, 16.0]];") == 2

    # HTML-only series must not create unmatched virtual MLflow test entries.
    exported = generate_metrics_from_kpis(output, tmp_path)
    assert exported.status == "success"
    assert exported.tests_processed == len(nodes)
    assert not exported.warnings
    for node in nodes:
        metrics = json.loads((node.directory / "metrics.json").read_text())
        assert (
            metrics["aiperf_request_throughput_avg"]
            == int(node.test_labels["labels"]["concurrency"]) * 2
        )
        assert not any(key.endswith("_vs_concurrency") for key in metrics)
