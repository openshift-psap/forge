import logging
import signal

from projects.core.library import env
from projects.core.library.postprocess import (
    create_test_metadata,
    run_and_postprocess,
    update_test_labels_with_status,
    update_test_labels_with_timing,
)
from projects.foreign_testing.library import initialize as foreign_repository
from projects.inference_playbooks.orchestration.recipe_validation import validate_recipe_catalog

logger = logging.getLogger(__name__)


def _signal_handler_sigint(sig, frame):
    """Handle SIGINT for the Inference Playbooks project."""
    env.reset_artifact_dir()
    # Sample handler - does nothing


def _signal_handler_sigterm(sig, frame):
    """Handle SIGTERM for the Inference Playbooks project."""
    env.reset_artifact_dir()
    # Sample handler - does nothing


def _setup_sample_signal_handlers():
    """Set up sample signal handlers for demonstration."""
    try:
        signal.signal(signal.SIGINT, _signal_handler_sigint)
        signal.signal(signal.SIGTERM, _signal_handler_sigterm)
        logger.debug("Sample signal handlers installed")
    except Exception as e:
        logger.warning(f"Failed to set up sample signal handlers: {e}")


def test():
    """Main test function that wraps do_test() with outcome postprocessing."""
    return run_and_postprocess(do_test)


def create_custom_test_metadata():
    labels = {
        "project": "inference-playbooks",
        "validation": "clusterless-recipe-yaml",
    }
    test_dir = env.ARTIFACT_DIR
    create_test_metadata(
        test_dir,
        labels,
    )

    return test_dir


def do_test():
    logger.info("=== Inference Playbooks Project Test Phase ===")
    with env.NextArtifactDir("inference_playbooks_test_dir"):
        test_dir = create_custom_test_metadata()
        try:
            update_test_labels_with_timing(test_dir, "test", "start")
            repository_path = foreign_repository.initialize()
            recipe_count = validate_recipe_catalog(repository_path)
            logger.info("Validated %d Inference Playbooks recipe manifest(s)", recipe_count)

        except Exception as e:
            logger.exception("❌ Test failed with exception")
            update_test_labels_with_status(test_dir, False, f"Test failed with exception: {str(e)}")

            raise
        finally:
            update_test_labels_with_timing(test_dir, "test", "end")

    update_test_labels_with_status(test_dir, True, "Test completed successfully")

    return 0


def fournos_resolve_hardware_request(hardware_spec: dict):
    """
    Resolve hardware requirements for FournosJob based on Inference Playbooks configuration.

    This is a stub implementation. Update spec.hardware based on project configuration.

    Args:
        hardware_spec: The current spec.hardware dict from the FournosJob. This object should be updated.

    """
    logger.info("Hardware resolution: stub implementation - no changes made")

    # Stub implementation - could be extended to:
    # - Read hardware config from project configuration
    # - Set hardware requirements based on workload needs
    # - Handle different hardware profiles (GPU, CPU, memory requirements)
    # - Example: return {"gpu": {"type": "nvidia-tesla-v100", "count": 1}, "memory": "32Gi"}

    return hardware_spec
