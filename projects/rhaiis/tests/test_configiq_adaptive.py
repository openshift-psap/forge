import json
import math
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from projects.rhaiis.orchestration.configiq_adaptive import (
    AdaptiveRatePlan,
    ConfigIQSaturationAnalysis,
    GuideLLMSaturationResult,
    KneeResult,
    SaturationAssessment,
    adaptive_pass_enabled,
    adaptive_rate_options,
    analyze_guidellm_report,
    analyze_guidellm_report_file,
    assess_saturation,
    build_saturation_analysis_artifact,
    extract_guidellm_saturation,
    find_throughput_knee,
    generate_adaptive_rate_plan,
    locate_guidellm_report,
)

CONCURRENCIES = [1, 10, 20, 30, 40, 50, 60]


def _benchmark(
    concurrency: int,
    is_over_saturated: bool | None,
    *,
    constraint_group: str = "scheduler_constraints",
) -> dict:
    scheduler_state = {}
    if is_over_saturated is not None:
        scheduler_state[constraint_group] = {
            "over_saturation": {
                "metadata": {
                    "is_over_saturated": is_over_saturated,
                    "concurrent_slope": 0.25,
                    "concurrent_slope_moe": 0.1,
                    "concurrent_n": 20,
                    "ttft_slope": 0.02,
                    "ttft_slope_moe": 0.01,
                    "ttft_n": 20,
                    "ttft_violations": 2,
                }
            }
        }
    return {
        "config": {"strategy": {"max_concurrency": concurrency}},
        "scheduler_state": scheduler_state,
    }


class ThroughputKneeTests(unittest.TestCase):
    def test_finds_clear_throughput_plateau(self) -> None:
        result = find_throughput_knee(CONCURRENCIES, [1, 10, 20, 30, 30, 30, 30])

        self.assertEqual(result.status, "ok")
        self.assertAlmostEqual(result.knee, 30)
        self.assertEqual(result.breakpoint_concurrency, 30)
        self.assertEqual(result.saturation_concurrency, 30)
        self.assertAlmostEqual(result.slope_ratio, 0)

    def test_allows_small_tail_gain(self) -> None:
        result = find_throughput_knee(CONCURRENCIES, [1, 10, 20, 30, 31, 32, 33])

        self.assertEqual(result.status, "ok")
        self.assertAlmostEqual(result.knee, 30)
        self.assertAlmostEqual(result.slope_ratio, 0.1, delta=0.02)

    def test_finds_plateau_despite_tail_noise(self) -> None:
        result = find_throughput_knee(CONCURRENCIES, [1, 10, 20, 30, 31, 29, 30])

        self.assertEqual(result.status, "ok")
        self.assertAlmostEqual(result.knee, 30.29, places=2)
        self.assertEqual(result.breakpoint_concurrency, 30)
        self.assertEqual(result.saturation_concurrency, 40)

    def test_rejects_linear_curve(self) -> None:
        result = find_throughput_knee(CONCURRENCIES, [1, 10, 20, 30, 40, 50, 60])

        self.assertEqual(result.status, "no_knee")
        self.assertIn("linear", result.reason)

    def test_requires_five_distinct_valid_points(self) -> None:
        result = find_throughput_knee([1, 2, 3, 4], [10, 20, 20, 20])

        self.assertEqual(result.status, "no_knee")
        self.assertIn("at least 5", result.reason)

    def test_averages_duplicate_concurrency_measurements(self) -> None:
        result = find_throughput_knee(
            [1, 10, 20, 30, 30, 40, 50, 60],
            [1, 10, 20, 29, 31, 30, 30, 30],
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.breakpoint_concurrency, 30)

    def test_rejects_mismatched_input_lengths(self) -> None:
        with self.assertRaisesRegex(ValueError, "same length"):
            find_throughput_knee([1, 2, 3], [10, 20])

    def test_rejects_invalid_measurements(self) -> None:
        cases = [
            ([0, 1, 2, 3, 4], [1, 2, 3, 4, 5], "Concurrency"),
            ([1, 2, 3, 4, 5], [1, 2, -1, 4, 5], "Throughput"),
            ([1, 2, 3, 4, 5], [1, 2, math.nan, 4, 5], "Throughput"),
        ]
        for concurrencies, throughputs, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    find_throughput_knee(concurrencies, throughputs)

    def test_gemma_short_run_does_not_claim_saturation(self) -> None:
        result = find_throughput_knee(
            [1, 2, 5, 10, 25, 50, 75, 100, 200, 300],
            [
                209.63,
                419.93,
                843.22,
                1411.77,
                2911.77,
                4488.20,
                5400.58,
                6190.11,
                7829.66,
                8328.99,
            ],
        )

        self.assertEqual(result.status, "no_knee")
        self.assertIn("thresholds", result.reason)


