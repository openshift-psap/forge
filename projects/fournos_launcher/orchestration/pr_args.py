#!/usr/bin/env python3
"""
FOURNOS PR Arguments Parser

Parses PR trigger comment for FOURNOS-specific directives and applies them to configuration.
"""

import importlib
import logging
from pathlib import Path
from typing import Any

import yaml

from projects.core.ci_entrypoint.github.directive_parser import (
    create_help_directive_handler,
    parse_directives_generic,
)
from projects.core.ci_entrypoint.github.pr_args import format_help_text
from projects.core.ci_entrypoint.prepare_ci import CI_METADATA_DIRNAME
from projects.core.library import env
from projects.core.library.config import VARIABLE_OVERRIDES_FILENAME

logger = logging.getLogger(__name__)


def get_supported_fournos_directives() -> dict[str, str]:
    """
    Get a dictionary of supported FOURNOS directives and their descriptions.

    Returns:
        Dictionary mapping directive names to detailed descriptions
    """
    return {
        "/cluster": """Set target cluster for FOURNOS job execution.
                      Format: /cluster cluster_name
                      Example: /cluster psap-mgmt
                      Effect: Sets cluster.name in configuration.""",
        "/exclusive": """Set job exclusivity (whether job runs alone on nodes).
                        Format: /exclusive true|false
                        Example: /exclusive false
                        Effect: Sets fournos.job.exclusive in configuration (default: true).""",
        "/clusterless": """Enable clusterless mode for FOURNOS job execution.
                          Format: /clusterless
                          Example: /clusterless
                          Effect: Sets fournos.job.exclusive=false and fournos.job.clusterless=true.
                          Note: Clusterless and exclusive modes are mutually exclusive.""",
        "/fournos": """Set FOURNOS namespace environment.
                      Format: /fournos environment
                      Example: /fournos wip
                               /fournos staging
                      Effect: Sets fournos.namespace to psap-automation-{environment}.""",
        "/pipeline": """Set FOURNOS pipeline name for job execution.
                       Format: /pipeline pipeline_name
                       Example: /pipeline llm-load-test
                       Effect: Sets fournos.job.pipeline_name in configuration.""",
        "/gpu": """Set GPU hardware requirements for job execution.
                  Format: /gpu gpu_type gpu_count
                  Example: /gpu h100 4
                           /gpu a100 8
                  Effect: Sets fournos.job.hardware.gpu_type and fournos.job.hardware.gpu_count.""",
        "/parallel": """Set parallel job presets for specific indices.
                        Format: /parallel idx preset1 preset2 preset3...
                        Example: /parallel 1 gpu-basic cpu-intensive
                                 /parallel 2 memory-heavy network-test
                        Effect: Sets fournos_launcher.parallel_jobs[idx] = [preset1, preset2, ...]""",
        "/replot.url": """Set URL for downloading artifacts to replot.
                         Format: /replot.url URL
                         Example: /replot.url s3://bucket/path/to/artifacts
                         Effect: Sets caliper.replot.url in configuration.""",
        "/external": """Use the external (psap-mgmt) FOURNOS instance instead of intlab.
                       Format: /external
                       Effect: Sets fournos.instance to psap-mgmt.""",
        "/help": """Show all supported FOURNOS directives.
                   Format: /help
                   Effect: Logs available directive information.""",
    }


def handle_cluster_directive(line: str) -> dict[str, str]:
    """
    Handle /cluster directive for setting cluster override.

    Format: /cluster cluster_name

    Args:
        line: The directive line

    Returns:
        Dictionary with cluster configuration

    Raises:
        ValueError: If cluster_name is empty
    """
    cluster_name = line.removeprefix("/cluster ").strip()

    if not cluster_name:
        raise ValueError(f"Invalid /cluster directive: cluster name cannot be empty in '{line}'")

    return {"cluster.name": cluster_name, "fournos.job.clusterless": False}


def handle_exclusive_directive(line: str) -> dict[str, str]:
    """
    Handle /exclusive directive for setting job exclusivity.

    Format: /exclusive true|false

    Args:
        line: The directive line

    Returns:
        Dictionary with exclusivity configuration

    Raises:
        ValueError: If value is not true or false
    """
    exclusive_value = line.removeprefix("/exclusive ").strip().lower()

    if not exclusive_value:
        raise ValueError(f"Invalid /exclusive directive: value cannot be empty in '{line}'")

    if exclusive_value not in ["true", "false"]:
        raise ValueError(
            f"Invalid /exclusive directive: value must be 'true' or 'false', got '{exclusive_value}' in '{line}'"
        )

    return {"fournos.job.exclusive": exclusive_value == "true"}


