"""Parse orchestration: traverse → plugin → unified model → cache."""

from __future__ import annotations

from pathlib import Path

from projects.caliper.engine.cache import (
    cache_path_for_test_base,
    fingerprint_test_base,
    read_test_base_cache,
    test_base_cache_is_valid,
    write_test_base_cache,
)
from projects.caliper.engine.model import UnifiedRunModel
from projects.caliper.engine.parameter_matrix import (
    analyze_parameter_matrix,
    format_parameter_matrix_summary,
)
from projects.caliper.engine.traverse import discover_test_bases

# Validation functions no longer needed - using per-test-base caching


def run_parse(
    *,
    base_dir: Path,
    plugin_module: str,
    plugin: object,
    use_cache: bool,
    force_report_partial: bool = True,
    show_parameter_matrix: bool = True,
) -> UnifiedRunModel:
    """
    Run full parse or load valid cache.

    plugin must implement parse(nodes).
    """
    base_dir = base_dir.resolve()

    # Discover test bases
    nodes = discover_test_bases(base_dir)

    # Always use per-test-base caching
    all_records = []
    cache_refs = []
    all_warnings = []

    parse_fn = plugin.parse

    for node in nodes:
        test_base_dir = node.directory
        cache_file = cache_path_for_test_base(test_base_dir, plugin_module)
        fp = fingerprint_test_base(test_base_dir, plugin_module)

        # Try to load from cache
        cached_records = None
        if use_cache:
            raw = read_test_base_cache(test_base_dir, plugin_module)
            if raw is not None and test_base_cache_is_valid(
                raw,
                expected_fingerprint=fp,
                plugin_module=plugin_module,
                test_base_dir=test_base_dir,
            ):
                cached_records = raw["records"]

        if cached_records is not None:
            # Use cached records
            all_records.extend(cached_records)
            cache_refs.append(str(cache_file))
        else:
            # Parse this test base
            result = parse_fn([node])  # Parse just this node
            records = result.records
            warnings = getattr(result, "warnings", [])

            all_records.extend(records)
            all_warnings.extend(warnings)

            # Write cache for this test base
            cache_file = write_test_base_cache(
                test_base_dir,
                plugin_module=plugin_module,
                test_base_records=records,
                fingerprint=fp,
            )
            cache_refs.append(str(cache_file))

    # Create unified model with all records
    cache_ref_summary = f"per-test-base: {len(cache_refs)} cache files"
    model = UnifiedRunModel(
        plugin_module=plugin_module,
        base_directory=str(base_dir),
        test_nodes=nodes,
        unified_result_records=all_records,
        parse_cache_ref=cache_ref_summary,
    )

    if all_warnings and force_report_partial:
        for w in all_warnings:
            print(f"[parse warning] {w}")  # noqa: T201 — CLI feedback

    # Show parameter matrix if requested
    if show_parameter_matrix:
        print()  # noqa: T201 — CLI feedback
        matrix_analysis = analyze_parameter_matrix(all_records)
        matrix_summary = format_parameter_matrix_summary(matrix_analysis)
        print(matrix_summary)  # noqa: T201 — CLI feedback
        print()  # noqa: T201 — CLI feedback

    return model
