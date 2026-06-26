#!/usr/bin/env python3

from __future__ import annotations

import json
import logging

import yaml

from projects.core.dsl import always, entrypoint, execute_tasks, retry, shell, task, template

logger = logging.getLogger("DSL")


@entrypoint
def run(
    package_name: str,
    target_namespace: str,
    source_name: str,
    channel: str,
    *,
    source_namespace: str = "openshift-marketplace",
    installplan_approval: str = "Automatic",
    display_name: str = "",
    install_mode: str = "auto",
) -> int:
    """
    Deploy an OLM operator and wait for its CSV to succeed.

    Args:
        package_name: Operator package/subscription name
        target_namespace: Namespace where the operator will be installed
        source_name: CatalogSource name providing the operator
        channel: Subscription channel to use
        source_namespace: CatalogSource namespace
        installplan_approval: InstallPlan approval mode
        display_name: Optional human-friendly name used in logs
        install_mode: Install mode - "auto" (default), "namespace-scoped", or "cluster-wide"
    """

    execute_tasks(locals())
    return 0


@task
def setup_directories(args, ctx):
    """Create command source and artifact directories"""

    shell.mkdir("src")
    shell.mkdir("artifacts")
    ctx.display_name = args.display_name or args.package_name
    return f"Prepared directories for {ctx.display_name}"


@task
def validate_parameters(args, ctx):
    """Validate command parameters"""

    if not args.package_name:
        raise ValueError("package_name is required")
    if not args.target_namespace:
        raise ValueError("target_namespace is required")
    if not args.source_name:
        raise ValueError("source_name is required")
    if not args.channel:
        raise ValueError("channel is required")
    if args.installplan_approval not in {"Automatic", "Manual"}:
        raise ValueError("installplan_approval must be either 'Automatic' or 'Manual'")
    if args.install_mode not in {"auto", "namespace-scoped", "cluster-wide"}:
        raise ValueError("install_mode must be 'auto', 'namespace-scoped', or 'cluster-wide'")

    # Convert install_mode to cluster_wide boolean and store in context
    if args.install_mode == "cluster-wide":
        ctx.cluster_wide = True
    elif args.install_mode == "namespace-scoped":
        ctx.cluster_wide = False
    else:  # auto mode - will be determined in check_install_modes task
        ctx.cluster_wide = None

    return (
        f"Validated deployment parameters for {ctx.display_name} "
        f"from {args.source_name}/{args.source_namespace} (install_mode: {args.install_mode})"
    )


@retry(attempts=60, delay=15)
@task
def wait_for_catalog_source_ready(args, ctx):
    """Wait for the CatalogSource to report READY"""

    result = shell.run(
        "oc get catalogsource "
        f"{args.source_name} -n {args.source_namespace} "
        "-o jsonpath='{.status.connectionState.lastObservedState}'",
        check=False,
        log_stdout=False,
    )
    if not result.success:
        return (False, f"failed to query CatalogSource {args.source_name}")

    state = result.stdout.strip()
    if state == "READY":
        return f"CatalogSource {args.source_name} is READY"

    # Provide specific reason based on the current state
    if state:
        return (False, f"CatalogSource {args.source_name} is in {state} state")
    else:
        return (False, f"CatalogSource {args.source_name} status is empty")


@retry(attempts=60, delay=15)
@task
def wait_for_package_manifest(args, ctx):
    """Wait for the operator PackageManifest to become available"""

    result = shell.run(
        f"oc get packagemanifest {args.package_name} -n {args.source_namespace} -o json",
        check=False,
        log_stdout=False,
    )
    if not result.success:
        return (False, f"PackageManifest {args.package_name} not found in {args.source_namespace}")

    # Save the package manifest to a file instead of storing in context
    try:
        package_data = json.loads(result.stdout)
        package_manifest_file = (
            args.artifact_dir / "src" / f"{args.package_name}-packagemanifest.json"
        )
        with open(package_manifest_file, "w") as f:
            json.dump(package_data, f, indent=2)
        ctx.package_manifest_file = package_manifest_file
    except json.JSONDecodeError:
        logger.warning("Failed to parse PackageManifest JSON")
        ctx.package_manifest_file = None

    return f"PackageManifest {args.package_name} is available"


