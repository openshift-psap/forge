"""Throughput saturation detection for the ConfigIQ adaptive pass."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

MIN_POINTS = 5
MAX_TAIL_SLOPE_RATIO = 0.25
MIN_FIT_IMPROVEMENT = 0.50
MIN_THROUGHPUT_FRACTION = 0.85


@dataclass(frozen=True)
class KneeResult:
    """Result of fitting a rising segment and a saturation segment."""

    status: Literal["ok", "no_knee"]
    reason: str
    knee: float | None = None
    saturation_concurrency: float | None = None
    breakpoint_concurrency: float | None = None
    pre_slope: float | None = None
    tail_slope: float | None = None
    slope_ratio: float | None = None
    fit_improvement: float | None = None
    throughput_fraction: float | None = None


@dataclass(frozen=True)
class GuideLLMSaturationPoint:
    """Final GuideLLM v0.6 over-saturation snapshot for one concurrency."""

    concurrency: float
    is_over_saturated: bool | None
    concurrent_slope: float | None = None
    concurrent_slope_moe: float | None = None
    concurrent_n: float | None = None
    ttft_slope: float | None = None
    ttft_slope_moe: float | None = None
    ttft_n: float | None = None
    ttft_violations: float | None = None


@dataclass(frozen=True)
class GuideLLMSaturationResult:
    """Over-saturation boundary inferred from GuideLLM benchmark snapshots."""

    status: Literal["detected", "not_detected", "unavailable", "inconsistent"]
    reason: str
    points: tuple[GuideLLMSaturationPoint, ...]
    previous_safe_concurrency: float | None = None
    first_oversaturated_concurrency: float | None = None


@dataclass(frozen=True)
class SaturationAssessment:
    """Relationship between the cross-run knee and GuideLLM's temporal signal."""

    status: Literal[
        "corroborated",
        "compatible",
        "throughput_only",
        "oversaturation_boundary",
        "no_saturation",
        "disagreement",
    ]
    reason: str
    selection_center: float | None = None
    refinement_lower: float | None = None
    refinement_upper: float | None = None


@dataclass(frozen=True)
class ConfigIQSaturationAnalysis:
    """Combined saturation analysis for a ConfigIQ Tier 1 report."""

    throughput: KneeResult
    guidellm: GuideLLMSaturationResult
    assessment: SaturationAssessment


@dataclass(frozen=True)
class AdaptiveRatePlan:
    """Concurrency rates selected for the ConfigIQ adaptive benchmark."""

    status: Literal["ready", "skipped"]
    reason: str
    rates: tuple[int, ...] = ()
    selection_center: float | None = None
    anchor: int | None = None
    step: int | None = None
    candidate_rates: tuple[int, ...] = ()
    excluded_tier1_rates: tuple[int, ...] = ()


@dataclass(frozen=True)
class Tier1ReuseConfig:
    """Source configuration for reusing a prior ConfigIQ Tier 1 report."""

    enabled: bool
    run_uuid: str | None = None


@dataclass(frozen=True)
class Tier1ReuseProvenance:
    """Resolved identity of a reused ConfigIQ Tier 1 report."""

    run_uuid: str
    mlflow_run_id: str


def build_saturation_analysis_artifact(
    analysis: ConfigIQSaturationAnalysis,
    *,
    tier1_report: str,
    adaptive_rate_plan: AdaptiveRatePlan,
    adaptive_report: str | None = None,
    tier1_reuse: Tier1ReuseProvenance | None = None,
) -> dict:
    """Build the public artifact describing a ConfigIQ Tier 1 analysis."""
    if not tier1_report:
        raise ValueError("ConfigIQ analysis artifact requires a Tier 1 report path")
    return {
        "schema_version": 3,
        "reports": {
            "tier1": tier1_report,
            "adaptive": adaptive_report,
        },
        "tier1_reuse": asdict(tier1_reuse) if tier1_reuse is not None else None,
        "adaptive_rate_plan": asdict(adaptive_rate_plan),
        **asdict(analysis),
    }


def adaptive_pass_enabled(workload: dict) -> bool:
    """Return whether ConfigIQ should analyze Tier 1 for an adaptive pass."""
    adaptive_pass = workload.get("adaptive_pass", {})
    if not isinstance(adaptive_pass, dict):
        raise ValueError("ConfigIQ adaptive_pass must be a mapping")

    enabled = adaptive_pass.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("ConfigIQ adaptive_pass.enabled must be a boolean")
    return enabled