def handle_clusterless_directive(line: str) -> dict[str, str]:
    """
    Handle /clusterless directive for enabling clusterless mode.

    Format: /clusterless

    Args:
        line: The directive line

    Returns:
        Dictionary with clusterless configuration (sets exclusive=false, clusterless=true)
    """
    return {"fournos.job.exclusive": False, "fournos.job.clusterless": True}


def handle_fournos_directive(line: str) -> dict[str, str]:
    """
    Handle /fournos directive for setting FOURNOS namespace environment.

    Format: /fournos environment

    Args:
        line: The directive line

    Returns:
        Dictionary with namespace configuration

    Raises:
        ValueError: If environment is not wip or staging
    """
    FOURNOS_NAMESPACE_BASE = "psap-automation"
    VALID_ENVIRONMENTS = {"wip", "staging"}

    environment = line.removeprefix("/fournos ").strip()

    if not environment:
        raise ValueError(f"Invalid /fournos directive: environment cannot be empty in '{line}'")

    if environment not in VALID_ENVIRONMENTS:
        raise ValueError(
            f"Invalid /fournos directive: environment must be one of {VALID_ENVIRONMENTS}, got '{environment}' in '{line}'"
        )

    namespace = f"{FOURNOS_NAMESPACE_BASE}-{environment}"
    return {"fournos.namespace": namespace}


def handle_pipeline_directive(line: str) -> dict[str, str]:
    """
    Handle /pipeline directive for setting pipeline name.

    Format: /pipeline pipeline_name

    Args:
        line: The directive line

    Returns:
        Dictionary with pipeline configuration

    Raises:
        ValueError: If pipeline_name is empty
    """
    pipeline_name = line.removeprefix("/pipeline ").strip()

    if not pipeline_name:
        raise ValueError(f"Invalid /pipeline directive: pipeline name cannot be empty in '{line}'")

    return {"fournos.job.pipeline_name": pipeline_name}


def handle_ttl_directive(line: str) -> dict[str, str]:
    """
    Handle /ttl directive for setting the Fjob TTL name.

    Format: /ttl duration

    Args:
        line: The directive line

    Returns:
        Dictionary with ttl configuration

    Raises:
        ValueError: If ttl_value is empty
    """
    ttl_value = line.removeprefix("/ttl ").strip()

    if not ttl_value:
        raise ValueError(f"Invalid /ttl directive: ttl value cannot be empty in '{line}'")

    return {"fournos.job.ttl": ttl_value}


def handle_gpu_directive(line: str) -> dict[str, str]:
    """
    Handle /gpu directive for setting GPU hardware requirements.

    Format: /gpu gpu_type gpu_count

    Args:
        line: The directive line

    Returns:
        Dictionary with GPU configuration

    Raises:
        ValueError: If format is invalid or values are missing
    """
    gpu_spec = line.removeprefix("/gpu ").strip()

    if not gpu_spec:
        raise ValueError(f"Invalid /gpu directive: GPU specification cannot be empty in '{line}'")

    parts = gpu_spec.split()
    if len(parts) != 2:
        raise ValueError(
            f"Invalid /gpu directive: format must be '/gpu gpu_type gpu_count' in '{line}'"
        )

    gpu_type = parts[0].strip()
    gpu_count_str = parts[1].strip()

    if not gpu_type:
        raise ValueError(f"Invalid /gpu directive: GPU type cannot be empty in '{line}'")

    if not gpu_count_str:
        raise ValueError(f"Invalid /gpu directive: GPU count cannot be empty in '{line}'")

    try:
        gpu_count = int(gpu_count_str)
        if gpu_count <= 0:
            raise ValueError(
                f"Invalid /gpu directive: GPU count must be positive, got {gpu_count} in '{line}'"
            )
    except ValueError as e:
        if "positive" in str(e):
            raise
        raise ValueError(
            f"Invalid /gpu directive: GPU count must be a number, got '{gpu_count_str}' in '{line}'"
        ) from None

    return {"fournos.job.hardware.gpu_type": gpu_type, "fournos.job.hardware.gpu_count": gpu_count}


