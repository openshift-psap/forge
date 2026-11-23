#!/usr/bin/env python3
"""Deploy the vLLM profiler env-injector on the target cluster.

Vendors the injector deployment quay.io/mimehta/vllmprofiler: a mutating 
admission webhook that injects the profiler ``sitecustomize.py`` into 
pods matching the target labels in the target namespace, plus the 
per-namespace ConfigMap holding the profiler configuration.

Deploys into a dedicated injector namespace (default ``vllm-profiler``):

- ServiceAccount, TLS serving certificate and key (generated on first run
  with openssl; the private key never leaves the cluster or the local
  temp directory, and is never written to the artifact directory)
- Deployment + Service for the webhook server
- MutatingWebhookConfiguration with the CA bundle

and into the *target* namespace:

- ConfigMap ``env-injector-files`` with ``sitecustomize.py`` and the
  templated ``profiler_config.yaml``

Idempotent: an existing TLS secret and webhook CA bundle are reused;
``verify_profiler_prereqs`` (kserve toolbox) validates the result.
"""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path

import yaml

from projects.core.dsl import (
    entrypoint,
    execute_tasks,
    shell,
    task,
)

DEFAULT_IMAGE = "quay.io/mimehta/vllmprofiler"
DEFAULT_INJECTOR_NAMESPACE = "vllm-profiler"
DEFAULT_TARGET_LABELS = "vllm-profiler/enabled=true"
WEBHOOK_NAME = "env-injector-webhook"
WEBHOOK_SERVICE_PATH = "/mutate"
CERT_SECRET_NAME = "env-injector-certs"
CONFIGMAP_NAME = "env-injector-files"
MIN_CA_BUNDLE_LENGTH = 100

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


@entrypoint
def run(
    *,
    namespace: str,
    injector_namespace: str = DEFAULT_INJECTOR_NAMESPACE,
    image: str = DEFAULT_IMAGE,
    target_labels: str = DEFAULT_TARGET_LABELS,
    profiling_ranges: str = "500-503",
    inject_env_name: str = "PYTHONPATH",
    inject_env_value: str = "/home/vllm/profiler",
):
    """Deploy the vLLM profiler env-injector.

    Args:
        namespace: Target namespace of the profiled pods (the ConfigMap is
            created here and the webhook only injects into this namespace).
        injector_namespace: Namespace hosting the webhook deployment.
        image: env-injector container image.
        target_labels: Labels a pod must carry to be injected.
        profiling_ranges: Forward-pass ranges written into profiler_config.yaml.
        inject_env_name: Environment variable used to inject the profiler path.
        inject_env_value: Value of the injection environment variable.
    """
    return execute_tasks(locals())


@task
def ensure_injector_namespace(args, context):
    result = shell.run(f"oc get namespace {args.injector_namespace}", check=False)
    if result.returncode != 0:
        shell.run(f"oc create namespace {args.injector_namespace}")
        return f"Created namespace {args.injector_namespace}"
    return f"Namespace {args.injector_namespace} already exists"


@task
def ensure_service_account(args, context):
    result = shell.run(
        f"oc get serviceaccount env-injector -n {args.injector_namespace}", check=False
    )
    if result.returncode != 0:
        shell.run(f"oc create serviceaccount env-injector -n {args.injector_namespace}")
        return "Created serviceaccount env-injector"
    return "Serviceaccount env-injector already exists"


def _webhook_ca_bundle() -> str | None:
    """Return the existing webhook caBundle if it is present and plausible."""
    result = shell.run(
        f"oc get mutatingwebhookconfiguration {WEBHOOK_NAME} "
        "-o jsonpath='{.webhooks[0].clientConfig.caBundle}'",
        check=False,
    )
    ca_bundle = result.stdout.strip().strip("'")
    if result.returncode != 0 or len(ca_bundle) < MIN_CA_BUNDLE_LENGTH:
        return None
    return ca_bundle


def _generate_tls_certificate(injector_namespace: str) -> str:
    """Generate a CA and serving certificate; create the TLS secret.

    The private key is created in a temporary directory outside the
    artifact directory and never logged. Returns the CA certificate
    PEM, base64-encoded for the webhook caBundle.
    """
    with tempfile.TemporaryDirectory(prefix="profiler-injector-certs-") as cert_dir:
        cert_dir = Path(cert_dir)
        ca_key = cert_dir / "ca.key"
        ca_crt = cert_dir / "ca.crt"
        tls_key = cert_dir / "tls.key"
        tls_csr = cert_dir / "tls.csr"
        tls_crt = cert_dir / "tls.crt"
        ext_file = cert_dir / "ext.cnf"

        shell.run(
            f"openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes "
            f"-keyout {ca_key} -out {ca_crt} -subj '/CN=env-injector-ca'"
        )
        ext_file.write_text(
            "[ req ]\n"
            "distinguished_name = dn\n"
            "req_extensions = v3_req\n"
            "[ dn ]\n"
            f"CN = env-injector.{injector_namespace}.svc\n"
            "[ v3_req ]\n"
            "keyUsage = keyEncipherment, dataEncipherment\n"
            "extendedKeyUsage = serverAuth\n"
            "subjectAltName = @alt_names\n"
            "[ alt_names ]\n"
            f"DNS.1 = env-injector.{injector_namespace}.svc\n"
            f"DNS.2 = env-injector.{injector_namespace}.svc.cluster.local\n"
        )
        shell.run(f"openssl genrsa -out {tls_key} 2048")
        shell.run(f"openssl req -new -key {tls_key} -out {tls_csr} -config {ext_file}")
        shell.run(
            f"openssl x509 -req -in {tls_csr} -CA {ca_crt} -CAkey {ca_key} "
            f"-CAcreateserial -out {tls_crt} -days 365 -sha256 "
            f"-extensions v3_req -extfile {ext_file}"
        )

        shell.run(
            f"oc create secret tls {CERT_SECRET_NAME} "
            f"-n {injector_namespace} --cert={tls_crt} --key={tls_key}"
        )
        return base64.b64encode(ca_crt.read_bytes()).decode()