def adaptive_rate_options(workload: dict) -> tuple[int, int]:
    """Return validated point-count and maximum-step settings."""
    adaptive_pass = workload.get("adaptive_pass", {})
    if not isinstance(adaptive_pass, dict):
        raise ValueError("ConfigIQ adaptive_pass must be a mapping")

    points_each_side = adaptive_pass.get("points_each_side", 5)
    max_step = adaptive_pass.get("max_step", 5)
    for name, value in (
        ("points_each_side", points_each_side),
        ("max_step", max_step),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"ConfigIQ adaptive_pass.{name} must be a positive integer")
    return points_each_side, max_step


def tier1_reuse_config(workload: dict) -> Tier1ReuseConfig:
    """Return validated configuration for an existing Tier 1 report."""
    adaptive_pass = workload.get("adaptive_pass", {})
    if not isinstance(adaptive_pass, dict):
        raise ValueError("ConfigIQ adaptive_pass must be a mapping")
    reuse = adaptive_pass.get("reuse", {})
    if not isinstance(reuse, dict):
        raise ValueError("ConfigIQ adaptive_pass.reuse must be a mapping")

    enabled = reuse.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("ConfigIQ adaptive_pass.reuse.enabled must be a boolean")
    run_uuid = reuse.get("run_uuid")
    if run_uuid is not None and not isinstance(run_uuid, str):
        raise ValueError("ConfigIQ adaptive_pass.reuse.run_uuid must be a string")
    if enabled and not run_uuid:
        raise ValueError("ConfigIQ adaptive_pass.reuse.run_uuid is required when reuse is enabled")
    if run_uuid:
        try:
            run_uuid = str(UUID(run_uuid))
        except ValueError as error:
            raise ValueError(
                "ConfigIQ adaptive_pass.reuse.run_uuid must be a valid UUID"
            ) from error
    return Tier1ReuseConfig(enabled=enabled, run_uuid=run_uuid or None)


def generate_adaptive_rate_plan(
    analysis: ConfigIQSaturationAnalysis,
    tier1_rates: Sequence[int],
    *,
    points_each_side: int = 5,
    max_step: int = 5,
) -> AdaptiveRatePlan:
    """Generate unmeasured integer rates around the detected saturation region."""
    for name, value in (
        ("points_each_side", points_each_side),
        ("max_step", max_step),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")

    measured_rates: set[int] = set()
    for rate in tier1_rates:
        if isinstance(rate, bool) or not isinstance(rate, int) or rate <= 0:
            raise ValueError("Tier 1 rates must be positive integers")
        measured_rates.add(rate)

    assessment = analysis.assessment
    if assessment.status in {"disagreement", "no_saturation"}:
        return AdaptiveRatePlan(
            status="skipped",
            reason=f"Cannot safely generate adaptive rates: {assessment.reason}",
        )

    center = assessment.selection_center
    if center is None:
        lower = assessment.refinement_lower
        upper = assessment.refinement_upper
        if lower is not None and upper is not None:
            center = (lower + upper) / 2
        else:
            center = upper if upper is not None else lower
    if center is None:
        return AdaptiveRatePlan(
            status="skipped",
            reason="The saturation assessment did not provide a refinement center or boundary",
        )
    if not math.isfinite(center) or center <= 0:
        raise ValueError("Adaptive rate selection center must be positive and finite")

    # Prefer a clean max-step grid, but reduce the step when that grid would
    # reach zero. At the very bottom of the integer range, unit spacing may
    # still yield fewer than the requested number of lower points.
    step = 1
    anchor = max(1, math.floor(center + 0.5))
    for candidate_step in range(max_step, 0, -1):
        candidate_anchor = max(
            candidate_step,
            math.floor(center / candidate_step + 0.5) * candidate_step,
        )
        step = candidate_step
        anchor = candidate_anchor
        if anchor - points_each_side * step > 0:
            break
    candidate_rates = tuple(
        sorted(
            {
                anchor + offset * step
                for offset in range(-points_each_side, points_each_side + 1)
                if anchor + offset * step > 0
            }
        )
    )
    excluded_rates = tuple(rate for rate in candidate_rates if rate in measured_rates)
    rates = tuple(rate for rate in candidate_rates if rate not in measured_rates)
    if not rates:
        return AdaptiveRatePlan(
            status="skipped",
            reason="All adaptive candidate rates were already measured by Tier 1",
            selection_center=center,
            anchor=anchor,
            step=step,
            candidate_rates=candidate_rates,
            excluded_tier1_rates=excluded_rates,
        )
    return AdaptiveRatePlan(
        status="ready",
        reason="Generated unmeasured concurrency rates around the saturation region",
        rates=rates,
        selection_center=center,
        anchor=anchor,
        step=step,
        candidate_rates=candidate_rates,
        excluded_tier1_rates=excluded_rates,
    )


def _line_fit(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float, float]:
    """Return the intercept, slope, and squared error of an OLS line."""
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    slope = (
        sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True)) / denominator
        if denominator
        else 0.0
    )
    intercept = y_mean - slope * x_mean
    error = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys, strict=True))
    return intercept, slope, error


