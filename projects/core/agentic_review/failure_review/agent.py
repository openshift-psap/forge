"""
FORGE Failure Review Agent Implementation

This module contains the core NOOA agent classes for analyzing FORGE test failures.
The agent uses an object-oriented approach where analysis state and capabilities
are expressed through typed Python classes.
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
class FailureAnalysis:
    """
    Structured representation of a FORGE test failure analysis

    This dataclass provides typed, structured output from the failure analysis
    process, replacing the previous unstructured dictionary approach.
    """

    root_cause: str
    failed_step: str
    category: str
    severity: str  # low, medium, high, critical
    summary: str
    technical_details: str
    timeline: str | None = None
    configuration_issues: str | None = None
    recommended_actions: list[str] | None = None


@dataclass
class FailureContext:
    """
    Context information for failure analysis

    Contains all the relevant information about a failure that the agent
    needs to perform analysis.
    """

    failure_file: Path
    base_artifact_dir: Path
    failure_content: str
    log_content: str
    agent_md_content: str
    available_files: list[str]
    additional_file_contents: dict[str, str] = None

    def __post_init__(self):
        if self.additional_file_contents is None:
            self.additional_file_contents = {}


class FailureReviewAgent(Agent if _NOOA_AVAILABLE else object):
    """
    FORGE Failure Review Agent using NOOA framework

    This agent specializes in analyzing FORGE test failures and providing
    structured, actionable insights about what went wrong and why.

    The agent maintains analysis context across multiple analysis steps
    and can request additional files as needed for deeper investigation.
    """

    def __init__(self, llm=None, **kwargs):
        """
        Initialize the Failure Review Agent

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
        self.file_investigation_results: dict[str, str] = {}

    # Core analysis methods - these will use NOOA's ... syntax when available

    def categorize_failure(self, context: FailureContext) -> str:
        """
        Categorize the type and severity of the failure

        Args:
            context: Failure context with logs and metadata

        Returns:
            Failure category and severity assessment
        """
        return self._direct_llm_categorize_failure(context)

    def identify_root_cause(self, context: FailureContext, category: str) -> str:
        """
        Perform deep root cause analysis based on available evidence

        Args:
            context: Failure context with logs and metadata
            category: Previously determined failure category

        Returns:
            Technical root cause explanation
        """
        return self._direct_llm_identify_root_cause(context, category)

    def identify_failed_step(self, context: FailureContext) -> str:
        """
        Identify the exact step or operation that failed

        Args:
            context: Failure context with logs and metadata

        Returns:
            Specific failed step identification
        """
        return self._direct_llm_identify_failed_step(context)

    def investigate_files(
        self, context: FailureContext, requested_files: list[str]
    ) -> dict[str, str]:
        """
        Read and analyze specific artifact files for deeper insights

        Args:
            context: Failure context
            requested_files: List of files to investigate

        Returns:
            Dictionary mapping file paths to their analysis results
        """
        return self._direct_llm_investigate_files(context, requested_files)

    def generate_technical_analysis(
        self,
        context: FailureContext,
        root_cause: str,
        failed_step: str,
        file_analysis: dict[str, str],
    ) -> str:
        """
        Generate comprehensive technical analysis combining all findings

        Args:
            context: Failure context
            root_cause: Identified root cause
            failed_step: Failed step identification
            file_analysis: Results from file investigation

        Returns:
            Detailed technical analysis document
        """
        return self._direct_llm_generate_technical_analysis(
            context, root_cause, failed_step, file_analysis
        )

    def create_executive_summary(self, full_analysis: FailureAnalysis) -> str:
        """
        Create a concise executive summary for stakeholders

        Args:
            full_analysis: Complete failure analysis

        Returns:
            Executive summary suitable for management reporting
        """
        return self._direct_llm_create_executive_summary(full_analysis)

    # Main orchestration method - regular Python (deterministic)
    def analyze_failure(self, context: FailureContext, verbose: bool = False) -> FailureAnalysis:
        """
        Orchestrate the complete failure analysis workflow

        This method coordinates the analysis steps and maintains state
        across the investigation process.

        Args:
            context: Failure context with all relevant information
            verbose: Whether to log detailed progress

        Returns:
            Structured failure analysis result
        """
        logger.info(f"🔍 Starting failure analysis for: {context.failure_file.parent.name}")

        try:
            # Step 1: Categorize the failure
            if verbose:
                logger.info("📋 Step 1: Categorizing failure...")
            logger.debug("🤖 Starting LLM call: categorize_failure")
            category = self.categorize_failure(context)
            logger.debug(f"✅ LLM call completed: categorize_failure = {category}")

            # Step 2: Identify root cause
            if verbose:
                logger.info("🔍 Step 2: Analyzing root cause...")
            logger.debug("🤖 Starting LLM call: identify_root_cause")
            root_cause = self.identify_root_cause(context, category)
            logger.debug(f"✅ LLM call completed: identify_root_cause = {root_cause[:100]}...")

            # Step 3: Identify failed step
            if verbose:
                logger.info("⚙️ Step 3: Identifying failed step...")
            logger.debug("🤖 Starting LLM call: identify_failed_step")
            failed_step = self.identify_failed_step(context)
            logger.debug(f"✅ LLM call completed: identify_failed_step = {failed_step[:100]}...")

            # Step 4: Investigate additional files if needed
            if verbose:
                logger.info("📁 Step 4: Investigating additional files...")
            # Determine which files to investigate based on initial analysis
            requested_files = self._determine_files_to_investigate(context, category, root_cause)
            file_analysis = {}
            if requested_files:
                file_analysis = self.investigate_files(context, requested_files)

            # Step 5: Generate comprehensive technical analysis
            if verbose:
                logger.info("📝 Step 5: Generating technical analysis...")
            technical_details = self.generate_technical_analysis(
                context, root_cause, failed_step, file_analysis
            )

            # Step 6: Create structured result
            analysis = FailureAnalysis(
                root_cause=root_cause,
                failed_step=failed_step,
                category=category,
                severity=self._determine_severity(category, root_cause),
                summary=self._create_summary(root_cause, failed_step),
                technical_details=technical_details,
            )

            # Step 7: Generate executive summary
            if verbose:
                logger.info("📊 Step 6: Creating executive summary...")
            executive_summary = self.create_executive_summary(analysis)
            analysis.summary = executive_summary

            logger.info("✅ Failure analysis completed successfully")
            return analysis

        except Exception as e:
            logger.error(f"❌ Failure analysis failed: {e}")
            raise

    # Helper methods (deterministic Python)
    def _determine_files_to_investigate(
        self, context: FailureContext, category: str, root_cause: str
    ) -> list[str]:
        """Determine which additional files should be investigated"""
        # Logic to select relevant files based on category and initial findings
        high_priority_patterns = [
            "_description.txt",
            "description.txt",
            ".yaml",
            ".yml",
            "endpoint.url",
            ".log",
            "AGENT.md",
        ]

        relevant_files = []
        for file_path in context.available_files:
            file_lower = file_path.lower()
            if any(pattern in file_lower for pattern in high_priority_patterns):
                relevant_files.append(file_path)

        # Limit to avoid overwhelming the analysis
        return relevant_files[:10]

    def _determine_severity(self, category: str, root_cause: str) -> str:
        """Determine failure severity based on category and root cause"""
        # Simple heuristic - can be enhanced with more sophisticated logic
        critical_indicators = ["timeout", "crash", "fatal", "critical"]
        high_indicators = ["error", "failed", "exception"]

        root_cause_lower = (root_cause or "").lower()
        category_lower = (category or "").lower()

        if any(
            indicator in root_cause_lower or indicator in category_lower
            for indicator in critical_indicators
        ):
            return "critical"
        elif any(
            indicator in root_cause_lower or indicator in category_lower
            for indicator in high_indicators
        ):
            return "high"
        else:
            return "medium"

    def _create_summary(self, root_cause: str, failed_step: str) -> str:
        """Create a brief summary from root cause and failed step"""
        return f"Test failed at step '{failed_step}' due to: {root_cause}"

    # Mock implementations for development (will be removed when NOOA is fully integrated)
    def _mock_categorize_failure(self, context: FailureContext) -> str:
        """Mock implementation of failure categorization"""
        # Simple pattern matching for development
        failure_content = context.failure_content.lower()
        log_content = context.log_content.lower()

        if "timeout" in failure_content or "timeout" in log_content:
            return "timeout"
        elif "connection" in failure_content or "connection" in log_content:
            return "connectivity"
        elif "permission" in failure_content or "permission" in log_content:
            return "permissions"
        elif "not found" in failure_content or "404" in log_content:
            return "resource_missing"
        else:
            return "unknown"

    def _mock_identify_root_cause(self, context: FailureContext, category: str) -> str:
        """Mock implementation of root cause analysis"""
        return (
            f"Mock root cause analysis for {category} failure in {context.failure_file.parent.name}"
        )

    def _mock_identify_failed_step(self, context: FailureContext) -> str:
        """Mock implementation of failed step identification"""
        # Look for actual failure patterns in logs
        lines = context.log_content.split("\n")
        for line in lines:
            if "==> TASK FAILED:" in line or "FAILED" in line:
                return f"Failed step identified from logs: {line.strip()[:100]}"
        return "Failed step not clearly identified in logs"

    def _mock_investigate_files(
        self, context: FailureContext, requested_files: list[str]
    ) -> dict[str, str]:
        """Mock implementation of file investigation"""
        results = {}
        for file_path in requested_files:
            results[file_path] = (
                f"Mock analysis of {file_path} - would contain actual file investigation results"
            )
        return results

    def _mock_generate_technical_analysis(
        self,
        context: FailureContext,
        root_cause: str,
        failed_step: str,
        file_analysis: dict[str, str],
    ) -> str:
        """Mock implementation of technical analysis generation"""
        return f"""
## Technical Analysis

**Root Cause**: {root_cause}

**Failed Step**: {failed_step}

**File Investigation Results**:
{chr(10).join([f"- {file}: {analysis}" for file, analysis in file_analysis.items()])}

**Timeline**: Mock timeline reconstruction would be provided here.

**Additional Context**: This is a mock technical analysis. The actual implementation
will use NOOA to generate comprehensive analysis based on all available evidence.
"""

    def _mock_create_executive_summary(self, full_analysis: FailureAnalysis) -> str:
        """Mock implementation of executive summary creation"""
        return f"""Test failure in category '{full_analysis.category}' with {full_analysis.severity} severity.
Root cause: {full_analysis.root_cause}
Failed step: {full_analysis.failed_step}"""

    # Direct LLM implementations (no NOOA framework required)
    def _direct_llm_categorize_failure(self, context: FailureContext) -> str:
        """Direct LLM call to categorize failure"""
        prompt = f"""
Categorize this test failure:

Failure: {context.failure_content[:500]}
Log: {context.log_content[:1000]}

Answer in 1-2 words: infrastructure, configuration, timeout, resource, or other specific category.
"""
        if hasattr(self.llm, "generate"):
            response = self.llm.generate(prompt)
        elif hasattr(self.llm, "complete"):
            response = self.llm.complete(prompt)
        elif callable(self.llm):
            response = self.llm(prompt)
        else:
            return "unknown"

        return response.strip() if isinstance(response, str) else str(response).strip()

    def _direct_llm_identify_root_cause(self, context: FailureContext, category: str) -> str:
        """Direct LLM call to identify root cause"""
        prompt = f"""
Find the root cause of this {category} failure:

Failure: {context.failure_content[:500]}
Log: {context.log_content[:1000]}

Answer in one sentence: What specifically caused this failure?
"""
        if hasattr(self.llm, "generate"):
            response = self.llm.generate(prompt)
        elif hasattr(self.llm, "complete"):
            response = self.llm.complete(prompt)
        elif callable(self.llm):
            response = self.llm(prompt)
        else:
            return f"Root cause analysis for {category} failure"

        return response.strip() if isinstance(response, str) else str(response).strip()

    def _direct_llm_identify_failed_step(self, context: FailureContext) -> str:
        """Direct LLM call to identify failed step"""
        prompt = f"""
What step failed in this test?

Failure: {context.failure_content[:500]}
Log: {context.log_content[:1000]}

Answer in one sentence: Which specific step or operation failed?
"""
        if hasattr(self.llm, "generate"):
            response = self.llm.generate(prompt)
        elif hasattr(self.llm, "complete"):
            response = self.llm.complete(prompt)
        elif callable(self.llm):
            response = self.llm(prompt)
        else:
            return "Failed step not clearly identified in logs"

        return response.strip() if isinstance(response, str) else str(response).strip()

    def _direct_llm_investigate_files(
        self, context: FailureContext, requested_files: list[str]
    ) -> dict[str, str]:
        """Direct LLM call to investigate files"""
        prompt = f"""
Analyze these files from test failure:

Files to check: {requested_files[:5]}  # Limit to 5 files
Available: {context.available_files[:10]}  # Limit context

For each file, respond briefly:
filename: what this reveals (1 short sentence)

Keep response under 200 words total.
"""
        if hasattr(self.llm, "generate"):
            response = self.llm.generate(prompt)
        elif hasattr(self.llm, "complete"):
            response = self.llm.complete(prompt)
        elif callable(self.llm):
            response = self.llm(prompt)
        else:
            return {file: f"File analysis for {file}" for file in requested_files}

        # Parse response into dictionary
        response_text = response if isinstance(response, str) else str(response)
        results = {}
        for line in response_text.split("\n"):
            if ":" in line:
                parts = line.split(":", 1)
                if len(parts) == 2:
                    filename = parts[0].strip()
                    analysis = parts[1].strip()
                    results[filename] = analysis

        # Ensure we have results for all requested files
        for file in requested_files:
            if file not in results:
                results[file] = f"Analysis for {file} (no specific insights found)"

        return results

    def _direct_llm_generate_technical_analysis(
        self,
        context: FailureContext,
        root_cause: str,
        failed_step: str,
        file_analysis: dict[str, str],
    ) -> str:
        """Direct LLM call to generate technical analysis"""
        prompt = f"""
Technical analysis for FORGE test failure:

Root Cause: {root_cause}
Failed Step: {failed_step}

Provide brief technical summary in 3-4 sentences:
1. What happened
2. Why it failed
3. Impact/severity

Keep under 100 words.
"""
        if hasattr(self.llm, "generate"):
            response = self.llm.generate(prompt)
        elif hasattr(self.llm, "complete"):
            response = self.llm.complete(prompt)
        elif callable(self.llm):
            response = self.llm(prompt)
        else:
            return f"Technical analysis for failure: {root_cause}"

        return response.strip() if isinstance(response, str) else str(response).strip()

    def _direct_llm_create_executive_summary(self, full_analysis: FailureAnalysis) -> str:
        """Direct LLM call to create executive summary"""
        prompt = f"""
Executive summary for {full_analysis.category} failure ({full_analysis.severity} severity):

Root Cause: {full_analysis.root_cause}

Write 1-2 sentences for management: what failed and business impact.
"""
        if hasattr(self.llm, "generate"):
            response = self.llm.generate(prompt)
        elif hasattr(self.llm, "complete"):
            response = self.llm.complete(prompt)
        elif callable(self.llm):
            response = self.llm(prompt)
        else:
            return f"Executive summary: {full_analysis.category} failure with {full_analysis.severity} severity"

        return response.strip() if isinstance(response, str) else str(response).strip()
