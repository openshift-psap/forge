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
    retry,
    shell,
    task,
)

DEFAULT_IMAGE = "quay.io/mimehta/vllmprofiler"
INJECTOR_NAMESPACE = "vllm-profiler"
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
    image: str = DEFAULT_IMAGE,
    target_labels: str = DEFAULT_TARGET_LABELS,
    profiling_ranges: str = "500-503",
    inject_env_name: str = "PYTHONPATH",
    inject_env_value: str = "/home/vllm/profiler",
    labels: dict[str, str] | None = None,
):
    """Deploy the vLLM profiler env-injector.

    Args:
        namespace: Target namespace of the profiled pods (the ConfigMap is
            created here and the webhook only injects into this namespace).
        image: env-injector container image.
        target_labels: Labels a pod must carry to be injected.
        profiling_ranges: Forward-pass ranges written into profiler_config.yaml.
        inject_env_name: Environment variable used to inject the profiler path.
        inject_env_value: Value of the injection environment variable.
        labels: Ownership labels applied to the created resources. Resources
            that already exist (deployed by someone else) are reused but not
            labelled, so a matching teardown never removes them.
    """
    return execute_tasks(locals())


def _render_labels(labels: dict[str, str] | None) -> str:
    """Render ownership labels as separate oc label arguments (k=v k=v).

    Each key=value must be its own argument: oc label rejects a single
    comma-joined spec ("invalid label spec").
    """
    if not labels:
        return ""
    return " ".join(f"{key}={value}" for key, value in sorted(labels.items()))


@task
def ensure_injector_namespace(args, context):
    """Create the injector namespace, labelled, in one atomic apply.

    Creating via a labelled manifest (instead of create-then-label) closes
    the crash window that would otherwise leave an unlabelled namespace no
    later run adopts and no teardown removes.
    """
    result = shell.run(f"oc get namespace {INJECTOR_NAMESPACE}", check=False)
    if result.returncode == 0:
        return f"Namespace {INJECTOR_NAMESPACE} already exists"

    manifest = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": INJECTOR_NAMESPACE,
            "labels": dict(args.labels or {}),
        },
    }
    shell.run("oc apply -f -", input_text=yaml.safe_dump(manifest))
    return f"Created namespace {INJECTOR_NAMESPACE}"


@task
def ensure_service_account(args, context):
    """Create the service account, labelled, in one atomic apply."""
    result = shell.run(f"oc get serviceaccount env-injector -n {INJECTOR_NAMESPACE}", check=False)
    if result.returncode == 0:
        return "Serviceaccount env-injector already exists"

    manifest = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": "env-injector",
            "namespace": INJECTOR_NAMESPACE,
            "labels": dict(args.labels or {}),
        },
    }
    shell.run("oc apply -f -", input_text=yaml.safe_dump(manifest))
    return "Created serviceaccount env-injector"


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


