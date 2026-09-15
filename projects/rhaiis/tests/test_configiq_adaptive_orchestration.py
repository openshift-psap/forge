import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from projects.core.library import env
from projects.rhaiis.orchestration import test_phase


def _tier1_report() -> dict:
    benchmarks = []
    for concurrency, throughput, saturated in (
        (1, 1, False),
        (10, 10, False),
        (20, 20, False),
        (30, 30, False),
        (40, 30, True),
        (50, 30, True),
        (60, 30, True),
    ):
        benchmarks.append(
            {
                "config": {"strategy": {"max_concurrency": concurrency}},
                "metrics": {"output_tokens_per_second": {"successful": {"mean": throughput}}},
                "scheduler_state": {
                    "scheduler_constraints": {
                        "over_saturation": {"metadata": {"is_over_saturated": saturated}}
                    }
                },
            }
        )
    return {
        "args": {
            "data": ["prompt_tokens=1000,output_tokens=1000"],
            "backend_kwargs": {"model": "example/gemma"},
        },
        "benchmarks": benchmarks,
    }


class ConfigIQAdaptiveOrchestrationTests(unittest.TestCase):
    def test_separates_and_labels_tier1_and_adaptive_passes(self) -> None:
        workload = {
            "data": "prompt_tokens=1000,output_tokens=1000",
            "rates": [1, 10, 20, 30, 40, 50, 60],
            "max_seconds": 450,
            "adaptive_pass": {
                "enabled": True,
                "points_each_side": 5,
                "max_step": 5,
            },
        }
        label_calls = []
        benchmark_calls = []

        def record_labels(directory, labels, **kwargs):
            label_calls.append((Path(directory), labels.copy(), kwargs))

        def write_fake_benchmark(**kwargs):
            benchmark_calls.append(kwargs)
            with env.NextArtifactDir("run_guidellm_benchmark"):
                report_path = Path(env.ARTIFACT_DIR) / "artifacts" / "results" / "benchmarks.json"
                report_path.parent.mkdir(parents=True)
                report_path.write_text(json.dumps(_tier1_report()), encoding="utf-8")
            return 0

        with TemporaryDirectory() as directory:
            previous_artifact_dir = env.get_tls_artifact_dir()
            env._set_tls_artifact_dir(Path(directory))
            try:
                with (
                    patch.object(test_phase.runtime_config, "get_workload", return_value=workload),
                    patch.object(test_phase, "write_test_labels", side_effect=record_labels),
                    patch(
                        "projects.guidellm.toolbox.run_guidellm_benchmark.main.run",
                        side_effect=write_fake_benchmark,
                    ),
                    patch("projects.core.library.config.project") as project_config,
                ):
                    project_config.get_config.return_value = True
                    test_phase._run_workload_benchmark(
                        model_key="gemma",
                        workload_key="configiq",
                        model_cfg={"hf_model_id": "example/gemma"},
                        accelerator="H200",
                        accelerator_key="h200",
                        gpu_type="h200",
                        serving_image="example/vllm:test",
                        engine_args={"tensor-parallel-size": 1},
                        benchmark_cfg={},
                        deployment_name="gemma",
                        namespace="test",
                        endpoint_url="https://example.invalid",
                        benchmark_timeout=14400,
                        run_uuid="test-run",
                        version="test",
                        cluster_tag="test",
                    )
            finally:
                env._set_tls_artifact_dir(previous_artifact_dir)

            analysis_paths = list(
                Path(directory).glob(
                    "*__benchmark_configiq/artifacts/configiq-saturation-analysis.json"
                )
            )
            self.assertEqual(len(analysis_paths), 1)
            analysis_artifact = json.loads(analysis_paths[0].read_text(encoding="utf-8"))

        self.assertEqual(len(label_calls), 2)
        self.assertEqual(
            [labels["configiq_pass"] for _, labels, _ in label_calls],
            ["tier1", "adaptive"],
        )
        self.assertEqual(
            [labels["configiq_data_source"] for _, labels, _ in label_calls],
            ["measured", "measured"],
        )
        self.assertTrue(label_calls[0][0].name.endswith("__tier1"))
        self.assertTrue(label_calls[1][0].name.endswith("__adaptive"))
        self.assertTrue(all(labels["workload_key"] == "configiq" for _, labels, _ in label_calls))

        self.assertEqual(len(benchmark_calls), 2)
        self.assertTrue(benchmark_calls[0]["name"].startswith("guidellm-bench-configiq"))
        self.assertTrue(benchmark_calls[1]["name"].startswith("guidellm-adaptive-configiq"))
        self.assertIn("__tier1/", analysis_artifact["reports"]["tier1"])
        self.assertIn("__adaptive/", analysis_artifact["reports"]["adaptive"])

    def test_reuses_tier1_report_and_runs_only_adaptive_benchmark(self) -> None:
        reused_run_uuid = "12345678-1234-5678-9234-567812345678"
        workload = {
            "data": "prompt_tokens=1000,output_tokens=1000",
            "rates": [1, 10, 20, 30, 40, 50, 60],
            "max_seconds": 450,
            "adaptive_pass": {
                "enabled": True,
                "points_each_side": 5,
                "max_step": 5,
                "reuse": {"enabled": True, "run_uuid": reused_run_uuid},
            },
        }
        label_calls = []
        import_calls = []
        benchmark_calls = []

        def record_labels(directory, labels, **kwargs):
            label_calls.append((Path(directory), labels.copy(), kwargs))

        def download_tier1(**kwargs):
            import_calls.append(kwargs)
            report_path = Path(kwargs["output_dir"]) / "benchmarks.json"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(json.dumps(_tier1_report()), encoding="utf-8")
            return "resolved-mlflow-run-id"

        def write_fake_adaptive_benchmark(**kwargs):
            benchmark_calls.append(kwargs)
            with env.NextArtifactDir("run_guidellm_benchmark"):
                report_path = Path(env.ARTIFACT_DIR) / "artifacts" / "results" / "benchmarks.json"
                report_path.parent.mkdir(parents=True)
                report_path.write_text(json.dumps(_tier1_report()), encoding="utf-8")
            return 0

        with TemporaryDirectory() as directory:
            previous_artifact_dir = env.get_tls_artifact_dir()
            env._set_tls_artifact_dir(Path(directory))
            try:
                with (
                    patch.object(test_phase.runtime_config, "get_workload", return_value=workload),
                    patch.object(test_phase, "write_test_labels", side_effect=record_labels),
                    patch(
                        "projects.rhaiis.orchestration.configiq_reuse.download_tier1_report_by_uuid",
                        side_effect=download_tier1,
                    ),
                    patch(
                        "projects.guidellm.toolbox.run_guidellm_benchmark.main.run",
                        side_effect=write_fake_adaptive_benchmark,
                    ),
                    patch("projects.core.library.config.project") as project_config,
                ):
                    project_config.get_config.return_value = True
                    test_phase._run_workload_benchmark(
                        model_key="gemma",
                        workload_key="configiq",
                        model_cfg={"hf_model_id": "example/gemma"},
                        accelerator="H200",
                        accelerator_key="h200",
                        gpu_type="h200",
                        serving_image="example/vllm:test",
                        engine_args={"tensor-parallel-size": 1},
                        benchmark_cfg={},
                        deployment_name="gemma",
                        namespace="test",
                        endpoint_url="https://example.invalid",
                        benchmark_timeout=14400,
                        run_uuid="test-run",
                        version="test",
                        cluster_tag="test",
                    )
            finally:
                env._set_tls_artifact_dir(previous_artifact_dir)

            analysis_path = next(
                Path(directory).glob(
                    "*__benchmark_configiq/artifacts/configiq-saturation-analysis.json"
                )
            )
            analysis_artifact = json.loads(analysis_path.read_text(encoding="utf-8"))

        self.assertEqual(len(import_calls), 1)
        self.assertEqual(import_calls[0]["run_uuid"], reused_run_uuid)
        self.assertEqual(len(benchmark_calls), 1)
        self.assertTrue(benchmark_calls[0]["name"].startswith("guidellm-adaptive-configiq"))
        self.assertEqual(
            [labels["configiq_pass"] for _, labels, _ in label_calls],
            ["tier1", "adaptive"],
        )
        self.assertEqual(
            [labels["configiq_data_source"] for _, labels, _ in label_calls],
            ["reused", "measured"],
        )
        self.assertTrue(label_calls[0][0].name.endswith("__tier1-reused"))
        self.assertIn("__tier1-reused/", analysis_artifact["reports"]["tier1"])
        self.assertIn("__adaptive/", analysis_artifact["reports"]["adaptive"])
        self.assertEqual(
            analysis_artifact["tier1_reuse"],
            {
                "run_uuid": reused_run_uuid,
                "mlflow_run_id": "resolved-mlflow-run-id",
            },
        )

    def test_nonadaptive_workload_keeps_single_parent_test_node(self) -> None:
        workload = {
            "data": "prompt_tokens=1000,output_tokens=1000",
            "rates": [1],
            "max_seconds": 60,
        }
        label_calls = []
        benchmark_calls = []

        def record_labels(directory, labels, **_kwargs):
            label_calls.append((Path(directory), labels.copy()))

        def record_benchmark(**kwargs):
            benchmark_calls.append(kwargs)
            return 0

        with TemporaryDirectory() as directory:
            previous_artifact_dir = env.get_tls_artifact_dir()
            env._set_tls_artifact_dir(Path(directory))
            try:
                with (
                    patch.object(test_phase.runtime_config, "get_workload", return_value=workload),
                    patch.object(test_phase, "write_test_labels", side_effect=record_labels),
                    patch(
                        "projects.guidellm.toolbox.run_guidellm_benchmark.main.run",
                        side_effect=record_benchmark,
                    ),
                    patch("projects.core.library.config.project") as project_config,
                ):
                    project_config.get_config.return_value = True
                    test_phase._run_workload_benchmark(
                        model_key="gemma",
                        workload_key="profile1",
                        model_cfg={"hf_model_id": "example/gemma"},
                        accelerator="H200",
                        accelerator_key="h200",
                        gpu_type="h200",
                        serving_image="example/vllm:test",
                        engine_args={"tensor-parallel-size": 1},
                        benchmark_cfg={},
                        deployment_name="gemma",
                        namespace="test",
                        endpoint_url="https://example.invalid",
                        benchmark_timeout=14400,
                        run_uuid="test-run",
                        version="test",
                        cluster_tag="test",
                    )
            finally:
                env._set_tls_artifact_dir(previous_artifact_dir)

        self.assertEqual(len(label_calls), 1)
        self.assertTrue(label_calls[0][0].name.endswith("__benchmark_profile1"))
        self.assertNotIn("configiq_pass", label_calls[0][1])
        self.assertEqual(len(benchmark_calls), 1)


if __name__ == "__main__":
    unittest.main()
