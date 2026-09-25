from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import click
import yaml

logger = logging.getLogger(__name__)

VALID_ON_ERROR = ("ignore", "fail")
METRIC_FIELDS = {
    "category",
    "tags",
    "description",
    "unit",
    "on_error",
    "params",
    "promql",
}


@dataclass(frozen=True)
class MetricParam:
    name: str
    description: str
    default: str | None = None

    @classmethod
    def from_dict(cls, name: str, data: dict) -> MetricParam:
        return cls(
            name=name,
            description=data["description"],
            default=data.get("default"),
        )


@dataclass(frozen=True)
class MetricDefinition:
    key: str
    category: str
    description: str
    unit: str
    promql: str
    on_error: str = "ignore"
    tags: tuple[str, ...] = ()
    params: tuple[MetricParam, ...] = ()

    @classmethod
    def from_dict(cls, key: str, data: dict) -> MetricDefinition:
        unknown = set(data) - METRIC_FIELDS
        if unknown:
            raise ValueError(f"metric {key!r}: unknown fields {unknown}")

        for required in ("category", "description", "unit", "promql"):
            if required not in data:
                raise ValueError(f"metric {key!r}: missing required field {required!r}")

        on_error = data.get("on_error", "ignore")
        if on_error not in VALID_ON_ERROR:
            raise ValueError(
                f"metric {key!r}: invalid on_error {on_error!r}, must be one of {VALID_ON_ERROR}"
            )

        raw_params = data.get("params", {})
        params = tuple(MetricParam.from_dict(n, v) for n, v in raw_params.items())

        return cls(
            key=key,
            category=data["category"],
            description=data["description"],
            unit=data["unit"],
            promql=data["promql"],
            on_error=on_error,
            tags=tuple(data.get("tags", ())),
            params=params,
        )


FILE_LEVEL_KEYS = {"params"}


def load_definitions(*paths: str | Path) -> list[MetricDefinition]:
    seen_keys: dict[str, Path] = {}
    definitions: list[MetricDefinition] = []

    for path in paths:
        path = Path(path)
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        if not isinstance(raw, dict) or not raw:
            raise ValueError(f"{path}: expected a non-empty mapping of metric definitions")

        file_params = raw.get("params", {})

        for key, data in raw.items():
            if key in FILE_LEVEL_KEYS:
                continue
            if key in seen_keys:
                raise ValueError(
                    f"duplicate metric key {key!r} in {path} (already defined in {seen_keys[key]})"
                )
            seen_keys[key] = path

            if file_params and "params" not in data:
                data = {**data, "params": file_params}

            definitions.append(MetricDefinition.from_dict(key, data))

    return definitions


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


def select(
    definitions: list[MetricDefinition],
    *,
    categories: list[str] | None = None,
    tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
    keys: list[str] | None = None,
) -> list[MetricDefinition]:
    result = definitions

    if categories is not None:
        cat_set = set(categories)
        result = [d for d in result if d.category in cat_set]

    if tags is not None:
        tag_set = set(tags)
        result = [d for d in result if tag_set.intersection(d.tags)]

    if exclude_tags is not None:
        excl_set = set(exclude_tags)
        result = [d for d in result if not excl_set.intersection(d.tags)]

    if keys is not None:
        key_set = set(keys)
        result = [d for d in result if d.key in key_set]

    return result


def interpolate_params(params: dict[str, str], variables: dict[str, str]) -> dict[str, str]:
    unset = [k for k, v in variables.items() if isinstance(v, str) and v == "SET_AT_RUNTIME"]
    if unset:
        raise ValueError(f"metrics config variables not set at runtime: {', '.join(unset)}")

    result = {}
    for key, value in params.items():
        if isinstance(value, str):
            for var_name, var_value in variables.items():
                value = value.replace(f"{{{var_name}}}", str(var_value))
        result[key] = value
    return result


def resolve(
    definitions: list[MetricDefinition],
    params: dict[str, str],
) -> dict[str, str]:
    missing: list[str] = []
    queries: dict[str, str] = {}

    for defn in definitions:
        resolved_params: dict[str, str] = {}
        defn_missing = False
        for p in defn.params:
            if p.name in params:
                resolved_params[p.name] = params[p.name]
            elif p.default is not None:
                resolved_params[p.name] = p.default
            else:
                missing.append(f"{defn.key}.{p.name}")
                defn_missing = True

        if not defn_missing:
            promql = defn.promql
            for pname, pvalue in resolved_params.items():
                promql = promql.replace(f"{{{pname}}}", pvalue)
            queries[defn.key] = promql

    if missing:
        logger.error("Skipped metrics with missing mandatory params: %s", ", ".join(missing))

    return queries


