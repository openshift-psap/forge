#!/usr/bin/env python3
"""
AG Praxis Project PR Arguments Parser

Parses ag_praxis-specific directives from PR trigger comments.
"""

from __future__ import annotations

import logging
from typing import Any

from projects.rhoai.library.pr_args import get_rhoai_directive_handlers

logger = logging.getLogger(__name__)


def get_supported_ag_praxis_directives() -> dict[str, str]:
    """
    Get a dictionary of supported ag_praxis-specific PR trigger forms.

    Returns:
        Dictionary mapping trigger forms to detailed descriptions
    """
    return {}


def parse_project_directives(comment_text: str) -> tuple[dict[str, Any], list[str]]:
    """
    Parse ag_praxis-specific behavior from PR trigger comments.

    Args:
        comment_text: Text from PR trigger comment

    Returns:
        Tuple of (configuration overrides dict, list of parsed directive lines)
    """
    directive_handlers = get_rhoai_directive_handlers()
    config_overrides: dict[str, Any] = {}
    parsed_directives: list[str] = []

    for raw_line in comment_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        for prefix, handler in directive_handlers.items():
            if line.startswith(prefix):
                result = handler(line)
                config_overrides.update(result)
                parsed_directives.append(line)
                logger.info("Parsed ag_praxis directive: %s", line)
                break

    return config_overrides, parsed_directives