def handle_external_directive(line: str) -> dict[str, str]:
    """
    Handle /external directive for using the psap-mgmt FOURNOS instance.

    Format: /external

    Args:
        line: The directive line

    Returns:
        Dictionary with instance configuration
    """
    return {"fournos.instance": "psap-mgmt"}


def handle_replot_url_directive(line: str) -> dict[str, str]:
    """
    Handle /replot.url directive for setting replot URL.

    Format: /replot.url URL

    Args:
        line: The directive line

    Returns:
        Dictionary with replot URL configuration

    Raises:
        ValueError: If URL is empty
    """
    replot_url = line.removeprefix("/replot.url").strip()

    if not replot_url:
        raise ValueError(f"Invalid /replot.url directive: URL cannot be empty in '{line}'")

    return {"caliper.replot.url": replot_url}


def handle_help_directive(line: str) -> dict[str, str]:
    """Handle /help directive for FOURNOS directives."""
    # Create help directive handler using the factory
    _help_handler = create_help_directive_handler(
        get_supported_fournos_directives(), "FOURNOS", format_help_text
    )

    result = _help_handler(line)

    # Store help text on this function for pr_config.txt writing
    if hasattr(_help_handler, "_help_text"):
        handle_help_directive._help_text = _help_handler._help_text

    return result


def handle_parallel_directive(line: str) -> dict[str, str]:
    """
    Handle /parallel directive for setting parallel job presets by index.

    Format: /parallel idx preset1 preset2 preset3...

    Args:
        line: The directive line

    Returns:
        Dictionary with parallel_jobs configuration

    Raises:
        ValueError: If format is invalid or values are missing
    """
    parallel_content = line.removeprefix("/parallel ").strip()

    if not parallel_content:
        raise ValueError(f"Invalid /parallel directive: parameters cannot be empty in '{line}'")

    parts = parallel_content.split()
    if len(parts) < 2:
        raise ValueError(
            f"Invalid /parallel directive: format must be '/parallel idx preset1 [preset2...]' in '{line}'"
        )

    idx_str = parts[0].strip()
    presets = [p.strip() for p in parts[1:] if p.strip()]

    if not idx_str:
        raise ValueError(f"Invalid /parallel directive: index cannot be empty in '{line}'")

    if not presets:
        raise ValueError(
            f"Invalid /parallel directive: at least one preset must be specified in '{line}'"
        )

    try:
        idx = int(idx_str)
        if idx < 0:
            raise ValueError(
                f"Invalid /parallel directive: index must be non-negative, got {idx} in '{line}'"
            )
    except ValueError as e:
        if "non-negative" in str(e):
            raise
        raise ValueError(
            f"Invalid /parallel directive: index must be a number, got '{idx_str}' in '{line}'"
        ) from None

    return {f"fournos_launcher.parallel_jobs.{idx}": presets}


def get_fournos_directive_handlers() -> dict[str, callable]:
    """
    Get a mapping of FOURNOS directive prefixes to their handler functions.

    Returns:
        Dictionary mapping directive prefixes to handler functions
    """
    return {
        "/cluster": handle_cluster_directive,
        "/exclusive": handle_exclusive_directive,
        "/clusterless": handle_clusterless_directive,
        "/fournos": handle_fournos_directive,
        "/pipeline": handle_pipeline_directive,
        "/ttl": handle_ttl_directive,
        "/gpu": handle_gpu_directive,
        "/parallel": handle_parallel_directive,
        "/external": handle_external_directive,
        "/replot.url": handle_replot_url_directive,
        "/help": handle_help_directive,
    }


def parse_fournos_directives(
    project: str | None, comment_text: str
) -> tuple[dict[str, str], list[str]]:
    """
    Parse FOURNOS-specific directives from PR trigger comment.

    Supported directives are defined in get_supported_fournos_directives().

    Args:
        project: Project name (e.g., "skeleton", "llm_d") for project-specific parsing
        comment_text: Text from PR trigger comment

    Returns:
        Tuple of (configuration overrides dict, list of parsed directive lines)
    """
    directive_handlers = get_fournos_directive_handlers()

    # Use shared parsing logic
    config_overrides, parsed_directives = parse_directives_generic(
        text=comment_text,
        directive_handlers=directive_handlers,
        system_name="FOURNOS",
        required_directives=None,  # No required directives for FOURNOS
    )

    # Validate directive conflicts
    if config_overrides.get("fournos.job.clusterless") and config_overrides.get(
        "fournos.job.exclusive"
    ):
        raise ValueError(
            "Conflicting directives: /clusterless and /exclusive true cannot both be used"
        )

    if (
        "fournos.namespace" in config_overrides
        and config_overrides.get("fournos.instance") != "psap-mgmt"
    ):
        raise ValueError(
            "/fournos directive requires /external (namespace override only applies to the psap-mgmt instance)"
        )

    # Log successful parses at info level for FOURNOS
    for directive in parsed_directives:
        logger.info(f"Parsed FOURNOS directive: {directive}")

    return config_overrides, parsed_directives