class GuideLLMSaturationTests(unittest.TestCase):
    def test_extracts_first_oversaturated_and_previous_safe_concurrency(self) -> None:
        result = extract_guidellm_saturation(
            {
                "benchmarks": [
                    _benchmark(100, True),
                    _benchmark(50, False),
                    _benchmark(200, True),
                    _benchmark(75, False),
                ]
            }
        )

        self.assertEqual(result.status, "detected")
        self.assertEqual(result.previous_safe_concurrency, 75)
        self.assertEqual(result.first_oversaturated_concurrency, 100)
        self.assertEqual([point.concurrency for point in result.points], [50, 75, 100, 200])
        self.assertEqual(result.points[0].ttft_slope, 0.02)

    def test_reports_when_saturation_is_not_detected(self) -> None:
        result = extract_guidellm_saturation(
            {"benchmarks": [_benchmark(1, False), _benchmark(2, False)]}
        )

        self.assertEqual(result.status, "not_detected")
        self.assertIsNone(result.first_oversaturated_concurrency)

    def test_reports_when_detector_metadata_is_unavailable(self) -> None:
        result = extract_guidellm_saturation(
            {"benchmarks": [_benchmark(1, None), _benchmark(2, None)]}
        )

        self.assertEqual(result.status, "unavailable")
        self.assertTrue(all(point.is_over_saturated is None for point in result.points))

    def test_reads_end_processing_constraint_snapshot(self) -> None:
        result = extract_guidellm_saturation(
            {"benchmarks": [_benchmark(100, True, constraint_group="end_processing_constraints")]}
        )

        self.assertEqual(result.status, "detected")
        self.assertEqual(result.first_oversaturated_concurrency, 100)

    def test_rejects_duplicate_concurrency(self) -> None:
        with self.assertRaisesRegex(ValueError, "Duplicate GuideLLM concurrency"):
            extract_guidellm_saturation(
                {"benchmarks": [_benchmark(50, False), _benchmark(50, True)]}
            )

    def test_rejects_malformed_detector_metadata(self) -> None:
        benchmark = _benchmark(50, False)
        metadata = benchmark["scheduler_state"]["scheduler_constraints"]["over_saturation"][
            "metadata"
        ]
        metadata["is_over_saturated"] = "false"

        with self.assertRaisesRegex(ValueError, "boolean"):
            extract_guidellm_saturation({"benchmarks": [benchmark]})

    def test_rejects_non_monotonic_saturation_states(self) -> None:
        result = extract_guidellm_saturation(
            {
                "benchmarks": [
                    _benchmark(50, False),
                    _benchmark(75, True),
                    _benchmark(100, False),
                ]
            }
        )

        self.assertEqual(result.status, "inconsistent")


class SaturationAssessmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.knee = KneeResult(status="ok", reason="test", knee=90)

    def test_corroborates_knee_inside_guidellm_boundary(self) -> None:
        result = assess_saturation(
            self.knee,
            GuideLLMSaturationResult(
                status="detected",
                reason="test",
                points=(),
                previous_safe_concurrency=75,
                first_oversaturated_concurrency=100,
            ),
        )

        self.assertEqual(result.status, "corroborated")
        self.assertEqual(result.selection_center, 90)
        self.assertEqual(result.refinement_lower, 75)
        self.assertEqual(result.refinement_upper, 100)

    def test_keeps_throughput_knee_without_guidellm_boundary(self) -> None:
        result = assess_saturation(
            self.knee,
            GuideLLMSaturationResult(status="not_detected", reason="test", points=()),
        )

        self.assertEqual(result.status, "throughput_only")
        self.assertEqual(result.selection_center, 90)

    def test_uses_guidellm_boundary_when_throughput_has_no_knee(self) -> None:
        result = assess_saturation(
            KneeResult(status="no_knee", reason="test"),
            GuideLLMSaturationResult(
                status="detected",
                reason="test",
                points=(),
                previous_safe_concurrency=75,
                first_oversaturated_concurrency=100,
            ),
        )

        self.assertEqual(result.status, "oversaturation_boundary")
        self.assertEqual(result.refinement_lower, 75)
        self.assertEqual(result.refinement_upper, 100)

    def test_rejects_guidellm_boundary_below_throughput_knee(self) -> None:
        result = assess_saturation(
            self.knee,
            GuideLLMSaturationResult(
                status="detected",
                reason="test",
                points=(),
                first_oversaturated_concurrency=75,
            ),
        )

        self.assertEqual(result.status, "disagreement")
        self.assertIsNone(result.selection_center)

    def test_rejects_inconsistent_guidellm_states(self) -> None:
        result = assess_saturation(
            self.knee,
            GuideLLMSaturationResult(
                status="inconsistent",
                reason="test",
                points=(),
                first_oversaturated_concurrency=75,
            ),
        )

        self.assertEqual(result.status, "disagreement")


class ConfigIQReportAnalysisTests(unittest.TestCase):
    def test_combines_throughput_and_guidellm_evidence(self) -> None:
        report = {
            "benchmarks": [
                self._benchmark_with_throughput(1, 1, False),
                self._benchmark_with_throughput(10, 10, False),
                self._benchmark_with_throughput(20, 20, False),
                self._benchmark_with_throughput(30, 30, False),
                self._benchmark_with_throughput(40, 30, True),
                self._benchmark_with_throughput(50, 30, True),
                self._benchmark_with_throughput(60, 30, True),
            ]
        }

        result = analyze_guidellm_report(report)

        self.assertEqual(result.throughput.status, "ok")
        self.assertEqual(result.guidellm.status, "detected")
        self.assertEqual(result.assessment.status, "corroborated")

    def test_requires_output_throughput_metric(self) -> None:
        with self.assertRaisesRegex(ValueError, "output throughput"):
            analyze_guidellm_report({"benchmarks": [_benchmark(1, False)]})

    @staticmethod
    def _benchmark_with_throughput(
        concurrency: int,
        throughput: float,
        is_over_saturated: bool,
    ) -> dict:
        benchmark = _benchmark(concurrency, is_over_saturated)
        benchmark["metrics"] = {"output_tokens_per_second": {"successful": {"mean": throughput}}}
        return benchmark