@task
def check_install_modes(args, ctx):
    """Check supported install modes and determine final cluster_wide setting"""

    if not hasattr(ctx, "package_manifest_file") or ctx.package_manifest_file is None:
        if ctx.cluster_wide is None:  # auto mode
            logger.warning(
                "PackageManifest file not available, defaulting to namespace-scoped mode"
            )
            ctx.cluster_wide = False
        return f"Using install mode (cluster_wide={ctx.cluster_wide})"

    try:
        with open(ctx.package_manifest_file) as f:
            package_manifest = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(f"Failed to read PackageManifest file: {e}")
        if ctx.cluster_wide is None:  # auto mode
            logger.warning("Defaulting to namespace-scoped mode")
            ctx.cluster_wide = False
        return f"Using install mode (cluster_wide={ctx.cluster_wide})"

    try:
        # Navigate to install modes in the PackageManifest
        default_channel = package_manifest.get("status", {}).get("defaultChannel", "")
        channels = package_manifest.get("status", {}).get("channels", [])

        # Find the channel we're using (or default channel)
        target_channel = args.channel if args.channel else default_channel
        channel_data = None

        for channel in channels:
            if channel.get("name") == target_channel:
                channel_data = channel
                break

        if not channel_data:
            logger.warning(f"Channel '{target_channel}' not found in PackageManifest")
            if ctx.cluster_wide is None:  # auto mode
                logger.warning("Defaulting to namespace-scoped mode")
                ctx.cluster_wide = False
            return f"Using install mode (cluster_wide={ctx.cluster_wide})"

        # Get install modes from the current CSV entry
        current_csv_desc = channel_data.get("currentCSVDesc", {})
        install_modes = current_csv_desc.get("installModes", [])

        if not install_modes:
            logger.warning("No install modes found in PackageManifest")
            if ctx.cluster_wide is None:  # auto mode
                logger.warning("Defaulting to namespace-scoped mode")
                ctx.cluster_wide = False
            return f"Using install mode (cluster_wide={ctx.cluster_wide})"

        # Check what install modes are supported
        supported_modes = {}
        for mode in install_modes:
            mode_type = mode.get("type", "")
            supported = mode.get("supported", False)
            supported_modes[mode_type] = supported

        logger.info(f"Operator {args.package_name} supported install modes: {supported_modes}")

        owns_namespace_supported = supported_modes.get("OwnNamespace", False)
        all_namespaces_supported = supported_modes.get("AllNamespaces", False)

        original_install_mode = args.install_mode
        original_cluster_wide = ctx.cluster_wide

        # Handle auto mode - choose the best available mode
        if ctx.cluster_wide is None:  # auto mode
            if owns_namespace_supported and all_namespaces_supported:
                # Both supported - prefer namespace-scoped (safer)
                ctx.cluster_wide = False
                logger.info("Auto mode: choosing namespace-scoped (both modes supported)")
            elif owns_namespace_supported:
                # Only namespace-scoped supported
                ctx.cluster_wide = False
                logger.info("Auto mode: choosing namespace-scoped (only mode supported)")
            elif all_namespaces_supported:
                # Only cluster-wide supported
                ctx.cluster_wide = True
                logger.info("Auto mode: choosing cluster-wide (only mode supported)")
            else:
                # Neither supported - error
                logger.error(
                    f"Operator {args.package_name} doesn't support OwnNamespace or AllNamespaces modes"
                )
                raise ValueError(
                    f"Operator {args.package_name} doesn't support standard install modes"
                )

        # Handle explicit mode requests
        elif not ctx.cluster_wide and not owns_namespace_supported:
            if all_namespaces_supported:
                logger.warning(
                    f"Operator {args.package_name} doesn't support namespace-scoped mode, switching to cluster-wide"
                )
                ctx.cluster_wide = True
            else:
                logger.error(
                    f"Operator {args.package_name} doesn't support namespace-scoped mode and no suitable alternative found"
                )
                raise ValueError(
                    f"Operator {args.package_name} doesn't support the requested install mode"
                )

        elif ctx.cluster_wide and not all_namespaces_supported:
            logger.error(f"Operator {args.package_name} doesn't support cluster-wide mode")
            raise ValueError(
                f"Operator {args.package_name} doesn't support cluster-wide installation"
            )

        # Build result message
        mode_desc = "cluster-wide" if ctx.cluster_wide else "namespace-scoped"

        if original_install_mode == "auto":
            return f"Auto-selected install mode: {mode_desc}"
        elif original_cluster_wide != ctx.cluster_wide:
            return f"Adjusted install mode: {mode_desc} (requested: {original_install_mode})"
        else:
            return f"Validated install mode: {mode_desc}"

    except Exception as e:
        logger.warning(f"Failed to parse install modes from PackageManifest: {e}")
        if ctx.cluster_wide is None:  # auto mode
            logger.warning("Defaulting to namespace-scoped mode")
            ctx.cluster_wide = False
        return f"Using install mode (cluster_wide={ctx.cluster_wide})"


