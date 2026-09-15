"""
FORGE Config Review Agent Implementation

This module contains the core NOOA agent classes for analyzing FORGE test configurations.
The agent uses an object-oriented approach to understand what is being tested based on
configuration changes and test parameters.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Import NOOA components when available
try:
    from nooa import Agent

    _NOOA_AVAILABLE = True
except ImportError:
    # Temporary fallback while NOOA is being set up
    _NOOA_AVAILABLE = False

    class Agent:
        def __init__(self, **kwargs):
            self.llm = kwargs.get("llm")


logger = logging.getLogger(__name__)


@dataclass
class ConfigAnalysis:
    """
    Structured representation of a FORGE configuration analysis

    This dataclass provides typed, structured output from the configuration analysis
    process, describing what is being tested and how.
    """

    test_description: str
    changes_summary: str
    testing_focus: str
    configuration_context: str
    test_type: str  # performance, functionality, stress, etc.
    estimated_scope: str  # small, medium, large
    key_parameters: list[str]
    risk_assessment: str  # low, medium, high
    expected_outcomes: list[str] | None = None
    configuration_issues: str | None = None


@dataclass
class ConfigContext:
    """
    Context information for configuration analysis

    Contains all the relevant information about a test configuration that the agent
    needs to perform analysis.
    """

    artifact_dir: Path
    config_data: dict[str, Any]
    execution_engine: dict[str, Any]
    presets_applied: str
    config_overrides: list[str]
    project_name: str
    available_files: list[str]

    def __post_init__(self):
        if not hasattr(self, "additional_context"):
            self.additional_context = {}


class ConfigReviewAgent(Agent if _NOOA_AVAILABLE else object):
    """
    FORGE Config Review Agent using NOOA framework

    This agent specializes in analyzing FORGE test configurations to understand
    what is being tested, focusing on configuration changes and test parameters.

    The agent can identify test purposes, analyze configuration differences,
    and provide clear descriptions of testing objectives.
    """

    def __init__(self, llm=None, **kwargs):
        """
        Initialize the Config Review Agent

        Args:
            llm: Language model client for analysis
            **kwargs: Additional initialization parameters
        """
        if _NOOA_AVAILABLE:
            # Pass llm to NOOA Agent parent class
            super().__init__(llm=llm, **kwargs)
        else:
            # Fallback when NOOA not available
            self.llm = llm

        # Agent state
        self.analysis_history: list[dict[str, Any]] = []

    # Core analysis methods - these will use NOOA's ... syntax when available

    def identify_test_purpose(self, context: ConfigContext) -> str:
        """
        Identify what is being tested based on configuration

        Args:
            context: Configuration context with all test details

        Returns:
            Clear description of test purpose and objectives
        """
        return self._direct_llm_identify_test_purpose(context)

    def analyze_configuration_changes(self, context: ConfigContext) -> str:
        """
        Analyze configuration changes and their implications

        Args:
            context: Configuration context

        Returns:
            Summary of key configuration changes being tested
        """
        return self._direct_llm_analyze_configuration_changes(context)

    def determine_testing_focus(self, context: ConfigContext) -> str:
        """
        Determine the main focus of the testing effort

        Args:
            context: Configuration context

        Returns:
            Description of testing focus areas
        """
        return self._direct_llm_determine_testing_focus(context)

    def assess_test_scope(self, context: ConfigContext) -> str:
        """
        Assess the scope and scale of the test

        Args:
            context: Configuration context

        Returns:
            Assessment of test scope (small, medium, large)
        """
        return self._direct_llm_assess_test_scope(context)

    def extract_key_parameters(self, context: ConfigContext) -> list[str]:
        """
        Extract key parameters being tested

        Args:
            context: Configuration context

        Returns:
            List of key parameters and settings being tested
        """
        return self._direct_llm_extract_key_parameters(context)

    def generate_context_analysis(self, context: ConfigContext) -> str:
        """
        Generate broader context analysis of the test

        Args:
            context: Configuration context

        Returns:
            Analysis of the broader testing context and environment
        """
        return self._direct_llm_generate_context_analysis(context)

    # Main orchestration method - regular Python (deterministic)
    def analyze_config(self, context: ConfigContext, verbose: bool = False) -> ConfigAnalysis:
        """
        Orchestrate the complete configuration analysis workflow

        This method coordinates the analysis steps and maintains state
        across the investigation process.

        Args:
            context: Configuration context with all relevant information
            verbose: Whether to log detailed progress

        Returns:
            Structured configuration analysis result
        """
        logger.info(f"🔍 Starting configuration analysis for: {context.project_name}")

        try:
            # Step 1: Identify test purpose
            if verbose:
                logger.info("📋 Step 1: Identifying test purpose...")
            test_description = self.identify_test_purpose(context)

            # Step 2: Analyze configuration changes
            if verbose:
                logger.info("🔄 Step 2: Analyzing configuration changes...")
            changes_summary = self.analyze_configuration_changes(context)

            # Step 3: Determine testing focus
            if verbose:
                logger.info("🎯 Step 3: Determining testing focus...")
            testing_focus = self.determine_testing_focus(context)

            # Step 4: Assess scope
            if verbose:
                logger.info("📏 Step 4: Assessing test scope...")
            estimated_scope = self.assess_test_scope(context)

            # Step 5: Extract key parameters
            if verbose:
                logger.info("🔧 Step 5: Extracting key parameters...")
            key_parameters = self.extract_key_parameters(context)

            # Step 6: Generate context analysis
            if verbose:
                logger.info("🌍 Step 6: Generating context analysis...")
            configuration_context = self.generate_context_analysis(context)

            # Step 7: Create structured result
            analysis = ConfigAnalysis(
                test_description=test_description,
                changes_summary=changes_summary,
                testing_focus=testing_focus,
                configuration_context=configuration_context,
                test_type=self._determine_test_type(test_description, testing_focus),
                estimated_scope=estimated_scope,
                key_parameters=key_parameters,
                risk_assessment=self._assess_risk(estimated_scope, key_parameters),
            )

            logger.info("✅ Configuration analysis completed successfully")
            return analysis

        except Exception as e:
            import traceback

            logger.error(f"❌ Configuration analysis failed: {e}")
            logger.error("Full traceback:")
            traceback.print_exc()
            raise

    # Helper methods (deterministic Python)
    def _determine_test_type(self, test_description: str, testing_focus: str) -> str:
        """Determine the type of test based on description and focus"""
        description_lower = (test_description or "").lower()
        focus_lower = (testing_focus or "").lower()

        if any(
            word in description_lower or word in focus_lower
            for word in ["performance", "benchmark", "load", "stress"]
        ):
            return "performance"
        elif any(
            word in description_lower or word in focus_lower
            for word in ["functionality", "feature", "behavior"]
        ):
            return "functionality"
        elif any(
            word in description_lower or word in focus_lower
            for word in ["scale", "scaling", "capacity"]
        ):
            return "scalability"
        elif any(
            word in description_lower or word in focus_lower
            for word in ["reliability", "stability", "robustness"]
        ):
            return "reliability"
        else:
            return "general"

    def _assess_risk(self, scope: str, parameters: list[str]) -> str:
        """Assess risk level based on scope and parameters"""
        param_count = len(parameters) if parameters else 0
        if scope == "large" or param_count > 5:
            return "high"
        elif scope == "medium" or param_count > 2:
            return "medium"
        else:
            return "low"

    # Mock implementations for development (will be removed when NOOA is fully integrated)
    def _mock_identify_test_purpose(self, context: ConfigContext) -> str:
        """Mock implementation of test purpose identification"""
        if context.project_name:
            return f"Mock test analysis for {context.project_name} - determining test objectives from configuration"
        return "Mock test purpose identification - analyzing configuration to understand test goals"

    def _mock_analyze_configuration_changes(self, context: ConfigContext) -> str:
        """Mock implementation of configuration change analysis"""
        if context.config_overrides:
            override_count = len(context.config_overrides) if context.config_overrides else 0
            return f"Mock analysis: {override_count} configuration overrides detected"
        return "Mock configuration change analysis - examining parameter modifications"

    def _mock_determine_testing_focus(self, context: ConfigContext) -> str:
        """Mock implementation of testing focus determination"""
        return "Mock testing focus analysis - identifying key areas of validation"

    def _mock_assess_test_scope(self, context: ConfigContext) -> str:
        """Mock implementation of test scope assessment"""
        # Determine scope based on available data
        override_count = len(context.config_overrides) if context.config_overrides else 0
        if override_count > 5:
            return "large"
        elif override_count > 2:
            return "medium"
        else:
            return "small"

    def _mock_extract_key_parameters(self, context: ConfigContext) -> list[str]:
        """Mock implementation of key parameter extraction"""
        if context.config_overrides:
            return context.config_overrides[:3]  # Return first 3 as mock
        return ["mock-parameter-1", "mock-parameter-2"]

    def _mock_generate_context_analysis(self, context: ConfigContext) -> str:
        """Mock implementation of context analysis generation"""
        return f"Mock context analysis for {context.project_name} - broader testing environment assessment"

    # Direct LLM implementations (no NOOA framework required)
    def _direct_llm_identify_test_purpose(self, context: ConfigContext) -> str:
        """Direct LLM call to identify test purpose"""
        prompt = f"""