def write_capture_input(
    queries: dict[str, str],
    path: str | Path,
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(queries, f, default_flow_style=False, sort_keys=False)
    return path


def build_index(
    definitions: list[MetricDefinition],
    params: dict[str, str],
    results_dir: Path,
    *,
    timestamp: str | None = None,
) -> Path:
    if timestamp is None:
        timestamp = datetime.now(UTC).isoformat()

    results: dict[str, dict] = {}
    for defn in definitions:
        result_file = results_dir / f"{defn.key}.json"

        if result_file.exists():
            try:
                payload = json.loads(result_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                status = "error"
            else:
                prom_status = payload.get("status") if isinstance(payload, dict) else None
                if prom_status != "success":
                    status = "error"
                elif not payload.get("data", {}).get("result"):
                    status = "no_data"
                else:
                    status = "ok"
        else:
            status = "no_data"

        used_params = {}
        for p in defn.params:
            if p.name in params:
                used_params[p.name] = params[p.name]
            elif p.default is not None:
                used_params[p.name] = p.default

        resolved_promql = defn.promql
        for pname, pvalue in used_params.items():
            resolved_promql = resolved_promql.replace(f"{{{pname}}}", pvalue)

        entry: dict = {
            "status": status,
            "description": defn.description,
            "unit": defn.unit,
            "category": defn.category,
            "on_error": defn.on_error,
            "promql": resolved_promql,
        }
        if defn.tags:
            entry["tags"] = list(defn.tags)
        if used_params:
            entry["params"] = used_params

        results[defn.key] = entry

    index = {
        "timestamp": timestamp,
        "results": results,
    }

    index_path = results_dir / "index.yaml"
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
    "--param",
    "params_cli",
    multiple=True,
    help="Parameter key=value, overrides group config (repeatable)",
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
    "--files", "file_names", multiple=True, help="Metric definition files to include (repeatable)"
)
@click.option("--tags", multiple=True, help="Filter by tag (repeatable)")
@click.option("--exclude-tags", multiple=True, help="Exclude metrics with tag (repeatable)")
@click.option("--keys", multiple=True, help="Select specific metric keys (repeatable)")
@click.option(
    "--step",
    type=int,
    default=None,
    help="Query step in seconds (default: from group/config or 15)",
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
    params_cli,
    start_time,
    end_time,
    file_names,
    tags,
    exclude_tags,
    keys,
    step,
    output_dir,
    include_dirs,
):
    """Capture Prometheus metrics using PromQL definition files.

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

    cli_params = {}
    for kv in params_cli:
        if "=" not in kv:
            raise click.ClickException(f"Invalid --param format {kv!r}, expected key=value")
        k, v = kv.split("=", 1)
        cli_params[k] = v

    if file_names:
        groups_to_run = {
            "cli": {
                "files": list(file_names),
                "step_seconds": step or 15,
                "params": cli_params,
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

        grp_step = step or grp_cfg.get("step_seconds", 15)

        try:
            yaml_paths = resolve_files(grp_files, all_include_dirs)

            defs = load_definitions(*yaml_paths)
            logger.info("Group %s: %d metrics loaded", grp_name, len(defs))

            defs = select(
                defs,
                tags=list(tags) or None,
                exclude_tags=list(exclude_tags) or None,
                keys=list(keys) or None,
            )
            logger.info("Group %s: %d metrics after filtering", grp_name, len(defs))

            if not defs:
                logger.warning("Group %s: no metrics after filtering, skipping.", grp_name)
                continue

            try:
                from projects.core.library import config as _config_mod

                raw_variables = _config_mod.project.get_config(
                    "prom.capture.metrics.config", {}, warn=False, print=False
                )
                variables = {
                    k: _config_mod.project.resolve_reference(v) if isinstance(v, str) else v
                    for k, v in raw_variables.items()
                }
            except Exception:
                variables = {}
            params = interpolate_params(grp_cfg.get("params", {}), variables)
            params.update(cli_params)

            queries = resolve(defs, params)
            logger.info("Group %s: %d queries resolved", grp_name, len(queries))

            tmp_dir = Path("/tmp/prom_metrics_capture")
            input_path = write_capture_input(queries, tmp_dir / f"{grp_name}.yaml")

            grp_out = str(output_dir / grp_name) if output_dir else None
            grp_output_dir = _capture_prometheus_metrics(
                str(input_path),
                start_time,
                end_time,
                grp_out,
                step_seconds=grp_step,
                artifact_dirname_suffix=grp_name,
            )

            if grp_output_dir and Path(grp_output_dir).exists():
                index_path = build_index(defs, params, Path(grp_output_dir))
                logger.info("Group %s: index written to %s", grp_name, index_path)
        except Exception:
            logger.exception("Group %s: capture failed", grp_name)
            errors.append(grp_name)

    if errors:
        raise click.ClickException(f"Capture failed for groups: {', '.join(errors)}")