class AdaptivePassConfigurationTests(unittest.TestCase):
    def test_adaptive_pass_defaults_to_disabled(self) -> None:
        self.assertFalse(adaptive_pass_enabled({}))

    def test_adaptive_pass_can_be_enabled(self) -> None:
        self.assertTrue(adaptive_pass_enabled({"adaptive_pass": {"enabled": True}}))

    def test_adaptive_pass_requires_mapping(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be a mapping"):
            adaptive_pass_enabled({"adaptive_pass": True})

    def test_adaptive_pass_requires_boolean_enabled_value(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            adaptive_pass_enabled({"adaptive_pass": {"enabled": "true"}})

    def test_adaptive_rate_options_have_defaults(self) -> None:
        self.assertEqual(adaptive_rate_options({}), (5, 5))

    def test_adaptive_rate_options_can_be_configured(self) -> None:
        workload = {"adaptive_pass": {"points_each_side": 3, "max_step": 2}}

        self.assertEqual(adaptive_rate_options(workload), (3, 2))

    def test_adaptive_rate_options_require_positive_integers(self) -> None:
        for name, value in (("points_each_side", 0), ("max_step", True)):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    adaptive_rate_options({"adaptive_pass": {name: value}})


class AdaptiveRatePlanTests(unittest.TestCase):
    TIER1_RATES = [1, 2, 5, 10, 25, 50, 75, 100, 200, 300]

    @staticmethod
    def _analysis(assessment: SaturationAssessment) -> ConfigIQSaturationAnalysis:
        return ConfigIQSaturationAnalysis(
            throughput=KneeResult(status="no_knee", reason="test"),
            guidellm=GuideLLMSaturationResult(
                status="not_detected",
                reason="test",
                points=(),
            ),
            assessment=assessment,
        )

    def test_generates_five_step_rates_around_knee_and_excludes_tier1(self) -> None:
        analysis = self._analysis(
            SaturationAssessment(
                status="throughput_only",
                reason="test",
                selection_center=100,
            )
        )

        result = generate_adaptive_rate_plan(analysis, self.TIER1_RATES)

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.anchor, 100)
        self.assertEqual(result.step, 5)
        self.assertEqual(result.candidate_rates, tuple(range(75, 126, 5)))
        self.assertEqual(result.excluded_tier1_rates, (75, 100))
        self.assertEqual(result.rates, (80, 85, 90, 95, 105, 110, 115, 120, 125))

    def test_uses_smaller_steps_and_positive_rates_around_low_knee(self) -> None:
        analysis = self._analysis(
            SaturationAssessment(
                status="throughput_only",
                reason="test",
                selection_center=5,
            )
        )

        result = generate_adaptive_rate_plan(analysis, self.TIER1_RATES)

        self.assertEqual(result.step, 1)
        self.assertEqual(result.candidate_rates, tuple(range(1, 11)))
        self.assertEqual(result.rates, (3, 4, 6, 7, 8, 9))

    def test_uses_midpoint_of_guidellm_boundary_without_knee(self) -> None:
        analysis = self._analysis(
            SaturationAssessment(
                status="oversaturation_boundary",
                reason="test",
                refinement_lower=75,
                refinement_upper=100,
            )
        )

        result = generate_adaptive_rate_plan(analysis, self.TIER1_RATES)

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.selection_center, 87.5)
        self.assertEqual(result.anchor, 90)

    def test_aligns_fractional_knee_to_step_grid(self) -> None:
        analysis = self._analysis(
            SaturationAssessment(
                status="throughput_only",
                reason="test",
                selection_center=113.5,
            )
        )

        result = generate_adaptive_rate_plan(analysis, self.TIER1_RATES)

        self.assertEqual(result.anchor, 115)
        self.assertEqual(result.step, 5)
        self.assertEqual(result.candidate_rates, tuple(range(90, 141, 5)))
        self.assertNotIn(100, result.rates)

    def test_skips_unsafe_assessment(self) -> None:
        analysis = self._analysis(SaturationAssessment(status="disagreement", reason="test"))

        result = generate_adaptive_rate_plan(analysis, self.TIER1_RATES)

        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.rates, ())

    def test_skips_when_every_candidate_was_measured(self) -> None:
        analysis = self._analysis(
            SaturationAssessment(
                status="throughput_only",
                reason="test",
                selection_center=5,
            )
        )

        result = generate_adaptive_rate_plan(analysis, list(range(1, 11)))

        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.excluded_tier1_rates, tuple(range(1, 11)))

    def test_rejects_invalid_inputs(self) -> None:
        analysis = self._analysis(
            SaturationAssessment(
                status="throughput_only",
                reason="test",
                selection_center=100,
            )
        )

        with self.assertRaisesRegex(ValueError, "Tier 1 rates"):
            generate_adaptive_rate_plan(analysis, [0])
        with self.assertRaisesRegex(ValueError, "points_each_side"):
            generate_adaptive_rate_plan(analysis, self.TIER1_RATES, points_each_side=0)


