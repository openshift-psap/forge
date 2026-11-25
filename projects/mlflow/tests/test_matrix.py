from dataclasses import replace
from unittest import TestCase

from projects.mlflow.orchestration.test_matrix import MatrixCell, build_test_matrix, job_name
from projects.mlflow.toolbox.k6 import with_suffix


class TestBenchmarkMatrix(TestCase):
    def setUp(self):
        self.inputs = {
            "concurrency": [4],
            "pg_sizes_gb": [0, 10],
            "span_sizes": [None],
            "tenant_counts": [1, 2],
            "experiment_counts": [None],
            "seed_trace_ratios": [2, 3],
            "explicit_cells": None,
        }

    def test_regular_sweep_uses_one_ratio_for_unseeded_cases(self):
        cases = build_test_matrix(**self.inputs, mlflow_replicas=[1, 2])
        self.assertEqual(
            [
                (
                    case.pg_size_gb,
                    case.tenant_count,
                    case.seed_traces_per_experiment,
                    case.mlflow_replicas,
                    case.users,
                )
                for case in cases
            ],
            [
                (0, 1, 1, 1, 4),
                (0, 1, 1, 2, 4),
                (0, 2, 1, 1, 8),
                (0, 2, 1, 2, 8),
                (10, 1, 2, 1, 4),
                (10, 1, 2, 2, 4),
                (10, 1, 3, 1, 4),
                (10, 1, 3, 2, 4),
                (10, 2, 2, 1, 8),
                (10, 2, 2, 2, 8),
                (10, 2, 3, 1, 8),
                (10, 2, 3, 2, 8),
            ],
        )

    def test_mixed_concurrency_is_total_and_must_divide_across_tenants(self):
        cases = build_test_matrix(**self.inputs, read_concurrencies=[8], write_concurrencies=[4])
        for case in cases:
            self.assertEqual(case.users, 8)
            self.assertEqual(case.users_per_tenant, 8 // case.tenant_count)
            self.assertEqual(case.write_concurrency, 4)

        with self.assertRaisesRegex(ValueError, "divide evenly"):
            build_test_matrix(**self.inputs, read_concurrencies=[7], write_concurrencies=[4])

    def test_explicit_write_rows_override_workload_axes(self):
        self.inputs["explicit_cells"] = [
            {
                "users_per_tenant": 5,
                "tenant_count": 2,
                "span_size_bytes": 4096,
                "experiments_per_test": None,
                "pg_size_gb": None,
                "seed_traces_per_experiment": None,
            },
            {
                "users_per_tenant": 3,
                "tenant_count": 1,
                "span_size_bytes": 8192,
                "experiments_per_test": None,
                "pg_size_gb": 10,
                "seed_traces_per_experiment": 7,
            },
        ]
        self.assertEqual(
            build_test_matrix(**self.inputs),
            [
                MatrixCell(pg_size_gb=0, users_per_tenant=5, tenant_count=2, span_size_bytes=4096),
                MatrixCell(
                    pg_size_gb=10,
                    users_per_tenant=3,
                    span_size_bytes=8192,
                    seed_traces_per_experiment=7,
                ),
            ],
        )

    def test_long_case_names_remain_distinct_with_job_suffixes(self):
        cell = MatrixCell(
            pg_size_gb=1000,
            users_per_tenant=64,
            tenant_count=64,
            span_size_bytes=1048576,
            experiments_per_test=64,
            read_concurrency=4096,
            write_concurrency=4096,
            mlflow_replicas=16,
            mlflow_workers=64,
        )
        first = job_name(cell, "mixed")
        second = job_name(replace(cell, seed_traces_per_experiment=10), "mixed")
        for suffix in ("", "-read", "-write", "-read-warmup", "-write-warmup"):
            with self.subTest(suffix=suffix):
                first_job = with_suffix(first, suffix)
                second_job = with_suffix(second, suffix)
                self.assertNotEqual(first_job, second_job)
                self.assertTrue(first_job.endswith(suffix))
                self.assertLessEqual(len(first_job), 63)
