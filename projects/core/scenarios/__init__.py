"""Declarative scenario generation with config inheritance.

Generate benchmark scenarios from YAML configuration using matrix expansion.
Supports accelerator-specific settings via inheritance chain.

Example:
    from projects.core.scenarios import ConfigLoader, ScenarioGenerator

    # Load config with accelerator-specific inheritance
    loader = ConfigLoader("config/", accelerator="nvidia")
    model = loader.load_model("llama-3.3-70b-fp8")

    # Generate scenarios from matrix
    gen = ScenarioGenerator("config/projects/rhaiis.yaml", config_loader=loader)
    gen.load()

    for scenario in gen.expand():
        print(scenario.scenario_id)  # e.g., llama-70b-fp8_balanced_direct_tp4
"""

from .config import ScenarioConfig
from .config_loader import ConfigLoader, ResolvedModelConfig, ResolvedWorkloadConfig
from .generator import DeploymentGroup, ExpandedScenario, ParsedConfig, ScenarioGenerator

__all__ = [
    "ConfigLoader",
    "DeploymentGroup",
    "ExpandedScenario",
    "ParsedConfig",
    "ResolvedModelConfig",
    "ResolvedWorkloadConfig",
    "ScenarioConfig",
    "ScenarioGenerator",
]
