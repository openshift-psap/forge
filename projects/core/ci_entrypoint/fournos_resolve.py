"""
FOURNOS configuration resolver for CI entrypoints.

Provides functionality to resolve FournosJob configurations by populating
spec.secretRefs with vault information from project configuration.
"""

import logging
import os
import sys
from collections.abc import Callable

import click
import yaml

from projects.core.library import ci as ci_lib
from projects.core.library import env, run

logger = logging.getLogger(__name__)


def is_interactive_tty() -> bool:
    """Check if running in an interactive TTY environment."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def fetch_fournos_job() -> tuple[str, str, dict]:
    """
    Fetch and parse a FournosJob object from the cluster using environment variables.

    Returns:
        Tuple of (job_name, namespace, fjob_obj)

    Raises:
        ValueError: If required environment variables are missing
        RuntimeError: If fetch or parsing fails
    """
    # Get environment variables
    job_name = os.environ.get("FJOB_NAME")
    namespace = os.environ.get("FOURNOS_WORKLOAD_NAMESPACE")

    if not job_name:
        raise ValueError("FJOB_NAME environment variable is required")
    if not namespace:
        raise ValueError("FOURNOS_WORKLOAD_NAMESPACE environment variable is required")

    logger.info(f"Fetching FournosJob: {job_name} in namespace: {namespace}")

    # Fetch the FournosJob object
    try:
        result = run.run(
            f"oc get fjob/{job_name} -n {namespace} -o yaml", capture_stdout=True, check=True
        )
        fjob_yaml = result.stdout
    except Exception as e:
        logger.error(f"Failed to fetch FournosJob {job_name}: {e}")
        raise RuntimeError(f"Failed to fetch FournosJob: {e}") from e

    # Parse the YAML
    try:
        fjob_obj = yaml.safe_load(fjob_yaml)
    except yaml.YAMLError as e:
        logger.error(f"Failed to parse FournosJob YAML: {e}")
        raise RuntimeError(f"Failed to parse FournosJob YAML: {e}") from e

    return job_name, namespace, fjob_obj


def update_fournos_job(job_name: str, namespace: str, fjob_obj: dict) -> None:
    """
    Update a FournosJob object in the cluster.

    Args:
        job_name: Name of the FournosJob
        namespace: Namespace where the FournosJob is located
        fjob_obj: Modified FournosJob object to apply

    Raises:
        RuntimeError: If update fails
    """
    logger.info(f"Updating FournosJob: {job_name} in namespace: {namespace}")

    # Apply the updated object back to the cluster
    try:
        # Save resolved YAML to artifact directory
        resolved_file_path = env.ARTIFACT_DIR / "fjob.resolved.yaml"
        with open(resolved_file_path, "w") as resolved_file:
            yaml.dump(fjob_obj, resolved_file)

        try:
            result = run.run(
                f"oc apply -f {resolved_file_path}",
                capture_stdout=True,
                capture_stderr=True,
                check=True,
            )
            logger.info(f"Successfully updated FournosJob {job_name}")
            logger.debug(f"Apply output: {result.stdout}")
            if result.stderr:
                logger.info(f"Apply stderr: {result.stderr}")
            logger.info(f"Resolved FournosJob saved to: {resolved_file_path}")
        except Exception as apply_error:
            logger.error(
                f"oc apply failed with stderr: {getattr(apply_error, 'stderr', 'No stderr available')}"
            )
            raise
    except Exception as e:
        logger.error(f"Failed to apply updated FournosJob: {e}")
        raise RuntimeError(f"Failed to apply updated FournosJob: {e}") from e


def resolve_fournos_config(
    *,
    dry_run: bool = False,
    vaults: list[str],
    hardware_resolver_func: Callable[[dict], dict] | None = None,
) -> int:
    """
    Resolve the FournosJob object configuration by populating spec.secretRefs and spec.hardware.

    Args:
        dry_run: If True, show the updated spec without applying changes to the cluster
        vaults: a list of vault names required for the rest of the testing
        hardware_resolver_func: Optional function that takes spec.hardware dict and returns updated hardware dict

    Returns:
        Exit code (0 for success)

    Raises:
        ValueError: If required environment variables are missing
        RuntimeError: If FournosJob operations fail
    """
    # Check if we're in an interactive TTY environment
    is_tty = is_interactive_tty()

    # Fetch the FournosJob object
    try:
        job_name, namespace, fjob_obj = fetch_fournos_job()

        logger.info(f"Resolving FournosJob: {job_name} in namespace: {namespace}")
    except ValueError as e:
        if not dry_run and not is_tty:
            raise

        if is_tty:
            logger.warning(f"TTY MODE: {e}")
            logger.warning(
                "Running in interactive mode - will show vault configuration without applying to cluster"
            )
        else:
            logger.info("DRY RUN: not using any existing FournosJob")

        fjob_obj = {"spec": {}}

    # Ensure spec exists
    assert "spec" in fjob_obj, "FournosJob must have a spec section"

    # Create secretRefs list with vault names
    fjob_obj["spec"]["secretRefs"] = list(vaults)

    logger.info(f"Vault configuration ({len(vaults)} vault references):")
    for i, vault_name in enumerate(vaults, 1):
        logger.info(f"  {i}. {vault_name}")

    logger.info(f"Updated spec.secretRefs with {len(vaults)} vault references")

    # Apply hardware resolution if function provided
    if hardware_resolver_func:
        try:
            hardware_spec = fjob_obj["spec"].get("hardware", {})
            updated_hardware = hardware_resolver_func(hardware_spec)

            # Set hardware to null if empty or None, otherwise use the resolved hardware
            if updated_hardware and any(updated_hardware.values()):
                fjob_obj["spec"]["hardware"] = updated_hardware
                logger.info("Applied hardware resolution configuration")
            else:
                fjob_obj["spec"]["hardware"] = None
                logger.info("Set hardware to null (no hardware configuration)")
        except Exception as e:
            logger.error(f"Failed to apply hardware resolution: {e}")
            raise RuntimeError(f"Failed to apply hardware resolution: {e}") from e

    # Show the updated spec
    logger.info("Updated FournosJob spec:")
    print("=" * 60)
    print("Updated FournosJob spec:")
    print("=" * 60)
    print(yaml.dump({"spec": fjob_obj["spec"]}, default_flow_style=False, sort_keys=False))
    print("=" * 60)

    if dry_run:
        logger.info("DRY RUN: Not applying changes to cluster")
        return 0

    # Check if we have valid job info for cluster update
    if is_tty and ("job_name" not in locals() or "namespace" not in locals()):
        logger.info("TTY MODE: Not applying changes to cluster (no FournosJob available)")
        return 0

    # Update the FournosJob object
    update_fournos_job(job_name, namespace, fjob_obj)

    return 0


def create_fournos_resolve_entrypoint(
    vault_list_func: Callable[[], list[str]] | None = None,
    vault_list_funcs: list[Callable[[], list[str]]] | None = None,
    hardware_resolver_func: Callable[[dict], dict] | None = None,
):
    """
    Create a FournosJob resolve command with the given vault list and hardware resolver functions.

    Args:
        vault_list_func: Function that returns a list of vault names (deprecated, use vault_list_funcs)
        vault_list_funcs: List of functions that each return a list of vault names
        hardware_resolver_func: Optional function that takes spec.hardware dict and returns updated hardware dict

    Returns:
        Click command for FournosJob resolution
    """
    # Handle backward compatibility
    if vault_list_func is not None and vault_list_funcs is not None:
        raise ValueError("Cannot specify both vault_list_func and vault_list_funcs")

    if vault_list_func is not None:
        # Convert single function to list for backward compatibility
        vault_functions = [vault_list_func]
    elif vault_list_funcs is not None:
        vault_functions = vault_list_funcs
    else:
        raise ValueError("Must specify either vault_list_func or vault_list_funcs")

    @click.command("resolve-fournos-config")
    @click.option(
        "--fjob-name",
        help="FournosJob name (sets FJOB_NAME if provided)",
        envvar="FJOB_NAME",
    )
    @click.option(
        "--namespace",
        help="Namespace for the FournosJob (sets FOURNOS_WORKLOAD_NAMESPACE if provided)",
        envvar="FOURNOS_WORKLOAD_NAMESPACE",
    )
    @click.option(
        "--dry-run",
        is_flag=True,
        help="Show the updated FournosJob spec without applying changes to the cluster",
    )
    @click.pass_context
    @ci_lib.safe_ci_entrypoint
    def fournos_resolve_command(ctx, fjob_name, namespace, dry_run):
        """Resolve the FournosJob object configuration."""

        if fjob_name:
            os.environ["FJOB_NAME"] = fjob_name
        if namespace:
            os.environ["FOURNOS_WORKLOAD_NAMESPACE"] = namespace

        # Get vault lists from all provided functions
        try:
            all_vaults = []
            for i, func in enumerate(vault_functions):
                func_vaults = func()
                logger.info(
                    f"Vault function {i + 1} returned {len(func_vaults)} vaults: {func_vaults}"
                )
                all_vaults.extend(func_vaults)

            # Remove duplicates while preserving order
            vaults = list(dict.fromkeys(all_vaults))
            logger.info(f"Combined vault list ({len(vaults)} unique vaults): {vaults}")

        except Exception as e:
            logger.error(f"Failed to get vault lists: {e}")
            raise RuntimeError(f"Failed to get vault lists: {e}") from e

        return resolve_fournos_config(
            dry_run=dry_run, vaults=vaults, hardware_resolver_func=hardware_resolver_func
        )

    return fournos_resolve_command
