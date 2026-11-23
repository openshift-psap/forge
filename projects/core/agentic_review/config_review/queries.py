"""
FORGE Test Review Queries

This module contains all the query templates and logic for the multi-query
test analysis approach. Each query is focused on describing what is being
tested based on FORGE artifact directories.
"""

import time
import warnings
from datetime import datetime
from typing import Any

import urllib3
import yaml


class ConfigReviewQueries:
    """Container for all FORGE test review query templates and logic"""

    def __init__(self, artifact_data: dict[str, Any], base_artifact_dir: str = "unknown"):
        """
        Initialize with FORGE test artifact context

        Args:
            artifact_data: Dictionary containing parsed artifact directory data
            base_artifact_dir: Path to the base artifact directory
        """
        self.artifact_data = artifact_data
        self.base_artifact_dir = base_artifact_dir

        # Extract key components
        self.config_data = artifact_data.get("config", {})
        self.execution_engine = artifact_data.get("execution_engine", {})
        self.presets_applied = artifact_data.get("presets_applied", "")

        # Convert config to YAML string for LLM analysis
        self.config_yaml = (
            yaml.dump(self.config_data, default_flow_style=False, indent=2)
            if self.config_data
            else ""
        )

        # Build execution engine context
        self.execution_context = ""
        if self.execution_engine:
            project = self.execution_engine.get("project", "")
            args = self.execution_engine.get("args", [])
            config_overrides = self.execution_engine.get("configOverrides", [])

            self.execution_context = f"""
## EXECUTION ENGINE:
Project: {project}
Preset Arguments: {args}
Config Overrides: {len(config_overrides)} overrides
"""

        # Build presets applied context
        self.presets_context = ""
        if self.presets_applied:
            self.presets_context = f"""
## PRESETS APPLIED (Runtime Changes):
```
{self.presets_applied}
```
"""

    def build_test_context(self) -> str:
        """Build the complete test context for queries"""
        context = f"""
## FORGE TEST ARTIFACT DIRECTORY:
Directory: {self.base_artifact_dir}

{self.execution_context}

{self.presets_context}
"""

        if self.config_yaml:
            context += f"""
## FINAL CONFIGURATION:
```yaml
{self.config_yaml}
```
"""

        return context

    def query_test_description(self) -> dict[str, str]:
        """Generate a short description of what is being tested"""
        return {
            "type": "Test Description",
            "content": f"""
Analyze this FORGE test to provide a SHORT description of what is being tested.
{self.build_test_context()}
## TASK:
Provide a SHORT (1-2 sentences) description of what this test is doing:

1. Focus ONLY on what is being changed/tested, NOT the base configuration
2. Use the presets applied and execution engine information to understand the test focus
3. Mention the specific project being tested
4. Describe the key testing parameters or variations being applied

IMPORTANT:
- Keep it SHORT and focused on changes/testing, not base configuration
- Use the presets applied section to understand what was modified
- Be specific about what aspects are being tested
""",
        }

    def query_changes_summary(self) -> dict[str, str]:
        """Summarize the specific changes made for this test"""
        return {
            "type": "Changes Summary",
            "content": f"""
Summarize the specific configuration changes made for this FORGE test.
{self.build_test_context()}
## TASK:
Based on the presets applied and execution engine data, summarize the key changes:

1. **Preset Changes**: List the specific configuration changes from the presets applied section
2. **Override Changes**: Note any additional config overrides from the execution engine
3. **Parameter Variations**: Identify what parameters or settings are being varied for testing

Focus on the CHANGES and VARIATIONS, not the baseline configuration.
""",
        }

    def query_testing_focus(self) -> dict[str, str]:
        """Identify what aspect of the system is being tested"""
        return {
            "type": "Testing Focus",
            "content": f"""
Identify what specific aspect or component of the system is being tested.
{self.build_test_context()}
## TASK:
Based on the configuration changes and test setup, identify the testing focus:

1. **Primary Testing Target**: What system component or feature is the main focus?
2. **Test Scenario**: What specific scenario or use case is being exercised?
3. **Expected Behavior**: What behavior or performance characteristics are likely being measured?

Keep the response focused and concise.
""",
        }

    def query_configuration_context(self) -> dict[str, str]:
        """Provide minimal context about the configuration (not the changes)"""
        return {
            "type": "Configuration Context",
            "content": f"""
Provide MINIMAL context about the type of configuration being tested.
{self.build_test_context()}
## TASK:
Provide a brief (1 sentence) context about what type of system/application is being configured:

1. **System Type**: What kind of system or application this configuration is for
2. **Configuration Domain**: What domain or area the configuration covers (e.g., database, web server, ML pipeline)

Keep this VERY brief - just enough context to understand what's being tested.
""",
        }

    def query_test_summary(self, analysis_results: dict[str, str]) -> dict[str, str]:
        """Create synthetic summary of what is being tested"""
        return {
            "type": "Test Summary",
            "content": f"""
Create a concise summary of what this FORGE test is doing.

## ALL ANALYSIS RESULTS:

- **Test Description**: {analysis_results.get("test_description", "")}

- **Changes Summary**: {analysis_results.get("changes_summary", "")}

- **Testing Focus**: {analysis_results.get("testing_focus", "")}

- **Configuration Context**: {analysis_results.get("configuration_context", "")}

## TASK:
Create a brief executive summary (2-3 sentences) that captures:
- What is being tested (focus on changes/variations, not base config)
- What aspect of the system is being exercised
- The key testing parameters or scenarios

Keep it SHORT and focused on the test purpose, not configuration details.
""",
        }

    def get_all_queries(self) -> list[dict[str, Any]]:
        """
        Get all queries in execution order.

        This method handles the sequential execution of queries where later
        queries depend on results from earlier ones.

        Returns:
            List of query dictionaries with 'query_func', 'dependencies', and 'result_key'
        """
        return [
            {
                "query_func": self.query_test_description,
                "dependencies": [],
                "result_key": "test_description",
                "description": "Short description of what is being tested",
            },
            {
                "query_func": self.query_changes_summary,
                "dependencies": [],
                "result_key": "changes_summary",
                "description": "Summary of configuration changes made for the test",
            },
            {
                "query_func": self.query_testing_focus,
                "dependencies": [],
                "result_key": "testing_focus",
                "description": "Identify what aspect of the system is being tested",
            },
            {
                "query_func": self.query_configuration_context,
                "dependencies": [],
                "result_key": "configuration_context",
                "description": "Minimal context about the type of configuration",
            },
            {
                "query_func": self.query_test_summary,
                "dependencies": [
                    "test_description",
                    "changes_summary",
                    "testing_focus",
                    "configuration_context",
                ],
                "result_key": "summary",
                "description": "Synthetic test summary",
            },
        ]


