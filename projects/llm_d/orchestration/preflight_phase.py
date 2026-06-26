from __future__ import annotations

import logging

from projects.core.dsl.utils.k8s import oc_resource_exists
from projects.llm_d.orchestration import runtime_config

logger = logging.getLogger(__name__)


def run() -> int:
    """Run preflight checks before testing phase.

    Validates that required CRDs exist in the cluster:
    - leaderworkersets.leaderworkerset.x-k8s.io
    - llminferenceservices.serving.kserve.io

    Returns:
        0 on success, non-zero on failure
    """
    logger.info("Starting preflight checks")

    # Get required CRDs from platform configuration
    platform = runtime_config.get_platform_config()
    rhoai = platform["rhoai"]
    required_crds = rhoai["required_crds_after_dsc"]

    missing_crds = []

    for crd_name in required_crds:
        logger.info(f"Checking for CRD: {crd_name}")
        if not oc_resource_exists("crd", crd_name):
            missing_crds.append(crd_name)
            logger.error(f"Required CRD not found: {crd_name}")
        else:
            logger.info(f"CRD found: {crd_name}")

    if missing_crds:
        logger.error(
            f"Preflight check failed - missing {len(missing_crds)} required CRDs: {', '.join(missing_crds)}"
        )
        return 1

    logger.info("Preflight checks completed successfully - all required CRDs are available")
    return 0