def _prepare_points(
    concurrencies: Sequence[int | float],
    output_throughputs: Sequence[int | float | None],
) -> tuple[list[float], list[float]]:
    if len(concurrencies) != len(output_throughputs):
        raise ValueError("Concurrency and throughput lists must have the same length")

    by_concurrency: dict[float, list[float]] = {}
    for concurrency, throughput in zip(concurrencies, output_throughputs, strict=True):
        if throughput is None:
            continue
        concurrency_value = float(concurrency)
        throughput_value = float(throughput)
        if not math.isfinite(concurrency_value) or concurrency_value <= 0:
            raise ValueError(f"Concurrency must be a positive finite number: {concurrency!r}")
        if not math.isfinite(throughput_value) or throughput_value < 0:
            raise ValueError(f"Throughput must be a non-negative finite number: {throughput!r}")
        by_concurrency.setdefault(concurrency_value, []).append(throughput_value)

    prepared_concurrencies = sorted(by_concurrency)
    prepared_throughputs = [
        sum(by_concurrency[concurrency]) / len(by_concurrency[concurrency])
        for concurrency in prepared_concurrencies
    ]
    return prepared_concurrencies, prepared_throughputs


def find_throughput_knee(
    concurrencies: Sequence[int | float],
    output_throughputs: Sequence[int | float | None],
) -> KneeResult:
    """Find where output throughput changes from rising to substantially flatter.

    Duplicate concurrency measurements are averaged. Missing throughput values
    are ignored. A knee is accepted only when segmented regression improves the
    fit, the tail is substantially flatter, and the breakpoint has reached most
    of the observed peak throughput.
    """
    xs, ys = _prepare_points(concurrencies, output_throughputs)
    if len(xs) < MIN_POINTS:
        return KneeResult(
            status="no_knee",
            reason=f"Need at least {MIN_POINTS} distinct concurrency points; found {len(xs)}",
        )

    x_min, x_max = xs[0], xs[-1]
    y_min, y_max = min(ys), max(ys)
    if x_max == x_min or y_max == y_min:
        return KneeResult(status="no_knee", reason="The throughput curve has no usable range")

    normalized_xs = [(x - x_min) / (x_max - x_min) for x in xs]
    normalized_ys = [(y - y_min) / (y_max - y_min) for y in ys]

    _, _, single_line_error = _line_fit(normalized_xs, normalized_ys)
    if single_line_error <= 1e-12:
        return KneeResult(status="no_knee", reason="The throughput curve is approximately linear")

    candidates: list[tuple[float, int, float, float, float, float, float, float, float]] = []
    candidate_stop = len(xs) - 1 if len(xs) == MIN_POINTS else len(xs) - 2
    for index in range(2, candidate_stop):
        pre_intercept, pre_slope, pre_error = _line_fit(
            normalized_xs[: index + 1], normalized_ys[: index + 1]
        )
        tail_intercept, tail_slope, tail_error = _line_fit(
            normalized_xs[index:], normalized_ys[index:]
        )
        if pre_slope <= 0:
            continue

        total_error = pre_error + tail_error
        slope_ratio = tail_slope / pre_slope
        fit_improvement = 1.0 - total_error / single_line_error
        throughput_fraction = ys[index] / y_max if y_max > 0 else 0.0

        if slope_ratio > MAX_TAIL_SLOPE_RATIO:
            continue
        if fit_improvement < MIN_FIT_IMPROVEMENT:
            continue
        if throughput_fraction < MIN_THROUGHPUT_FRACTION:
            continue

        candidates.append(
            (
                total_error,
                index,
                pre_intercept,
                pre_slope,
                tail_intercept,
                tail_slope,
                slope_ratio,
                fit_improvement,
                throughput_fraction,
            )
        )

    if not candidates:
        return KneeResult(
            status="no_knee",
            reason="No breakpoint satisfied the saturation thresholds",
        )

    (
        _,
        best_index,
        pre_intercept,
        pre_slope,
        tail_intercept,
        tail_slope,
        slope_ratio,
        fit_improvement,
        throughput_fraction,
    ) = min(candidates)

    slope_delta = pre_slope - tail_slope
    knee_normalized = (
        (tail_intercept - pre_intercept) / slope_delta
        if abs(slope_delta) > 1e-12
        else normalized_xs[best_index]
    )
    if not normalized_xs[best_index - 1] <= knee_normalized <= normalized_xs[best_index + 1]:
        knee_normalized = normalized_xs[best_index]

    knee = x_min + knee_normalized * (x_max - x_min)
    epsilon = (x_max - x_min) * 1e-9
    saturation_concurrency = next((x for x in xs if x >= knee - epsilon), xs[-1])

    return KneeResult(
        status="ok",
        reason="A rising segment followed by a substantially flatter tail was detected",
        knee=knee,
        saturation_concurrency=saturation_concurrency,
        breakpoint_concurrency=xs[best_index],
        pre_slope=pre_slope,
        tail_slope=tail_slope,
        slope_ratio=slope_ratio,
        fit_improvement=fit_improvement,
        throughput_fraction=throughput_fraction,
    )


