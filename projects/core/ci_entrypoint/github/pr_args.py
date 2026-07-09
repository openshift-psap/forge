#!/usr/bin/env python3
"""
GitHub PR Arguments Parser

Fetches GitHub PR comments, finds the last comment from the PR author or COLLABORATOR,
and extracts test configuration from special directives.

Converts the bash script pr_args.sh to Python with enhanced error handling and structure.
"""

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from .directive_parser import create_help_directive_handler, parse_directives_generic

# Avoid circular import - define locally
CI_METADATA_DIRNAME = "000__ci_metadata"

logger = logging.getLogger(__name__)

REQUIRED_AUTHOR_ASSOCIATION = "COLLABORATOR"

DEFAULT_REPO_OWNER = "openshift-psap"
DEFAULT_REPO_NAME = "forge"


def get_directive_handlers() -> dict[str, Callable[[str], dict[str, Any]]]:
    """
    Get a mapping of directive prefixes to their handler functions.

    Returns:
        Dictionary mapping directive prefixes to handler functions
    """

    return {
        "/test": handle_test_directive,
        "/var": handle_var_directive,
        "/help": handle_help_directive,
    }


def handle_test_directive(line: str) -> dict[str, Any]:
    """
    Handle /test directive for test commands and generate PR positional arguments.

    Format: /test test_name project arg1 arg2

    Args:
        line: The directive line

    Returns:
        Dictionary with test information and PR positional arguments
    """
    # Extract test name and arguments
    parts = line[6:].strip().split()
    if not parts:
        raise ValueError("Found an empty /test directive")

    test_name = parts.pop(0)

    if parts:
        project_name = parts.pop(0)
    else:
        project_name = "project_not_set"

    args = parts  # allowed to be empty
    result = {}
    # Special handling for jump CI - extract cluster and target project info
    if test_name.endswith("jump-ci"):
        # Format: /test jump-ci target_project [additional_args...]
        target_project = project_name

        result.update(
            {
                "project.name": target_project,
                "project.args": args,
            }
        )

        logger.info(f"Jump CI configuration: target_project={target_project}, args={args}")
    else:
        # Build result with test info and PR positional arguments
        result.update(
            {
                "ci_job.name": test_name,
                "ci_job.project": project_name,
                "ci_job.args": args,
            }
        )

    return result


def handle_var_directive(line: str) -> dict[str, Any]:
    """
    Handle /var directive for setting variables.

    Format: /var key: value

    Args:
        line: The directive line

    Returns:
        Dictionary with parsed variables

    Raises:
        Exception: If the directive format is invalid
    """
    var_content = line[5:].strip()

    try:
        parsed = yaml.safe_load(var_content)
    except yaml.YAMLError as e:
        raise Exception(f"Invalid /var directive: {line} - {e}") from e

    if not isinstance(parsed, dict) or len(parsed) != 1:
        raise Exception(f"Invalid /var directive format: {line} (expected 'key: value')")

    key, value = next(iter(parsed.items()))
    if not isinstance(key, str) or not key.strip():
        raise Exception(f"Invalid /var directive format: {line} (expected a non-empty key)")

    return {key.strip(): value}


def format_help_text(directives_dict: dict[str, str], title: str) -> str:
    """
    Format directives help text with semantic line breaks and proper formatting.

    Args:
        directives_dict: Dictionary of directives and their descriptions
        title: Title for the help section (e.g., "Supported GitHub PR directives")

    Returns:
        Formatted help text string
    """
    help_text = f"## {title}:\n"

    for directive, description in directives_dict.items():
        # Skip /help directive since it's redundant in help output
        if directive == "/help":
            continue

        # Preserve original newlines but normalize whitespace within lines
        help_text += f"\n{directive}\n"

        # Split on semantic breaks: Format:, Effect:, Example:, Note:
        semantic_breaks = ["Format:", "Effect:", "Example:", "Note:"]

        # Find positions of semantic breaks while preserving newlines
        parts = [description.strip()]
        for break_word in semantic_breaks:
            new_parts = []
            for part in parts:
                if break_word in part:
                    split_parts = part.split(break_word)
                    if len(split_parts) > 1:
                        new_parts.append(split_parts[0].strip())
                        for sp in split_parts[1:]:
                            new_parts.append(f"{break_word} {sp.strip()}")
                    else:
                        new_parts.append(part)
                else:
                    new_parts.append(part)
            parts = new_parts

        # Format each part with proper indentation, preserving intentional newlines
        for part in parts:
            part = part.strip()
            if not part:
                continue

            # Handle each line within the part separately to preserve newlines
            lines = part.split("\n")
            for line in lines:
                line = line.strip()
                if not line:
                    help_text += "\n"  # Preserve empty lines
                    continue

                # Normalize whitespace within the line
                clean_line = " ".join(line.split())

                # Split long lines at 80 characters
                if len(clean_line) > 80:
                    words = clean_line.split()
                    current_line = ""
                    for word in words:
                        if len(current_line + " " + word) > 80 and current_line:
                            help_text += f"  {current_line}\n"
                            current_line = word
                        else:
                            current_line = current_line + " " + word if current_line else word
                    if current_line:
                        help_text += f"  {current_line}\n"
                else:
                    help_text += f"  {clean_line}\n"

    return help_text


