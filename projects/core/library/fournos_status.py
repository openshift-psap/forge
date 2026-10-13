"""Publish active deployment references on FournosJob status."""

from __future__ import annotations

import json
import logging
import os
import shlex

from projects.core.library import env, run

logger = logging.getLogger(__name__)


def relevant_deployments_status_patch(reference: dict | None) -> dict:
    """Build a narrow merge patch for Forge-owned FournosJob status."""
    deployments = [reference] if reference is not None else None
    return {
        "status": {
            "engineStatus": {
                "forge": {
                    "relevantDeployments": deployments,
                }
            }
        }
    }


def patch_fjob_relevant_deployments(
    job_name: str,
    namespace: str,
    reference: dict | None,
) -> None:
    """Patch only the Forge deployment reference in FournosJob status."""
    if not env.running_inside_fournos():
        return

    if not job_name:
        raise ValueError("FournosJob name is required")

    command = [
        "oc",
        "patch",
        "fournosjob",
        job_name,
        "-n",
        namespace,
        "--type=merge",
        "--subresource=status",
        "-p",
        json.dumps(relevant_deployments_status_patch(reference)),
    ]
    management_env = {key: value for key, value in os.environ.items() if key != "KUBECONFIG"}
    action = "clear" if reference is None else "publish"
    result = run.run(
        shlex.join(command),
        check=False,
        capture_stderr=True,
        timeout=10,
        env=management_env,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Could not {action} deployment reference on FournosJob {job_name} "
            f"(exit={result.returncode}): {(result.stderr or '').strip()[:500]}"
        )

    logger.info("%s deployment reference on FournosJob %s", action.title(), job_name)