def parse_project_directives(
    project_module: Any, comment_text: str
) -> tuple[dict[str, str], list[str]]:
    """
    Parse project-specific directives using a loaded project module.

    Args:
        project_module: Loaded project pr_args module or None
        comment_text: Text from PR trigger comment

    Returns:
        Tuple of (configuration overrides dict, list of parsed directive lines)
    """
    if not project_module:
        logger.debug("No project module provided for project-specific directive parsing")
        return {}, []

    # Check if the module has parse_project_directives function
    if not hasattr(project_module, "parse_project_directives"):
        raise ImportError(
            f"Module {project_module.__name__} does not have parse_project_directives function"
        )

    # Call the project-specific function
    try:
        return project_module.parse_project_directives(comment_text)
    except Exception as e:
        logger.warning(
            f"Error calling project-specific directive parser for {project_module.__name__}: {e}"
        )
        return {}, []


def lookup_project_name(variable_overrides_path: Path) -> str | None:
    """
    Look up project name from variable_overrides.yaml file.

    Args:
        variable_overrides_path: Path to variable_overrides.yaml file

    Returns:
        Project name if found, None otherwise
    """
    if not variable_overrides_path.exists():
        logger.debug("Variable overrides file does not exist")
        return None

    try:
        with open(variable_overrides_path) as f:
            overrides = yaml.safe_load(f) or {}

        project_name = overrides.get("ci_job.project") or overrides.get("project.name")
        if project_name:
            logger.debug(f"Found project name: {project_name}")
            return project_name
        else:
            logger.debug("No project name found in variable overrides")
            return None

    except Exception as e:
        logger.warning(f"Failed to read variable overrides for project lookup: {e}")
        return None


def lookup_project_pr_parser(variable_overrides_path: Path) -> tuple[str | None, Any]:
    """
    Look up project name and load project-specific pr_args module.

    Args:
        variable_overrides_path: Path to variable_overrides.yaml file

    Returns:
        Tuple of (project_name, project_module) where project_module is None if not found
    """
    project_name = lookup_project_name(variable_overrides_path)

    if not project_name:
        return None, None

    # Try to import project-specific pr_args module
    module_name = f"projects.{project_name}.orchestration.pr_args"
    try:
        project_module = importlib.import_module(module_name)
        logger.debug(f"Successfully loaded project parser: {module_name}")
        return project_name, project_module
    except ImportError:
        logger.debug(f"No project-specific pr_args module found: {module_name}")
        return project_name, None


def _get_project_help_text(project: str | None, project_module: Any) -> str | None:
    """
    Get help text from project-specific module if available.

    Args:
        project: Project name to get help for
        project_module: Loaded project module

    Returns:
        Project help text or None if not available
    """
    if not project or not project_module:
        return None

    # Check if the module has supported directives function
    supported_directives_func_name = f"get_supported_{project}_directives"
    if hasattr(project_module, supported_directives_func_name):
        try:
            from projects.core.ci_entrypoint.github.pr_args import format_help_text

            directives_func = getattr(project_module, supported_directives_func_name)
            directives = directives_func()
            return format_help_text(directives, f"Supported {project.upper()} directives")
        except Exception as e:
            logger.warning(f"Error getting project help for {project}: {e}")
            return None

    return None


