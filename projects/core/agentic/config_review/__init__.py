#!/usr/bin/env python3

"""
Config Review Agent - An agent that analyzes FORGE test configurations

This agent analyzes FORGE test artifact directories and provides synthetic descriptions
of what is being tested, focusing on configuration changes and test parameters. It supports:

- Artifact directory parsing and validation
- Test configuration analysis
- Preset and override detection
- Short test descriptions focused on changes

Example usage:
    from projects.core.agentic.config_review import run_config_review_agent, get_test_analysis

    # Full analysis with metadata
    result = run_config_review_agent(base_artifact_dir="/path/to/artifacts")
    analysis = result['analysis']
    print(f"Test Description: {analysis['test_description']}")
    print(f"Configuration Changes: {analysis['changes_summary']}")

    # Simplified - just structured analysis
    analysis = get_test_analysis("/path/to/artifacts")
    print(f"What is being tested: {analysis['test_description']}")
"""

import logging
import sys
import warnings
from pathlib import Path
from typing import Any, Optional, Union

import yaml

from projects.core.agentic.analysis_utils import extract_structured_analysis
from projects.core.agentic.config_review.queries import (
    ConfigReviewQueries,
    execute_config_query_sequence,
)
from projects.core.agentic.config_review.report import generate_config_html_report
from projects.core.agentic.models import create_llm_client, load_model_config
from projects.core.library import ci as ci_lib
from projects.core.library import vault

# Check for optional agentic dependencies
_AGENTIC_AVAILABLE = True
_MISSING_PACKAGES = []

try:
    import urllib3
except ImportError:
    _AGENTIC_AVAILABLE = False
    _MISSING_PACKAGES.append("urllib3")

try:
    from langchain_core.messages import HumanMessage
except ImportError:
    _AGENTIC_AVAILABLE = False
    _MISSING_PACKAGES.append("langchain_core")


def _is_agentic_dependencies_available():
    """Check if required agentic dependencies are available"""
    if not _AGENTIC_AVAILABLE:
        logger.info(
            f"🤖 Agentic processing disabled - missing packages: {', '.join(_MISSING_PACKAGES)}"
        )
        return False
    return True


MODEL_KEY = "qwen-3-6-35b"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# Suppress HTTP request logging
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("langchain_openai").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def _ensure_vault_initialized():
    """
    Ensure vault is initialized for model access

    Raises:
        RuntimeError: If vault cannot be initialized (e.g., missing environment variables)
    """
    try:
        # Test if vault is already initialized by trying to access it
        vault.get_vault_content_path("psap-models-corp-rh", "agent-models.yaml")
        logger.debug("Vault already initialized and accessible")
    except RuntimeError as e:
        if "not initialized" in str(e):
            logger.info("Initializing vault manager...")
            try:
                vault.init(vaults=["psap-models-corp-rh"])
                logger.info("✅ Vault initialized successfully")
            except RuntimeError as init_error:
                logger.error(f"❌ Vault initialization failed: {init_error}")
                raise RuntimeError(
                    f"Cannot initialize vault (missing environment variables?): {init_error}"
                ) from init_error
        else:
            # Re-raise if it's a different vault error
            raise