Analyze this FORGE test configuration to identify what is being tested.

Project: {context.project_name}
Config Data: {context.config_data}
Config Overrides: {context.config_overrides}
Available Files: {context.available_files[:10]}  # Limit for prompt size

Based on the configuration, describe in 1-2 sentences what this test is trying to accomplish and what aspects are being validated.

Focus on:
- What functionality or performance aspect is being tested
- What the test objectives appear to be based on the configuration

Response should be clear and specific, not generic.
"""
        # Call LLM directly - let it fail if it doesn't work
        if hasattr(self.llm, "generate"):
            response = self.llm.generate(prompt)
        elif hasattr(self.llm, "complete"):
            response = self.llm.complete(prompt)
        elif callable(self.llm):
            response = self.llm(prompt)
        else:
            logger.warning(f"Unknown LLM client interface. Available methods: {dir(self.llm)}")
            return f"LLM available but interface unknown - analyzing {context.project_name}"

        return response.strip() if isinstance(response, str) else str(response).strip()

    def _direct_llm_analyze_configuration_changes(self, context: ConfigContext) -> str:
        """Direct LLM call to analyze configuration changes"""
        prompt = f"""
Analyze the configuration changes and overrides in this FORGE test setup.

Project: {context.project_name}
Config Overrides: {context.config_overrides}
Execution Engine: {context.execution_engine}

