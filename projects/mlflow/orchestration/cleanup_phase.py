"""
Cleanup phases for MLflow tracing performance tests.

run() = pre-cleanup (test resources only):
- MLflow CR, secrets, per-project config
- PostgreSQL and MinIO deployments, PVCs

run_platform_cleanup() = post-cleanup (full platform teardown):
- All test resources (same as pre-cleanup)
- Disable MLflow operator
- Delete performance-testing namespace
"""

from __future__ import annotations

import logging

from projects.mlflow.orchestration.runtime_config import cfg
from projects.mlflow.toolbox.cleanup_test_resources import main as cleanup_test_resources_mod

logger = logging.getLogger(__name__)


def run() -> int:
    """Pre-cleanup: remove per-test infrastructure only."""
    logger.info("=== MLflow Pre-Cleanup Phase ===")

    rhoai = cfg.get_platform_config()["rhoai"]
    cleanup_test_resources_mod.run(
        namespace=cfg.get_namespace(), operator_namespace=rhoai["namespace"]
    )

    logger.info("=== Pre-cleanup phase complete ===")
    return 0


def run_platform_cleanup() -> int:
    """Post-cleanup: remove test resources + disable operator + delete namespace."""
    from projects.mlflow.toolbox.cleanup_platform import main as cleanup_platform_mod

    logger.info("=== MLflow Post-Cleanup Phase ===")

    rhoai = cfg.get_platform_config()["rhoai"]
    cleanup_test_resources_mod.run(
        namespace=cfg.get_namespace(), operator_namespace=rhoai["namespace"]
    )
    cleanup_platform_mod.run(
        namespace=cfg.get_namespace(),
        operator_namespace=rhoai["namespace"],
        datasciencecluster_name=rhoai["datasciencecluster_name"],
    )

    logger.info("=== Post-cleanup phase complete ===")
    return 0
