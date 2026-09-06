from __future__ import annotations

import csv
import json

import yaml

from projects.caliper.engine.model import TestBaseNode as CaliperTestBaseNode
from projects.caliper.engine.model import UnifiedRunModel
from projects.llm_d.postprocess.llm_d.csv_dashboard import DASHBOARD_FIELDNAMES
from projects.llm_d.postprocess.llm_d.plugin import LlmDGuideLLMPlugin


def _metric(**values):
    return {"successful": values}


def test_llmd_plugin_exports_dashboard_compatible_csv(tmp_path):
    benchmark_path = tmp_path / "benchmarks.json"
    benchmark_path.write_text(
        json.dumps(
            {
                "metadata": {"guidellm_version": "0.5.3"},
                "args": {"data": ["prompt_tokens=1000,output_tokens=1000"]},
                "benchmarks": [
                    {
                        "config": {
                            "run_id": "guidellm-run-1",
                            "strategy": {"type_": "concurrent", "streams": 8},
                        },
                        "scheduler": {"state": {"start_time": 10, "end_time": 20}},
                        "scheduler_metrics": {
                            "start_time": 10,
                            "end_time": 20,
                            "requests_made": {"successful": 80, "errored": 2},
                        },
                        "metrics": {
                            "requests_per_second": _metric(mean=7.5),
                            "request_concurrency": _metric(mean=7.8),
                            "output_tokens_per_second": {
                                "successful": {"mean": 900},
                                "total": {"mean": 900},
                            },
                            "tokens_per_second": {"total": {"mean": 1800}},
                            "time_to_first_token_ms": _metric(
                                mean=12,
                                median=10,
                                percentiles={"p01": 2, "p95": 20, "p99": 25, "p999": 30},
                            ),
                            "time_per_output_token_ms": _metric(
                                median=3,
                                percentiles={"p01": 1, "p95": 5, "p99": 6, "p999": 7},
                            ),
                            "inter_token_latency_ms": _metric(
                                mean=4,
                                median=3,
                                percentiles={"p01": 1, "p95": 6, "p99": 7, "p999": 8},
                            ),
                            "request_latency": _metric(median=100, min=80, max=140),
                            "prompt_token_count": _metric(mean=1000, percentiles={"p99": 1000}),
                            "output_token_count": _metric(mean=1000, percentiles={"p99": 1000}),
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    llmisvc_path = tmp_path / "llminferenceservice.yaml"
    llmisvc_path.write_text(
        yaml.safe_dump(
            {
                "metadata": {
                    "annotations": {"forge.openshift.io/deployment-profile": "precise-prefix-cache"}
                },
                "spec": {
                    "replicas": 4,
                    "model": {"name": "redhatai-llama-3-3-70b-instruct"},
                    "parallelism": {"tensor": 2},
                    "router": {"scheduler": {"config": "precise"}},
                    "template": {
                        "containers": [
                            {
                                "image": "registry.example/vllm:ea2",
                                "env": [
                                    {
                                        "name": "VLLM_ADDITIONAL_ARGS",
                                        "value": "--enable-prefix-caching",
                                    }
                                ],
                            }
                        ]
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    node = CaliperTestBaseNode(
        directory=tmp_path,
        test_path=tmp_path,
        artifact_paths=[benchmark_path, llmisvc_path],
        test_labels={
            "labels": {
                "load_shape": "concurrent-1k-1k",
                "model_name": "RedHatAI/Llama-3.3-70B-Instruct-FP8-dynamic",
            },
            "kpi_labels": {
                "gpu_type": "H200",
                "product_version": "RHOAI-3.5-EA2",
                "test_harness": "rhoai-release",
            },
        },
    )
    plugin = LlmDGuideLLMPlugin()
    parsed = plugin.parse([node])
    model = UnifiedRunModel(
        plugin_module="projects.llm_d.postprocess.llm_d.plugin",
        base_directory=str(tmp_path),
        test_nodes=[node],
        unified_result_records=parsed.records,
    )

    output_path = tmp_path / "dashboard.csv"
    plugin.export_dashboard_csv(model, output_path)

    with output_path.open(newline="", encoding="utf-8") as output:
        reader = csv.DictReader(output)
        rows = list(reader)
        assert reader.fieldnames == DASHBOARD_FIELDNAMES
    return
    assert len(rows) == 1
    row = rows[0]
    assert row["run"] == "H200-RedHatAI-Llama-3.3-70B-Instruct-FP8-dynamic-2"
    assert row["accelerator"] == "H200"
    assert row["model"] == "RedHatAI/Llama-3.3-70B-Instruct-FP8-dynamic"
    assert row["version"] == "RHOAI-3.5-EA2-precise-prefix-cache"
    assert row["TP"] == "2"
    assert row["DP"] == "0"
    assert row["EP"] == "0"
    assert row["replicas"] == "4"
    assert row["router_config"] == '{"config":"precise"}'
    assert row["prompt toks"] == "1000"
    assert row["successful_requests"] == "80.0"
    assert row["uuid"] == "guidellm-run-1"
    assert row["ttft_median"] == "10.0"
    assert row["runtime_args"] == "--enable-prefix-caching"


def test_llmd_plugin_recovers_deployment_metadata_from_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "cpt": {
                    "kpi": {
                        "labels": {
                            "gpu_type": "H200",
                            "product_version": "RHOAI-3.5-EA2",
                        }
                    }
                },
                "runtime": {
                    "model_name": "RedHatAI/Llama-3.3-70B-Instruct-FP8-dynamic",
                    "deployment_profile": "precise-prefix-cache",
                },
                "deployments": {
                    "defaults": {
                        "replicas": 1,
                        "tensor_parallelism": 1,
                        "vllm_extra": {
                            "args": {
                                "gpu_memory_utilization": 0.92,
                                "enable_prefix_caching": True,
                            }
                        },
                    },
                    "profiles": {
                        "precise-prefix-cache": {
                            "replicas": 4,
                            "tensor_parallelism": 2,
                            "scheduler": {},
                            "vllm_extra": {"args": {"block_size": 64}},
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    node = CaliperTestBaseNode(
        directory=tmp_path,
        test_path=tmp_path,
        artifact_paths=[config_path],
        test_labels={"labels": {}},
    )

    from projects.llm_d.postprocess.llm_d.plugin import _extract_deployment_metadata

    metadata = _extract_deployment_metadata(node)

    assert metadata["replicas"] == 4
    assert metadata["tensor_parallel_size"] == 2
    assert metadata["router_config"] == "{}"
    assert metadata["gpu_type"] == "H200"
    assert metadata["product_version"] == "RHOAI-3.5-EA2"
    assert set(metadata["runtime_args"].split()) == {
        "--gpu-memory-utilization=0.92",
        "--enable-prefix-caching",
        "--block-size=64",
    }


def test_llmd_plugin_infers_accelerator_from_serving_pod_node(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "runtime": {"deployment_profile": "default"},
                "deployments": {"defaults": {}, "profiles": {"default": {}}},
            }
        ),
        encoding="utf-8",
    )
    pods_path = tmp_path / "llminferenceservice.pods.yaml"
    pods_path.write_text(
        yaml.safe_dump(
            {
                "items": [
                    {
                        "spec": {
                            "nodeName": "psap-worker-2-gpu-h200-k6qsd",
                        }
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    node = CaliperTestBaseNode(
        directory=tmp_path,
        test_path=tmp_path,
        artifact_paths=[config_path, pods_path],
        test_labels={"labels": {}},
    )

    from projects.llm_d.postprocess.llm_d.plugin import _extract_deployment_metadata

    assert _extract_deployment_metadata(node)["gpu_type"] == "H200"
