from __future__ import annotations

import logging

from projects.core.dsl.utils.k8s import oc_resource_exists
from projects.core.library import ci as ci_lib
from projects.core.library.ci import ExitCategory
from projects.llm_d.orchestration import runtime_config

logger = logging.getLogger(__name__)


def check_crds() -> bool:
    """Check that required CRDs exist in the cluster.

    Returns:
        True if all CRDs are present, False otherwise
    """
    logger.info("Checking required CRDs")

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
        error_message = f"Missing {len(missing_crds)} required CRDs:\n" + "\n".join(
            f"- {crd}" for crd in missing_crds
        )
        logger.error(f"CRD check failed - {error_message}")
        ci_lib.add_notification_file("MISSING_CRDS", error_message)
        return False

    logger.info("CRD check passed - all required CRDs are available")
    return True


def run() -> int:
    """Run preflight checks before testing phase.

    Validates that required components exist in the cluster based on
    platform configuration settings.

    Returns:
        0 on success, non-zero on failure
    """
    logger.info("Starting preflight checks")

    platform = runtime_config.get_platform_config()
    preflight_config = platform.get("preflight", {})

    # Track overall success across all checks
    all_checks_passed = True

    # Run CRD checks if enabled
    if preflight_config.get("check_crds", True):
        if not check_crds():
            all_checks_passed = False
    else:
        logger.info("CRD checks disabled by platform configuration")

    # Future checks can be added here:
    # if preflight_config.get("check_operators", True):
    #     if not check_operators():
    #         all_checks_passed = False

    if all_checks_passed:
        msg = "Preflight checks completed successfully"
        logger.info(msg)
        return 0, ExitCategory.SUCCESS, msg
    else:
        msg = "One or more preflight checks failed"
        logger.error(msg)
        return 1, ExitCategory.INTERNAL_ERROR, msg