Summarize in 1-2 sentences what configuration parameters have been modified and what these changes indicate about the test focus.

If no overrides are present, indicate what the base configuration suggests about the test approach.
"""
        if hasattr(self.llm, "generate"):
            response = self.llm.generate(prompt)
        elif hasattr(self.llm, "complete"):
            response = self.llm.complete(prompt)
        elif callable(self.llm):
            response = self.llm(prompt)
        else:
            return f"Configuration changes analysis for {context.project_name}"

        return response.strip() if isinstance(response, str) else str(response).strip()

    def _direct_llm_determine_testing_focus(self, context: ConfigContext) -> str:
        """Direct LLM call to determine testing focus"""
        prompt = f"""
Determine the main focus areas for this FORGE test based on the configuration.

Project: {context.project_name}
Config Data: {context.config_data}
Config Overrides: {context.config_overrides}

Identify in 1-2 sentences the primary areas this test is focusing on (e.g., performance, functionality, scalability, reliability).

Base your analysis on configuration parameters, overrides, and any patterns you see.
"""
        if hasattr(self.llm, "generate"):
            response = self.llm.generate(prompt)
        elif hasattr(self.llm, "complete"):
            response = self.llm.complete(prompt)
        elif callable(self.llm):
            response = self.llm(prompt)
        else:
            return f"Testing focus analysis for {context.project_name}"

        return response.strip() if isinstance(response, str) else str(response).strip()

    def _direct_llm_assess_test_scope(self, context: ConfigContext) -> str:
        """Direct LLM call to assess test scope"""
        prompt = f"""
