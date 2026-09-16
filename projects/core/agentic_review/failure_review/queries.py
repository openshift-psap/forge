"""
FORGE Failure Analysis Queries

This module contains all the query templates and logic for the multi-query
failure analysis approach. Each query is focused on a specific aspect of
failure investigation.
"""

import time
import warnings
from datetime import datetime
from typing import Any

import urllib3


class FailureAnalysisQueries:
    """Container for all failure analysis query templates and logic"""

    def __init__(self, failure_data: dict[str, Any], available_files: list[str]):
        """
        Initialize with failure context

        Args:
            failure_data: Dictionary containing failure information
            available_files: List of available files in artifact directory
        """
        self.failure_content = failure_data["failure_content"]
        self.log_content = failure_data.get("log_content") or failure_data.get(
            "ansible_content", ""
        )
        self.agent_md_content = failure_data.get("agent_md_content", "")
        self.failed_tasks_section = ""
        if failure_data.get("failed_tasks_summary"):
            self.failed_tasks_section = f"""
## FAILED TASKS IDENTIFIED:
{failure_data["failed_tasks_summary"]}
"""
        self.available_files = available_files[:10]  # Limit for prompt size
        self.requested_file_contents = {}  # Will be populated when files are read

    def _smart_truncate_content(self, file_path: str, content: str, max_chars: int = 3000) -> str:
        """
        Smart truncation strategy based on file type to preserve the most relevant content

        Args:
            file_path: Path to the file (used to determine type)
            content: File content to truncate
            max_chars: Maximum characters to include

        Returns:
            Truncated content optimized for the file type
        """
        if len(content) <= max_chars:
            return content

        file_lower = file_path.lower()

        # For kubectl describe outputs and similar diagnostic files - END is most important
        if "_description.txt" in file_lower or "describe" in file_lower:
            # Keep the end (last 80%) and a small beginning (first 20%)
            beginning_chars = int(max_chars * 0.2)
            end_chars = max_chars - beginning_chars - 50  # Reserve space for separator

            beginning = content[:beginning_chars]
            end = content[-end_chars:]

            return f"{beginning}\n\n... [MIDDLE TRUNCATED - showing last {end_chars} chars with diagnostic info] ...\n\n{end}"

        # For logs - show both beginning and end
        elif ".log" in file_lower:
            # Split 50/50 between beginning and end
            half_chars = int(max_chars / 2) - 25  # Reserve space for separator

            beginning = content[:half_chars]
            end = content[-half_chars:]

            return f"{beginning}\n\n... [MIDDLE TRUNCATED] ...\n\n{end}"

        # For config files (yaml, json) - beginning is most important
        elif any(ext in file_lower for ext in [".yaml", ".yml", ".json", ".conf", ".cfg"]):
            return content[:max_chars] + f"\n\n... [TRUNCATED - showing first {max_chars} chars]"

        # For other files - show beginning and end
        else:
            beginning_chars = int(max_chars * 0.6)
            end_chars = max_chars - beginning_chars - 50

            beginning = content[:beginning_chars]
            end = content[-end_chars:]

            return f"{beginning}\n\n... [MIDDLE TRUNCATED] ...\n\n{end}"

    def query_synthetic_summary_final(self, analysis_results: dict[str, str]) -> dict[str, str]:
        """Final synthetic summary with detailed file analysis"""

        # Build comprehensive analysis results including detailed file analysis
        analysis_sections = []
        analysis_sections.append(
            f"- **Categorization**: {analysis_results.get('categorization', '')}"
        )
        analysis_sections.append(f"- **Root Cause**: {analysis_results.get('root_cause', '')}")
        analysis_sections.append(f"- **Failed Step**: {analysis_results.get('failed_step', '')}")

        # Include detailed file analysis (contains the definitive root cause)
        if analysis_results.get("detailed_file_analysis"):
            analysis_sections.append(
                f"- **Detailed File Analysis**: {analysis_results.get('detailed_file_analysis', '')}"
            )

        return {
            "type": "Final Synthetic Summary",
            "content": f"""
Create the definitive executive summary of this FORGE test failure.

## ALL ANALYSIS RESULTS:

{chr(10).join(analysis_sections)}

## TASK:
Create a brief executive summary (2-3 sentences) based on the analysis above that captures:
- What failed
- Why it failed (using the most specific root cause available)

IMPORTANT:
- PRIORITIZE findings from Detailed File Analysis if available - it contains the definitive root cause based on actual artifact files
- If Detailed File Analysis contradicts earlier analysis, use the detailed findings as they're based on actual evidence
- Use ONLY the information from the analysis results above
- Do NOT add speculations or generic advice
""",
        }

    def _get_truncation_type(self, file_path: str) -> str:
        """Get description of truncation strategy used for logging"""
        file_lower = file_path.lower()
        if "_description.txt" in file_lower or "describe" in file_lower:
            return "end-focused (20% start + 80% end)"
        elif ".log" in file_lower:
            return "balanced (50% start + 50% end)"
        elif any(ext in file_lower for ext in [".yaml", ".yml", ".json", ".conf", ".cfg"]):
            return "start-focused"
        else:
            return "balanced (60% start + 40% end)"

    def build_common_context(self, log_excerpt_length: int = None) -> str:
        """Build the common failure and execution log context for queries"""
        log_excerpt = self.log_content
        if log_excerpt_length:
            log_excerpt = f"{self.log_content[:log_excerpt_length]}..."

        context = f"""
## FAILURE:
```
{self.failure_content}
{self.failed_tasks_section}
```

## EXECUTION LOG:
```
{log_excerpt}
```"""

        # Add available files information
        if self.available_files:
            context += f"""

## AVAILABLE ARTIFACT FILES:
The following files are available for deeper analysis if needed:
```
{chr(10).join(self.available_files)}
```

**NOTE**: If you need to examine specific artifact files that were captured during test execution,
mention them in your response using the format: NEED_FILES: filename1.yaml, filename2.log
Common FORGE artifacts include:
- Manifest files: *.yaml, *.yml (deployment configs) - may be in src/, artifacts/, or other subdirs
- Captured descriptions: *_description.txt (kubectl describe outputs) - usually in artifacts/
- Generated configs: *.json, *.conf, *.cfg
- Scripts and logs: *.sh, *.py, *.log files
- Endpoint/URL files: *.url, endpoint.*
Files will be searched in both the current failure directory and the broader artifact tree.
Only request files you can see in the available files list above."""

        # Add requested file contents if available
        if hasattr(self, "requested_file_contents") and self.requested_file_contents:
            context += """

## ARTIFACT FILES FOR ANALYSIS:"""
            files_included = []
            files_failed = []

            for file_path, content in self.requested_file_contents.items():
                if not content.startswith("FILE NOT FOUND") and not content.startswith(
                    "ERROR READING"
                ):
                    # Smart truncation: for diagnostic files, show both beginning and end
                    display_content = self._smart_truncate_content(file_path, content)
                    context += f"""

### {file_path}:
```
{display_content}
```"""
                    files_included.append(file_path)
                else:
                    context += f"""

### {file_path}: {content}"""
                    files_failed.append(file_path)

            if files_included:
                context += f"""

**FILES SUCCESSFULLY PROVIDED**: {", ".join(files_included)}
**IMPORTANT**: The above files contain the actual artifact content. DO NOT request more files."""

                # Log which files are being passed to the model
                import logging

                logger = logging.getLogger(__name__)
                logger.info(
                    f"🗂️  Passing {len(files_included)} files to LLM: {', '.join(files_included)}"
                )

                # Log truncation info for large files
                for file_path, content in self.requested_file_contents.items():
                    if (
                        not content.startswith(("FILE NOT FOUND", "ERROR READING"))
                        and len(content) > 3000
                    ):
                        truncation_type = self._get_truncation_type(file_path)
                        logger.info(
                            f"📄 {file_path}: {len(content)} chars → using {truncation_type} truncation strategy"
                        )

            if files_failed:
                context += f"""

**FILES UNAVAILABLE**: {", ".join(files_failed)}"""

        # Add AGENT.md content if available
        if self.agent_md_content:
            context += f"""

## POST-MORTEM ANALYSIS GUIDANCE:
```
{self.agent_md_content}
```"""

        return context

    def build_failure_context(self) -> str:
        """Build just the failure context without execution log"""
        context = f"""
## FAILURE:
```
{self.failure_content}
{self.failed_tasks_section}
```"""

        # Add AGENT.md content if available
        if self.agent_md_content:
            context += f"""

## POST-MORTEM ANALYSIS GUIDANCE:
```
{self.agent_md_content}
```"""

        return context

    def query_categorization(self) -> dict[str, str]:
        """Initial categorization and identification"""
        return {
            "type": "Initial Categorization",
            "content": f"""
Analyze this FORGE test failure for initial categorization.
{self.build_common_context(log_excerpt_length=2000)}
## TASK:
Provide a brief initial categorization based ONLY on the actual failure content and logs above:

1. **Failure Type**: Based on what is actually shown in the logs
2. **Severity**: Based on the actual impact observed
3. **Component**: Which specific system/component failed according to the logs
4. **Quick Summary**: 1-2 sentences describing what actually happened

IMPORTANT: Use ONLY information from the logs above. Do NOT speculate or invent generic possibilities.
""",
        }

    def query_root_cause(self) -> dict[str, str]:
        """Root cause analysis"""
        return {
            "type": "Root Cause Analysis",
            "content": f"""
Based on this FORGE test failure, provide a deep technical root cause analysis.
{self.build_common_context()}
## TASK:
Perform deep root cause analysis using the available evidence:

**Root Cause**: Identify the fundamental technical reason for the failure based on what is actually shown in the execution log and failure message above.

**Analysis Strategy**:
1. First analyze the failure message and execution log provided
2. Review the available artifact files listed above
3. If the root cause is unclear, examine relevant captured artifacts such as:
   - Deployment manifests (*.yaml, *.yml files in artifacts)
   - Captured kubectl descriptions (*_description.txt files)
   - Generated configuration files (*.json, *.conf)
   - Additional captured logs (*.log files beyond task.log)
   - FORGE-generated scripts or configs (*.sh, *.py files)

IMPORTANT:
- Use ONLY specific evidence from the logs and files - exact error messages, exit codes, file paths, command outputs
- If you need to examine specific files for deeper analysis, use: NEED_FILES: filename1, filename2
- Do NOT speculate about possible causes that aren't shown in the available evidence
- Quote specific log entries and file contents that support your analysis
""",
        }

    def query_failed_step(self) -> dict[str, str]:
        """Failed step breakdown"""
        return {
            "type": "Failed Step Breakdown",
            "content": f"""
Analyze the specific step that failed in this FORGE test.
{self.build_common_context()}
## TASK:
Identify the exact step that failed based on the actual log content:

**Failed Step**: Find the specific step/task/operation that failed by looking for:
- In DSL logs: "==> TASK FAILED:" patterns
- In execution logs: "----- FAILED ----" patterns
- Specific commands that returned error codes
- Operations that timed out or were interrupted

**Additional Investigation**: If the logs don't provide sufficient detail about what the step was trying to do, examine captured artifacts such as:
- Deployment manifests in the artifacts directory (*.yaml, *.yml)
- Captured service/pod descriptions (*_description.txt files)
- FORGE-generated task configurations or scripts
- Any additional captured log files beyond the main task.log

Provide the exact step name from the logs, what it was trying to do, and the immediate error message.

IMPORTANT:
- Only report what is actually shown in the logs and available files
- If you need more context about a specific step, use: NEED_FILES: filename1, filename2
- Do NOT invent or assume failure scenarios
""",
        }

    def query_synthetic_summary(self, analysis_results: dict[str, str]) -> dict[str, str]:
        """Synthetic summary (preliminary - before detailed file analysis)"""
        return {
            "type": "Synthetic Summary",
            "content": f"""
Create a concise executive summary of this FORGE test failure.

## ALL ANALYSIS RESULTS:

- **Categorization**: {analysis_results.get("categorization", "")}

- **Root Cause**: {analysis_results.get("root_cause", "")}

- **Failed Step**: {analysis_results.get("failed_step", "")}

## TASK:
Create a brief executive summary (2-3 sentences) based on the analysis above that captures:
- What failed
- Why it failed

NOTE: This is a preliminary summary. A final summary will be generated after detailed file analysis.

Use ONLY the information from the analysis results above. Do NOT add speculations or generic advice.
""",
        }

    def query_full_analysis(self, analysis_results: dict[str, str]) -> dict[str, str]:
        """Full technical analysis"""
        return {
            "type": "Full Technical Analysis",
            "content": f"""
Create a comprehensive technical analysis document for this FORGE test failure.

## ALL ANALYSIS RESULTS:
{chr(10).join([f"**{k.title().replace('_', ' ')}**: {v}" for k, v in analysis_results.items()])}

## TASK:
Create a detailed technical analysis document that combines all findings into a coherent narrative. Include:

1. **Overview**: What happened and impact
2. **Technical Details**: Deep dive into the failure mechanism
3. **Timeline**: Sequence of events leading to the failure

IMPORTANT:
- If the Detailed File Analysis contains definitive findings, incorporate them as the primary root cause
- Focus on understanding and documenting what actually occurred based on the evidence gathered
- Do NOT add speculative solutions or generic prevention strategies
""",
        }

    def query_detailed_file_analysis(self, analysis_results: dict[str, str]) -> dict[str, str]:
        """Detailed analysis using additional artifact files"""

        # Build context and check if files are actually included
        context = self.build_common_context()

        # Add debug info about file availability
        file_status = ""
        if hasattr(self, "requested_file_contents") and self.requested_file_contents:
            available_files = [
                f
                for f in self.requested_file_contents.keys()
                if not self.requested_file_contents[f].startswith(
                    ("FILE NOT FOUND", "ERROR READING")
                )
            ]
            file_status = f"FILES AVAILABLE FOR ANALYSIS: {', '.join(available_files)}\n\n"
        else:
            file_status = "NO ADDITIONAL FILES AVAILABLE - analyzing with logs only.\n\n"

        return {
            "type": "Detailed File Analysis",
            "content": f"""
Now analyze this FORGE test failure using any additional artifact files that were captured.

{context}

## PREVIOUS ANALYSIS RESULTS:
{chr(10).join([f"**{k.title()}**: {v}" for k, v in analysis_results.items()])}

## TASK:
{file_status}Provide comprehensive analysis:

1. **Enhanced Root Cause**: Using ALL available evidence (logs + any artifact files), what is the definitive root cause?
2. **Configuration Analysis**: If config/manifest files are available, identify any issues or relevant settings
3. **Deployment State Analysis**: If service descriptions are available, what was the actual state at failure?
4. **Timeline Reconstruction**: What sequence of events led to this failure?

IMPORTANT:
- If additional files are available above, quote specific content from them in your analysis
- If no additional files are available, provide the best analysis possible with the logs
- Do NOT request more files - work with what is provided above
- Focus on definitive conclusions based on actual evidence
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
                "query_func": self.query_categorization,
                "dependencies": [],
                "result_key": "categorization",
                "description": "Initial failure categorization",
            },
            {
                "query_func": self.query_root_cause,
                "dependencies": [],
                "result_key": "root_cause",
                "description": "Deep technical root cause analysis",
            },
            {
                "query_func": self.query_failed_step,
                "dependencies": [],
                "result_key": "failed_step",
                "description": "Exact failed step identification",
            },
            {
                "query_func": self.query_synthetic_summary,
                "dependencies": ["categorization", "root_cause", "failed_step"],
                "result_key": "synthetic_summary",
                "description": "Executive summary",
            },
            {
                "query_func": self.query_full_analysis,
                "dependencies": ["categorization", "root_cause", "failed_step"],
                "result_key": "full_analysis",
                "description": "Comprehensive technical analysis",
            },
            {
                "query_func": self.query_detailed_file_analysis,
                "dependencies": ["categorization", "root_cause", "failed_step"],
                "result_key": "detailed_file_analysis",
                "description": "Deep analysis with additional artifact files",
                "requires_files": True,
            },
            {
                "query_func": self.query_synthetic_summary_final,
                "dependencies": [
                    "categorization",
                    "root_cause",
                    "failed_step",
                    "detailed_file_analysis",
                ],
                "result_key": "synthetic_summary_final",
                "description": "Final executive summary with detailed analysis",
                "requires_files": True,
            },
        ]


def execute_query_sequence(
    queries_handler: FailureAnalysisQueries, llm, verbose: bool = False, file_reader_callback=None
) -> dict[str, Any]:
    """
    Execute the full sequence of failure analysis queries.

    Args:
        queries_handler: Initialized FailureAnalysisQueries instance
        llm: LangChain LLM client
        verbose: Whether to show verbose output
        file_reader_callback: Function to call when files are requested (base_artifact_dir, file_list) -> dict

    Returns:
        Dictionary containing all analysis results and tracking information
    """
    from langchain_core.messages import HumanMessage

    analysis_results = {}
    queries_and_responses = []
    requested_files = []

    query_sequence = queries_handler.get_all_queries()

    # First pass: execute queries that don't require files
    for i, query_def in enumerate(query_sequence, 1):
        # Skip file-dependent queries in first pass
        if query_def.get("requires_files"):
            continue

        query_func = query_def["query_func"]
        dependencies = query_def["dependencies"]
        result_key = query_def["result_key"]
        description = query_def["description"]

        if verbose:
            print(f"\n🤖 QUERY {i} - {query_def['query_func'].__name__.upper().replace('_', ' ')}")

        # Build query with dependencies
        if dependencies:
            # Check if this is a method that expects the full analysis_results dict
            if query_func.__name__ in [
                "query_synthetic_summary",
                "query_synthetic_summary_final",
                "query_full_analysis",
                "query_detailed_file_analysis",
            ]:
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

        # Check for file requests
        if "NEED_FILES:" in response_content:
            lines = response_content.split("\n")
            for line in lines:
                if "NEED_FILES:" in line:
                    file_request_part = line.split("NEED_FILES:", 1)[1].strip()
                    files = [f.strip() for f in file_request_part.split(",") if f.strip()]
                    requested_files.extend(files)

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

    # Second pass: if files were requested, read them and run file-dependent queries
    import logging

    logger = logging.getLogger(__name__)

    logger.info(f"🔍 First pass complete. Requested files found: {requested_files}")
    logger.info(
        f"🔍 Second pass check: requested_files={len(requested_files)}, file_reader_callback={file_reader_callback is not None}"
    )

    if requested_files and file_reader_callback:
        logger.info("✅ Both conditions met - entering second pass")
    else:
        logger.warning(
            f"❌ Second pass skipped - requested_files: {bool(requested_files)}, file_reader_callback: {file_reader_callback is not None}"
        )
        return {
            "analysis_results": analysis_results,
            "queries_and_responses": queries_and_responses,
            "query_count": len(queries_and_responses),
            "files_requested": requested_files,
        }

    if True:  # Always execute this block if we get here
        # Remove duplicates while preserving order
        unique_files = list(dict.fromkeys(requested_files))

        logger.info(
            f"📁 Reading {len(unique_files)} requested files from failure dir and base dir: {', '.join(unique_files)}"
        )

        # Read the requested files
        file_contents = file_reader_callback(unique_files)
        queries_handler.requested_file_contents = file_contents

        # Log which files were successfully read
        successful_files = [
            f
            for f, content in file_contents.items()
            if not content.startswith(("FILE NOT FOUND", "ERROR READING"))
        ]
        failed_files = [
            f
            for f, content in file_contents.items()
            if content.startswith(("FILE NOT FOUND", "ERROR READING"))
        ]

        if successful_files:
            logger.info(
                f"✅ Successfully read {len(successful_files)} files: {', '.join(successful_files)}"
            )
        if failed_files:
            logger.warning(
                f"❌ Failed to read {len(failed_files)} files: {', '.join(failed_files)}"
            )

        if verbose:
            print(
                f"\n📋 Files processed: {len(successful_files)} success, {len(failed_files)} failed"
            )

        # Now run the file-dependent queries
        logger.info(
            f"🔄 Starting second pass: checking {len(query_sequence)} queries for requires_files=True"
        )

        file_queries_found = 0
        for _i, query_def in enumerate(query_sequence, 1):
            if query_def.get("requires_files"):
                file_queries_found += 1
                logger.info(f"🔍 Found file-dependent query: {query_def['query_func'].__name__}")

        logger.info(f"🔄 Found {file_queries_found} file-dependent queries to execute")

        for i, query_def in enumerate(query_sequence, 1):
            if not query_def.get("requires_files"):
                continue

            logger.info(f"🤖 Executing file-dependent query: {query_def['query_func'].__name__}")

            query_func = query_def["query_func"]
            dependencies = query_def["dependencies"]
            result_key = query_def["result_key"]
            description = query_def["description"]

            if verbose:
                print(
                    f"\n🤖 FILE QUERY {i} - {query_def['query_func'].__name__.upper().replace('_', ' ')}"
                )

            # Log when running detailed file analysis
            if query_func.__name__ == "query_detailed_file_analysis":
                import logging

                logger = logging.getLogger(__name__)
                available_files = getattr(queries_handler, "requested_file_contents", {})
                if available_files:
                    successful_files = [
                        f
                        for f, content in available_files.items()
                        if not content.startswith(("FILE NOT FOUND", "ERROR READING"))
                    ]
                    logger.info(
                        f"🔍 Running detailed analysis with {len(successful_files)} files included in context"
                    )
                else:
                    logger.info("🔍 Running detailed analysis with no additional files (logs only)")

            # Build query with dependencies
            if dependencies:
                query_data = query_func(analysis_results)
            else:
                query_data = query_func()

            query_type = query_data["type"]
            query_content = query_data["content"]

            # Execute query with timing
            start_time = time.time()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
                response = llm.invoke([HumanMessage(content=query_content)])
            end_time = time.time()

            response_content = response.content.strip()

            # Extract metadata
            response_metadata = getattr(response, "response_metadata", {})
            token_usage = response_metadata.get("token_usage", {})

            processing_time = end_time - start_time
            prompt_tokens = token_usage.get("prompt_tokens", 0)
            completion_tokens = token_usage.get("completion_tokens", 0)
            total_tokens = token_usage.get("total_tokens", prompt_tokens + completion_tokens)

            # Store result
            analysis_results[result_key] = response_content

            # Track for HTML report
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

    return {
        "analysis_results": analysis_results,
        "queries_and_responses": queries_and_responses,
        "query_count": len(queries_and_responses),
        "files_requested": requested_files,
    }