def load_artifact_directory_data(base_artifact_dir: str | Path) -> dict[str, Any]:
    """
    Load and parse FORGE test artifact directory data

    Args:
        base_artifact_dir: Path to FORGE test artifact directory

    Returns:
        Dictionary containing parsed artifact data

    Raises:
        FileNotFoundError: If required files don't exist
        yaml.YAMLError: If YAML parsing fails
    """
    base_artifact_dir = Path(base_artifact_dir)

    if not base_artifact_dir.exists():
        raise FileNotFoundError(f"Artifact directory not found: {base_artifact_dir}")

    # Define required files
    required_files = {
        "config": base_artifact_dir / "config.yaml",
        "presets_applied": ci_lib.get_ci_metadata_dir() / "presets_applied.txt",
        "fournos_fjob": ci_lib.get_ci_metadata_dir() / "fournos_fjob.yaml",
    }

    result = {
        "base_artifact_dir": str(base_artifact_dir),
        "config": None,
        "presets_applied": "",
        "execution_engine": None,
        "missing_files": [],
    }

    # Load config.yaml
    try:
        with open(required_files["config"], encoding="utf-8") as f:
            result["config"] = yaml.safe_load(f) or {}
            logger.info("Successfully loaded config.yaml")
    except FileNotFoundError:
        logger.warning(f"config.yaml not found: {required_files['config']}")
        result["missing_files"].append("config.yaml")
    except yaml.YAMLError as e:
        logger.error(f"Failed to parse config.yaml: {e}")
        raise
    except Exception as e:
        logger.error(f"Failed to read config.yaml: {e}")
        raise

    # Load presets_applied.txt
    try:
        with open(required_files["presets_applied"], encoding="utf-8") as f:
            result["presets_applied"] = f.read().strip()
            logger.info("Successfully loaded presets_applied.txt")
    except FileNotFoundError:
        logger.warning(f"presets_applied.txt not found: {required_files['presets_applied']}")
        result["missing_files"].append("presets_applied.txt")
    except Exception as e:
        logger.error(f"Failed to read presets_applied.txt: {e}")
        raise

    # Load fournos_fjob.yaml and extract spec.executionEngine
    try:
        with open(required_files["fournos_fjob"], encoding="utf-8") as f:
            fjob_data = yaml.safe_load(f) or {}

            # Extract spec.executionEngine
            execution_engine = fjob_data.get("spec", {}).get("executionEngine", {})
            if execution_engine:
                result["execution_engine"] = {
                    "project": execution_engine.get("project", ""),
                    "args": execution_engine.get("args", []),
                    "configOverrides": execution_engine.get("configOverrides", []),
                }
                logger.info("Successfully extracted execution engine data")
            else:
                logger.warning("No spec.executionEngine found in fournos_fjob.yaml")
    except FileNotFoundError:
        logger.warning(f"fournos_fjob.yaml not found: {required_files['fournos_fjob']}")
        result["missing_files"].append("fournos_fjob.yaml")
    except yaml.YAMLError as e:
        logger.error(f"Failed to parse fournos_fjob.yaml: {e}")
        raise
    except Exception as e:
        logger.error(f"Failed to read fournos_fjob.yaml: {e}")
        raise

    # Check if we have essential data
    if not result["config"] and not result["execution_engine"]:
        raise FileNotFoundError(
            "No essential configuration data found - both config.yaml and fournos_fjob.yaml are missing or invalid"
        )

    return result


def validate_artifact_structure(artifact_data: dict[str, Any]) -> dict[str, Any]:
    """
    Validate artifact directory structure and extract key information

    Args:
        artifact_data: Parsed artifact directory data

    Returns:
        Validation results and metadata
    """
    validation_result = {
        "is_valid": True,
        "errors": [],
        "warnings": [],
        "structure_info": {
            "has_config": artifact_data.get("config") is not None,
            "has_execution_engine": artifact_data.get("execution_engine") is not None,
            "has_presets_applied": bool(artifact_data.get("presets_applied")),
            "missing_files": artifact_data.get("missing_files", []),
            "project": "",
            "preset_count": 0,
            "config_override_count": 0,
        },
    }

    # Check for missing critical files
    missing_files = artifact_data.get("missing_files", [])
    if missing_files:
        validation_result["warnings"].extend([f"Missing file: {f}" for f in missing_files])

    # Extract execution engine info
    execution_engine = artifact_data.get("execution_engine")
    if execution_engine:
        validation_result["structure_info"]["project"] = execution_engine.get("project", "")
        validation_result["structure_info"]["preset_count"] = len(execution_engine.get("args", []))
        validation_result["structure_info"]["config_override_count"] = len(
            execution_engine.get("configOverrides", [])
        )

    # Validate config structure
    config_data = artifact_data.get("config")
    if config_data:
        if not isinstance(config_data, dict):
            validation_result["errors"].append(
                f"Config expected dict, got {type(config_data).__name__}"
            )
            validation_result["is_valid"] = False
        elif len(config_data) == 0:
            validation_result["warnings"].append("Configuration appears to be empty")

    # Check if we have enough data for analysis
    if not config_data and not execution_engine:
        validation_result["errors"].append(
            "No configuration or execution engine data available for analysis"
        )
        validation_result["is_valid"] = False

    logger.debug(f"Artifact validation: {validation_result}")
    return validation_result


