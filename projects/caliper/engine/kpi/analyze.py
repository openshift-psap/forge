"""Generic KPI regression analysis against historical baselines."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from projects.caliper.engine.kpi.dataclasses import (
    HierarchicalKpi,
    HierarchicalKpiFormat,
    HierarchicalTestEntry,
)
from projects.caliper.engine.kpi.report_dataclasses import (
    Algorithm,
    AnalysisSection,
    AnalysisSummary,
    CurrentValueInfo,
    InputDataSection,
    OverallSection,
    OverallStatus,
    RegressionReport,
    RegressionTestResult,
    ResultEntry,
    ResultLabels,
    TestedSection,
    Verdict,
)
from projects.caliper.public import KpiAnalysisStatus, StatusLevel
from projects.caliper.public.status_models import (
    create_failure_status,
    create_success_status,
)

logger = logging.getLogger(__name__)


@dataclass
class AnalysisConfig:
    """Configuration for KPI regression analysis.

    comparison_labels: Label keys that define what we compare against.
        Records must differ on at least one comparison key to be relevant baselines.
        E.g. ["version"] means we test the current version against other versions.
    ignored_labels: Label keys excluded when matching current to baseline records.
        E.g. ["os"] means we match across operating systems.
    sorting_labels: Label keys used to order entries in the output report.
    max_relative_regression: Fraction threshold for flagging regression (0.1 = 10%).
    min_baseline_points: Minimum number of baseline data points required to run a test.
    """

    comparison_labels: list[str] = field(default_factory=list)
    ignored_labels: list[str] = field(default_factory=list)
    sorting_labels: list[str] = field(default_factory=list)
    regression_config: dict[str, Any] = field(default_factory=dict)


def create_analysis_summary(
    results: list[RegressionTestResult],
    config: AnalysisConfig,
    current_source: dict[str, Any],
    relevant_sources: list[dict[str, Any]],
    irrelevant_sources: list[dict[str, Any]],
    baseline_skipped: dict[str, int],
    improvement_count: int = 0,
    message: str = "",
) -> AnalysisSummary:
    """Create AnalysisSummary from analysis data."""
    from .dataclasses import BaselineSummary, ConfigSummary, TestSummary

    passes = [r for r in results if r.verdict == Verdict.PASS]
    regressions = [r for r in results if r.verdict == Verdict.REGRESSION]
    skipped = [r for r in results if r.verdict == Verdict.SKIPPED]

    test_summary = TestSummary(
        total_kpis=len(results),
        pass_count=len(passes),
        regression_count=len(regressions),
        skipped_count=len(skipped),
        improvement_count=improvement_count,
    )

    config_summary = ConfigSummary(
        comparison_labels=config.comparison_labels,
        ignored_labels=config.ignored_labels,
        sorting_labels=config.sorting_labels,
        regression_config=config.regression_config,
    )

    baseline_summary = BaselineSummary(
        relevant_sources=relevant_sources,
        irrelevant_sources=irrelevant_sources,
        baseline_source_count=len(relevant_sources) + len(irrelevant_sources),
        baseline_skipped=baseline_skipped,
        current_source=current_source,
    )

    return AnalysisSummary(
        tested=test_summary,
        config=config_summary,
        baseline_info=baseline_summary,
        message=message,
    )


def _load_analysis_config(plugin_module: str) -> AnalysisConfig:
    """Load analysis config from plugin module.

    Plugins must expose an `analysis_config` dict or `get_analysis_config()` callable.
    Raises ValueError if config is not available.
    """
    try:
        mod = __import__(plugin_module, fromlist=[""])
    except ImportError as exc:
        raise ValueError(
            f"Failed to import plugin module '{plugin_module}': {exc}. "
            f"Check that the plugin module exists and is importable."
        ) from exc

    if hasattr(mod, "get_analysis_config"):
        try:
            raw = mod.get_analysis_config()
        except Exception as exc:
            raise ValueError(
                f"Plugin module '{plugin_module}' has get_analysis_config() but calling it failed: {exc}"
            ) from exc
    elif hasattr(mod, "analysis_config"):
        raw = mod.analysis_config
    else:
        raise ValueError(
            f"Plugin module '{plugin_module}' is missing required analysis configuration. "
            f"Plugin must provide either 'analysis_config' attribute or 'get_analysis_config()' function."
        )

    if isinstance(raw, AnalysisConfig):
        config = raw
    elif isinstance(raw, dict):
        try:
            config = AnalysisConfig(
                **{k: v for k, v in raw.items() if k in AnalysisConfig.__dataclass_fields__}
            )
        except Exception as exc:
            raise ValueError(
                f"Plugin module '{plugin_module}' analysis config has invalid format: {exc}. "
                f"Config must be a dict with valid AnalysisConfig fields or an AnalysisConfig instance."
            ) from exc
    else:
        raise ValueError(
            f"Plugin module '{plugin_module}' analysis config has unsupported type '{type(raw).__name__}'. "
            f"Must be a dict or AnalysisConfig instance."
        )

    # Validate the configuration fields
    try:
        _validate_analysis_config(config, plugin_module)
        return config
    except ValueError:
        raise


def _validate_analysis_config(config: AnalysisConfig, plugin_module: str) -> None:
    """Validate AnalysisConfig fields and raise ValueError for invalid values."""
    # Validate list fields contain only strings
    for field_name in ["comparison_labels", "ignored_labels", "sorting_labels"]:
        field_value = getattr(config, field_name)
        if not isinstance(field_value, list):
            raise ValueError(
                f"Plugin module '{plugin_module}' analysis config field '{field_name}' must be a list, "
                f"got {type(field_value).__name__}: {field_value}"
            )
        for i, item in enumerate(field_value):
            if not isinstance(item, str):
                raise ValueError(
                    f"Plugin module '{plugin_module}' analysis config field '{field_name}' "
                    f"must contain only strings, got {type(item).__name__} at index {i}: {item}"
                )

    if not isinstance(config.regression_config, dict):
        raise ValueError(
            f"Plugin module '{plugin_module}' analysis config field 'regression_config' "
            f"must be a dict, got {type(config.regression_config).__name__}"
        )


def _filter_labels_for_matching(
    labels: dict[str, Any], excluded_labels: set[str]
) -> dict[str, Any]:
    """Filter labels for matching, removing KPI metadata and excluded labels.

    Returns only labels that should be used for baseline matching logic.
    This ensures consistent filtering between analysis and reporting.
    """
    # Known KPI metadata fields that shouldn't be used for matching
    kpi_metadata_fields = {"higher_is_better", "unit", "help", "is_curve"}

    return {
        k: v for k, v in labels.items() if k not in kpi_metadata_fields and k not in excluded_labels
    }


def _match_key(
    labels: dict[str, Any], ignored_labels: list[str], comparison_labels: list[str]
) -> tuple:
    """Build a hashable match key from labels, excluding ignored and comparison labels."""
    excluded = set(ignored_labels) | set(comparison_labels)
    filtered_labels = _filter_labels_for_matching(labels, excluded)
    return tuple(sorted((k, str(v)) for k, v in filtered_labels.items()))


def _is_relevant_baseline(
    labels: dict[str, Any],
    current_keys: dict[str, set[str]],
    config: AnalysisConfig,
) -> bool:
    """Return True if a baseline entry is relevant to the current data.

    A baseline entry is relevant if for every current test label (excluding
    ignored/comparison labels), the baseline:
    - Has the same label with a value that appears in current data

    Baseline entries missing current test labels are rejected (different config).
    Extra baseline labels not present in current are ignored.
    Uses unified filtering logic for consistency between analysis and reporting.
    """
    excluded_labels = set(config.ignored_labels) | set(config.comparison_labels)
    baseline_filtered = _filter_labels_for_matching(labels, excluded_labels)

    # Check each current test label against baseline
    for k, current_values in current_keys.items():
        if k in excluded_labels:
            continue  # Skip ignored/comparison labels
        if k not in baseline_filtered:
            # Baseline missing this label - different configuration, reject
            return False
        # Baseline has this label - check if value matches
        if str(baseline_filtered[k]) not in current_values:
            return False

    return True


def _build_baseline_index(
    baseline_kpi_data: dict[Path, HierarchicalKpiFormat],
    config: AnalysisConfig,
    current_keys: dict[str, set[str]],
) -> dict[tuple, dict[frozenset, tuple[HierarchicalKpi, HierarchicalTestEntry]]]:
    """Index baseline records by (kpi_id, match_key) for fast lookup.

    Records with unexpected labels or irrelevant values are excluded.
    Deduplication by comparison keys: only one record per unique comparison
    key combination is kept (last one wins across baseline files).

    Returns mapping from (kpi_id, match_key) -> {comparison_keys_frozenset: (kpi, test)}.
    Same-version entries are excluded at query time by the caller.
    """

    index: dict[tuple, dict[frozenset, tuple[HierarchicalKpi, HierarchicalTestEntry]]] = {}
    for _path, kpi_format in baseline_kpi_data.items():
        for test_entry in kpi_format.tests:
            test_labels = test_entry.labels
            if not _is_relevant_baseline(test_labels, current_keys, config):
                continue

            for kpi in test_entry.kpis:
                kpi_id = kpi.kpi_id
                mk = _match_key(test_labels, config.ignored_labels, config.comparison_labels)
                ck = frozenset(
                    (k, test_labels[k]) for k in config.comparison_labels if k in test_labels
                )
                index.setdefault((kpi_id, mk), {})[ck] = (kpi, test_entry)
    return index


def _run_regression_test(
    current: HierarchicalKpi,
    current_test: HierarchicalTestEntry,
    baselines: list[tuple[HierarchicalKpi, HierarchicalTestEntry]],
    config: AnalysisConfig,
) -> RegressionTestResult:
    """Run a regression test for a single KPI record against its baselines.

    Handles all skip logic internally (non-scalar value, insufficient baselines).
    Returns a complete result dict ready for inclusion in the report.

    Args:
        current: Current KPI dataclass
        current_test: Current test entry containing labels and metadata
        baselines: List of (baseline_kpi, baseline_test) tuples
        config: Analysis configuration
    """
    kpi_id = current.kpi_id
    run_id = current_test.run_id
    raw_labels = current_test.labels
    is_curve = current.is_curve
    value = current.values if is_curve else current.value
    higher_is_better = current.higher_is_better

    if "higher_is_better" not in config.ignored_labels:
        logging.info(
            "Adding 'higher_is_better' in the ignored_labels (workaround an incorrect label in old KPIs)"
        )
        config.ignored_labels.append("higher_is_better")

    comparison = set(config.comparison_labels)
    distinct_keys = {
        k: v
        for k, v in raw_labels.items()
        if k not in comparison and k not in config.ignored_labels
    }
    labels = {
        "comparison_keys": {k: raw_labels[k] for k in config.comparison_labels if k in raw_labels},
        "distinct_keys": distinct_keys,
        "ignore_keys": {k: raw_labels[k] for k in config.ignored_labels if k in raw_labels},
    }

    current_comparison_keys = labels["comparison_keys"]

    baseline_values_list = [
        {
            "comparison_keys": {
                k: baseline_test.labels[k]
                for k in config.comparison_labels
                if k in baseline_test.labels
            },
            "value": baseline_kpi.values if is_curve else baseline_kpi.value,
        }
        for baseline_kpi, baseline_test in baselines
    ]

    base = RegressionTestResult(
        kpi_id=kpi_id,
        verdict=Verdict.SKIPPED,  # Temporary, will be overridden
        labels=labels,
        run_id=run_id,
        is_curve=is_curve,
        higher_is_better=higher_is_better,
        current_value=CurrentValueInfo(
            comparison_keys=current_comparison_keys,
            values=value if is_curve else None,
            value=None if is_curve else value,
        ),
        baseline_values=baseline_values_list,
        baseline_count=len(baseline_values_list),
    )

    if is_curve:
        return _curve_auc_change_regression(
            base, value, higher_is_better, baseline_values_list, config.regression_config
        )
    else:
        return _scalar_relative_change_regression(
            base, value, higher_is_better, baseline_values_list, config.regression_config
        )


def _scalar_relative_change_regression(
    base: RegressionTestResult,
    current_value: float | int,
    higher_is_better: bool,
    baseline_values_list: list[dict[str, Any]],
    regression_config: dict[str, Any],
) -> RegressionTestResult:
    """Scalar regression mechanism: compare current value against baseline mean via relative change."""

    relative_change_config = regression_config.get(Algorithm.SCALAR_RELATIVE_CHANGE, {})
    min_baseline_points = relative_change_config.get("min_baseline_points", 1)
    max_relative_regression = relative_change_config.get("max_relative_regression", 0.1)

    scalar_entries = [e for e in baseline_values_list if isinstance(e["value"], (int, float))]

    if not isinstance(current_value, (int, float)):
        base.verdict = Verdict.SKIPPED
        base.reason = "non-scalar current value"
        return base

    if len(scalar_entries) < min_baseline_points:
        base.verdict = Verdict.SKIPPED
        base.reason = f"insufficient baselines ({len(scalar_entries)} < {min_baseline_points})"
        return base

    scalar_values = [entry["value"] for entry in scalar_entries]
    baseline_mean = sum(scalar_values) / len(scalar_values)
    relative_change = (
        0.0 if baseline_mean == 0 else (float(current_value) - baseline_mean) / abs(baseline_mean)
    )

    regression = abs(relative_change) > abs(max_relative_regression)
    reason = None

    if regression:
        direction = "decrease" if higher_is_better else "increase"
        reason = (
            f"relative {direction} of {abs(relative_change * 100):.1f}% "
            f"exceeds threshold {max_relative_regression * 100:.0f}%"
        )

    details = {
        "algorithm": Algorithm.SCALAR_RELATIVE_CHANGE,
        "baseline_mean": round(baseline_mean, 6),
        "relative_change": round(relative_change, 6),
        "config": {"max_relative_regression": max_relative_regression},
    }

    base.verdict = Verdict.REGRESSION if regression else Verdict.PASS
    base.reason = reason
    base.details = details
    return base


def _curve_auc_change_regression(
    base: RegressionTestResult,
    current_value: list,
    higher_is_better: bool,
    baseline_values_list: list[dict[str, Any]],
    regression_config: dict[str, Any],
) -> RegressionTestResult:
    """Curve regression via AUC → scalar relative change.

    Converts each curve to a scalar Area Under Curve (trapezoidal rule),
    then applies the same relative change test as SCALAR_RELATIVE_CHANGE.
    baseline_values_list entries: {"comparison_keys": {...}, "value": [[x, y], ...]}
    """
    curve_config = regression_config.get(Algorithm.CURVE_AUC_CHANGE, {})
    min_baseline_points = curve_config.get("min_baseline_points", 1)
    max_relative_regression = curve_config.get("max_relative_regression", 0.1)

    # Filter for curve baselines that have coordinate pair data
    auc_baselines = [
        e for e in baseline_values_list if e and isinstance(e.get("value"), list) and e["value"]
    ]

    if len(auc_baselines) < min_baseline_points:
        base.verdict = Verdict.SKIPPED
        base.reason = f"insufficient curve baselines ({len(auc_baselines)} < {min_baseline_points})"
        return base

    current_auc = _compute_auc(current_value)

    baseline_auc_entries = [
        {
            "value": round(_compute_auc(e["value"]), 6),
            "comparison_keys": e["comparison_keys"],
        }
        for e in auc_baselines
    ]
    baseline_auc_values = [entry["value"] for entry in baseline_auc_entries]
    baseline_mean_auc = sum(baseline_auc_values) / len(baseline_auc_values)

    relative_change = (
        0.0
        if baseline_mean_auc == 0
        else (current_auc - baseline_mean_auc) / abs(baseline_mean_auc)
    )
    regression = abs(relative_change) > abs(max_relative_regression)
    reason = None
    if regression:
        direction = "decrease" if higher_is_better else "increase"
        reason = (
            f"AUC relative {direction} of {abs(relative_change * 100):.1f}% "
            f"exceeds threshold {max_relative_regression * 100:.0f}%"
        )

    details = {
        "algorithm": Algorithm.CURVE_AUC_CHANGE,
        "current_auc": round(current_auc, 6),
        "baseline_mean_auc": round(baseline_mean_auc, 6),
        "baseline_aucs": baseline_auc_entries,
        "relative_change": round(relative_change, 6),
        "config": {"max_relative_regression": max_relative_regression},
    }

    base.verdict = Verdict.REGRESSION if regression else Verdict.PASS
    base.reason = reason
    base.details = details
    return base


def _compute_auc(curve: list) -> float:
    """Compute area under a curve using the trapezoidal rule.

    Accepts a list of [x, y] pairs or {"x": ..., "y": ...} dicts.
    Points are sorted by x before integration.
    """

    def _xy(point):
        if isinstance(point, dict):
            return float(point["x"]), float(point["y"])
        return float(point[0]), float(point[1])

    if len(curve) < 2:
        return 0.0

    points = sorted((_xy(p) for p in curve), key=lambda p: p[0])
    return sum(
        (points[i + 1][0] - points[i][0]) * (points[i][1] + points[i + 1][1]) / 2
        for i in range(len(points) - 1)
    )


def _sort_results(
    results: list[RegressionTestResult], sorting_labels: list[str]
) -> list[RegressionTestResult]:
    """Sort results by sorting labels extracted from labels, then by kpi_id.
    SKIPPED entries are placed after tested entries."""

    verdict_order = {Verdict.PASS: 0, Verdict.REGRESSION: 1, Verdict.SKIPPED: 2}

    def sort_key(r: RegressionTestResult) -> tuple:
        label_key = tuple(str(r.labels.get(k, "")) for k in sorting_labels)
        return (verdict_order.get(r.verdict, 9), *label_key, r.kpi_id)

    return sorted(results, key=sort_key)


def _summarize_label_sets(
    data: HierarchicalKpiFormat,
    config: AnalysisConfig,
    current_keys: dict[str, set[str]] | None = None,
    current_comparison_combinations: set[frozenset] | None = None,
) -> dict[str, Any]:
    """Summarize label sets found in a hierarchical KPI doc.

    Args:
        data: Hierarchical KPI data
        comparison_labels: List of comparison label keys
        ignored_labels: List of ignored label keys
        current_keys: Universe of label keys and values from current test data
        current_comparison_combinations: Set of frozenset comparison key combinations from current data

    Returns:
      - comparison_labels: unique values per comparison key
      - ignored_labels: unique values per ignored key
      - relevant_common_keys: labels whose value is identical across all test entries (as key=val string)
      - relevant_distinct_keys: per-entry labels that differ (as key=val string), relevant entries only
    """
    all_labels = [test.labels for test in data.tests]

    def _unique_values(key: str) -> list[str]:
        seen: list[str] = []
        for labels in all_labels:
            val = str(labels[key]) if key in labels else None
            if val is not None and val not in seen:
                seen.append(val)
        return sorted(seen)

    seen_all = []
    seen_filtered = []
    for labels in all_labels:
        # Use unified filtering logic
        filtered = _filter_labels_for_matching(labels, set(config.ignored_labels))
        if filtered and filtered not in seen_all:
            seen_all.append(filtered)
        if current_keys is not None:
            if not _is_relevant_baseline(labels, current_keys, config):
                continue

        # Filter out entries with identical comparison key combinations
        if current_comparison_combinations is not None and config.comparison_labels:
            baseline_comparison_keys = frozenset(
                (k, str(labels[k])) for k in config.comparison_labels if k in labels
            )
            if baseline_comparison_keys in current_comparison_combinations:
                continue

        if filtered and filtered not in seen_filtered:
            seen_filtered.append(filtered)

    if seen_filtered:
        all_keys = set().union(*seen_filtered)
        common_keys = {
            k for k in all_keys if all(ls.get(k) == seen_filtered[0].get(k) for ls in seen_filtered)
        }
        common = [f"{k}={seen_filtered[0][k]}" for k in sorted(common_keys)]
        distinct_keys = [
            ",".join(f"{k}={v}" for k, v in sorted(ls.items()) if k not in common_keys)
            for ls in seen_filtered
        ]
        distinct_labels: list[str] = []
        for ls in seen_filtered:
            for k in sorted(ls.keys()):
                if k not in common_keys:
                    if k not in distinct_labels:
                        distinct_labels.append(k)
        distinct_labels.sort()

    else:
        common, distinct_keys, distinct_labels = [], [], []

    return {
        "comparison_keys": sorted(
            f"{k}={v}" for k in config.comparison_labels for v in _unique_values(k)
        ),
        "ignored_keys": sorted(
            f"{k}={v}" for k in config.ignored_labels for v in _unique_values(k)
        ),
        "relevant_common_keys": common,
        "relevant_distinct_keys": distinct_keys,
        "relevant_distinct_labels": distinct_labels,
        "relevant_count": len(seen_filtered),
        "irrelevant_count": len(seen_all) - len(seen_filtered),
    }


def _build_report(
    results: list[RegressionTestResult],
    config: AnalysisConfig,
    current_source: dict[str, Any],
    relevant_sources: list[dict[str, Any]],
    irrelevant_sources: list[dict[str, Any]],
    baseline_skipped: dict[str, int],
) -> tuple[OverallStatus, RegressionReport]:
    """Build the final report structure using original format."""
    regressions = [r for r in results if r.verdict == Verdict.REGRESSION]
    passes = [r for r in results if r.verdict == Verdict.PASS]
    skipped = [r for r in results if r.verdict == Verdict.SKIPPED]

    if regressions:
        overall_status = OverallStatus.REGRESSION_DETECTED
    elif not passes:
        overall_status = OverallStatus.NO_TEST_PERFORMED
    else:
        overall_status = OverallStatus.PASS

    # Build analysis section
    analysis = AnalysisSection(
        status=overall_status, timestamp=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    # Build config section (convert AnalysisConfig to dict)
    config_dict = {
        "comparison_labels": config.comparison_labels,
        "ignored_labels": config.ignored_labels,
        "sorting_labels": config.sorting_labels,
        "regression_config": config.regression_config,
    }

    # Build tested section
    tested = TestedSection(
        total_kpis=len(results),
        pass_count=len(passes),
        regression=len(regressions),
        skipped=len(skipped),
    )

    # Build overall section
    overall = OverallSection(
        verdict=overall_status,
        regression_count=len(regressions),
        total_tested=len(results),
        total_skipped=len(skipped),
    )

    # Build input_data section
    input_data = InputDataSection(
        current_source=current_source,
        baseline_sources={
            "relevant_sources": relevant_sources,
            "irrelevant_sources": irrelevant_sources,
            "baseline_source_count": len(relevant_sources) + len(irrelevant_sources),
            "baseline_skipped": baseline_skipped,
        },
    )

    # Build results array
    result_entries = []
    for result in results:
        # Extract values from result details
        details = result.details or {}
        current_value_info = result.current_value

        # Skip KPIs without current value data - this indicates a data problem
        if not current_value_info:
            logger.warning(
                "Skipping KPI %s (run_id=%s) - missing current value data",
                result.kpi_id,
                result.run_id,
            )
            continue

        # Extract labels for ResultLabels
        current_labels = current_value_info.comparison_keys

        entry = ResultEntry(
            kpi_id=result.kpi_id,
            verdict=result.verdict,  # Already a Verdict enum
            labels=ResultLabels(comparison_keys=current_labels, distinct_keys={}, ignore_keys={}),
            run_id=result.run_id,
            is_curve=result.is_curve,
            higher_is_better=result.higher_is_better,
            current_value=current_value_info,
            baseline_values=result.baseline_values,
            baseline_count=result.baseline_count,
            details=details,
        )
        result_entries.append(entry)

    report = RegressionReport(
        analysis=analysis,
        config=config_dict,
        tested=tested,
        overall=overall,
        input_data=input_data,
        results=result_entries,
    )

    return overall_status, report


def _log_baseline_miss(
    kpi_id: str,
    current_mk: tuple,
    baseline_index: dict[tuple, dict[frozenset, tuple[HierarchicalKpi, HierarchicalTestEntry]]],
) -> None:
    """Log why no baseline matched for a KPI record."""
    candidate_keys = [k for k in baseline_index if k[0] == kpi_id]
    logger.debug("")
    if not candidate_keys:
        logger.debug("  No baseline entry at all for kpi_id=%r", kpi_id)
        return

    current_mk_dict = dict(current_mk)
    logger.debug("  kpi_id=%r: no match. current match_key=\n%s", kpi_id, current_mk_dict)

    for _, baseline_mk in candidate_keys:
        baseline_mk_dict = dict(baseline_mk)
        in_baseline_not_current = {
            k: v for k, v in baseline_mk_dict.items() if current_mk_dict.get(k) != v
        }
        in_current_not_baseline = {
            k: v for k, v in current_mk_dict.items() if baseline_mk_dict.get(k) != v
        }
        logger.debug(
            "    candidate=\n%s\n| baseline_differs=\n%s\n| current_differs=\n%s",
            baseline_mk_dict,
            in_baseline_not_current,
            in_current_not_baseline,
        )


def run_kpi_analysis(
    current_kpi_file: Path,
    historical_data_dir: Path,
    output_file: Path,
    plugin_module: str,
) -> tuple[KpiAnalysisStatus, RegressionReport | None]:
    """Run KPI regression analysis and generate a JSON report.

    Args:
        current_kpi_file: Path to current KPI JSON file (hierarchical schema v2)
        historical_data_dir: Directory containing historical KPI files (kpis.json)
        output_file: Path where JSON analysis report will be written
        plugin_module: Plugin module name (for loading analysis config)

    Returns:
        Tuple of (KpiAnalysisStatus, analysis_report)
    """
    try:
        logger.info("Running KPI regression analysis")
        logger.info("  current_kpi_file: %s", current_kpi_file)
        logger.info("  historical_data_dir: %s", historical_data_dir)
        logger.info("  output_file: %s", output_file)
        logger.info("  plugin_module: %s", plugin_module)

        if not current_kpi_file.exists():
            logger.error("Current KPI file not found: %s", current_kpi_file)
            return create_failure_status(
                KpiAnalysisStatus,
                error=f"Current KPI file not found: {current_kpi_file}",
                exit_code=1,
            ), None

        if not historical_data_dir.exists():
            logger.error("Historical data directory not found: %s", historical_data_dir)
            return create_failure_status(
                KpiAnalysisStatus,
                error=f"Historical data directory not found: {historical_data_dir}",
                exit_code=1,
            ), None

        try:
            config = _load_analysis_config(plugin_module)
        except ValueError as exc:
            logger.error("Failed to load analysis config: %s", exc)
            return create_failure_status(
                KpiAnalysisStatus,
                error=f"Analysis configuration error: {exc}",
                exit_code=1,
            ), None

        logger.info(
            "  config: comparison_labels=%s, ignored_labels=%s",
            config.comparison_labels,
            config.ignored_labels,
        )

        # Load current KPIs using dataclasses
        with open(current_kpi_file) as f:
            current_raw_data = json.load(f)

        if current_raw_data.get("schema_version") != "2":
            logger.error("Current KPI file must be schema_version 2 (hierarchical)")
            return create_failure_status(
                KpiAnalysisStatus,
                error="Current KPI file must be schema_version 2 (hierarchical)",
                exit_code=1,
            ), None

        try:
            current_data = HierarchicalKpiFormat.from_dict(current_raw_data)
        except Exception as e:
            logger.error("Failed to parse current KPI data: %s", e)
            return create_failure_status(
                KpiAnalysisStatus,
                error=f"Invalid current KPI data: {e}",
                exit_code=1,
            ), None

        # Count total KPI records
        total_current_records = sum(len(test.kpis) for test in current_data.tests)
        if total_current_records == 0:
            logger.warning("No KPI records found in current file")
            return create_failure_status(
                KpiAnalysisStatus,
                error="No KPI records found in current file",
                exit_code=1,
            ), None

        # Load baseline KPIs
        baseline_kpi_data = find_baseline_kpis(historical_data_dir)
        if not baseline_kpi_data:
            _write_no_baseline_report(
                output_file, current_kpi_file, plugin_module, total_current_records, config
            )
            return KpiAnalysisStatus(
                status=StatusLevel.WARNING,
                success=True,
                message="no historical KPI found for regression testing",
                output_file=str(output_file),
                exit_code=2,
                completed_at=time.time(),
                total_kpis=total_current_records,
                baseline_files_count=0,
            ), None

        # Build label universe from current data for baseline filtering
        current_keys: dict[str, set[str]] = {}
        current_comparison_combinations: set[frozenset] = set()

        for test in current_data.tests:
            labels = test.labels
            for k, v in labels.items():
                current_keys.setdefault(k, set()).add(str(v))

            # Build comparison key combinations from current data
            if config.comparison_labels:
                comparison_combo = frozenset(
                    (k, str(labels[k])) for k in config.comparison_labels if k in labels
                )
                current_comparison_combinations.add(comparison_combo)

        # Build baseline index (irrelevant entries are filtered out)
        baseline_index = _build_baseline_index(baseline_kpi_data, config, current_keys)

        # Run regression tests
        results: list[RegressionTestResult] = []

        baseline_skipped_totals: dict[str, int] = {"same_version": 0, "duplicate": 0}

        for test in current_data.tests:
            test_labels = test.labels
            for kpi in test.kpis:
                mk = _match_key(test_labels, config.ignored_labels, config.comparison_labels)
                key = (kpi.kpi_id, mk)

                baseline_dict = baseline_index.get(key, {})
                current_ck = frozenset(
                    (k, test_labels[k]) for k in config.comparison_labels if k in test_labels
                )
                baselines = [
                    baseline_entry
                    for ck, baseline_entry in baseline_dict.items()
                    if ck != current_ck
                ]
                baseline_skipped_totals["same_version"] += len(baseline_dict) - len(baselines)

                if not baselines:
                    _log_baseline_miss(kpi.kpi_id, mk, baseline_index)

                result = _run_regression_test(kpi, test, baselines, config)
                results.append(result)

        # Sort results
        results = _sort_results(results, config.sorting_labels)

        # Build report
        excluded_labels = set(config.ignored_labels) | set(config.comparison_labels)
        relevant_sources = []
        irrelevant_sources = []

        for path, kpi_format in baseline_kpi_data.items():
            summary = _summarize_label_sets(
                kpi_format,
                config,
                current_keys,
                current_comparison_combinations,
            )

            unexpected_labels: set[str] = set()
            irrelevant_keys: set[str] = set()
            for test in kpi_format.tests:
                for k, v in test.labels.items():
                    if k in excluded_labels:
                        continue
                    if k not in current_keys:
                        unexpected_labels.add(k)
                        continue
                    sv = str(v)
                    if sv not in current_keys[k]:
                        entry = f"{k}={sv}"
                        irrelevant_keys.add(entry)

            source_entry = {
                "path": str(path),
                **summary,
                "unexpected_labels": sorted(unexpected_labels),
                "irrelevant_keys": sorted(irrelevant_keys),
            }

            if not unexpected_labels:
                source_entry.pop("unexpected_labels")
            if not irrelevant_keys:
                source_entry.pop("irrelevant_keys")

            # Split sources based on relevant_count
            if summary.get("relevant_count", 0) > 0:
                relevant_sources.append(source_entry)
            else:
                irrelevant_sources.append(source_entry)

        current_source = {
            "path": str(current_kpi_file),
            **_summarize_label_sets(current_data, config, None, None),
        }

        if (irr_count := current_source.pop("irrelevant_count")) != 0:
            logger.error(f"Found {irr_count} irrelevant entries in the current_source. Expected 0.")
        logger.info(f"Found {len(relevant_sources)} relevant files")
        if not baseline_skipped_totals["same_version"]:
            baseline_skipped_totals.pop("same_version")
        if not baseline_skipped_totals["duplicate"]:
            baseline_skipped_totals.pop("duplicate")

        overall_status, report = _build_report(
            results=results,
            config=config,
            current_source=current_source,
            relevant_sources=relevant_sources,
            irrelevant_sources=irrelevant_sources,
            baseline_skipped=baseline_skipped_totals,
        )

        # Write JSON report using dataclass serialization
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w") as f:
            json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)

        regressions = report.overall.regression_count
        total = report.overall.total_tested
        logger.info(
            "Analysis complete: %d/%d KPIs tested, %d regressions",
            total,
            total_current_records,
            regressions,
        )

        # Return status based on the overall verdict from the enum
        baseline_files_count = len(baseline_kpi_data)
        total_kpis = total_current_records

        if overall_status == OverallStatus.REGRESSION_DETECTED:
            return KpiAnalysisStatus(
                status=StatusLevel.REGRESSION_DETECTED,
                success=False,
                regressions_detected=True,
                output_file=str(output_file),
                exit_code=3,
                completed_at=time.time(),
                regression_count=regressions,
                total_kpis=total_kpis,
                baseline_files_count=baseline_files_count,
            ), report
        elif overall_status == OverallStatus.NO_TEST_PERFORMED:
            return KpiAnalysisStatus(
                status=StatusLevel.WARNING,
                success=True,
                message="all KPIs were skipped, no regression test performed",
                output_file=str(output_file),
                exit_code=2,
                completed_at=time.time(),
                total_kpis=total_kpis,
                baseline_files_count=baseline_files_count,
            ), report
        else:  # "success"
            return create_success_status(
                KpiAnalysisStatus,
                output_file=str(output_file),
                total_kpis=total_kpis,
                regression_count=0,
                baseline_files_count=baseline_files_count,
            ), report

    except Exception as e:
        logger.exception("KPI analysis failed")
        return create_failure_status(
            KpiAnalysisStatus,
            error=f"KPI analysis failed: {e}",
            exit_code=1,
        ), None


def _write_no_baseline_report(
    output_file: Path,
    current_kpi_file: Path,
    plugin_module: str,
    current_kpi_count: int,
    config: AnalysisConfig,
) -> None:
    """Write a warning-level report when no baselines are available."""
    # Create an empty RegressionReport for no-baseline case
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    analysis = AnalysisSection(status=OverallStatus.NO_BASELINE, timestamp=timestamp)

    config_dict = {
        "comparison_labels": config.comparison_labels,
        "ignored_labels": config.ignored_labels,
        "sorting_labels": config.sorting_labels,
        "regression_config": config.regression_config,
    }

    tested = TestedSection(total_kpis=current_kpi_count, pass_count=0, regression=0, skipped=0)

    overall = OverallSection(
        verdict=OverallStatus.NO_BASELINE,
        regression_count=0,
        total_tested=current_kpi_count,
        total_skipped=0,
    )

    input_data = InputDataSection(
        current_source={"file": str(current_kpi_file)},
        baseline_sources={
            "relevant_sources": [],
            "irrelevant_sources": [],
            "baseline_source_count": 0,
            "baseline_skipped": {},
        },
    )

    report = RegressionReport(
        analysis=analysis,
        config=config_dict,
        tested=tested,
        overall=overall,
        input_data=input_data,
        results=[],
    )
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w") as f:
        json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)


def find_baseline_kpis(historical_dir: Path) -> dict[Path, HierarchicalKpiFormat]:
    """Load all kpis.json files from historical directory.

    Returns mapping of file paths to loaded hierarchical KPI data (schema v2 only).
    """
    baseline_kpis: dict[Path, HierarchicalKpiFormat] = {}
    kpi_files = list(historical_dir.rglob("kpis.json"))

    if not kpi_files:
        logger.warning("No kpis.json files found in: %s", historical_dir)
        return baseline_kpis

    logger.info("Found %d historical KPI files to load", len(kpi_files))

    for kpi_file in kpi_files:
        try:
            if kpi_file.lstat().st_size == 0:
                logger.warning("Skipping %s: empty", kpi_file)
                continue

            with open(kpi_file) as f:
                raw_kpi_data = json.load(f)

            schema_version = raw_kpi_data.get("schema_version", "unknown")
            if schema_version != "2":
                logger.warning(
                    "Skipping %s: unsupported schema version %s", kpi_file, schema_version
                )
                continue

            # Parse into dataclass
            try:
                kpi_data = HierarchicalKpiFormat.from_dict(raw_kpi_data)
                baseline_kpis[kpi_file] = kpi_data
                logger.debug("Loaded baseline: %s", kpi_file)
            except Exception as parse_error:
                logger.error("Failed to parse KPI data from %s: %s", kpi_file, parse_error)
                continue

        except json.JSONDecodeError as e:
            # Try to read first line to check if it's v1 format (JSONL)
            try:
                with open(kpi_file) as f:
                    first_line = f.readline().strip()
                    if first_line:
                        first_line_data = json.loads(first_line)
                        if first_line_data.get("schema_version") == "1":
                            logger.info(
                                "Skipping %s: v1 KPI format (JSONL) is not supported, use v2 hierarchical format",
                                kpi_file,
                            )
                            continue
            except Exception:
                pass  # If first line check fails, fall back to original error

            logger.exception("Failed to load %s: %s", kpi_file, e)
        except Exception as e:
            logger.exception("Failed to load %s: %s", kpi_file, e)

    logger.info("Successfully loaded %d historical KPI files", len(baseline_kpis))
    return baseline_kpis


def run_analyze(
    *,
    current_path: Any,
    baseline_kpis: dict[Path, dict[str, Any]],
    output_path: Any,
    plugin: Any = None,
) -> dict[str, Any]:
    """Run KPI analysis against ALL baseline files (CLI interface)."""
    if not baseline_kpis:
        return {
            "status": "failed",
            "error": "No baseline KPI files provided",
            "completed_at": time.time(),
        }

    first_baseline_path = next(iter(baseline_kpis.keys()))
    historical_dir = first_baseline_path.parent

    plugin_module = getattr(plugin, "__module__", "unknown") if plugin else "unknown"

    return run_kpi_analysis(
        current_kpi_file=Path(current_path),
        historical_data_dir=historical_dir,
        output_file=Path(output_path),
        plugin_module=plugin_module,
    )


def analyze_kpis(
    current_kpis_file: Path,
    historical_kpis_dir: Path,
    output_file: Path,
    plugin_module: str,
) -> tuple[KpiAnalysisStatus, dict[str, Any]]:
    """Analyze KPIs with automatic v1/v2 format conversion.

    This function handles:
    1. Format detection and conversion from v1 (JSONL) to v2 (hierarchical)
    2. Plugin loading
    3. Analysis execution
    4. Temporary file cleanup
    5. Result formatting

    Args:
        current_kpis_file: Path to current KPIs file (v1 or v2 format)
        historical_kpis_dir: Directory containing historical KPI files
        output_file: Path where analysis results will be written
        plugin_module: Plugin module name for KPI definitions and analysis rules

    Returns:
        Dictionary with status information:
        - success: bool - whether analysis completed successfully
        - output_file: str - path to analysis results (if successful)
        - error: str - error message (if failed)
    """

    try:
        return run_kpi_analysis(
            current_kpi_file=current_kpis_file,
            historical_data_dir=historical_kpis_dir,
            output_file=output_file,
            plugin_module=plugin_module,
        )

    except Exception as e:
        logger.exception("KPI analysis with format conversion failed")
        return create_failure_status(
            KpiAnalysisStatus,
            error=str(e),
            exit_code=1,
        ), {}
