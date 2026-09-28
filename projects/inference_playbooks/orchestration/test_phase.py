import json
import logging
import os
import signal
import uuid

import yaml

from projects.core.dsl.utils import slugify_identifier
from projects.core.dsl.utils.k8s import oc
from projects.core.library import config, env
from projects.core.library.postprocess import (
    create_test_metadata,
    run_and_postprocess,
    update_test_labels_with_status,
    update_test_labels_with_timing,
)
from projects.foreign_testing.library import initialize as foreign_repository
from projects.inference_playbooks.orchestration.recipe_validation import load_recipe_catalog
from projects.kserve.toolbox.deploy_llmisvc import main as deploy_llmisvc

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


def create_custom_test_metadata(recipe_id: str | None = None):
    labels = {
        "project": "inference-playbooks",
        "validation": "recipe-deployment" if recipe_id else "recipe-catalog-validation",
    }
    test_dir = env.ARTIFACT_DIR
    create_test_metadata(
        test_dir,
        labels,
    )

    return test_dir


def do_test():
    logger.info("=== Inference Playbooks Project Test Phase ===")
    recipe_id = config.project.get_config("inference_playbooks.recipe")
    if recipe_id is not None and not isinstance(recipe_id, str):
        raise ValueError("inference_playbooks.recipe must be a recipe ID string")

    with env.NextArtifactDir("inference_playbooks_test_dir"):
        test_dir = create_custom_test_metadata(recipe_id)
        try:
            update_test_labels_with_timing(test_dir, "test", "start")
            repository_path = foreign_repository.initialize()
            recipes = load_recipe_catalog(repository_path)
            logger.info("Validated %d Inference Playbooks recipe manifest(s)", len(recipes))

            if recipe_id is None:
                logger.info("No recipe selected; validated the catalog without launching a service")
            elif recipe_id not in recipes:
                available = ", ".join(sorted(recipes))
                raise ValueError(f"Unknown recipe ID {recipe_id!r}; available recipes: {available}")
            else:
                _launch_recipe(recipe_id, recipes[recipe_id])

        except Exception as e:
            logger.exception("❌ Test failed with exception")
            update_test_labels_with_status(test_dir, False, f"Test failed with exception: {str(e)}")

            raise
        finally:
            update_test_labels_with_timing(test_dir, "test", "end")

    update_test_labels_with_status(test_dir, True, "Test completed successfully")

    return 0


def _launch_recipe(recipe_id: str, recipe: dict) -> None:
    """Deploy one catalogued LLMInferenceService and remove the test instance."""
    if os.environ.get("CLUSTERLESS_MODE", "").lower() == "true" or not os.environ.get("KUBECONFIG"):
        raise RuntimeError(
            "A recipe launch needs a target cluster; rerun the PR test with /cluster <registered-target>"
        )

    manifest = recipe["manifest_data"]
    metadata = manifest["metadata"]
    namespace = config.project.get_config("inference_playbooks.namespace") or metadata.get(
        "namespace"
    )
    if not isinstance(namespace, str) or not namespace:
        raise ValueError(
            "Recipe launch needs a namespace; set /var inference_playbooks.namespace: <namespace>"
        )
    if slugify_identifier(namespace) != namespace:
        raise ValueError(f"Invalid Kubernetes namespace: {namespace!r}")

    model_uri = manifest["spec"]["model"]["uri"]
    model_source = recipe.get("model_source", {})
    if model_uri.startswith("pvc://"):
        pvc_name = model_uri.removeprefix("pvc://").split("/", maxsplit=1)[0]
        result = oc(
            "get", "pvc", pvc_name, "-n", namespace, "-o", "json", check=False, log_stdout=False
        )
        if result.returncode != 0:
            source_uri = model_source.get("source_uri")
            source_note = f" from {source_uri}" if source_uri else ""
            raise RuntimeError(
                f"Recipe model URI {model_uri!r} requires an existing PVC in namespace "
                f"{namespace!r}; Forge will not populate it{source_note}."
            )
        pvc = json.loads(result.stdout)
        if pvc.get("status", {}).get("phase") != "Bound":
            raise RuntimeError(f"Recipe PVC {pvc_name!r} in namespace {namespace!r} is not Bound")
        logger.info(
            "Recipe records model source %s; PVC %s must already contain those weights",
            model_source.get("source_uri", "(not recorded)"),
            pvc_name,
        )

    original_name = metadata["name"]
    if slugify_identifier(original_name) != original_name:
        raise ValueError(f"Recipe has an invalid Kubernetes service name: {original_name!r}")
    service_name = f"{slugify_identifier(original_name, max_length=54)}-{uuid.uuid4().hex[:8]}"
    launch_manifest = {
        **manifest,
        "metadata": {**metadata, "name": service_name, "namespace": namespace},
    }
    manifest_path = env.ARTIFACT_DIR / "src" / f"{service_name}.yaml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(yaml.safe_dump(launch_manifest, sort_keys=False), encoding="utf-8")

    logger.info(
        "Launching Inference Playbooks recipe %s as %s in namespace %s",
        recipe_id,
        service_name,
        namespace,
    )
    try:
        endpoint_url = deploy_llmisvc.run(
            namespace=namespace,
            inference_service_manifest_path=str(manifest_path),
            gateway_status_address_name=None,
            deploy_monitor=False,
        )
        logger.info("Recipe %s reached Ready at %s", recipe_id, endpoint_url)
    finally:
        oc(
            "delete",
            "llminferenceservice",
            service_name,
            "-n",
            namespace,
            "--ignore-not-found=true",
            "--timeout=120s",
        )
        logger.info("Removed test LLMInferenceService %s", service_name)


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
