#!/usr/bin/env python3

from pathlib import Path


def on_wait_pods_appear_failure(args, ctx, exception):
    """Generate AGENT.md file for post-mortem analysis of pod deployment failure"""

    agent_md_path = args.artifact_dir / "AGENT.md"

    service_name = getattr(ctx, "service_name", args.inference_service_name)

    failure_info = f"""# Post-Mortem: LLM Service Pod Deployment Failure

## Failure Details
- **Service**: {service_name}
- **Namespace**: {args.namespace}
- **Failed Task**: wait_pods_appear
- **Exception**: {exception.__class__.__name__}: {exception}

## What Happened
The LLM inference service ({service_name}) was deployed but **pods did not appear** within the expected timeframe.
The deployment likely failed due to resource, image, or configuration issues.

## Available Files for Analysis
- **Manifest**: `src/{Path(args.inference_service_manifest_path).name}`
- **LLMISV Description**: `artifacts/llmisv_description.txt` (contains K8s events and status)
- **ReplicaSet Description**: `artifacts/replicaset_description.txt` (contains pod creation details)
- **Task logs**: Check task.log for deployment sequence

## Analysis Instructions
1. **Review K8s events** in the LLMISV description file - look for error events
2. **Check ReplicaSet status** in the replicaset description file - shows why pods weren't created
3. **Examine manifest** for resource requirements, image specs, and configuration
4. **Check task.log** for the deployment sequence and any error messages
"""

    with open(agent_md_path, "w", encoding="utf-8") as f:
        f.write(failure_info)
