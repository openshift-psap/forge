"""RHAIIS pipeline notification helpers."""

from __future__ import annotations

import logging
import os
import traceback

from projects.rhaiis.orchestration import runtime_config

logger = logging.getLogger(__name__)


def _send_alert(
    error_message: str,
    *,
    model_key: str,
    workload_keys: list[str],
) -> None:
    """Shared implementation for pipeline failure/warning Slack alerts."""
    from projects.core.library import config as _cfg
    from projects.rhaiis.postprocess.regression import send_failure_notification

    model_cfg = runtime_config.get_model(model_key)
    accelerator = runtime_config.get_accelerator()
    cluster_tag = _cfg.project.get_config("rhaiis.cluster_tag", "")
    engine = runtime_config.get_engine()
    engine_args = runtime_config.merge_engine_args(
        runtime_config.get_engine_args(engine),
        model_cfg,
        runtime_config.get_workload(workload_keys[0]),
        engine,
    )
    tp = (
        engine_args.get("tensor-parallel-size")
        or engine_args.get("tp-size")
        or engine_args.get("tp_size")
        or ""
    )
    dp = engine_args.get("data-parallel-size") or engine_args.get("dp-size") or ""

    send_failure_notification(
        error=error_message,
        model=model_cfg.get("hf_model_id", model_key),
        accelerator=accelerator,
        job_id=os.environ.get("FJOB_NAME", ""),
        slack_user=_cfg.project.get_config("tests.rhaiis.slack_user", ""),
        owner=_cfg.project.get_config("ci_job.owner", "") or "",
        notification_vault="psap-forge-notifications",
        tp=str(tp),
        dp=str(dp),
        version=_cfg.project.get_config("tests.rhaiis.version", ""),
        workload_keys=workload_keys,
        cluster=cluster_tag,
        engine=engine,
    )


def send_pipeline_failure_alert(
    exc: Exception,
    *,
    model_key: str,
    workload_keys: list[str],
) -> None:
    """Best-effort Slack alert when the pipeline fails."""
    try:
        error_message = traceback.format_exception_only(type(exc), exc)[-1].strip()
        _send_alert(error_message, model_key=model_key, workload_keys=workload_keys)
    except Exception:
        logger.warning("Failed to send pipeline failure alert", exc_info=True)


def send_pipeline_warning(
    *,
    warnings: list[str],
    model_key: str,
    workload_keys: list[str],
) -> None:
    """Best-effort Slack alert for non-critical pipeline warnings."""
    try:
        warning_text = "\n".join(f"• {w}" for w in warnings)
        error_message = f"Pipeline completed with warnings:\n{warning_text}"
        _send_alert(error_message, model_key=model_key, workload_keys=workload_keys)
    except Exception:
        logger.warning("Failed to send pipeline warning alert", exc_info=True)
