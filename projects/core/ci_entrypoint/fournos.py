#!/usr/bin/env python3
"""
FOURNOS CI Integration Module

This module handles FOURNOS-specific CI operations including environment
variable processing and configuration transformation.
"""

import logging
import os
import shutil
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

FJOB_FORGE_ENGINE_NAME = "forge"


def check_fjob_resolver_error():
    """Fetch the FournosJob from the cluster and fail if a resolver error is set."""

    import json

    from projects.core.library import run

    job_name = os.environ.get("FJOB_NAME")
    namespace = os.environ.get("FOURNOS_WORKLOAD_NAMESPACE")

    if not job_name or not namespace:
        logger.info(
            "FJOB_NAME or FOURNOS_WORKLOAD_NAMESPACE not set, skipping resolver error check"
        )
        return

    logger.info(f"Checking FournosJob resolver status for fjob/{job_name} in {namespace}")

    # Unset KUBECONFIG to use the pod SA access (fjob lives on the Fournos cluster)
    original_kubeconfig = os.environ.get("KUBECONFIG")
    if "KUBECONFIG" in os.environ:
        del os.environ["KUBECONFIG"]

    try:
        try:
            result = run.run(
                f"oc get fjob/{job_name} -n {namespace} -o json",
                capture_stdout=True,
                check=True,
            )
            fjob_data = json.loads(result.stdout)
        except Exception as e:
            logger.warning(f"Could not fetch FournosJob for resolver error check: {e}")
            return

        resolver_error = (
            fjob_data.get("status", {})
            .get("engineStatus", {})
            .get("forge", {})
            .get("resolver", {})
            .get("error")
        )

        resolver_status = (
            fjob_data.get("status", {}).get("engineStatus", {}).get("forge", {}).get("resolver", {})
        )
        resolver_pod = resolver_status.get("pod")
        logs_captured = resolver_status.get("logsCaptured", False)

        if resolver_pod and not logs_captured:
            logger.info(f"Capturing logs from resolver pod: {resolver_pod}")
            _capture_resolver_pod_logs(job_name, namespace, resolver_pod)

        if resolver_error:
            raise RuntimeError(f"FournosJob resolver failed: {resolver_error}")

        logger.info("FournosJob resolver status: OK (no errors)")
    finally:
        if original_kubeconfig is not None:
            os.environ["KUBECONFIG"] = original_kubeconfig


def _capture_resolver_pod_logs(job_name: str, namespace: str, pod_name: str) -> None:
    """Fetch resolver pod logs, save to metadata dir, and mark as captured on the fjob."""

    import json

    from projects.core.library import run

    # Fetch the logs
    try:
        result = run.run(
            f"oc logs {pod_name} -n {namespace}",
            capture_stdout=True,
            check=False,
        )
    except Exception as e:
        logger.warning(f"Could not fetch resolver pod logs: {e}")
        return

    resolver_logs = result.stdout or ""

    # Save to metadata dir
    artifact_dir = os.environ.get("ARTIFACT_DIR")
    if artifact_dir:
        from .prepare_ci import CI_METADATA_DIRNAME

        metadata_dir = Path(artifact_dir) / CI_METADATA_DIRNAME
        metadata_dir.mkdir(parents=True, exist_ok=True)
        log_file = metadata_dir / "resolver_pod.log"
        log_file.write_text(resolver_logs)
        logger.info(f"Saved resolver pod logs to {log_file}")

    # Mark logs as captured on the fjob status
    patch_data = {
        "status": {
            "engineStatus": {
                "forge": {
                    "resolver": {
                        "logsCaptured": True,
                    }
                }
            }
        }
    }
    patch_json = json.dumps(patch_data)
    try:
        run.run(
            f"oc patch fjob/{job_name} -n {namespace} --type=merge --subresource=status -p '{patch_json}'",
            check=True,
        )
        logger.info(f"Marked resolver logs as captured on fjob/{job_name}")
    except Exception as e:
        logger.warning(f"Could not mark resolver logs as captured: {e}")


def process_fjob_environment(fjob_spec):
    """
    Process FOURNOS environment variables from FournosJob YAML.

    Reads environment variables from FournosJob spec.env section and sets them.
    """

    try:
        # Extract environment variables from spec.env
        env_vars = fjob_spec.get("env", {})
        if not env_vars:
            logger.info("No environment variables found in FournosJob spec")
            return

        logger.info(f"Loading {len(env_vars)} environment variables from FournosJob")

        # Update environment variables
        for key, value in env_vars.items():
            os.environ[key] = str(value)
            logger.debug(f"Set environment variable: {key}={value}")

        logger.info(f"Successfully set {len(env_vars)} environment variables from FournosJob")

    except Exception as e:
        logger.exception(f"Failed to process FOURNOS environment: {e}")
        raise