def _constraint_metadata(benchmark: dict) -> dict | None:
    scheduler_state = benchmark.get("scheduler_state")
    if not isinstance(scheduler_state, dict):
        return None

    for constraint_group in (
        "scheduler_constraints",
        "end_processing_constraints",
        "end_queuing_constraints",
    ):
        constraints = scheduler_state.get(constraint_group)
        if not isinstance(constraints, dict):
            continue
        action = constraints.get("over_saturation") or constraints.get("detect_saturation")
        if not isinstance(action, dict):
            continue
        metadata = action.get("metadata")
        if isinstance(metadata, dict):
            return metadata
    return None


def _optional_finite_number(metadata: dict, key: str) -> float | None:
    value = metadata.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"GuideLLM saturation metadata {key!r} must be numeric")
    if not math.isfinite(value):
        raise ValueError(f"GuideLLM saturation metadata {key!r} must be finite")
    return float(value)


def extract_guidellm_saturation(report: dict) -> GuideLLMSaturationResult:
    """Extract final GuideLLM v0.6 detector snapshots from a benchmark report."""
    benchmarks = report.get("benchmarks")
    if not isinstance(benchmarks, list):
        raise ValueError("GuideLLM report must contain a benchmarks list")

    points: list[GuideLLMSaturationPoint] = []
    seen_concurrencies: set[float] = set()
    for benchmark in benchmarks:
        if not isinstance(benchmark, dict):
            raise ValueError("Each GuideLLM benchmark must be an object")
        config = benchmark.get("config")
        strategy = config.get("strategy") if isinstance(config, dict) else None
        concurrency = strategy.get("max_concurrency") if isinstance(strategy, dict) else None
        if isinstance(concurrency, bool) or not isinstance(concurrency, (int, float)):
            raise ValueError("Each GuideLLM benchmark must have a numeric max_concurrency")
        concurrency_value = float(concurrency)
        if not math.isfinite(concurrency_value) or concurrency_value <= 0:
            raise ValueError("GuideLLM benchmark max_concurrency must be positive and finite")
        if concurrency_value in seen_concurrencies:
            raise ValueError(f"Duplicate GuideLLM concurrency: {concurrency_value:g}")
        seen_concurrencies.add(concurrency_value)

        metadata = _constraint_metadata(benchmark)
        if metadata is None:
            points.append(
                GuideLLMSaturationPoint(
                    concurrency=concurrency_value,
                    is_over_saturated=None,
                )
            )
            continue

        is_over_saturated = metadata.get("is_over_saturated")
        if not isinstance(is_over_saturated, bool):
            raise ValueError(
                "GuideLLM saturation metadata must contain boolean 'is_over_saturated'"
            )
        points.append(
            GuideLLMSaturationPoint(
                concurrency=concurrency_value,
                is_over_saturated=is_over_saturated,
                concurrent_slope=_optional_finite_number(metadata, "concurrent_slope"),
                concurrent_slope_moe=_optional_finite_number(metadata, "concurrent_slope_moe"),
                concurrent_n=_optional_finite_number(metadata, "concurrent_n"),
                ttft_slope=_optional_finite_number(metadata, "ttft_slope"),
                ttft_slope_moe=_optional_finite_number(metadata, "ttft_slope_moe"),
                ttft_n=_optional_finite_number(metadata, "ttft_n"),
                ttft_violations=_optional_finite_number(metadata, "ttft_violations"),
            )
        )

    sorted_points = tuple(sorted(points, key=lambda point: point.concurrency))
    available_points = [point for point in sorted_points if point.is_over_saturated is not None]
    if not available_points:
        return GuideLLMSaturationResult(
            status="unavailable",
            reason="The report contains no GuideLLM over-saturation metadata",
            points=sorted_points,
        )

    oversaturated_points = [point for point in available_points if point.is_over_saturated is True]
    if not oversaturated_points:
        return GuideLLMSaturationResult(
            status="not_detected",
            reason="GuideLLM did not report over-saturation at any measured concurrency",
            points=sorted_points,
        )

    first_oversaturated = oversaturated_points[0].concurrency
    safe_before = [
        point.concurrency
        for point in available_points
        if point.is_over_saturated is False and point.concurrency < first_oversaturated
    ]
    previous_safe = max(safe_before) if safe_before else None
    safe_after = [
        point.concurrency
        for point in available_points
        if point.is_over_saturated is False and point.concurrency > first_oversaturated
    ]
    if safe_after:
        return GuideLLMSaturationResult(
            status="inconsistent",
            reason=(
                "GuideLLM reported a safe concurrency above its first over-saturated concurrency"
            ),
            points=sorted_points,
            previous_safe_concurrency=previous_safe,
            first_oversaturated_concurrency=first_oversaturated,
        )
    return GuideLLMSaturationResult(
        status="detected",
        reason="GuideLLM reported an over-saturated concurrency",
        points=sorted_points,
        previous_safe_concurrency=previous_safe,
        first_oversaturated_concurrency=first_oversaturated,
    )