def handle_help_directive(line: str) -> dict[str, Any]:
    """Handle /help directive for GitHub PR directives."""
    # Create help directive handler using the factory
    _help_handler = create_help_directive_handler(
        get_supported_directives(), "GitHub PR", format_help_text
    )

    _help_handler(line)  # Process and store help text
    # Return help text in format expected by GitHub system
    if hasattr(_help_handler, "_help_text"):
        return {"__help_output__": _help_handler._help_text}
    return {}


def get_directive_prefixes() -> list[str]:
    """
    Get a list of supported directive prefixes.

    Returns:
        List of directive prefixes (e.g., ['/var', '/skip', '/only', ...])
    """
    return list(get_directive_handlers().keys())


def get_supported_directives() -> dict[str, str]:
    """
    Get a dictionary of supported directives and their comprehensive descriptions.

    Returns:
        Dictionary mapping directive names to detailed descriptions
    """
    return {
        "/test": """Execute a test command with optional arguments.

                    Format: /test fournos project_name [arg1] [arg2] ...
                    Example: /test fournos llm_d preset1 preset2
                             /test forunos skeleton preset1 preset2
                    Note: This is the primary directive for triggering CI test runs.""",
        "/var": """Set configuration variables in YAML format.
                   Format: /var key: value
                   Example: /var tests.llmd.inference_service.model: facebook-opt-125m
                            /var tests.llmd.flavors: [simple, simple-tp4, simple-tp2-x4]
                            /var tests.llmd.benchmarks.guidellm.rate: "1,10,50".
                   """,
        "/help": """Show all supported GitHub PR directives.
                   Format: /help
                   Effect: Logs available directive information to help users understand available options.""",
    }


def parse_directives(
    text: str, artifact_path: Path | None = None, last_comment: str | None = None
) -> tuple[dict[str, Any], list[str]]:
    """
    Parse all directives from the given text using handler mapping.

    Supported directives are defined in get_directive_handlers().
    See get_supported_directives() for format documentation.

    Args:
        text: Text containing directives (PR body + comments)
        artifact_path: Artifact directory path for saving last comment
        last_comment: Last comment text to save

    Returns:
        Tuple of (configuration dictionary, list of found directive lines)

    Raises:
        Exception: If any directive has invalid format
    """
    # Save last comment if provided
    if artifact_path and last_comment:
        metadata_dir = artifact_path / CI_METADATA_DIRNAME
        metadata_dir.mkdir(parents=True, exist_ok=True)
        comment_file = metadata_dir / "pr_trigger_comment.txt"
        with open(comment_file, "w") as f:
            f.write(last_comment)
        logger.info(f"Saved last comment to {comment_file}")

    directive_handlers = get_directive_handlers()

    # Use shared parsing logic
    config, found_directives = parse_directives_generic(
        text=text,
        directive_handlers=directive_handlers,
        system_name="GitHub PR",
        required_directives=["/test"],
    )

    return config, found_directives


def fetch_url(url: str, cache_file: Path | None = None) -> dict[str, Any]:
    """
    Fetch JSON data from URL with optional caching.

    Args:
        url: URL to fetch
        cache_file: Optional file path to cache the response

    Returns:
        JSON data as dictionary

    Raises:
        Exception: If HTTP request fails
    """

    # Check cache first
    if cache_file and cache_file.exists():
        logger.info(f"Using cached file: {cache_file}")
        with open(cache_file) as f:
            return json.load(f)

    # Fetch from URL
    logger.info(f"Fetching from URL: {url}")
    try:
        with urllib.request.urlopen(url) as response:
            data = json.load(response)

        # Save to cache if specified
        if cache_file:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_file, "w") as f:
                json.dump(data, f, indent=2)

        return data

    except urllib.error.URLError as e:
        raise RuntimeError(f"Failed to fetch {url}: {e}") from e