@task
def render_namespace_manifest(args, ctx):
    """Render the target namespace manifest"""

    ctx.namespace_manifest_file = (
        args.artifact_dir / "src" / f"{args.target_namespace}-namespace.yaml"
    )
    template.render_template_to_file("namespace.yaml.j2", ctx.namespace_manifest_file)
    return f"Rendered namespace manifest for {args.target_namespace}"


@task
def apply_namespace(args, ctx):
    """Apply the target namespace manifest"""

    shell.run(f"oc apply -f {ctx.namespace_manifest_file}")
    return f"Ensured namespace {args.target_namespace}"


@task
def render_operator_group_manifest(args, ctx):
    """Render the OperatorGroup manifest, unless a suitable one already exists"""

    result = shell.run(
        f"oc get operatorgroup -n {args.target_namespace} -o json",
        check=False,
        log_stdout=False,
    )
    if result.success:
        operator_groups = json.loads(result.stdout).get("items", [])
        for operator_group in operator_groups:
            spec = operator_group.get("spec", {})
            target_namespaces = spec.get("targetNamespaces", [])

            # For cluster-wide mode, reuse OperatorGroup with no targetNamespaces
            if ctx.cluster_wide and not target_namespaces:
                ctx.operator_group_manifest_file = None
                ctx.operator_group_name = operator_group["metadata"]["name"]
                return (
                    f"Reusing existing cluster-wide OperatorGroup {ctx.operator_group_name} "
                    f"in {args.target_namespace}"
                )
            # For namespace-scoped mode, reuse if target namespace is included
            elif ctx.cluster_wide is False and (
                not target_namespaces or args.target_namespace in target_namespaces
            ):
                ctx.operator_group_manifest_file = None
                ctx.operator_group_name = operator_group["metadata"]["name"]
                return (
                    f"Reusing existing OperatorGroup {ctx.operator_group_name} "
                    f"in {args.target_namespace}"
                )

        if operator_groups:
            names = ", ".join(
                operator_group["metadata"]["name"] for operator_group in operator_groups
            )
            if ctx.cluster_wide is None:
                mode_desc = "auto-detected"
            else:
                mode_desc = "cluster-wide" if ctx.cluster_wide else "namespace-scoped"
            raise RuntimeError(
                f"Namespace {args.target_namespace} already has OperatorGroups "
                f"that are incompatible with {mode_desc} mode: {names}"
            )

    ctx.operator_group_manifest_file = (
        args.artifact_dir / "src" / f"{args.package_name}-operatorgroup.yaml"
    )
    template.render_template_to_file("operator_group.yaml.j2", ctx.operator_group_manifest_file)
    mode_desc = "cluster-wide" if ctx.cluster_wide else "namespace-scoped"
    return f"Rendered {mode_desc} OperatorGroup manifest for {args.package_name}"