def assess_saturation(
    throughput: KneeResult,
    guidellm: GuideLLMSaturationResult,
) -> SaturationAssessment:
    """Combine the primary throughput knee with GuideLLM's supporting signal."""
    if guidellm.status == "inconsistent":
        return SaturationAssessment(
            status="disagreement",
            reason="GuideLLM's over-saturation states are not monotonic across concurrency",
        )

    if throughput.status == "ok":
        if throughput.knee is None:
            raise ValueError("A successful throughput knee result must contain a knee")
        if guidellm.status != "detected":
            return SaturationAssessment(
                status="throughput_only",
                reason="Throughput saturated without a GuideLLM over-saturation boundary",
                selection_center=throughput.knee,
            )

        first_oversaturated = guidellm.first_oversaturated_concurrency
        if first_oversaturated is None:
            raise ValueError("A detected GuideLLM result must contain its first boundary")
        if first_oversaturated < throughput.knee:
            return SaturationAssessment(
                status="disagreement",
                reason="GuideLLM reported over-saturation below the throughput knee",
            )

        previous_safe = guidellm.previous_safe_concurrency
        if previous_safe is not None and previous_safe <= throughput.knee:
            return SaturationAssessment(
                status="corroborated",
                reason="The throughput knee falls inside GuideLLM's safe-to-overloaded bracket",
                selection_center=throughput.knee,
                refinement_lower=previous_safe,
                refinement_upper=first_oversaturated,
            )
        return SaturationAssessment(
            status="compatible",
            reason="GuideLLM over-saturation occurs above the throughput knee",
            selection_center=throughput.knee,
            refinement_upper=first_oversaturated,
        )

    if guidellm.status == "detected":
        return SaturationAssessment(
            status="oversaturation_boundary",
            reason="No throughput knee was found; refine GuideLLM's boundary instead",
            refinement_lower=guidellm.previous_safe_concurrency,
            refinement_upper=guidellm.first_oversaturated_concurrency,
        )

    return SaturationAssessment(
        status="no_saturation",
        reason="Neither detector found saturation in the measured range",
    )