def parse_pr_arguments(
    repo_owner: str,
    repo_name: str,
    pull_number: int,
    test_name: str | None = None,
    artifact_path: Path | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """
    Parse GitHub PR arguments and configuration from comments.

    Args:
        repo_owner: GitHub repository owner
        repo_name: GitHub repository name
        pull_number: Pull request number
        test_name: Test name to search for (if not provided, derived from environment)
        artifact_path: Artifact directory path for saving last comment

    Returns:
        Tuple of (configuration dictionary with parsed arguments and directives, list of found directive lines)

    Raises:
        Exception: If required data cannot be found or parsed
    """
    # Determine test name
    if not test_name:
        if os.environ.get("OPENSHIFT_CI") == "true":
            job_name = os.environ.get("JOB_NAME", "")
            job_name_prefix = f"pull-ci-{repo_owner}-{repo_name}-main"
            test_name = job_name.replace(f"{job_name_prefix}-", "")
            if not test_name:
                raise Exception(f"Could not derive test name from JOB_NAME: {job_name}")
        else:
            test_name = os.environ.get("TEST_NAME")
            if not test_name:
                raise Exception("TEST_NAME not defined and not in OpenShift CI")

    # Build URLs
    pr_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/pulls/{pull_number}"
    pr_comments_url = (
        f"https://api.github.com/repos/{repo_owner}/{repo_name}/issues/{pull_number}/comments"
    )

    logger.info(f"# PR URL: {pr_url}")
    logger.info(f"# PR comments URL: {pr_comments_url}")

    # Fetch PR data
    pr_data = fetch_url(pr_url)

    # Calculate last comment page
    pr_comments_count = pr_data.get("comments", 0)
    comments_per_page = 30  # GitHub default
    last_comment_page = (pr_comments_count // comments_per_page) + (
        1 if pr_comments_count % comments_per_page else 0
    )
    if last_comment_page == 0:
        last_comment_page = 1

    # Fetch last comment page
    last_comment_page_url = f"{pr_comments_url}?page={last_comment_page}"
    last_comment_page_data = fetch_url(last_comment_page_url)

    # Find the last relevant comment
    pr_author = pr_data["user"]["login"]

    test_anchor = f"/test {test_name}"

    logger.info(
        f"# Looking for comments from author '{pr_author}' or '{REQUIRED_AUTHOR_ASSOCIATION}' containing '{test_anchor}'"
    )

    # Search comments in reverse order (most recent first)
    last_user_test_comment = None
    for comment in reversed(last_comment_page_data):
        author_login = comment.get("user", {}).get("login", "")
        author_association = comment.get("author_association", "")
        comment_body = comment.get("body", "")

        # Check if this is from the PR author or a contributor
        if author_login == pr_author or author_association == REQUIRED_AUTHOR_ASSOCIATION:
            if test_anchor in comment_body:
                last_user_test_comment = comment_body
                break

    if not last_user_test_comment:
        raise ValueError(
            f"No comment found from '{pr_author}' or '{REQUIRED_AUTHOR_ASSOCIATION}' containing '{test_anchor}'"
        )

    # Parse all directives from PR body and last comment
    combined_text = (pr_data.get("body", "") or "") + "\n" + last_user_test_comment

    # Parse directives using the modular parser
    config, found_directives = parse_directives(
        combined_text, artifact_path, last_user_test_comment
    )

    return config, found_directives


def main():
    """
    Main function for testing the PR arguments parser.

    Reads environment variables and prints the parsed configuration to stdout.
    Special argument '--help-directives' shows supported directives.
    """
    # Handle special help argument
    if len(sys.argv) > 1 and sys.argv[1] == "--help-directives":
        print("Forge CI PR directives:")
        for directive, description in get_supported_directives().items():
            print(f"  {directive}: {description}")
        print(f"\nSupported prefixes: {', '.join(get_directive_prefixes())}")
        return

    try:
        # Get required environment variables
        repo_owner = os.environ.get("REPO_OWNER") or DEFAULT_REPO_OWNER
        repo_name = os.environ.get("REPO_NAME") or DEFAULT_REPO_NAME

        pull_number_str = os.environ.get("PULL_NUMBER") or 1

        if not repo_owner:
            logger.error("REPO_OWNER environment variable not defined")
            sys.exit(1)

        if not repo_name:
            logger.error("REPO_NAME environment variable not defined")
            sys.exit(1)

        if not pull_number_str:
            logger.error("PULL_NUMBER environment variable not defined")
            sys.exit(1)

        try:
            pull_number = int(pull_number_str)
        except ValueError:
            logger.error(f"PULL_NUMBER must be an integer, got: {pull_number_str}")
            sys.exit(1)

        # Optional parameters
        test_name = os.environ.get("TEST_NAME") or "jump-ci"
        artifact_dir_str = os.environ.get("ARTIFACT_DIR")
        artifact_path = Path(artifact_dir_str) if artifact_dir_str else None

        # Parse PR arguments
        config, found_directives = parse_pr_arguments(
            repo_owner=repo_owner,
            repo_name=repo_name,
            pull_number=pull_number,
            test_name=test_name,
            artifact_path=artifact_path,
        )

        # Output configuration in YAML-like format (matching original script)
        for key, value in config.items():
            if isinstance(value, bool):
                print(f"{key}: {str(value).lower()}")
            elif isinstance(value, str):
                print(f"{key}: {value}")
            else:
                print(f"{key}: {value}")

    except Exception as e:
        logger.exception(f"{e.__class__.__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
