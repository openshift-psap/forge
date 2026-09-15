#!/usr/bin/env python3

"""
FORGE Config Review Agent - Pure NOOA Implementation

This module provides NOOA-based agents for analyzing FORGE test configurations.
Users interact directly with NOOA agents - no backwards compatibility wrappers.

Example usage:
    from projects.core.agentic.config_review import ConfigReviewAgent, ConfigContext
    from projects.core.agentic.models import create_llm_client, load_model_config

    # Load model and create agent
    models_config = load_model_config("psap-models-corp-rh", "agent-models.yaml")
    llm = create_llm_client(models_config["qwen-3-6-35b"])
    agent = ConfigReviewAgent(llm=llm)

    # Analyze configuration
    context = ConfigContext(...)
    analysis = agent.analyze_config(context)

    print(f"Test Description: {analysis.test_description}")
    print(f"Changes Summary: {analysis.changes_summary}")
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

from .agent import ConfigAnalysis, ConfigContext, ConfigReviewAgent
from .cli import cli

logger = logging.getLogger(__name__)

# Module-level validation - warn but allow mock functionality
if not _NOOA_AVAILABLE:
    logger.warning(
        "NOOA framework not available. Using mock implementations. "
        "Install NOOA for full functionality: pip install nooa"
    )
