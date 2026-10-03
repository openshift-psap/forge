from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import click
import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MetricMetadata:
    metric_name: str
    description: str
    unit: str


def load_profile_metadata(*paths: str | Path) -> list[MetricMetadata]:
    seen_names: dict[str, Path] = {}
    metadata: list[MetricMetadata] = []

    for path in paths:
        path = Path(path)
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        if not isinstance(raw, list) or not raw:
            raise ValueError(f"{path}: expected a non-empty list of metric entries")

        for entry in raw:
            if not isinstance(entry, dict):
                raise ValueError(f"{path}: each entry must be a mapping")

            metric_name = entry.get("metricName")
            if not metric_name:
                raise ValueError(f"{path}: entry missing required field 'metricName'")
            if "query" not in entry:
                raise ValueError(f"{path}: entry {metric_name!r} missing required field 'query'")

            if metric_name in seen_names:
                raise ValueError(
                    f"duplicate metricName {metric_name!r} in {path} "
                    f"(already defined in {seen_names[metric_name]})"
                )
            seen_names[metric_name] = path

            metadata.append(
                MetricMetadata(
                    metric_name=metric_name,
                    description=entry.get("description", ""),
                    unit=entry.get("unit", ""),
                )
            )

    return metadata


def resolve_files(
    names: list[str],
    include_dirs: list[str | Path],
) -> list[Path]:
    dirs = [Path(d) for d in include_dirs]
    resolved: list[Path] = []

    for name in names:
        p = Path(name)
        if "/" in name or p.is_absolute():
            if not p.suffix:
                p = p.with_suffix(".yaml")
            if not p.exists():
                raise FileNotFoundError(f"metrics file not found: {p}")
            resolved.append(p)
            continue

        for d in dirs:
            candidate = d / f"{name}.yaml"
            if candidate.exists():
                resolved.append(candidate)
                break
        else:
            searched = [str(d) for d in dirs]
            raise FileNotFoundError(f"metrics file {name!r} not found in include_dirs: {searched}")

    return resolved


def resolve_params(
    raw_params: dict[str, str],
    runtime_params: dict[str, str] | None = None,
) -> dict[str, str]:
    params = dict(raw_params)

    if runtime_params:
        params.update(runtime_params)

    unset = [k for k, v in params.items() if isinstance(v, str) and v == "set_at_runtime"]
    if unset:
        raise ValueError(f"metrics config params not set at runtime: {', '.join(unset)}")

    return params


def interpolate_variables(
    variables: dict[str, str],
    params: dict[str, str],
) -> dict[str, str]:
    result = {}
    for key, value in variables.items():
        if isinstance(value, str):
            for param_name, param_value in params.items():
                value = value.replace(f"{{{param_name}}}", str(param_value))
        result[key] = value
    return result