@task
def apply_operator_group(args, ctx):
    """Apply the OperatorGroup manifest"""

    if ctx.operator_group_manifest_file is None:
        return f"Using existing OperatorGroup {ctx.operator_group_name}"

    shell.run(f"oc apply -f {ctx.operator_group_manifest_file}")
    mode_desc = "cluster-wide" if ctx.cluster_wide else "namespace-scoped"
    return f"Ensured {mode_desc} OperatorGroup in {args.target_namespace}"


@task
def render_subscription_manifest(args, ctx):
    """Render the Subscription manifest"""

    ctx.subscription_manifest_file = (
        args.artifact_dir / "src" / f"{args.package_name}-subscription.yaml"
    )
    template.render_template_to_file("subscription.yaml.j2", ctx.subscription_manifest_file)
    return f"Rendered Subscription manifest for {args.package_name}"


@task
def apply_subscription(args, ctx):
    """Apply the Subscription manifest"""

    shell.run(f"oc apply -f {ctx.subscription_manifest_file}")
    return f"Applied Subscription for {ctx.display_name}"


@retry(attempts=30, delay=10)
@task
def wait_for_installplan(args, ctx):
    """Wait for InstallPlan and approve if manual approval is required"""

    # Get the subscription to check for install plan reference
    result = shell.run(
        f"oc get subscription {args.package_name} -n {args.target_namespace} -o yaml",
        stdout_dest=args.artifact_dir / "artifacts" / f"{args.package_name}-subscription.yaml",
        check=False,
        log_stdout=False,
    )

    if not result.success:
        return (False, f"Failed to get subscription {args.package_name}")

    subscription_data = yaml.safe_load(result.stdout)
    install_plan_ref = subscription_data.get("status", {}).get("installPlanRef", {})

    if not install_plan_ref or not install_plan_ref.get("name"):
        return (False, "No InstallPlan reference found in subscription status")

    install_plan_name = install_plan_ref["name"]

    # Get the install plan to check its approval mode
    result = shell.run(
        f"oc get installplan {install_plan_name} -n {args.target_namespace} -o yaml",
        stdout_dest=args.artifact_dir / "artifacts" / f"{install_plan_name}-installplan.yaml",
        check=False,
        log_stdout=False,
    )

    if not result.success:
        return (False, f"Failed to get InstallPlan {install_plan_name}")

    install_plan_data = yaml.safe_load(result.stdout)
    approval_mode = install_plan_data.get("spec", {}).get("approval", "")
    approved = install_plan_data.get("spec", {}).get("approved", False)

    if approval_mode.lower() == "automatic":
        return f"InstallPlan {install_plan_name} has automatic approval"

    elif approval_mode.lower() == "manual":
        if approved:
            return f"InstallPlan {install_plan_name} already approved"
        else:
            # Approve the manual install plan
            shell.run(
                f"oc patch installplan {install_plan_name} -n {args.target_namespace} "
                '--type merge -p \'{"spec":{"approved":true}}\''
            )
            return f"Approved manual InstallPlan {install_plan_name}"
    else:
        return (
            False,
            f"Unknown approval mode '{approval_mode}' for InstallPlan {install_plan_name}",
        )


@retry(attempts=30, delay=10)
@task
def wait_for_csv_to_appear(args, ctx):
    """Wait for the CSV to appear after subscription creation"""

    result = shell.run(
        "oc get csv "
        f"-n {args.target_namespace} "
        f"-l operators.coreos.com/{args.package_name}.{args.target_namespace} "
        "--no-headers",
        check=False,
        log_stdout=True,
    )
    if not result.success:
        return (False, f"failed to query CSVs for package {args.package_name}")

    # Parse CSV name from first column of output
    lines = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
    if not lines:
        return (False, f"no CSV found for package {args.package_name} in {args.target_namespace}")

    # Extract CSV name (first column)
    csv_name = lines[0].split()[0]
    ctx.csv_name = csv_name
    return f"CSV {ctx.csv_name} appeared"