def execute_config_query_sequence(
    queries_handler: ConfigReviewQueries, llm, verbose: bool = False
) -> dict[str, Any]:
    """
    Execute the full sequence of config review queries.

    Args:
        queries_handler: Initialized ConfigReviewQueries instance
        llm: LangChain LLM client
        verbose: Whether to show verbose output

    Returns:
        Dictionary containing all analysis results and tracking information
    """
    from langchain_core.messages import HumanMessage

    analysis_results = {}
    queries_and_responses = []

    query_sequence = queries_handler.get_all_queries()

    for i, query_def in enumerate(query_sequence, 1):
        query_func = query_def["query_func"]
        dependencies = query_def["dependencies"]
        result_key = query_def["result_key"]
        description = query_def["description"]

        if verbose:
            print(f"\n🤖 QUERY {i} - {query_def['query_func'].__name__.upper().replace('_', ' ')}")

        # Build query with dependencies
        if dependencies:
            # Check if this is a method that expects the full analysis_results dict
            if query_func.__name__ in ["query_test_summary"]:
                # Pass the full analysis_results dict
                query_data = query_func(analysis_results)
            else:
                # Pass individual dependency values as separate arguments
                dep_args = [analysis_results[dep] for dep in dependencies]
                query_data = query_func(*dep_args)
        else:
            # No dependencies, call without arguments
            query_data = query_func()

        query_type = query_data["type"]
        query_content = query_data["content"]

        # Execute query with timing and suppress SSL warnings for internal endpoints
        start_time = time.time()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
            response = llm.invoke([HumanMessage(content=query_content)])
        end_time = time.time()

        response_content = response.content.strip()

        # Extract token usage and other metadata from response
        response_metadata = getattr(response, "response_metadata", {})
        token_usage = response_metadata.get("token_usage", {})

        processing_time = end_time - start_time
        prompt_tokens = token_usage.get("prompt_tokens", 0)
        completion_tokens = token_usage.get("completion_tokens", 0)
        total_tokens = token_usage.get("total_tokens", prompt_tokens + completion_tokens)

        # Store result
        analysis_results[result_key] = response_content

        # Track for HTML report with enhanced metadata
        queries_and_responses.append(
            {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "query_type": query_type,
                "query": query_content,
                "response": response_content,
                "processing_time": processing_time,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "prompt_length": len(query_content),
                "response_length": len(response_content),
            }
        )

        if verbose:
            print(f"✅ {description}: {response_content[:100]}...")

    # Map specific result keys to expected format for backward compatibility
    # The main module expects certain key names
    if "changes_summary" in analysis_results:
        analysis_results["key_changes"] = analysis_results["changes_summary"]

    return {
        "analysis_results": analysis_results,
        "queries_and_responses": queries_and_responses,
        "query_count": len(query_sequence),
    }