class ConfigIQReportFileTests(unittest.TestCase):
    def test_locates_and_analyzes_report_file(self) -> None:
        report = {
            "benchmarks": [
                ConfigIQReportAnalysisTests._benchmark_with_throughput(1, 1, False),
                ConfigIQReportAnalysisTests._benchmark_with_throughput(10, 10, False),
                ConfigIQReportAnalysisTests._benchmark_with_throughput(20, 20, False),
                ConfigIQReportAnalysisTests._benchmark_with_throughput(30, 30, False),
                ConfigIQReportAnalysisTests._benchmark_with_throughput(40, 30, True),
                ConfigIQReportAnalysisTests._benchmark_with_throughput(50, 30, True),
                ConfigIQReportAnalysisTests._benchmark_with_throughput(60, 30, True),
            ]
        }
        with TemporaryDirectory() as directory:
            benchmark_dir = Path(directory)
            report_path = (
                benchmark_dir
                / "001__run_guidellm_benchmark"
                / "artifacts"
                / "results"
                / "benchmarks.json"
            )
            report_path.parent.mkdir(parents=True)
            report_path.write_text(json.dumps(report), encoding="utf-8")

            located_path = locate_guidellm_report(benchmark_dir)
            analysis = analyze_guidellm_report_file(located_path)
            artifact = build_saturation_analysis_artifact(
                analysis,
                tier1_report=str(located_path.relative_to(benchmark_dir)),
                adaptive_rate_plan=AdaptiveRatePlan(status="ready", reason="test", rates=(35,)),
                adaptive_report=("002__run_guidellm_benchmark/artifacts/results/benchmarks.json"),
            )

        self.assertEqual(located_path, report_path)
        self.assertEqual(analysis.assessment.status, "corroborated")
        self.assertEqual(artifact["schema_version"], 2)
        self.assertEqual(
            artifact["reports"]["tier1"],
            "001__run_guidellm_benchmark/artifacts/results/benchmarks.json",
        )
        self.assertEqual(
            artifact["reports"]["adaptive"],
            "002__run_guidellm_benchmark/artifacts/results/benchmarks.json",
        )
        self.assertEqual(artifact["throughput"]["status"], "ok")
        self.assertEqual(artifact["guidellm"]["status"], "detected")
        self.assertEqual(artifact["assessment"]["status"], "corroborated")
        self.assertEqual(artifact["adaptive_rate_plan"]["rates"], (35,))

    def test_analysis_artifact_requires_tier1_report(self) -> None:
        analysis = analyze_guidellm_report(
            {
                "benchmarks": [
                    ConfigIQReportAnalysisTests._benchmark_with_throughput(1, 1, False),
                    ConfigIQReportAnalysisTests._benchmark_with_throughput(10, 10, False),
                    ConfigIQReportAnalysisTests._benchmark_with_throughput(20, 20, False),
                    ConfigIQReportAnalysisTests._benchmark_with_throughput(30, 30, False),
                    ConfigIQReportAnalysisTests._benchmark_with_throughput(40, 30, True),
                ]
            }
        )
        with self.assertRaisesRegex(ValueError, "requires a Tier 1 report"):
            build_saturation_analysis_artifact(
                analysis,
                tier1_report="",
                adaptive_rate_plan=AdaptiveRatePlan(status="skipped", reason="test"),
            )

    def test_missing_report_raises(self) -> None:
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(FileNotFoundError, "No GuideLLM benchmarks.json"):
                locate_guidellm_report(Path(directory))

    def test_multiple_reports_raise(self) -> None:
        with TemporaryDirectory() as directory:
            benchmark_dir = Path(directory)
            for index in (1, 2):
                report_path = (
                    benchmark_dir
                    / f"{index:03d}__run_guidellm_benchmark"
                    / "artifacts"
                    / "results"
                    / "benchmarks.json"
                )
                report_path.parent.mkdir(parents=True)
                report_path.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "Expected one"):
                locate_guidellm_report(benchmark_dir)


if __name__ == "__main__":
    unittest.main()
