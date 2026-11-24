"""
Prepare phase for MLflow tracing performance tests.

1. Install/configure RHOAI from project configuration and enable MLflow
2. Create the performance-testing namespace
3. Generate trace templates into a PVC (persists between test runs)

Infrastructure (MinIO, PostgreSQL, MLflow server) is deployed fresh
before each test iteration and torn down afterwards — see test_phase.py.
"""

from __future__ import annotations

import logging
from pathlib import Path

from projects.cluster.library.prom.collection import prepare_user_workload_monitoring
from projects.core.dsl.utils.k8s import oc, oc_resource_exists
from projects.mlflow.orchestration.runtime_config import cfg
from projects.mlflow.toolbox.install_platform import main as install_platform_mod
from projects.rhoai.library.deploy import prepare_rhoai_operator
from projects.rhoai.toolbox.apply_datasciencecluster import main as apply_datasciencecluster_mod
from projects.rhoai.toolbox.wait_datasciencecluster_ready import (
    main as wait_datasciencecluster_ready_mod,
)

logger = logging.getLogger(__name__)
RHOAI_ICSP_MANIFEST = Path(__file__).resolve().parent / "manifests" / "rhoai-registry-icsp.yaml"


def _apply_rhoai_icsp() -> None:
    """Apply RHOAI registry mirrors when a custom catalog is selected."""
    oc("apply", "-f", str(RHOAI_ICSP_MANIFEST))


def _prepare_rhoai() -> None:
    platform = cfg.get_platform_config()
    rhoai = platform["rhoai"]
    prepare_rhoai_operator(
        platform=platform,
        rhoai=rhoai,
        icsp_applier=_apply_rhoai_icsp,
    )

    # Reuse an existing DSC without replacing its unrelated component settings.
    # On a clean cluster, create the minimal DSC this project needs.
    dsc_name = rhoai["datasciencecluster_name"]
    if not oc_resource_exists("datasciencecluster", dsc_name):
        apply_datasciencecluster_mod.run(
            datasciencecluster_name=dsc_name,
            namespace=rhoai["namespace"],
            components=rhoai.get("components", ["mlflowoperator"]),
        )
    wait_datasciencecluster_ready_mod.run(
        datasciencecluster_name=dsc_name,
        namespace=rhoai["namespace"],
    )


def run() -> int:
    namespace = cfg.get_namespace()

    logger.info("=== MLflow Prepare Phase ===")
    logger.info("Namespace: %s", namespace)

    prepare_user_workload_monitoring(during="prepare")
    _prepare_rhoai()
    rhoai = cfg.get_platform_config()["rhoai"]
    install_platform_mod.run(
        namespace=namespace,
        datasciencecluster_name=rhoai["datasciencecluster_name"],
        operator_namespace=rhoai["namespace"],
        mlflow_operator_image=rhoai["mlflow_operator_image"],
    )

    logger.info("=== Prepare phase complete ===")
    return 0