@task
def ensure_tls_certificate(args, context):
    """Create the TLS secret on first run; reuse an existing CA bundle when valid."""
    secret = shell.run(
        f"oc get secret {CERT_SECRET_NAME} -n {args.injector_namespace} -o name",
        check=False,
    )
    existing_ca = _webhook_ca_bundle()

    if secret.returncode == 0 and existing_ca:
        return "TLS secret and webhook CA bundle already in place"

    # Self-heal: an existing secret without a valid webhook CA bundle cannot
    # be recovered (the CA is not stored in the secret), so regenerate.
    if secret.returncode == 0:
        shell.run(f"oc delete secret {CERT_SECRET_NAME} -n {args.injector_namespace}")
        logger_note = "regenerated TLS certificate (webhook CA bundle was invalid)"
    else:
        logger_note = "generated TLS certificate"

    context.ca_bundle = _generate_tls_certificate(args.injector_namespace)
    return f"{logger_note}; webhook CA bundle prepared"


@task
def deploy_webhook_configuration(args, context):
    ca_bundle = getattr(context, "ca_bundle", None) or _webhook_ca_bundle()
    if not ca_bundle:
        raise RuntimeError(
            "No CA bundle available for the webhook configuration; "
            "the TLS certificate task must have failed"
        )

    webhook = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "MutatingWebhookConfiguration",
        "metadata": {"name": WEBHOOK_NAME},
        "webhooks": [
            {
                "name": "env-injector.vllmbench.local",
                "admissionReviewVersions": ["v1"],
                "clientConfig": {
                    "caBundle": ca_bundle,
                    "service": {
                        "name": "env-injector",
                        "namespace": args.injector_namespace,
                        "path": WEBHOOK_SERVICE_PATH,
                        "port": 443,
                    },
                },
                "failurePolicy": "Ignore",
                "reinvocationPolicy": "IfNeeded",
                "sideEffects": "None",
                "timeoutSeconds": 5,
                "rules": [
                    {
                        "apiGroups": [""],
                        "apiVersions": ["v1"],
                        "operations": ["CREATE"],
                        "resources": ["pods"],
                        "scope": "*",
                    }
                ],
            }
        ],
    }
    shell.run("oc apply -f -", input_text=yaml.safe_dump(webhook))
    return f"Applied MutatingWebhookConfiguration {WEBHOOK_NAME}"


@task
def deploy_injector_deployment(args, context):
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "env-injector",
            "namespace": args.injector_namespace,
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "env-injector"}},
            "template": {
                "metadata": {"labels": {"app": "env-injector"}},
                "spec": {
                    "serviceAccountName": "env-injector",
                    "containers": [
                        {
                            "name": "webhook",
                            "image": args.image,
                            "imagePullPolicy": "Always",
                            "env": [
                                {"name": "TARGET_NAMESPACE", "value": args.namespace},
                                {"name": "TARGET_LABELS", "value": args.target_labels},
                                {"name": "INJECT_ENV_NAME", "value": args.inject_env_name},
                                {"name": "INJECT_ENV_VALUE", "value": args.inject_env_value},
                            ],
                            "ports": [{"containerPort": 8443, "name": "https", "protocol": "TCP"}],
                            "volumeMounts": [
                                {"mountPath": "/tls", "name": "tls", "readOnly": True}
                            ],
                        }
                    ],
                    "volumes": [{"name": "tls", "secret": {"secretName": CERT_SECRET_NAME}}],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": "env-injector",
            "namespace": args.injector_namespace,
        },
        "spec": {
            "selector": {"app": "env-injector"},
            "ports": [{"port": 443, "targetPort": "https", "protocol": "TCP"}],
        },
    }
    shell.run("oc apply -f -", input_text=yaml.safe_dump(deployment))
    shell.run("oc apply -f -", input_text=yaml.safe_dump(service))
    return f"Applied env-injector Deployment and Service in {args.injector_namespace}"


@task
def deploy_profiler_configmap(args, context):
    """Create the env-injector-files ConfigMap in the target namespace."""
    sitecustomize = (_TEMPLATES_DIR / "sitecustomize.py").read_text()
    profiler_config = (_TEMPLATES_DIR / "profiler_config.yaml").read_text()
    profiler_config = profiler_config.replace(
        'profiling_ranges: "500-503"', f'profiling_ranges: "{args.profiling_ranges}"'
    )

    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": CONFIGMAP_NAME, "namespace": args.namespace},
        "data": {
            "sitecustomize.py": sitecustomize,
            "profiler_config.yaml": profiler_config,
        },
    }
    shell.run("oc apply -f -", input_text=yaml.safe_dump(configmap))
    return f"Applied ConfigMap {CONFIGMAP_NAME} in {args.namespace}"


@task
def wait_for_injector_pod(args, context):
    result = shell.run(
        f"oc get pods -n {args.injector_namespace} -l app=env-injector "
        "-o jsonpath='{.items[0].status.phase}'",
        check=False,
    )
    phase = result.stdout.strip().strip("'")
    if result.returncode != 0 or phase != "Running":
        raise RuntimeError(
            f"env-injector pod not running yet (status: {phase or 'not found'}); "
            "check the deployment in the injector namespace"
        )
    return "OK: env-injector pod is Running"