def _generate_tls_certificate() -> str:
    """Generate a CA and serving certificate; create the TLS secret.

    Mirrors gen-certs.sh from openshift-psap/vllm-profiler: a self-signed
    CA (10 years) signs a serving certificate (1 year) carrying the service
    DNS names, serverAuth usage, and key encipherment. Generated with the
    cryptography library because the forge image ships no openssl CLI.

    The private key is written only to a temporary directory outside the
    artifact directory and never logged. Returns the CA certificate PEM,
    base64-encoded for the webhook caBundle.
    """
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.datetime.now(datetime.UTC)

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "env-injector-ca")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    dns_names = [
        f"env-injector.{INJECTOR_NAMESPACE}.svc",
        f"env-injector.{INJECTOR_NAMESPACE}.svc.cluster.local",
    ]
    tls_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    tls_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_names[0])]))
        .issuer_name(ca_subject)
        .public_key(tls_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=True,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in dns_names]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    with tempfile.TemporaryDirectory(prefix="profiler-injector-certs-") as cert_dir:
        cert_dir = Path(cert_dir)
        tls_crt = cert_dir / "tls.crt"
        tls_key_path = cert_dir / "tls.key"
        tls_crt.write_bytes(tls_cert.public_bytes(serialization.Encoding.PEM))
        tls_key_path.write_bytes(
            tls_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        shell.run(
            f"oc create secret tls {CERT_SECRET_NAME} "
            f"-n {INJECTOR_NAMESPACE} --cert={tls_crt} --key={tls_key_path}"
        )

    return base64.b64encode(ca_cert.public_bytes(serialization.Encoding.PEM)).decode()


@task
def ensure_tls_certificate(args, context):
    """Create the TLS secret on first run; reuse an existing CA bundle when valid."""
    secret = shell.run(
        f"oc get secret {CERT_SECRET_NAME} -n {INJECTOR_NAMESPACE} -o name",
        check=False,
    )
    existing_ca = _webhook_ca_bundle()

    if secret.returncode == 0 and existing_ca:
        return "TLS secret and webhook CA bundle already in place"

    # Self-heal: an existing secret without a valid webhook CA bundle cannot
    # be recovered (the CA is not stored in the secret), so regenerate.
    if secret.returncode == 0:
        shell.run(f"oc delete secret {CERT_SECRET_NAME} -n {INJECTOR_NAMESPACE}")
        logger_note = "regenerated TLS certificate (webhook CA bundle was invalid)"
    else:
        logger_note = "generated TLS certificate"

    context.ca_bundle = _generate_tls_certificate()
    if args.labels:
        shell.run(
            f"oc label secret {CERT_SECRET_NAME} -n {INJECTOR_NAMESPACE} "
            f"{_render_labels(args.labels)}"
        )
    return f"{logger_note}; webhook CA bundle prepared"


@task
def deploy_webhook_configuration(args, context):
    ca_bundle = getattr(context, "ca_bundle", None) or _webhook_ca_bundle()
    if not ca_bundle:
        raise RuntimeError(
            "No CA bundle available for the webhook configuration; "
            "the TLS certificate task must have failed"
        )

    webhook_manifest = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "MutatingWebhookConfiguration",
        "metadata": {"name": WEBHOOK_NAME, "labels": dict(args.labels or {})},
        "webhooks": [
            {
                "name": "env-injector.vllmbench.local",
                "admissionReviewVersions": ["v1"],
                "clientConfig": {
                    "caBundle": ca_bundle,
                    "service": {
                        "name": "env-injector",
                        "namespace": INJECTOR_NAMESPACE,
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
    shell.run("oc apply -f -", input_text=yaml.safe_dump(webhook_manifest))
    return f"Applied MutatingWebhookConfiguration {WEBHOOK_NAME}"


@task
def deploy_injector_deployment(args, context):
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "env-injector",
            "namespace": INJECTOR_NAMESPACE,
            "labels": dict(args.labels or {}),
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
            "namespace": INJECTOR_NAMESPACE,
            "labels": dict(args.labels or {}),
        },
        "spec": {
            "selector": {"app": "env-injector"},
            "ports": [{"port": 443, "targetPort": "https", "protocol": "TCP"}],
        },
    }
    shell.run("oc apply -f -", input_text=yaml.safe_dump(deployment))
    shell.run("oc apply -f -", input_text=yaml.safe_dump(service))
    return f"Applied env-injector Deployment and Service in {INJECTOR_NAMESPACE}"


@task
def deploy_profiler_configmap(args, context):
    """Create the env-injector-files ConfigMap in the target namespace.

    Reuses an existing ConfigMap only when it is absent or carries the
    ownership labels; a foreign ConfigMap (e.g. from another injector
    instance targeting the same namespace) is left untouched.
    """
    if args.labels:
        result = shell.run(
            f"oc get configmap {CONFIGMAP_NAME} -n {args.namespace} "
            "-o jsonpath='{.metadata.labels}'",
            check=False,
        )
        if result.returncode == 0:
            import json as _json

            try:
                existing = _json.loads(result.stdout.strip().strip("'") or "{}")
            except ValueError:
                existing = {}
            if not isinstance(existing, dict) or not all(
                existing.get(key) == value for key, value in args.labels.items()
            ):
                return (
                    f"ConfigMap {CONFIGMAP_NAME} in {args.namespace} not owned by "
                    "this deployment, reusing as-is"
                )
    sitecustomize = (_TEMPLATES_DIR / "sitecustomize.py").read_text()
    profiler_config = (_TEMPLATES_DIR / "profiler_config.yaml").read_text()
    profiler_config = profiler_config.replace(
        'profiling_ranges: "500-503"', f'profiling_ranges: "{args.profiling_ranges}"'
    )

    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": CONFIGMAP_NAME,
            "namespace": args.namespace,
            "labels": dict(args.labels or {}),
        },
        "data": {
            "sitecustomize.py": sitecustomize,
            "profiler_config.yaml": profiler_config,
        },
    }
    shell.run("oc apply -f -", input_text=yaml.safe_dump(configmap))
    return f"Applied ConfigMap {CONFIGMAP_NAME} in {args.namespace}"


@retry(attempts=30, delay=10, backoff=1.0, retry_on_exceptions=True)
@task
def wait_for_injector_pod(args, context):
    result = shell.run(
        f"oc get pods -n {INJECTOR_NAMESPACE} -l app=env-injector "
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
