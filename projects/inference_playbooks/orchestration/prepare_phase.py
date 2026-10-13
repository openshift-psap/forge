import logging

logger = logging.getLogger(__name__)


def prepare():
    logger.info("=== Inference-Playbooks Project Prepare Phase ===")


def cleanup():
    logger.info("=== Inference-Playbooks Project Cleanup Phase ===")