def analyze_test_with_llm(
    artifact_data: dict[str, Any],
    llm,
    base_artifact_dir: Path | None = None,
    verbose: bool = False,
    generate_html: bool = True,
) -> dict[str, Any]:
    """
    Analyze FORGE test using LLM queries

    Args:
        artifact_data: Parsed artifact directory data
        llm: LangChain LLM client
        base_artifact_dir: Optional path to the artifact directory (for context)
        verbose: Whether to show verbose output
        generate_html: Whether to generate HTML report

    Returns:
        Analysis result dictionary
    """
    logger.info("Starting LLM-based test analysis")

    try:
        # Initialize query handler with artifact data
        queries_handler = ConfigReviewQueries(
            artifact_data=artifact_data,
            base_artifact_dir=str(base_artifact_dir) if base_artifact_dir else "unknown",
        )

        # Execute only the first query (test description)
        import time
        import warnings
        from datetime import datetime

        from langchain_core.messages import HumanMessage

        # Get the first query (test description)
        first_query = queries_handler.query_test_description()
        query_content = first_query["content"]

        # Execute query with timing
        start_time = time.time()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
            response = llm.invoke([HumanMessage(content=query_content)])
        end_time = time.time()

        test_description = response.content.strip()
        processing_time = end_time - start_time

        # Save to 000__ci_metadata/notifications/000__TEST_DESCRIPTION.txt
        try:
            if base_artifact_dir:
                notifications_dir = ci_lib.get_ci_metadata_dir() / "notifications"
                notifications_dir.mkdir(parents=True, exist_ok=True)
                test_description_path = notifications_dir / "000__TEST_DESCRIPTION.txt"
            else:
                test_description_path = Path("000__TEST_DESCRIPTION.txt")

            with open(test_description_path, "w", encoding="utf-8") as f:
                f.write(test_description)
            logger.info(f"📝 Test description saved: {test_description_path}")
        except Exception as e:
            logger.warning(f"Failed to save TEST_DESCRIPTION.txt: {e}")

        # Create structured analysis with only test description
        structured_analysis = {
            "test_description": test_description,
            "changes_summary": "",
            "testing_focus": "",
            "configuration_context": "",
            "raw_analysis": f"## Test Description\n{test_description}",
        }

        result = {
            "status": "success",
            "base_artifact_dir": str(base_artifact_dir) if base_artifact_dir else "unknown",
            "analysis": test_description,
            "structured_analysis": structured_analysis,
            "query_count": 1,
            "processing_time": processing_time,
            "test_description_file": str(test_description_path)
            if "test_description_path" in locals()
            else None,
        }

        # Generate HTML report if enabled
        if generate_html or verbose:
            logger.info("📝 Generating HTML report...")
            try:
                # Create queries_and_responses for HTML report
                queries_and_responses = [
                    {
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "query_type": first_query["type"],
                        "query": query_content,
                        "response": test_description,
                        "processing_time": processing_time,
                        "prompt_tokens": 0,  # Token counts not available in simplified version
                        "completion_tokens": 0,
                        "total_tokens": 0,
                        "prompt_length": len(query_content),
                        "response_length": len(test_description),
                    }
                ]

                html_path = generate_config_html_report(
                    queries_and_responses=queries_and_responses,
                    config_path=str(base_artifact_dir / "config.yaml")
                    if base_artifact_dir
                    else "unknown",
                    reference_config_path=None,  # No reference config in this workflow
                    output_dir=base_artifact_dir if base_artifact_dir else Path.cwd(),
                )
                logger.info(f"📄 Config review HTML report saved: {html_path}")
                result["html_report"] = str(html_path)
            except Exception as e:
                logger.warning(f"Failed to generate HTML report: {e}")
        else:
            logger.info(
                "ℹ️  HTML report generation disabled (set verbose=True or generate_html=True to enable)"
            )

        logger.info(f"✅ Config analysis complete: {len(queries_and_responses)} queries executed")
        return result

    except Exception as e:
        logger.error(f"❌ Test analysis failed: {e}")
        return {
            "status": "error",
            "base_artifact_dir": str(base_artifact_dir) if base_artifact_dir else "unknown",
            "error": str(e),
        }


def process_test_review(
    base_artifact_dir: str | Path, llm=None, verbose: bool = False
) -> dict[str, Any]:
    """
    Process complete test review workflow

    Args:
        base_artifact_dir: Path to FORGE test artifact directory
        llm: Configured LangChain LLM client (will create if not provided)
        verbose: Whether to show verbose output

    Returns:
        Dictionary containing review results
    """
    base_artifact_dir = Path(base_artifact_dir)
    logger.info(f"Processing test review for: {base_artifact_dir}")

    try:
        # Load artifact directory data
        artifact_data = load_artifact_directory_data(base_artifact_dir)

        # Validate structure
        validation_result = validate_artifact_structure(artifact_data)
        if not validation_result["is_valid"]:
            return {
                "status": "error",
                "base_artifact_dir": str(base_artifact_dir),
                "error": f"Invalid artifact structure: {validation_result['errors']}",
                "validation_result": validation_result,
            }

        # Create LLM client if not provided
        if llm is None:
            try:
                # Ensure vault is initialized before trying to load model config
                _ensure_vault_initialized()

                logger.info("Loading model configuration from vault...")
                models_config = load_model_config(
                    vault_name="psap-models-corp-rh", content_name="agent-models.yaml"
                )

                model_config = models_config.get(MODEL_KEY)
                if not model_config:
                    raise ValueError(f"Model key '{MODEL_KEY}' not found in the vault")

                logger.info(f"Creating LLM client for model: {model_config.get('model_id')}")
                llm = create_llm_client(model_config)

            except Exception as vault_error:
                logger.error(f"❌ Failed to initialize LLM client: {vault_error}")
                return {
                    "status": "error",
                    "base_artifact_dir": str(base_artifact_dir),
                    "error": f"Cannot create LLM client - vault configuration issue: {vault_error}. "
                    f"Please ensure PSAP_MODELS_CORP_RH environment variable is set, "
                    f"or provide an LLM client directly via the llm parameter.",
                    "validation_result": validation_result,
                }

        # Analyze test with LLM
        analysis_result = analyze_test_with_llm(
            artifact_data=artifact_data,
            llm=llm,
            base_artifact_dir=base_artifact_dir,
            verbose=verbose,
            generate_html=True,
        )

        # Add validation info to result
        analysis_result["validation_result"] = validation_result

        return analysis_result

    except Exception as e:
        logger.error(f"❌ Test review failed: {e}")
        return {"status": "error", "base_artifact_dir": str(base_artifact_dir), "error": str(e)}