def transform_fournos_config_to_variable_overrides(fjob: dict) -> dict:
    """
    Transform FournosJob format to variable_overrides format.

    Args:
        fjob: Full FournosJob dictionary with metadata and spec sections

    Returns:
        Dictionary in variable_overrides format:
        - metadata.name -> ci_job.fjob_name
        - spec.executionEngine.forge.project -> project.name
        - spec.executionEngine.forge.args -> project.args
        - spec.executionEngine.forge.configOverrides entries are flattened directly
        - spec.exclusive -> ci_job.exclusive
        - spec.hardware -> ci_job.hardware
        - spec.cluster -> ci_job.cluster
        - spec.owner -> ci_job.owner
        - spec.displayName -> ci_job.display_name
    """
    variable_overrides = {}

    # Extract metadata and spec sections
    metadata = fjob.get("metadata", {})
    fjob_spec = fjob.get("spec", {})

    fjob_engine = fjob_spec.get("executionEngine")
    forge_config = fjob_engine.get(FJOB_FORGE_ENGINE_NAME)
    if forge_config:
        # Process forge configuration
        # Transform project -> project.name
        if "project" in forge_config:
            variable_overrides["project.name"] = forge_config["project"]

        # Transform args -> project.args
        if "args" in forge_config:
            variable_overrides["project.args"] = forge_config["args"]

        # Add all configOverrides entries directly (flatten them)
        config_overrides = forge_config.get("configOverrides", {})
        variable_overrides.update(config_overrides)

    else:
        raise ValueError(
            f"Forge received an invalid fjob: spec.executionEngine.{FJOB_FORGE_ENGINE_NAME} not defined. Got {', '.join(fjob_engine.keys())}."
        )

    # Add ci_job mappings from spec
    if "exclusive" in fjob_spec:
        variable_overrides["ci_job.exclusive"] = fjob_spec["exclusive"]

    if "hardware" in fjob_spec:
        variable_overrides["ci_job.hardware"] = fjob_spec["hardware"]

    if "cluster" in fjob_spec:
        variable_overrides["ci_job.cluster"] = fjob_spec["cluster"]

    if "displayName" in fjob_spec:
        variable_overrides["ci_job.display_name"] = fjob_spec["displayName"]

    if "owner" in fjob_spec:
        variable_overrides["ci_job.owner"] = fjob_spec["owner"]

    # Add ci_job mappings from metadata
    if "name" in metadata:
        variable_overrides["ci_job.fjob_name"] = metadata["name"]

    return variable_overrides


def parse_and_save_pr_arguments_fournos():
    """
    Parse GitHub PR arguments for FOURNOS CI environment.

    Reads FournosJob YAML and converts forge spec to variable_overrides.yaml format.

    Returns:
        Parsed variable overrides or None on failure
    """
    artifact_dir = os.environ.get("ARTIFACT_DIR")
    if not artifact_dir:
        logger.warning("ARTIFACT_DIR not set, cannot parse FOURNOS config")
        return None

    artifact_path = Path(artifact_dir)
    from .prepare_ci import CI_METADATA_DIRNAME

    metadata_dir = artifact_path / CI_METADATA_DIRNAME
    # Create CI metadata directory
    metadata_dir.mkdir(parents=True, exist_ok=True)

    # Check for resolver errors before proceeding
    check_fjob_resolver_error()

    # Load FournosJob YAML
    fournos_fjob = metadata_dir.parent / "fournos_fjob.yaml"
    fjob, fjob_spec = load_fjob_yaml(fournos_fjob)

    if not fjob_spec:
        raise ValueError("FournosJob YAML not found, cannot parse FOURNOS PR arguments")

    # Move fournos_fjob to metadata_dir
    fournos_fjob_dest = metadata_dir / "fournos_fjob.yaml"
    shutil.move(str(fournos_fjob), str(fournos_fjob_dest))
    logger.info(f"Moved FournosJob YAML from {fournos_fjob} to {fournos_fjob_dest}")

    try:
        prepare_vault(fjob_spec)
        process_fjob_environment(fjob_spec)
        variable_overrides = process_forge_config(fjob_spec, metadata_dir, fjob)

        logger.info("Successfully parsed FOURNOS configuration")
        return variable_overrides

    except Exception as e:
        logger.exception(f"Failed to parse FOURNOS config: {e}")
        raise


