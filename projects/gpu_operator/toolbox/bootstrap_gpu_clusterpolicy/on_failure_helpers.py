"""Failure analysis helpers for GPU ClusterPolicy bootstrap"""


def generate_failure_analysis(args, ctx, exception=None):
    """Generate AGENT.md file for ClusterPolicy failures"""

    # Check if we have ClusterPolicy failure information
    if not hasattr(ctx, "clusterpolicy_error_message"):
        return "No ClusterPolicy failure detected"

    agent_md_path = args.artifact_dir / "AGENT.md"

    with open(agent_md_path, "w") as f:
        f.write("# GPU ClusterPolicy Failure Analysis\n\n")
        f.write("## Problem Summary\n")
        f.write(f"The ClusterPolicy `{args.clusterpolicy_name}` failed to become ready.\n\n")
        f.write("## Error Message\n")
        f.write(f"```\n{ctx.clusterpolicy_error_message}\n```\n\n")
        f.write("## Files to Review\n\n")
        f.write("Please review the following files for detailed diagnostics:\n\n")
        f.write("### ClusterPolicy Configuration and Status\n")
        f.write(
            "- `artifacts/clusterpolicy.yaml` - Full ClusterPolicy YAML configuration and status\n\n"
        )
        # Get the namespace, with fallback if not set
        namespace = getattr(ctx, "gpu_operator_namespace", "nvidia-gpu-operator")
        f.write("### GPU Operator Resources\n")
        f.write(
            f"- `artifacts/gpu-operator-daemonsets.yaml` - DaemonSets in {namespace} namespace (full YAML)\n"
        )
        f.write("- `artifacts/gpu-operator-daemonsets.status.txt` - DaemonSets status summary\n")
        f.write(
            "- `artifacts/gpu-operator-daemonsets.describe.txt` - DaemonSets detailed descriptions and events\n"
        )
        f.write(
            f"- `artifacts/gpu-operator-pods.yaml` - Pods in {namespace} namespace (full YAML)\n"
        )
        f.write("- `artifacts/gpu-operator-pods.status.txt` - Pods status summary\n")
        f.write(
            "- `artifacts/gpu-operator-pods.describe.txt` - Pods detailed descriptions and events\n\n"
        )
        f.write("## Investigation Steps\n\n")
        f.write("1. Check the ClusterPolicy status and configuration in `clusterpolicy.yaml`\n")
        f.write("2. Review DaemonSet status to identify which components are failing\n")
        f.write("3. Examine Pod logs and events in the status files\n")
        f.write("4. Look for resource constraints, node compatibility, or driver issues\n")
        f.write("5. Check if required CRDs are available and GPU nodes are properly labeled\n")

    return "Generated failure analysis in AGENT.md"
