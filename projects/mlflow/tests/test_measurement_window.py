from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from projects.mlflow.orchestration.test_runner import (
    _measurement_window,
    _prometheus_window,
    _reset_measurement_stats,
)
from projects.mlflow.toolbox.k6 import K6Results, run_job


class TestMeasurementWindow(TestCase):
    def test_uses_k6_timestamps_instead_of_job_lifecycle(self):
        result = K6Results.from_job_log(
            "Preparing experiment pool\n"
            "MLFLOW_K6_SCENARIO_START_MS=1780000000000\n"
            "MLFLOW_K6_SCENARIO_END_MS=1780000003050\n"
            "Writing summary\n"
        )
        start = datetime.fromtimestamp(1780000000, UTC)
        self.assertEqual(_measurement_window(result), (start, start + timedelta(seconds=3.05)))

    def test_requires_valid_k6_timestamps(self):
        for log in (
            "Job complete",
            "MLFLOW_K6_SCENARIO_START_MS=500\nMLFLOW_K6_SCENARIO_END_MS=499",
        ):
            with self.subTest(log=log), self.assertRaisesRegex(RuntimeError, "measurement window"):
                K6Results.from_job_log(log)

    def test_mixed_window_is_the_intersection_of_the_two_scenarios(self):
        start = datetime.fromtimestamp(1780000000, UTC)
        read = K6Results("", start, start + timedelta(seconds=5))
        write = K6Results("", start + timedelta(seconds=1), start + timedelta(seconds=6))
        self.assertEqual(
            _measurement_window(read, write),
            (write.measurement_start_time, read.measurement_end_time),
        )

        disjoint = K6Results("", start + timedelta(seconds=5), start + timedelta(seconds=6))
        with self.assertRaisesRegex(RuntimeError, "do not overlap"):
            _measurement_window(read, disjoint)

    def test_postgres_reset_failure_stops_measurement(self):
        with (
            patch(
                "projects.mlflow.orchestration.test_runner.reset_pg_stat_statements",
                return_value=False,
            ),
            self.assertRaisesRegex(RuntimeError, "Could not reset PostgreSQL"),
        ):
            _reset_measurement_stats("benchmark")

    def test_prometheus_rounds_inward_to_full_seconds(self):
        epoch = datetime.fromtimestamp(1780000000, UTC)
        self.assertEqual(
            _prometheus_window(epoch + timedelta(milliseconds=500), epoch + timedelta(seconds=5.5)),
            (epoch + timedelta(seconds=1), epoch + timedelta(seconds=5)),
        )


class TestK6JobPreparation(TestCase):
    def setUp(self):
        self.manifest = {
            "metadata": {"name": "measured", "namespace": "benchmark"},
            "spec": {"template": {"spec": {"containers": [{"args": ["script.js"]}]}}},
        }

    def test_setup_and_reset_run_before_resume(self):
        events = []

        def control(job, namespace, action, *, run_setup=False):
            events.append((action, run_setup))

        with (
            patch("projects.mlflow.toolbox.k6.oc_apply"),
            patch("projects.mlflow.toolbox.k6.oc", return_value=SimpleNamespace(stdout="job logs")),
            patch("projects.mlflow.toolbox.k6._control_job", side_effect=control),
        ):
            result = run_job(
                Path("/unused"),
                self.manifest,
                "measurement",
                on_ready=lambda: events.append("reset"),
                run_setup=True,
            )
        self.assertEqual(events, [("prepare", True), "reset", ("resume", False)])
        args = self.manifest["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertIn("--paused", args)
        self.assertIn("--no-setup", args)
        self.assertEqual(result, "job logs")

    def test_failed_reset_does_not_release_job(self):
        def failed_reset():
            raise RuntimeError("reset failed")

        with (
            patch("projects.mlflow.toolbox.k6.oc_apply"),
            patch("projects.mlflow.toolbox.k6.oc") as oc,
            patch("projects.mlflow.toolbox.k6._control_job") as control,
            self.assertRaisesRegex(RuntimeError, "reset failed"),
        ):
            run_job(Path("/unused"), self.manifest, "measurement", on_ready=failed_reset)
        control.assert_called_once_with("job/measured", "benchmark", "prepare", run_setup=False)
        self.assertEqual(oc.call_args.args[0], "logs")
