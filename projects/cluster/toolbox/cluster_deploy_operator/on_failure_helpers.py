#!/usr/bin/env python3

import logging

logger = logging.getLogger("DSL")


def handle_installplan_failure(args, ctx, exception):
    """Generate AGENT.md file when InstallPlan task fails"""

    agent_md_path = args.artifact_dir / "AGENT.md"

    # Extract error message from the exception
    error_message = str(exception) if exception else "InstallPlan task failed"

    with open(agent_md_path, "w") as f:
        f.write("# Operator InstallPlan Failure Analysis\n\n")
        f.write("## Problem Summary\n")
        f.write(f"The operator `{args.package_name}` failed during InstallPlan processing.\n\n")
        f.write("## Error Details\n")
        f.write(f"**Failure Reason**: {error_message}\n\n")
        f.write("## Files to Review\n\n")
        f.write("Please review the following files for detailed diagnostics:\n\n")
        f.write("### Subscription Analysis\n")
        f.write(
            f"- `artifacts/{args.package_name}-subscription.yaml` - Full subscription YAML configuration and status\n\n"
        )
        f.write("**Key areas to check in the subscription YAML:**\n")
        f.write("- `.status.installPlanRef` - Should contain a reference to the InstallPlan\n")
        f.write("- `.status.conditions` - Check for any error conditions\n")
        f.write("- `.spec.installPlanApproval` - Verify approval mode (Automatic vs Manual)\n")
        f.write("- `.status.state` - Overall subscription state\n\n")
        f.write("### Additional Resources\n")
        f.write(f"- `artifacts/pods.yaml` - Pods in {args.target_namespace} namespace\n")
        f.write("- `artifacts/pods.status` - Pod status overview\n\n")
        f.write("## Investigation Steps\n\n")
        f.write(
            "1. **Check Subscription Status**: Look for error conditions in the subscription YAML\n"
        )
        f.write(
            "2. **Verify Catalog Source**: Ensure the catalog source is available and healthy\n"
        )
        f.write(
            "3. **Check Operator Availability**: Verify the operator exists in the specified channel\n"
        )
        f.write("4. **Review InstallPlan Generation**: Check if OLM is creating InstallPlans\n")
        f.write("5. **Check Resource Constraints**: Look for resource limit or RBAC issues\n")

    logger.info("Generated InstallPlan failure analysis in AGENT.md")