def prepare_vault(fjob_spec) -> None:
    """
    Prepare vault system for FOURNOS CI operations.

    Scans $FOURNOS_SECRETS for vault directories and sets up
    environment variables to match vault definitions.

    Args:
        fjob_spec: The spec section from a FournosJob object

    Raises:
        RuntimeError: If vault preparation fails
    """
    try:
        fournos_secrets = os.environ.get("FOURNOS_SECRETS")
        if not fournos_secrets:
            logger.warning("FOURNOS_SECRETS not set, skipping vault preparation")
            return

        forge_home = Path(
            os.environ.get("FORGE_HOME", Path(__file__).resolve().parent.parent.parent.parent)
        )
        vaults_dir = forge_home / "vaults"

        fournos_secrets_path = Path(fournos_secrets)
        if not fournos_secrets_path.is_dir():
            raise RuntimeError(f"FOURNOS_SECRETS is not a directory: {fournos_secrets_path}")

        # Get required vaults from FournosJob spec.secretRefs
        required_vaults = fjob_spec.get("secretRefs", [])
        if not required_vaults:
            logger.info("No secretRefs found in FournosJob spec, skipping vault preparation")
            return

        logger.info(f"Preparing {len(required_vaults)} vaults from secretRefs: {required_vaults}")

        processed_count = 0
        for vault_name in required_vaults:
            vault_dir = fournos_secrets_path / vault_name

            # Verify vault directory exists
            if not vault_dir.exists() or not vault_dir.is_dir():
                raise RuntimeError(f"Required vault directory not found: {vault_dir}")

            vault_def_file = vaults_dir / f"{vault_name}.yaml"

            if not vault_def_file.exists():
                raise RuntimeError(f"Vault definition file not found: {vault_def_file}")

            try:
                with open(vault_def_file) as f:
                    vault_def = yaml.safe_load(f)
            except Exception as e:
                raise RuntimeError(f"Failed to load vault definition {vault_def_file}: {e}") from e

            env_key = vault_def.get("env_key")
            if not env_key:
                raise RuntimeError(f"Missing env_key in vault definition: {vault_def_file}")

            # Set environment variable
            os.environ[env_key] = str(vault_dir)
            logger.info(f"Set {env_key}={vault_dir}")
            processed_count += 1

        logger.info(f"Processed {processed_count} FOURNOS vaults successfully")

    except Exception as e:
        logger.exception(f"Failed to prepare FOURNOS vault system: {e}")
        raise RuntimeError(f"FOURNOS vault preparation failed: {e}") from e


def process_forge_config(fjob_spec, metadata_dir, fjob):
    """
    Process FOURNOS configuration from FournosJob.

    Handles vault preparation and environment variable setup.

    Args:
        fjob_spec: The spec section from a FournosJob object
        metadata_dir: Path to CI metadata directory
        fjob: The full FournosJob object with metadata and spec
    """
    logger.info("Processing FOURNOS fjob to variable_overrides format")
    logger.debug(f"Full fjob content: {fjob}")

    # Transform full fjob to variable_overrides format
    variable_overrides = transform_fournos_config_to_variable_overrides(fjob)

    output_file = metadata_dir / "variable_overrides.yaml"
    with open(output_file, "w") as f:
        yaml.dump(variable_overrides, f, default_flow_style=False, sort_keys=True)

    logger.info(f"Saved FOURNOS variable overrides to {output_file}")
    logger.info(f"Configuration contains {len(variable_overrides)} override(s)")

    logger.info("Successfully processed FOURNOS configuration")
    return variable_overrides


def load_fjob_yaml(fjob_yaml_path):
    """
    Load FournosJob YAML from file.

    Args:
        fjob_yaml_path: Path to the FournosJob YAML file

    Returns:
        Tuple of (full_fjob, fjob_spec), or (None, None) if not found
    """
    if not fjob_yaml_path.exists():
        logger.warning(f"FournosJob YAML file not found: {fjob_yaml_path}")
        return None, None

    try:
        with open(fjob_yaml_path) as f:
            fjob = yaml.safe_load(f)

        if not fjob:
            logger.warning(f"Empty or invalid FournosJob YAML: {fjob_yaml_path}")
            return None, None

        # Remove status if present
        try:
            del fjob["status"]
        except KeyError:
            pass  # ignore

        logger.info(f"Loaded FournosJob YAML from {fjob_yaml_path}")

        return fjob, fjob["spec"]

    except Exception as e:
        logger.error(f"Failed to load FournosJob YAML from {fjob_yaml_path}: {e}")
        return None, None
