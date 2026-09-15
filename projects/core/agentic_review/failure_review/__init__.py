#!/usr/bin/env python3

"""
FORGE Failure Review Agent - Pure NOOA Implementation

This module provides NOOA-based agents for analyzing FORGE test failures.
Users interact directly with NOOA agents - no backwards compatibility wrappers.

Example usage:
    from projects.core.agentic.failure_review import FailureReviewAgent, FailureContext
    from projects.core.agentic.models import create_llm_client, load_model_config

    # Load model and create agent
    model_config = load_model_config("psap-models-corp-rh", "agent-models.yaml")
    llm = create_llm_client(model_config["qwen-3-6-35b"])
    agent = FailureReviewAgent(llm=llm)

    # Analyze a failure
    context = FailureContext(...)
    analysis = agent.analyze_failure(context)

    print(f"Root Cause: {analysis.root_cause}")
    print(f"Severity: {analysis.severity}")
"""

import logging

# Import NOOA components - required for this module
try:
    from nooa import Agent

    _NOOA_AVAILABLE = True
except ImportError:
    _NOOA_AVAILABLE = False

    # Create fallback Agent class
    class Agent:
        def __init__(self, **kwargs):
            pass


# Re-export core agent classes
# Export utilities for agent creation
from projects.core.agentic.models import create_llm_client, is_nooa_available, load_model_config

from .agent import FailureAnalysis, FailureContext, FailureReviewAgent
from .cli import cli

logger = logging.getLogger(__name__)

# Module-level validation - warn but allow mock functionality
if not _NOOA_AVAILABLE:
    logger.warning(
        "NOOA framework not available. Using mock implementations. "
        "Install NOOA for full functionality: pip install nooa"
    )