Assess the scope of this FORGE test configuration.

Project: {context.project_name}
Config Overrides: {context.config_overrides}
Available Files: {len(context.available_files)} total files
Execution Engine: {context.execution_engine}

Based on the complexity, number of overrides, and configuration scope, classify this test as:
- "small" (limited scope, few parameters)
- "medium" (moderate complexity, several parameters)
- "large" (comprehensive testing, many parameters or complex setup)

Respond with just the single word: small, medium, or large
"""
        if hasattr(self.llm, "generate"):
            response = self.llm.generate(prompt)
        elif hasattr(self.llm, "complete"):
            response = self.llm.complete(prompt)
        elif callable(self.llm):
            response = self.llm(prompt)
        else:
            return "medium"  # Safe default

        # Parse response to extract just the scope word
        response_text = (
            response.strip().lower() if isinstance(response, str) else str(response).lower()
        )

        if "small" in response_text:
            return "small"
        elif "large" in response_text:
            return "large"
        elif "medium" in response_text:
            return "medium"
        else:
            return "medium"  # Safe default

    def _direct_llm_extract_key_parameters(self, context: ConfigContext) -> list[str]:
        """Direct LLM call to extract key parameters"""
        prompt = f"""
Extract the most important configuration parameters from this FORGE test setup.

Project: {context.project_name}
Config Data: {context.config_data}
Config Overrides: {context.config_overrides}

Identify the 3-5 most important configuration parameters or settings that are central to this test.

Return only the parameter names, one per line, no explanations.
"""
        if hasattr(self.llm, "generate"):
            response = self.llm.generate(prompt)
        elif hasattr(self.llm, "complete"):
            response = self.llm.complete(prompt)
        elif callable(self.llm):
            response = self.llm(prompt)
        else:
            return ["unknown-parameter"]

        # Parse response into list
        response_text = response if isinstance(response, str) else str(response)
        lines = [line.strip() for line in response_text.split("\n") if line.strip()]

        # If no meaningful lines, return context-based parameters
        if not lines or all("mock" in line.lower() for line in lines):
            if context.config_overrides:
                return context.config_overrides[:3]
            return ["config-parameter", "execution-parameter"]

        return lines[:5]  # Limit to 5 parameters max

    def _direct_llm_generate_context_analysis(self, context: ConfigContext) -> str:
        """Direct LLM call to generate context analysis"""
        prompt = f"""
Provide broader context analysis for this FORGE test configuration.

Project: {context.project_name}
Config Data: {context.config_data}
Available Files: {context.available_files[:5]}  # Sample of files

Analyze the broader testing context and environment in 1-2 sentences. Consider:
- What this test fits into in terms of overall system validation
- Environmental or infrastructure aspects being tested
- How this relates to typical FORGE testing patterns

Provide contextual insight beyond just the immediate configuration.
"""
        if hasattr(self.llm, "generate"):
            response = self.llm.generate(prompt)
        elif hasattr(self.llm, "complete"):
            response = self.llm.complete(prompt)
        elif callable(self.llm):
            response = self.llm(prompt)
        else:
            return f"Context analysis for {context.project_name}"

        return response.strip() if isinstance(response, str) else str(response).strip()