@retry(attempts=60, delay=15)
@task
def wait_for_csv_ready(args, ctx):
    """Wait for the installed CSV to reach Succeeded phase"""

    result = shell.run(
        f"oc get csv {ctx.csv_name} -n {args.target_namespace} -o jsonpath='{{.status.phase}}'",
        check=False,
        log_stdout=True,
    )
    if not result.success:
        return (False, f"failed to query CSV {ctx.csv_name}")

    if not result.stdout.strip():
        return (False, f"CSV {ctx.csv_name} status is empty")

    # Get phase directly from stdout since we're querying .status.phase
    phase = result.stdout.strip()

    if phase == "Succeeded":
        return f"CSV {ctx.csv_name} succeeded"
    if phase == "Failed":
        raise RuntimeError(f"CSV {ctx.csv_name} failed - aborting installation")
    if phase == "Replacing":
        logger.info("CSV %s currently in phase %s", ctx.csv_name, phase)
        return (False, f"CSV {ctx.csv_name} is in Replacing phase")

    # Handle other phases (Pending, Installing, etc.)
    return (False, f"CSV {ctx.csv_name} is in {phase} phase")


@always
@task
def capture_catalog_source(args, ctx):
    """Capture CatalogSource YAML for diagnostics"""

    shell.run(
        f"oc get catalogsource {args.source_name} -n {args.source_namespace} -o yaml",
        stdout_dest=args.artifact_dir / "artifacts" / "catalogsource.yaml",
        check=False,
    )
    return "Captured CatalogSource state"


@always
@task
def capture_package_manifest(args, ctx):
    """Capture PackageManifest YAML and JSON"""

    shell.run(
        f"oc get packagemanifest {args.package_name} -n {args.source_namespace} -o yaml",
        stdout_dest=args.artifact_dir / "artifacts" / "packagemanifest.yaml",
        check=False,
    )
    shell.run(
        f"oc get packagemanifest {args.package_name} -n {args.source_namespace} -o json",
        stdout_dest=args.artifact_dir / "artifacts" / "packagemanifest.json",
        check=False,
    )
    return "Captured PackageManifest state"


@always
@task
def capture_operator_group(args, ctx):
    """Capture OperatorGroup YAML"""

    shell.run(
        f"oc get operatorgroup -n {args.target_namespace} -o yaml",
        stdout_dest=args.artifact_dir / "artifacts" / "operatorgroup.yaml",
        check=False,
    )
    return "Captured OperatorGroup state"


@always
@task
def capture_subscription(args, ctx):
    """Capture Subscription YAML"""

    shell.run(
        f"oc get subscription {args.package_name} -n {args.target_namespace} -o yaml",
        stdout_dest=args.artifact_dir / "artifacts" / "subscription.yaml",
        check=False,
    )
    return "Captured Subscription state"


@always
@task
def capture_csv(args, ctx):
    """Capture installed CSV YAML when known"""

    csv_name = getattr(ctx, "csv_name", "")
    if not csv_name:
        # First check if any CSV exists to avoid array index out of bounds
        result = shell.run(
            "oc get csv "
            f"-n {args.target_namespace} "
            f"-l operators.coreos.com/{args.package_name}.{args.target_namespace} "
            "--no-headers -ocustom-columns=NAME:.metadata.name",
            check=False,
        )
        if not result.success:
            return "No CSV found to capture (command failed)"

        csv_name = result.stdout.strip()
        if not csv_name:
            return "No CSV found to capture (no matching CSV)"

        # If multiple CSVs, take the first one
        csv_name = csv_name.split("\n")[0].strip()

    shell.run(
        f"oc get csv {csv_name} -n {args.target_namespace} -o yaml",
        stdout_dest=args.artifact_dir / "artifacts" / "csv.yaml",
        check=False,
    )
    return f"Captured CSV {csv_name}"


@always
@task
def capture_target_namespace_pods(args, ctx):
    """Capture pods from the operator target namespace"""

    shell.run(
        f"oc get pods -n {args.target_namespace} -o wide",
        stdout_dest=args.artifact_dir / "artifacts" / "pods.status",
        check=False,
    )
    shell.run(
        f"oc get pods -n {args.target_namespace} -o yaml",
        stdout_dest=args.artifact_dir / "artifacts" / "pods.yaml",
        check=False,
    )
    return "Captured target namespace pods"


if __name__ == "__main__":
    run.main()
