#!/usr/bin/env python3
"""
FOURNOS job management utilities

Functions for labeling, tracking, and shutting down FournosJobs
"""

import logging
import os

from projects.core.library import config

logger = logging.getLogger(__name__)


def generate_ci_job_label():
    """
    Generate a unique label for CI job based on PR and build information.

    Returns a label in format: pr{PULL_NUMBER}_b{BUILD_ID}
    If environment variables are not available, returns None.
    """
    pull_number = os.environ.get("PULL_NUMBER")
    build_id = os.environ.get("BUILD_ID")

    if pull_number and build_id:
        return f"pr{pull_number}_b{build_id}"
    elif pull_number:
        # Fallback to just PR number if BUILD_ID is not available
        return f"pr{pull_number}"

    logger.warning("Unable to generate CI job label - PULL_NUMBER or BUILD_ID not available")
    return None


def shutdown_running_fjobs():
    """
    Gracefully shutdown all running FournosJobs that match the current CI run.

    This function uses the shutdown_fjobs toolbox to find and shutdown jobs.
    """
    ci_label = generate_ci_job_label()
    if not ci_label:
        logger.warning("Cannot shutdown fjobs - no CI label available")
        return

    # Get namespace from config, fallback to default
    namespace = config.project.get_config("fournos.namespace")

    # Get shutdown value from config
    shutdown_value = config.project.get_config("fournos.on_abort.shutdown_value")

    logger.info(f"Shutting down FournosJobs with CI label '{ci_label}' in namespace '{namespace}'")

    try:
        # Use the shutdown_fjobs toolbox for the actual shutdown work
        from projects.fournos_launcher.toolbox.shutdown_fjobs.main import run as shutdown_toolbox

        result = shutdown_toolbox(
            namespace=namespace,
            ci_label=ci_label,
            job_name=None,
            all_jobs=False,
            shutdown_value=shutdown_value,
        )

        logger.info("Shutdown operation completed via toolbox")
        return result

    except Exception as e:
        logger.error(f"Exception while shutting down FournosJobs: {e}")
        raise


def shutdown_fjobs_on_interrupt():
    """
    Entry point for shutting down FournosJobs when the process is interrupted.
    This function should be called by signal handlers.
    """
    try:
        logger.info("🚫 Process interrupted - shutting down running FournosJobs...")

        interrupt_on_push = config.project.get_config("fournos.job.interrupt_on_push", True)
        if not interrupt_on_push:
            logger.info("ℹ️ fournos.job.interrupt_on_push is disabled - skipping job shutdown")
            return

        shutdown_running_fjobs()
    except Exception as e:
        logger.error(f"Failed to shutdown FournosJobs: {e}")
        raise
