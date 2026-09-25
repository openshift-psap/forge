from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import yaml

from projects.cluster.library.prom import metrics as prom_metrics
from projects.cluster.toolbox.capture_prometheus_db.main import run as _capture_prometheus_db
from projects.cluster.toolbox.capture_prometheus_metrics.main import (
    run as _capture_prometheus_metrics,
)
from projects.cluster.toolbox.enable_user_workload_monitoring.main import (
    run as _enable_user_workload_monitoring,
)
from projects.core.dsl.utils.k8s import oc
from projects.core.library import config

logger = logging.getLogger(__name__)

UWM_NAMESPACE = "openshift-user-workload-monitoring"
UWM_POD = "prometheus-user-workload-0"
MONITORING_NAMESPACE = "openshift-monitoring"
CONFIGMAP_NAME = "cluster-monitoring-config"


def capture_prometheus_metrics(
    start_time: datetime,
    end_time: datetime,
    runtime_variables: dict[str, str] | None = None,
) -> list[str]:
    if not config.project.get_config("prom.capture.metrics.enabled", False):
        logger.info("Prometheus metrics capture not enabled, skipping.")
        return []

    groups = config.project.get_config("prom.capture.metrics.groups", {})
    if not groups:
        logger.warning("No metrics groups configured, skipping capture.")
        return []

    include_dirs = config.project.get_config("prom.capture.metrics.include_dirs", [])

    raw_variables = config.project.get_config("prom.capture.metrics.config", {})
    variables = {
        k: config.project.resolve_reference(v) if isinstance(v, str) else v
        for k, v in raw_variables.items()
    }
    if runtime_variables:
        variables.update(runtime_variables)

    errors: list[str] = []

    for group_name, group_cfg in groups.items():
        try:
            if not group_cfg.get("enabled", True):
                logger.info("Group %s: disabled, skipping.", group_name)
                continue

            file_names = group_cfg.get("files", [])
            if not file_names:
                logger.warning("Group %s: no files specified, skipping.", group_name)
                continue

            yaml_paths = prom_metrics.resolve_files(file_names, include_dirs)
            defs = prom_metrics.load_definitions(*yaml_paths)

            step = group_cfg.get("step_seconds", 15)
            params = prom_metrics.interpolate_params(group_cfg.get("params", {}), variables)

            if not defs:
                logger.warning("Group %s: no metrics after loading, skipping.", group_name)
                continue

            queries = prom_metrics.resolve(defs, params)
            logger.info("Group %s: capturing %d metrics queries", group_name, len(queries))

            tmp_dir = Path("/tmp/prom_metrics_capture")
            input_path = prom_metrics.write_capture_input(queries, tmp_dir / f"{group_name}.yaml")

            output_dir = _capture_prometheus_metrics(
                str(input_path),
                start_time,
                end_time,
                step_seconds=step,
                artifact_dirname_suffix=group_name,
            )

            prom_metrics.build_index(defs, params, output_dir)
        except Exception:
            logger.exception("Group %s: metrics capture failed", group_name)
            errors.append(group_name)

    return errors


def is_user_workload_monitoring_enabled() -> bool:
    result = oc(
        "-n",
        MONITORING_NAMESPACE,
        "get",
        "configmap",
        CONFIGMAP_NAME,
        "-o",
        "jsonpath={.data.config\\.yaml}",
        check=False,
    )

    if not result.success:
        return False

    monitoring_config = yaml.safe_load(result.stdout)

    if not isinstance(monitoring_config, dict):
        return False

    return bool(monitoring_config.get("enableUserWorkload", False))


def validate_user_workload_monitoring() -> None:
    if not config.project.get_config("prom.capture.db.user_workload.fail_if_not_enabled"):
        return

    if not is_user_workload_monitoring_enabled():
        raise RuntimeError(
            "User workload monitoring is not enabled on the cluster, "
            "but prom.capture.db.user_workload.fail_if_not_enabled is set"
        )


def prepare_user_workload_monitoring(*, during: str) -> None:
    if not config.project.get_config(f"prom.prepare.user_workload.during_{during}"):
        return

    logger.info("Enabling user workload monitoring on the cluster (during %s)", during)
    _enable_user_workload_monitoring()


def capture_prometheus(
    start_time: datetime,
    end_time: datetime,
    runtime_variables: dict[str, str] | None = None,
) -> None:
    if not config.project.get_config("prom.capture.enabled"):
        logger.info("Prometheus metrics capture not enabled.")
        return

    failures: list[str] = []

    try:
        failed_groups = capture_prometheus_metrics(start_time, end_time, runtime_variables)
        if failed_groups:
            failures.append(f"metrics groups: {', '.join(failed_groups)}")
    except Exception:
        logger.exception("Prometheus metrics capture failed")
        failures.append("metrics capture")

    if config.project.get_config("prom.capture.db.enabled"):
        if config.project.get_config("prom.capture.db.system_metrics.enabled"):
            try:
                logger.info("Capturing Prometheus system metrics")
                _capture_prometheus_db(
                    start_time,
                    end_time,
                    artifact_dirname_suffix="system",
                )
            except Exception:
                logger.exception("System Prometheus TSDB capture failed")
                failures.append("system TSDB")
        else:
            logger.info("Prometheus system metrics capture is not enabled, skipping.")

        if config.project.get_config("prom.capture.db.user_workload.enabled"):
            if not is_user_workload_monitoring_enabled():
                logger.warning(
                    "User workload monitoring is not enabled on the cluster, skipping UWM capture"
                )
            else:
                try:
                    logger.info("Capturing user-workload monitoring Prometheus metrics")
                    _capture_prometheus_db(
                        start_time,
                        end_time,
                        namespace=UWM_NAMESPACE,
                        pod_name=UWM_POD,
                        artifact_dirname_suffix="uwm",
                    )
                except Exception:
                    logger.exception("UWM Prometheus TSDB capture failed")
                    failures.append("UWM TSDB")
    else:
        logger.info("Prometheus DB capture is not enabled, skipping.")

    if failures:
        raise RuntimeError(f"Prometheus capture failed: {', '.join(failures)}")
