"""RHAIIS regression analysis and agent analysis helpers."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from projects.core.library import env

logger = logging.getLogger(__name__)


def run_standalone_analysis(
    model_cfg: dict,
    accelerator_key: str,
    engine_args: dict,
    *,
    run_uuid: str = "",
    restrict_profiles: list[str] | None = None,
) -> None:
    """Run regression check + agent analysis using existing S3 data (no new benchmark)."""
    from projects.caliper.cli.s3_export import create_s3_client, get_aws_credentials
    from projects.core.library import config

    agent_cfg = config.project.get_config("rhaiis.agent_analysis", {})
    if not agent_cfg.get("enabled", False):
        logger.info("Standalone analysis skipped: agent_analysis not enabled")
        return

    version = config.project.get_config("tests.rhaiis.version", "")
    compare_version = config.project.get_config("tests.rhaiis.compare_version", "")
    if not version or not compare_version:
        logger.info("Standalone analysis skipped: version or compare_version not configured")
        return

    s3_cfg = config.project.get_config("rhaiis.s3", {})
    csv_dashboard_cfg = config.project.get_config("caliper.postprocess.csv_dashboard", {})
    s3_bucket = s3_cfg.get("bucket", "")
    s3_key = csv_dashboard_cfg.get("s3_key", "")
    vault_name = s3_cfg.get("vault", "")
    credentials_file = s3_cfg.get("credentials_file", "aws.credentials")

    credentials_path = get_aws_credentials(vault_name, credentials_file)
    if not credentials_path:
        logger.warning("AWS credentials not available, skipping standalone analysis")
        return

    accelerator = (
        accelerator_key.split("_")[0].upper() if "_" in accelerator_key else accelerator_key.upper()
    )

    consolidated_path = None
    current_csv_path = None
    try:
        import pandas as pd

        s3 = create_s3_client(credentials_path)
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            consolidated_path = tmp.name
        s3.download_file(s3_bucket, s3_key, consolidated_path)

        df = pd.read_csv(consolidated_path, on_bad_lines="warn")
        for col in df.columns:
            if df[col].dtype == object:
                df[col] = df[col].str.strip()

        model_id = model_cfg.get("hf_model_id", "")
        tp = str(
            engine_args.get("tensor-parallel-size")
            or engine_args.get("tp-size")
            or engine_args.get("tp_size")
            or 1
        )

        current_rows = df[
            (df["version"] == version)
            & (df["model"] == model_id)
            & (df["accelerator"] == accelerator)
            & (pd.to_numeric(df["TP"], errors="coerce").fillna(-1).astype(int).astype(str) == tp)
        ]

        if current_rows.empty:
            logger.warning(
                "No data found in S3 for version=%s, model=%s, accelerator=%s, TP=%s",
                version,
                model_id,
                accelerator,
                tp,
            )
            return

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tmp:
            current_csv_path = tmp.name
            current_rows.to_csv(tmp, index=False)

        logger.info("Standalone analysis: found %d rows for version=%s", len(current_rows), version)
        run_regression_check(
            current_csv_path,
            compare_version,
            version,
            model_cfg,
            accelerator,
            run_uuid=run_uuid,
            restrict_profiles=restrict_profiles,
            engine_args=engine_args,
        )
    except Exception:
        logger.warning("Standalone analysis failed", exc_info=True)
    finally:
        if consolidated_path and os.path.exists(consolidated_path):
            os.unlink(consolidated_path)
        if current_csv_path and os.path.exists(current_csv_path):
            os.unlink(current_csv_path)


def run_regression_check(
    csv_path,
    compare_version: str,
    current_version: str,
    model_cfg: dict,
    accelerator: str,
    *,
    run_uuid: str = "",
    restrict_profiles: list[str] | None = None,
    engine_args: dict | None = None,
) -> None:
    from projects.caliper.cli.s3_export import create_s3_client, get_aws_credentials
    from projects.core.library import config
    from projects.rhaiis.postprocess.regression import METRICS, PROFILE_MAP, run_regression_analysis

    owner = config.project.get_config("ci_job.owner", "") or ""
    s3_cfg = config.project.get_config("rhaiis.s3", {})
    csv_dashboard_cfg = config.project.get_config("caliper.postprocess.csv_dashboard", {})
    s3_bucket = s3_cfg.get("bucket", "")
    s3_key = csv_dashboard_cfg.get("s3_key", "")
    vault_name = s3_cfg.get("vault", "")
    credentials_file = s3_cfg.get("credentials_file", "aws.credentials")

    credentials_path = get_aws_credentials(vault_name, credentials_file)
    if not credentials_path:
        logger.warning("AWS credentials not available, skipping regression analysis")
        return

    consolidated_path = None
    try:
        s3 = create_s3_client(credentials_path)
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            consolidated_path = tmp.name
        s3.download_file(s3_bucket, s3_key, consolidated_path)
    except Exception as e:
        logger.warning("Could not download consolidated CSV for regression check: %s", e)
        return

    try:
        output_file = Path(env.ARTIFACT_DIR) / "regression_analysis.json"
        analysis = run_regression_analysis(
            current_csv_path=Path(str(csv_path)),
            consolidated_csv_path=Path(consolidated_path),
            compare_version=compare_version,
            current_version=current_version,
            output_file=output_file,
            profile_map=PROFILE_MAP,
            metrics=METRICS,
            restrict_profiles=restrict_profiles,
        )

        _ea = engine_args or {}
        tp = _ea.get("tensor-parallel-size") or _ea.get("tp-size") or _ea.get("tp_size") or ""
        dp = _ea.get("data-parallel-size") or _ea.get("dp-size") or ""
        slack_user = config.project.get_config("tests.rhaiis.slack_user", "")

        if analysis.get("regression_count", 0) > 0 or analysis.get("improvement_count", 0) > 0:
            report_url = ""
            agent_cfg = config.project.get_config("rhaiis.agent_analysis", {})
            if agent_cfg.get("enabled", False):
                report_url = run_agent_analysis(
                    analysis,
                    model_cfg,
                    accelerator,
                    current_version,
                    compare_version,
                    run_uuid,
                    severity_threshold=agent_cfg.get("severity_threshold", 10),
                    engine_args=engine_args,
                )

            from projects.rhaiis.postprocess.regression import send_regression_notification

            send_regression_notification(
                analysis,
                model=model_cfg.get("hf_model_id", ""),
                accelerator=accelerator,
                job_id=run_uuid,
                slack_user=slack_user,
                owner=owner,
                notification_vault="psap-forge-notifications",
                report_url=report_url,
                tp=str(tp),
                dp=str(dp),
            )
    except Exception:
        logger.warning("Regression analysis failed; continuing", exc_info=True)
    finally:
        if consolidated_path and os.path.exists(consolidated_path):
            os.unlink(consolidated_path)


def run_agent_analysis(
    analysis: dict,
    model_cfg: dict,
    accelerator: str,
    current_version: str,
    compare_version: str,
    run_uuid: str,
    *,
    severity_threshold: int = 10,
    engine_args: dict | None = None,
) -> str:
    """Request AI agent analysis for severe regressions. Returns report URL or empty string."""
    from projects.core.library import ci, config, vault
    from projects.rhaiis.postprocess.agent import (
        AGENT_SEVERITY_THRESHOLD,
        build_pr_followup_prompt,
        check_agent_connectivity,
        markdown_to_html,
        request_agent_analysis,
        send_followup,
    )

    threshold = severity_threshold or AGENT_SEVERITY_THRESHOLD
    severe = [r for r in analysis.get("regressions", []) if abs(r["pct_diff"]) > threshold]
    if not severe:
        logger.info("No severe regressions (>%d%%), skipping agent analysis", threshold)
        return ""

    endpoint_path = vault.get_vault_content_path("psap-forge-rhaiis-agent-analysis", "agent-url")
    try:
        if endpoint_path is None or not endpoint_path.is_file():
            raise FileNotFoundError("agent endpoint secret content is unavailable")
        agent_url = endpoint_path.read_text(encoding="utf-8").strip()
        if not agent_url:
            raise ValueError("agent endpoint secret content is empty")
    except (OSError, ValueError):
        logger.error("Agent analysis endpoint is missing or unreadable in its configured vault")
        ci.add_notification_file(
            "rhaiis-agent-endpoint-unavailable",
            "RHAIIS agent analysis is enabled, but its endpoint URL could not be read "
            "from the configured secret.",
        )
        return ""

    ok, detail = check_agent_connectivity(agent_url)
    if not ok:
        logger.warning("Agent health check failed, skipping analysis: %s", detail)
        ci.add_notification_file(
            "rhaiis-agent-unreachable",
            "RHAIIS agent analysis was skipped because the configured endpoint "
            "health check failed.",
        )
        return ""

    ea = engine_args or {}
    tp = str(ea.get("tensor-parallel-size") or ea.get("tp-size") or ea.get("tp_size") or 1)
    model = model_cfg.get("hf_model_id", "")
    agent_model = config.project.get_config("rhaiis.agent_analysis.model")
    improvements = analysis.get("improvements", [])

    agent_response = request_agent_analysis(
        model=model,
        agent_model=agent_model,
        accelerator=accelerator,
        current_version=current_version,
        compare_version=compare_version,
        tp=tp,
        severe_regressions=severe,
        job_id=run_uuid,
        improvements=improvements if improvements else None,
        agent_url=agent_url,
    )
    if not agent_response:
        ci.add_notification_file(
            "rhaiis-agent-analysis-empty",
            "RHAIIS agent analysis did not return a usable response; see the job logs.",
        )
        return ""

    pr_prompt = build_pr_followup_prompt(current_version, compare_version)
    pr_analysis = send_followup(
        message=pr_prompt,
        job_id=run_uuid,
        agent_url=agent_url,
        agent_model=agent_model,
    )
    if pr_analysis:
        agent_response = f"{agent_response}\n\n---\n\n## Related Pull Requests\n\n{pr_analysis}"

    html_content = markdown_to_html(
        agent_response,
        run_uuid,
        model,
        current_version,
        compare_version,
    )
    html_path = Path(env.ARTIFACT_DIR) / f"agent_analysis_{run_uuid}.html"
    html_path.write_text(html_content, encoding="utf-8")
    logger.info("Agent analysis saved to %s", html_path)

    try:
        from projects.caliper.cli.s3_export import create_s3_client, get_aws_credentials

        s3_cfg = config.project.get_config("rhaiis.s3", {})
        vault_name = s3_cfg.get("vault", "")
        credentials_file = s3_cfg.get("credentials_file", "aws.credentials")
        credentials_path = get_aws_credentials(vault_name, credentials_file)
        if credentials_path:
            s3 = create_s3_client(credentials_path)
            s3_bucket = s3_cfg.get("bucket", "")
            s3_key = f"reports/rhaiis/{run_uuid}_analysis.html"
            s3.upload_file(
                str(html_path),
                s3_bucket,
                s3_key,
                ExtraArgs={"ContentType": "text/html"},
            )
            report_url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": s3_bucket, "Key": s3_key},
                ExpiresIn=2592000,
            )
            logger.info("Agent analysis uploaded to S3, presigned URL generated")
            return report_url
    except Exception:
        logger.warning("Failed to upload agent analysis to S3; continuing", exc_info=True)

    return ""
