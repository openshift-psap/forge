#!/usr/bin/env python3
"""MCP Gateway Project CI Operations"""

from pathlib import Path

from projects.agentic_tools.ci_base import CIApp, PhaseSpec

main = CIApp(
    project_name="mcp_gateway",
    description="MCP Gateway Project CI Operations for FORGE.",
    config_dir=Path(__file__).parent,
    phases={
        "prepare": PhaseSpec(
            func="projects.mcp_gateway.orchestration.prepare_phase:run",
            help="Prepare phase - Install platform at MCP_GATEWAY_VERSION.",
        ),
        "preflight": PhaseSpec(
            func="projects.mcp_gateway.orchestration.preflight_phase:run",
            help="Preflight check phase - Validate cluster readiness before testing.",
        ),
        "test": PhaseSpec(
            func="projects.mcp_gateway.orchestration.test_phase:run",
            help="Test phase - Execute load tests across the experiment matrix.",
        ),
        "pre-cleanup": PhaseSpec(
            func="projects.mcp_gateway.orchestration.cleanup_phase:run",
            help="Pre-cleanup phase - Remove test resources (Locust, mock server, routes).",
        ),
        "post-cleanup": PhaseSpec(
            func="projects.mcp_gateway.orchestration.cleanup_phase:run_platform_cleanup",
            help="Post-cleanup phase - Uninstall platform operators, CRs, and namespaces.",
        ),
    },
).build()

if __name__ == "__main__":
    main()