def get_test_analysis(base_artifact_dir: str | Path) -> dict[str, Any]:
    """
    Get structured test analysis only

    Args:
        base_artifact_dir: Path to FORGE test artifact directory

    Returns:
        Structured analysis dictionary with test_description, changes_summary, etc.
    """
    result = run_config_review_agent(base_artifact_dir, verbose=False)

    if result.get("status") == "success" and result.get("structured_analysis"):
        analysis = result["structured_analysis"].copy()
        analysis["base_artifact_dir"] = result["base_artifact_dir"]
        analysis["validation_result"] = result.get("validation_result")
        return analysis
    else:
        return {
            "base_artifact_dir": str(base_artifact_dir),
            "error": result.get("error", "Analysis failed"),
            "test_description": "Failed to analyze test configuration",
            "changes_summary": "",
            "testing_focus": "",
            "configuration_context": "",
        }


def trigger_config_review_for_ci(base_artifact_dir: Path, async_mode: bool = False) -> None:
    """
    Trigger config review analysis in CI environments

    This function is designed to be called from CI entrypoints after configuration
    initialization to provide automatic test analysis.

    Args:
        base_artifact_dir: Path to the base artifact directory
        async_mode: If True, run the analysis in a background thread
    """
    from projects.core.library import config

    # Check if agentic dependencies are available
    if not _is_agentic_dependencies_available():
        return

    # Check if agentic features are enabled (master switch)
    agentic_enabled = config.project.get_config("agentic.enabled", False, print=False, warn=False)
    if not agentic_enabled:
        logger.debug("Agentic features disabled (agentic.enabled=false) - skipping config review")
        return

    # Check if config review is specifically enabled
    config_review_enabled = config.project.get_config(
        "agentic.config_review.enabled", False, print=False, warn=False
    )
    if not config_review_enabled:
        logger.debug(
            "Config review disabled (agentic.config_review.enabled=false) - skipping config review"
        )
        return

    def _run_config_review():
        try:
            if not base_artifact_dir or not base_artifact_dir.exists():
                logger.debug(
                    f"Base artifact directory {base_artifact_dir} doesn't exist - skipping config review"
                )
                return

            logger.info("🤖 Triggering config review analysis...")
            result = run_config_review_agent(base_artifact_dir=base_artifact_dir, verbose=False)

            if result.get("status") == "success":
                structured = result.get("structured_analysis", {})
                test_description = structured.get("test_description", "")
                if test_description:
                    logger.info(f"🤖 Config review completed: {test_description}")
                else:
                    logger.info("🤖 Config review completed successfully")
            else:
                error_msg = result.get("error", "unknown error")
                logger.warning(f"🤖 Config review failed: {error_msg}")

        except Exception as e:
            logger.warning(f"🤖 Config review failed with exception: {e}")

    if async_mode:
        import threading

        logger.info("🤖 Starting config review analysis in background...")
        config_review_thread = threading.Thread(target=_run_config_review, daemon=True)
        config_review_thread.start()
    else:
        _run_config_review()


def run_config_review_agent(base_artifact_dir: str | Path, verbose: bool = False) -> dict[str, Any]:
    """
    Main programmatic interface for the Config Review Agent

    This function can be called by other Python modules to trigger test analysis.

    Args:
        base_artifact_dir: Path to FORGE test artifact directory
        verbose: Whether to show verbose output

    Returns:
        Dictionary containing analysis results
    """
    try:
        # Check if agentic dependencies are available
        if not _is_agentic_dependencies_available():
            return {"status": "disabled", "reason": "missing agentic dependencies", "analysis": {}}

        logger.info("🤖 Config Review Agent starting...")

        # Process the test review
        result = process_test_review(base_artifact_dir=base_artifact_dir, verbose=verbose)

        return result

    except Exception as e:
        logger.error(f"❌ Config Review Agent failed: {e}")
        return {"status": "error", "error": str(e)}