def build_metadata_index(
    metadata: list[MetricMetadata],
    results_dir: Path,
    *,
    variables: dict[str, str] | None = None,
    timestamp: str | None = None,
) -> Path:
    if timestamp is None:
        timestamp = datetime.now(UTC).isoformat()

    results: dict[str, dict] = {}
    for meta in metadata:
        result_file = results_dir / f"{meta.metric_name}.json"

        if result_file.exists():
            try:
                payload = json.loads(result_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                status = "error"
            else:
                if isinstance(payload, list) and len(payload) > 0:
                    status = "ok"
                elif isinstance(payload, list):
                    status = "no_data"
                else:
                    status = "error"
        else:
            status = "no_data"

        results[meta.metric_name] = {
            "status": status,
            "description": meta.description,
            "unit": meta.unit,
        }

    index = {
        "timestamp": timestamp,
        "variables": dict(variables) if variables else {},
        "results": results,
    }

    index_path = results_dir / "metrics_metadata.yaml"
    with index_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(index, f, default_flow_style=False, sort_keys=False)

    return index_path


def _get_config(key, default=None):
    try:
        from projects.core.library import config

        return config.project.get_config(f"prom.capture.metrics.{key}", default)
    except Exception:
        return default


@click.command("capture-metrics")
@click.option("--group", "group_name", default=None, help="Load defaults from a named config group")
@click.option(
    "--var",
    "vars_cli",
    multiple=True,
    help="Variable KEY=VALUE, overrides config (repeatable)",
)
@click.option(
    "--start-time",
    default=None,
    help="Start of capture window (ISO 8601, default: 1h ago)",
)
@click.option(
    "--end-time",
    default=None,
    help="End of capture window (ISO 8601, default: now)",
)
@click.option(
    "--files", "file_names", multiple=True, help="Metric profile files to include (repeatable)"
)
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Output directory for results",
)
@click.option(
    "--include-dirs",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Additional directories for file resolution (repeatable)",
)
@click.pass_context
def capture_metrics_command(
    ctx,
    group_name,
    vars_cli,
    start_time,
    end_time,
    file_names,
    output_dir,
    include_dirs,
):
    """Capture Prometheus metrics using kube-burner metric profiles.

    Without --group or --files, runs all enabled groups from config.
    Use --group to run a single configured group.
    Use --files for ad-hoc file selection.
    CLI options override group/config defaults.
    """

    from datetime import timedelta

    from projects.cluster.toolbox.capture_prometheus_metrics.main import (
        run as _capture_prometheus_metrics,
    )

    def _parse_iso_time(value):
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt

    if end_time is not None:
        end_time = _parse_iso_time(end_time)
    else:
        end_time = datetime.now(UTC)

    if start_time is not None:
        start_time = _parse_iso_time(start_time)
    else:
        start_time = end_time - timedelta(hours=1)

    all_include_dirs = list(_get_config("include_dirs", []))
    all_include_dirs.extend(str(d) for d in include_dirs)

    cli_vars = {}
    for kv in vars_cli:
        if "=" not in kv:
            raise click.ClickException(f"Invalid --var format {kv!r}, expected KEY=VALUE")
        k, v = kv.split("=", 1)
        cli_vars[k] = v

    try:
        from projects.core.library import config as _config_mod

        raw_params = _config_mod.project.get_config(
            "prom.capture.metrics.params", {}, warn=False, print=False
        )
        params = {
            k: _config_mod.project.resolve_reference(v) if isinstance(v, str) else v
            for k, v in raw_params.items()
        }
    except Exception:
        params = {}

    params = resolve_params(params, cli_vars)

    if file_names:
        groups_to_run = {
            "cli": {
                "files": list(file_names),
            }
        }
    elif group_name:
        group_cfg = _get_config(f"groups.{group_name}", {})
        if not group_cfg:
            raise click.ClickException(f"Group {group_name!r} not found in config")
        groups_to_run = {group_name: group_cfg}
    else:
        groups_to_run = _get_config("groups", {})
        if not groups_to_run:
            raise click.ClickException("No groups in config and no --files/--group specified")

    errors = []
    for grp_name, grp_cfg in groups_to_run.items():
        if not grp_cfg.get("enabled", True):
            logger.info("Group %s: disabled, skipping.", grp_name)
            continue

        grp_files = list(file_names) or grp_cfg.get("files", [])
        if not grp_files:
            logger.warning("Group %s: no files specified, skipping.", grp_name)
            continue

        try:
            yaml_paths = resolve_files(grp_files, all_include_dirs)

            metadata = load_profile_metadata(*yaml_paths)
            logger.info("Group %s: %d metrics loaded", grp_name, len(metadata))

            group_variables = interpolate_variables(grp_cfg.get("variables", {}), params)

            grp_output_dir = _capture_prometheus_metrics(
                metric_profiles=[str(p) for p in yaml_paths],
                start_time=start_time,
                end_time=end_time,
                variables=group_variables,
                artifact_dirname_suffix=grp_name,
            )

            if grp_output_dir and Path(grp_output_dir).exists():
                index_path = build_metadata_index(
                    metadata, Path(grp_output_dir), variables=group_variables
                )
                logger.info("Group %s: metadata index written to %s", grp_name, index_path)
        except Exception:
            logger.exception("Group %s: capture failed", grp_name)
            errors.append(grp_name)

    if errors:
        raise click.ClickException(f"Capture failed for groups: {', '.join(errors)}")


capture_metrics_command.__name__ = "capture-metrics"
