"""Visualization orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from projects.caliper.engine.label_filters import parse_filter_pairs
from projects.caliper.engine.parse import run_parse


def resolve_visualize_config(
    base_dir: Path,
    explicit_path: Path | None,
) -> dict[str, Any] | None:
    def _load(p: Path) -> dict[str, Any] | None:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        if data is None:
            return None
        if not isinstance(data, dict):
            raise ValueError(f"Visualize config must be a mapping at top level: {p}")
        return data

    if explicit_path is not None:
        p = explicit_path.resolve()
        if not p.is_file():
            raise FileNotFoundError(f"Visualize config not found: {p}")
        return _load(p)

    for name in ("visualize-groups.yaml", "visualize-groups.yml"):
        cand = base_dir / name
        if cand.is_file():
            return _load(cand)
    return None


def resolve_report_ids(
    *,
    reports_csv: str | None,
    report_group: str | None,
    config: dict[str, Any] | None,
) -> list[str]:
    if reports_csv:
        return [x.strip() for x in reports_csv.split(",") if x.strip()]
    if report_group and config:
        groups = config.get("groups", {})
        if report_group not in groups:
            raise ValueError(f"Unknown report group: {report_group!r}")
        g = groups[report_group]
        if isinstance(g, list):
            return [str(x) for x in g]
        if isinstance(g, str):
            return [g]
    raise ValueError("Provide --reports or --report-group with a valid visualize config.")


def run_visualize(
    *,
    base_dir: Path,
    plugin_module: str,
    plugin: object,
    output_dir: Path,
    reports_csv: str | None,
    report_group: str | None,
    visualize_config_path: Path | None,
    include_pairs: tuple[str, ...],
    exclude_pairs: tuple[str, ...],
    use_cache: bool,
    cache_path: Path | None,
    verbose_parsing: bool = False,
) -> list[str]:
    # Validate visualization configuration BEFORE parsing to avoid wasted work
    cfg = resolve_visualize_config(base_dir, visualize_config_path)
    ids = resolve_report_ids(
        reports_csv=reports_csv,
        report_group=report_group,
        config=cfg,
    )

    # Check if any report IDs were provided (early validation)
    if not ids:
        raise ValueError(
            "No report IDs provided. Use --reports with comma-separated report IDs "
            "or --report-group with a valid visualize config file."
        )

    # Parse include and exclude filters for directory-level filtering
    include_filters = None
    exclude_filters = None

    if include_pairs:
        include_filters = parse_filter_pairs(include_pairs, "include")

    if exclude_pairs:
        exclude_filters = parse_filter_pairs(exclude_pairs, "exclude")

    # Apply both include and exclude filtering during parsing
    model = run_parse(
        base_dir=base_dir,
        plugin_module=plugin_module,
        plugin=plugin,
        use_cache=use_cache,
        include_label_filter=include_filters,
        exclude_label_filter=exclude_filters,
        verbose_parsing=verbose_parsing,
        show_parameter_matrix=True,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    viz = plugin.visualize
    paths = viz(
        model,
        output_dir,
        ids,
        report_group,
        cfg,
    )

    # Check for error conditions
    if ids and not paths:
        # Reports were requested but no files were generated
        # This could be due to invalid plot types or other issues
        raise ValueError(
            f"No visualization files were generated despite requesting report IDs: {ids}. "
            f"This may indicate unsupported plot types or other visualization errors."
        )

    return paths
