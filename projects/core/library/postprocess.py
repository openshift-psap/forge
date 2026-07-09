"""
Shared Caliper parse / visualize orchestration for FORGE projects.

Registers a :mod:`click` subcommand that reads ``caliper.postprocess`` from project config and runs
:func:`projects.caliper.orchestration.postprocess.run_postprocess_from_orchestration_config`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import click
import yaml
from pydantic import ValidationError

from projects.caliper.orchestration.postprocess import (
    run_postprocess_from_orchestration_config,
)
from projects.caliper.orchestration.postprocess_config import (
    CaliperOrchestrationPostprocessConfig,
)
from projects.caliper.orchestration.postprocess_outcome import TestPhaseOutcome
from projects.core.library import ci as ci_lib
from projects.core.library import config, env
from projects.core.library.dict import get_nested, set_nested
from projects.core.library.reports_index import generate_caliper_reports_index
from projects.core.library.status_to_html import convert_status_yaml_to_html

logger = logging.getLogger(__name__)


def write_test_labels(
    directory: Path, labels: dict[str, str], *, version: str = "1", dump_config: bool = True
) -> Path:
    """Write a __test_labels__.yaml file to mark a directory as a Caliper test base.

    Args:
        directory: Directory to create the test labels file in
        labels: Dictionary of label key-value pairs
        version: Version string for the test labels format (default: "1")
        dump_config: Whether to save project configuration to config.yaml (default: True)

    Returns:
        Path to the created __test_labels__.yaml file

    Example:
        write_test_labels(
            test_dir,
            {
                "model": "llama-3",
                "deployment": "single-zone",
                "rate": "10"
            }
        )
    """
    test_labels_path = directory / "__test_labels__.yaml"
    payload = {
        "version": version,
        "labels": labels,
    }

    # Create directory and write YAML
    test_labels_path.parent.mkdir(parents=True, exist_ok=True)
    with test_labels_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, sort_keys=False)

    # Optionally save project configuration
    if dump_config:
        from projects.core.library import config

        try:
            if config.project is not None:
                config_path = directory / "config.yaml"
                config.project.save_config(dest=config_path)
                logger.info(f"Saved project configuration to {config_path}")
        except Exception as e:
            logger.warning(f"Failed to save project configuration: {e}")

    return test_labels_path


def generate_postprocess_status_report(
    status: dict, output_dir: Path | str, filename: str = "postprocess_status.html"
) -> str:
    """Generate an HTML report from Caliper postprocessing status.

    Args:
        status: Postprocessing status dictionary from orchestration
        output_dir: Directory to write the report
        filename: Name of the HTML file to generate

    Returns:
        Path to the generated HTML report
    """
    output_dir = Path(output_dir)
    output_file = output_dir / filename

    # Write the status as a temporary YAML file
    temp_yaml = output_dir / f"{filename}.temp.yaml"
    temp_yaml.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(temp_yaml, "w", encoding="utf-8") as f:
            yaml.dump(status, f, indent=2, default_flow_style=False)

        # Use the better organized convert_status_yaml_to_html function
        return convert_status_yaml_to_html(temp_yaml, output_file)

    finally:
        # Clean up temp file
        if temp_yaml.exists():
            temp_yaml.unlink()


def run_and_postprocess(test_func, *args, **kwargs):
    """
    Wrapper that runs a test function and handles outcome tracking with Caliper postprocessing.

    This wrapper:
    1. Executes the provided test function with given arguments
    2. Captures test outcomes (success, failure, exception details)
    3. Runs Caliper postprocessing with the test outcome
    4. Returns 1 if postprocessing fails when test succeeds
    5. Properly chains exceptions if both test and postprocess fail

    Args:
        test_func: Callable test function to execute
        *args: Positional arguments to pass to test_func
        **kwargs: Keyword arguments to pass to test_func

    Returns:
        The return value from the test function, or 1 if postprocessing fails

    Raises:
        The original exception from test_func. If both test and postprocess fail,
        exceptions are properly chained. If only postprocessing fails, returns 1.
    """
    artifact_base_dir = Path(env.ARTIFACT_DIR).resolve()

    exc_msg: str | None = None
    ret: int | None = None
    original_exc: BaseException | None = None  # Store the test exception

    try:
        ret = test_func(*args, **kwargs)
        return ret
    except BaseException as e:
        exc_msg = str(e)
        original_exc = e  # Capture before the name is cleared
        raise
    finally:
        # Determine test outcome based on exception/return code
        if exc_msg is not None:
            outcome = TestPhaseOutcome("FAILED", exc_msg)
        elif ret == 0:
            outcome = TestPhaseOutcome("SUCCESS")
        elif ret is None:
            outcome = TestPhaseOutcome("FAILED", "test aborted without exit code")
        else:
            outcome = TestPhaseOutcome("FAILED", f"exit_code={ret}")

        # Run postprocessing and check status for failures
        try:
            status = run_postprocess_after_test(artifact_base_dir, test_outcome=outcome)

            # Check if postprocessing failed
            success = status.get("success", False)
            if not success:
                final_status = status.get("final_status", "unknown")

                # Check if failure is only due to warnings
                if _is_warnings_only_failure(status):
                    if original_exc is not None:
                        # Test failed, postprocess has warnings: still fail due to test
                        logger.error(
                            "Test failed and postprocessing completed with warnings (final_status: %s)",
                            final_status,
                        )
                        raise  # Re-raise the original test exception
                    else:
                        # Test succeeded, postprocess has warnings only: treat as success
                        logger.warning(
                            "Test succeeded and postprocessing completed with warnings (final_status: %s) - returning exit code 0",
                            final_status,
                        )
                        return 0
                else:
                    # Actual postprocessing failures (not just warnings)
                    if original_exc is not None:
                        # Both test and postprocess failed: log both issues
                        logger.error(
                            "Both test and postprocessing failed (final_status: %s)", final_status
                        )
                        raise  # Re-raise the original test exception
                    else:
                        # Only postprocess failed: return failure code
                        logger.error(
                            "Test succeeded but postprocessing failed (final_status: %s) - returning exit code 1",
                            final_status,
                        )
                        return 1

        except Exception as postprocess_exc:
            logger.exception("Caliper postprocess after test failed with exception")
            if original_exc is not None:
                # Both test and postprocess failed: chain so both are visible in the traceback
                raise postprocess_exc from original_exc
            else:
                # Only postprocess failed: return failure code instead of raising
                logger.error(
                    "Test succeeded but postprocessing failed with exception - returning exit code 1"
                )
                return 1


def _is_warnings_only_failure(status: dict) -> bool:
    """Check if postprocessing failure is only due to warnings, not actual errors.

    Args:
        status: Postprocessing result dictionary

    Returns:
        True if all failures are actually warnings, False if there are real failures
    """
    steps = status.get("steps", [])
    if not steps:
        return False

    # Check each step for actual failures vs warnings
    has_any_problematic_steps = False
    has_actual_failures = False

    for step_dict in steps:
        for _step_name, step_data in step_dict.items():
            step_status = step_data.get("status")
            if step_status in ("failed", "warning"):
                has_any_problematic_steps = True

                if step_status == "failed":
                    has_actual_failures = True

    # Only treat as warnings-only if:
    # 1. We have some problematic steps (otherwise why did overall success=False?)
    # 2. None of them are actual failures (all are warnings)
    return has_any_problematic_steps and not has_actual_failures


def run_postprocess_after_test(
    artifact_root: Path | os.PathLike[str] | str | None,
    *,
    test_outcome: TestPhaseOutcome | None = None,
) -> None:
    """
    Run Caliper post-processing after the orchestration test phase.

    Uses ``artifact_root`` (typically :data:`env.ARTIFACT_BASE_DIR`) as the Caliper artifact tree,
    and :func:`env.NextArtifactDir` ``(\"postprocessing\")`` as the workspace for visualize output,
    KPI JSON, and regression artifacts.

    ``test_outcome`` feeds ``final_status`` computation together with parse/visualize/KPI outcomes.
    """
    try:
        postprocess_config_raw = config.project.get_config("caliper.postprocess", print=False) or {}
        postprocess_config = CaliperOrchestrationPostprocessConfig.model_validate(
            postprocess_config_raw
        )
    except ValidationError as e:
        logger.error("Invalid caliper.postprocess config: %s", e)
        raise

    if not postprocess_config.enabled:
        logger.info("Caliper post-processing disabled (caliper.postprocess.enabled: false).")
        return

    artifact_root_path = Path(artifact_root).resolve() if artifact_root is not None else None

    with env.NextArtifactDir("postprocessing"):
        workspace = Path(env.ARTIFACT_DIR).resolve()
        logger.info(
            "Running Caliper postprocess (artifacts=%s, workspace=%s, test_phase=%s)",
            artifact_root_path,
            workspace,
            test_outcome.phase if test_outcome else "SUCCESS",
        )
        status = run_orchestration_postprocess(
            artifact_dir=artifact_root_path,
            visualize_output_dir=workspace,
            test_outcome=test_outcome,
        )
        logger.info(
            "Caliper postprocess finished:\n%s",
            yaml.dump(status, indent=2, default_flow_style=False, sort_keys=False),
        )

    return status


def resolve_caliper_postprocess_artifacts_dir(
    *,
    artifact_dir: Path | None,
    postprocess_config: CaliperOrchestrationPostprocessConfig,
) -> Path:
    """
    Resolve the Caliper **artifact tree** root.

    Precedence: explicit ``artifact_dir``, ``caliper.postprocess.artifacts_dir``
    """
    if artifact_dir is not None:
        return artifact_dir.expanduser().resolve()

    if postprocess_config.artifacts_dir and postprocess_config.artifacts_dir.strip():
        return Path(postprocess_config.artifacts_dir).expanduser().resolve()

    raise ValueError(
        "Caliper postprocess requires the artifact tree root: use --artifact-dir, "
        "set caliper.postprocess.artifacts_dir in project config, or set ARTIFACT_BASE_DIR."
    )


def run_orchestration_postprocess(
    *,
    artifact_dir: Path | None,
    visualize_output_dir: Path | None = None,
    test_outcome: TestPhaseOutcome | None = None,
) -> dict[str, Any]:
    """Load ``caliper.postprocess`` from project config and run enabled post-processing steps."""

    try:
        postprocess_config_raw = config.project.get_config("caliper.postprocess", print=False) or {}
        postprocess_config = CaliperOrchestrationPostprocessConfig.model_validate(
            postprocess_config_raw
        )
    except ValidationError as e:
        logger.error("Invalid caliper.postprocess config: %s", e)
        raise

    artifacts_dir = resolve_caliper_postprocess_artifacts_dir(
        artifact_dir=artifact_dir,
        postprocess_config=postprocess_config,
    )

    # Resolve visualize_config path from FORGE_HOME if it's relative
    visualize_config_path = get_nested(postprocess_config_raw, "visualize.visualize_config")
    if visualize_config_path:
        config_path = Path(visualize_config_path)
        if not config_path.is_absolute():
            resolved_path = env.FORGE_HOME / config_path
            set_nested(postprocess_config_raw, "visualize.visualize_config", str(resolved_path))
            logger.info(
                "Resolved visualize_config path from %s to %s", visualize_config_path, resolved_path
            )

    result = run_postprocess_from_orchestration_config(
        postprocess_config_raw,
        artifacts_dir=artifacts_dir,
        visualize_output_dir=visualize_output_dir,
        test_outcome=test_outcome,
    )

    status_base = visualize_output_dir
    if status_base is None:
        return result

    status_path = Path(status_base) / "postprocess_status.yaml"
    try:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(
            yaml.dump(result, indent=2, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        logger.info("Wrote postprocess status YAML to %s", status_path)
    except OSError as e:
        logger.warning("Could not write %s: %s", status_path, e)

    # Generate HTML reports
    try:
        generate_caliper_reports_index(result, Path(status_base), "reports_index.html")
    except Exception as e:
        logger.warning("Failed to generate reports index: %s", e)

    try:
        generate_postprocess_status_report(result, Path(status_base), "postprocess_status.html")
    except Exception as e:
        logger.warning("Failed to generate postprocessing status report: %s", e)

    return result


@click.command("postprocess")
@click.option(
    "--artifact-dir",
    "artifact_dir",
    type=click.Path(path_type=Path, exists=True, file_okay=False, dir_okay=True),
    required=True,
    help=(
        "Caliper artifact tree root (directories with __test_labels__.yaml). "
        "Required parameter for post-processing."
    ),
)
@click.option(
    "--output-dir",
    "output_dir",
    type=click.Path(path_type=Path, exists=False, file_okay=False, dir_okay=True),
    required=True,
    help=(
        "Output directory, where the post processing results will be stored. "
        "Required parameter for post-processing."
    ),
)
@click.pass_context
@ci_lib.safe_ci_entrypoint
def postprocess_command(_ctx, artifact_dir: Path, output_dir: Path):
    """Run the post-processing pipeline."""

    status = run_orchestration_postprocess(
        artifact_dir=artifact_dir,
        test_outcome=TestPhaseOutcome("NOT_AVAILABLE"),
        visualize_output_dir=output_dir,
    )
    logger.info("Caliper postprocess status:\n" + yaml.dump(status, indent=2))

    # Check success flag and return appropriate exit code
    success = status.get("success", False)
    if not success:
        logger.error(
            "Postprocessing failed (final_status: %s) - returning exit code 1",
            status.get("final_status", "unknown"),
        )
        return 1

    return 0