def _extract_throughput_curve(report: dict) -> tuple[list[float], list[float]]:
    benchmarks = report.get("benchmarks")
    if not isinstance(benchmarks, list):
        raise ValueError("GuideLLM report must contain a benchmarks list")

    concurrencies: list[float] = []
    throughputs: list[float] = []
    for benchmark in benchmarks:
        if not isinstance(benchmark, dict):
            raise ValueError("Each GuideLLM benchmark must be an object")
        config = benchmark.get("config")
        strategy = config.get("strategy") if isinstance(config, dict) else None
        concurrency = strategy.get("max_concurrency") if isinstance(strategy, dict) else None
        metrics = benchmark.get("metrics")
        output_metric = (
            metrics.get("output_tokens_per_second") if isinstance(metrics, dict) else None
        )
        successful = output_metric.get("successful") if isinstance(output_metric, dict) else None
        throughput = successful.get("mean") if isinstance(successful, dict) else None
        if isinstance(concurrency, bool) or not isinstance(concurrency, (int, float)):
            raise ValueError("Each GuideLLM benchmark must have a numeric max_concurrency")
        if isinstance(throughput, bool) or not isinstance(throughput, (int, float)):
            raise ValueError(
                "Each GuideLLM benchmark must have a numeric successful mean output throughput"
            )
        concurrencies.append(float(concurrency))
        throughputs.append(float(throughput))
    return concurrencies, throughputs


def analyze_guidellm_report(report: dict) -> ConfigIQSaturationAnalysis:
    """Analyze a Tier 1 report using throughput as primary and GuideLLM as supporting evidence."""
    concurrencies, throughputs = _extract_throughput_curve(report)
    throughput_result = find_throughput_knee(concurrencies, throughputs)
    guidellm_result = extract_guidellm_saturation(report)
    return ConfigIQSaturationAnalysis(
        throughput=throughput_result,
        guidellm=guidellm_result,
        assessment=assess_saturation(throughput_result, guidellm_result),
    )


def locate_guidellm_report(benchmark_artifact_dir: Path) -> Path:
    """Locate the single GuideLLM report produced by one workload benchmark."""
    matches = sorted(
        benchmark_artifact_dir.glob("*__run_guidellm_benchmark/artifacts/results/benchmarks.json")
    )
    if not matches:
        raise FileNotFoundError(f"No GuideLLM benchmarks.json found under {benchmark_artifact_dir}")
    if len(matches) > 1:
        paths = ", ".join(str(path) for path in matches)
        raise RuntimeError(
            f"Expected one GuideLLM benchmarks.json under {benchmark_artifact_dir}; "
            f"found {len(matches)}: {paths}"
        )
    return matches[0]


def analyze_guidellm_report_file(report_path: Path) -> ConfigIQSaturationAnalysis:
    """Load and analyze one GuideLLM report file."""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError(f"GuideLLM report must be a JSON object: {report_path}")
    return analyze_guidellm_report(report)


def validate_and_analyze_reused_tier1_report(
    report_path: Path,
    *,
    model_id: str,
    data: str,
    tier1_rates: Sequence[int],
) -> ConfigIQSaturationAnalysis:
    """Validate a reused report's identity and analyze its saturation curve."""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError(f"GuideLLM report must be a JSON object: {report_path}")

    args = report.get("args")
    if not isinstance(args, dict):
        raise ValueError("Reused Tier 1 report must contain GuideLLM args")
    backend_kwargs = args.get("backend_kwargs")
    report_model = backend_kwargs.get("model") if isinstance(backend_kwargs, dict) else None
    if report_model != model_id:
        raise ValueError(
            f"Reused Tier 1 model mismatch: expected {model_id!r}, found {report_model!r}"
        )

    report_data = args.get("data")
    if report_data != [data]:
        raise ValueError(f"Reused Tier 1 data mismatch: expected {[data]!r}, found {report_data!r}")

    concurrencies, _ = _extract_throughput_curve(report)
    expected_rates = sorted(float(rate) for rate in tier1_rates)
    if sorted(concurrencies) != expected_rates:
        raise ValueError(
            "Reused Tier 1 concurrency mismatch: "
            f"expected {expected_rates!r}, found {sorted(concurrencies)!r}"
        )
    return analyze_guidellm_report(report)


def locate_reused_guidellm_report(reuse_artifact_dir: Path) -> Path:
    """Locate exactly one downloaded GuideLLM benchmarks.json report."""
    matches = sorted(reuse_artifact_dir.rglob("benchmarks.json"))
    if not matches:
        raise FileNotFoundError(
            f"No reused GuideLLM benchmarks.json found under {reuse_artifact_dir}"
        )
    if len(matches) > 1:
        paths = ", ".join(str(path) for path in matches)
        raise RuntimeError(
            f"Expected one reused GuideLLM benchmarks.json under {reuse_artifact_dir}; "
            f"found {len(matches)}: {paths}"
        )
    return matches[0]
