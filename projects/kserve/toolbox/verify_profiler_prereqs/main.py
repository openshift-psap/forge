#!/usr/bin/env python3
"""Verify that the vLLM profiler webhook infrastructure is ready.

Ported from model-furnace's oc_commands.verify_profiler_prerequisites().
Checks webhook pod, MutatingWebhookConfiguration, TLS secret, and
ConfigMap before any profiler run is attempted.

Args:
    namespace: Target namespace of the profiled inference service
        (must match the webhook deployment's TARGET_NAMESPACE).
    webhook_namespace: Namespace hosting the env-injector webhook
        (default "vllm-profiler").
"""

from projects.core.dsl import (
    entrypoint,
    execute_tasks,
    shell,
    task,
)

MIN_CA_BUNDLE_LENGTH = 100


@entrypoint
def run(*, namespace: str, webhook_namespace: str = "vllm-profiler"):
    return execute_tasks(locals())


@task
def check_webhook_pod(args, context):
    result = shell.run(
        f"oc get pods -n {args.webhook_namespace} -l app=env-injector "
        "-o jsonpath='{.items[0].status.phase}'",
        check=False,
    )
    phase = result.stdout.strip().strip("'")
    if result.returncode != 0 or phase != "Running":
        context.errors = getattr(context, "errors", [])
        context.errors.append(
            f"Webhook pod not running (status: {phase or 'not found'}). "
            "Deploy it with: ./scripts/deploy.sh"
        )
        return f"FAIL: webhook pod status={phase or 'not found'}"
    return "OK: webhook pod is Running"


@task
def check_webhook_target_namespace(args, context):
    result = shell.run(
        f"oc get deployment env-injector -n {args.webhook_namespace} "
        "-o jsonpath='{.spec.template.spec.containers[0]"
        '.env[?(@.name=="TARGET_NAMESPACE")].value}\'',
        check=False,
    )
    target_ns = result.stdout.strip().strip("'")
    if result.returncode != 0 or not target_ns:
        context.errors = getattr(context, "errors", [])
        context.errors.append("Could not read webhook TARGET_NAMESPACE from deployment")
        return "FAIL: could not read TARGET_NAMESPACE"
    if target_ns != args.namespace:
        context.errors = getattr(context, "errors", [])
        context.errors.append(
            f"Webhook TARGET_NAMESPACE is '{target_ns}' but deployment namespace "
            f"is '{args.namespace}'. Update manifests.yaml and run: "
            "oc apply -f manifests.yaml"
        )
        return f"FAIL: TARGET_NAMESPACE={target_ns} != {args.namespace}"
    return f"OK: TARGET_NAMESPACE matches '{args.namespace}'"


@task
def check_mutating_webhook(args, context):
    result = shell.run(
        "oc get mutatingwebhookconfiguration env-injector-webhook "
        "-o jsonpath='{.webhooks[0].clientConfig.caBundle}'",
        check=False,
    )
    ca_bundle = result.stdout.strip().strip("'")
    if result.returncode != 0 or len(ca_bundle) < MIN_CA_BUNDLE_LENGTH:
        context.errors = getattr(context, "errors", [])
        context.errors.append(
            "MutatingWebhookConfiguration missing or caBundle is empty. "
            "Run: bash scripts/gen-certs.sh && bash scripts/patch-ca-bundle.sh"
        )
        return "FAIL: MutatingWebhookConfiguration missing or bad caBundle"
    return "OK: MutatingWebhookConfiguration has valid caBundle"


@task
def check_tls_secret(args, context):
    result = shell.run(
        f"oc get secret env-injector-certs -n {args.webhook_namespace} -o name",
        check=False,
    )
    if result.returncode != 0:
        context.errors = getattr(context, "errors", [])
        context.errors.append(
            f"TLS secret 'env-injector-certs' not found in {args.webhook_namespace} namespace"
        )
        return "FAIL: TLS secret missing"
    return "OK: TLS secret exists"


@task
def check_configmap(args, context):
    result = shell.run(
        f"oc get configmap env-injector-files -n {args.namespace} -o jsonpath='{{.data}}'",
        check=False,
    )
    if result.returncode != 0 or "sitecustomize.py" not in result.stdout:
        context.errors = getattr(context, "errors", [])
        context.errors.append(
            f"ConfigMap 'env-injector-files' not found in {args.namespace}. "
            "Create it with: oc apply -k <profiler-repo-dir>"
        )
        return f"FAIL: ConfigMap missing in {args.namespace}"
    return f"OK: ConfigMap exists in {args.namespace}"


@task
def raise_on_errors(args, context):
    errors = getattr(context, "errors", [])
    if errors:
        raise ValueError("Profiler prerequisites not met:\n  - " + "\n  - ".join(errors))
    return "All profiler prerequisites verified"


if __name__ == "__main__":
    run.main()