def _handle_help_request(project: str | None = None, project_module: Any = None) -> None:
    """
    Handle /help directive by writing help information to pr_config.txt.

    Filters out test command lines but preserves help documentation.

    Args:
        project: Project name for project-specific help
    """
    pr_config_path = env.ARTIFACT_DIR / CI_METADATA_DIRNAME / "pr_config.txt"

    existing_content = ""
    if pr_config_path.exists():
        existing_content = pr_config_path.read_text()

    # Remove actual command lines like "/test fournos skeleton" but keep help docs like "/test"
    filtered_lines = []
    for line in existing_content.splitlines():
        stripped_line = line.strip()
        # Remove lines that look like actual test commands (have arguments after /test)
        if stripped_line.startswith("/test ") and len(stripped_line.split()) > 1:
            continue  # Skip this line

        filtered_lines.append(line)

    # Write back the filtered content plus FOURNOS and project help
    with open(pr_config_path, "w") as f:
        if filtered_lines:
            f.write("\n".join(filtered_lines))
            f.write("\n")

        # Write FOURNOS help
        if hasattr(handle_help_directive, "_help_text"):
            f.write(handle_help_directive._help_text)
        else:
            f.write("Help information not available\n")

        # Write project-specific help
        project_help = _get_project_help_text(project, project_module)
        if project_help:
            f.write("\n")
            f.write(project_help)

    logger.info("Help directive processed - wrote help information to pr_config.txt")


def _update_variable_overrides(config_overrides: dict[str, Any]) -> None:
    """
    Update variable_overrides.yaml with new configuration directives.

    Merges new overrides with existing ones and writes to file.

    Args:
        config_overrides: Dictionary of configuration overrides to apply
    """
    variable_overrides_path = env.ARTIFACT_DIR / VARIABLE_OVERRIDES_FILENAME
    variable_overrides_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing overrides if file exists
    existing_overrides = {}
    if variable_overrides_path.exists():
        try:
            with open(variable_overrides_path) as f:
                existing_overrides = yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning(f"Failed to read existing variable overrides: {e}")

    # Merge new overrides with existing ones
    existing_overrides.update(config_overrides)

    with open(variable_overrides_path, "w") as f:
        yaml.dump(existing_overrides, f, default_flow_style=False, sort_keys=True)

    logger.info(
        f"Written {len(config_overrides)} FOURNOS directive(s) to {variable_overrides_path}"
    )


def _write_directives_to_pr_config(parsed_directives: list[str]) -> None:
    """
    Write parsed directives to pr_config.txt.

    Appends directive text to the file, excluding /help which was already written.

    Args:
        parsed_directives: List of parsed directive strings
    """
    pr_config_path = env.ARTIFACT_DIR / CI_METADATA_DIRNAME / "pr_config.txt"

    # Write non-help directives
    non_help_directives = [d for d in parsed_directives if d != "/help"]
    if not non_help_directives:
        return

    with open(pr_config_path, "a") as f:
        for directive in non_help_directives:
            f.write(f"{directive}\n")

    logger.info(f"Written {len(non_help_directives)} directive line(s) to {pr_config_path}")


def apply_pr_directives() -> str | bool:
    """
    Apply PR trigger comment directives to configuration.

    Reads pr_trigger_comment.txt from CI metadata directory and writes
    any FOURNOS-specific configuration directives to variable_overrides.yaml.

    Returns:
        "help" if /help was requested (caller should exit 0)
        True if directives were found and applied
        False if no directives found
    """
    pr_comment_file = env.ARTIFACT_DIR / CI_METADATA_DIRNAME / "pr_trigger_comment.txt"

    # Guards: check file exists and is readable
    if not pr_comment_file.exists():
        logger.debug("No PR trigger comment file found")
        return False

    try:
        comment_text = pr_comment_file.read_text()
    except Exception as e:
        logger.warning(f"Failed to read PR trigger comment: {e}")
        return False

    # Look up project name and load project parser module
    project, project_module = lookup_project_pr_parser(
        env.ARTIFACT_DIR / VARIABLE_OVERRIDES_FILENAME
    )

    # Parse FOURNOS directives from comment
    fournos_overrides, fournos_directives = parse_fournos_directives(project, comment_text)

    # Parse project-specific directives from comment
    project_overrides, project_directives = parse_project_directives(project_module, comment_text)

    # Merge results - project-specific directives take precedence over FOURNOS ones
    config_overrides = {**fournos_overrides, **project_overrides}
    parsed_directives = fournos_directives + project_directives

    # Handle help request first
    if "/help" in parsed_directives:
        _handle_help_request(project, project_module)
        return "help"

    # Guard: check if we have any configuration directives
    if not config_overrides:
        logger.debug("No FOURNOS directives found in PR trigger comment")
        return False

    # Apply configuration directives
    _update_variable_overrides(config_overrides)
    _write_directives_to_pr_config(parsed_directives)

    # Log applied directives
    for key, value in config_overrides.items():
        logger.info(f"Applied FOURNOS directive: {key} = {value}")

    return True
