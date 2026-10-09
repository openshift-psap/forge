from __future__ import annotations

import logging

from projects.core.library import config
from projects.core.orchestration.utils.k8s import ensure_namespace

logger = logging.getLogger(__name__)


def get_model_name() -> str:
    return config.project.get_config("runtime.model_name")


def get_namespace() -> str:
    return config.project.get_config("runtime.namespace")


def prepare():
    logger.info("=== Minimal Project Prepare Phase ===")

    namespace = get_namespace()
    ensure_namespace(
        namespace,
        labels={
            "app.kubernetes.io/managed-by": "forge",
            "forge.openshift.io/project": "minimal",
        },
    )


def cleanup():
    logger.info("=== Minimal Project Cleanup Phase ===")
