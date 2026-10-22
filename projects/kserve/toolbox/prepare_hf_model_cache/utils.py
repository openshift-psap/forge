from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from projects.core.dsl import template
from projects.core.dsl.utils import slugify_identifier, truncate_k8s_name
from projects.core.dsl.utils.k8s import oc, oc_get_json


def build_model_cache_spec(
    *,
    namespace: str,
    model_key: str,
    model_uri: str,
    pvc_size: str,
    access_mode: str,
    storage_class_name: str | None,
    pvc_name_prefix: str,
    model_directory_name: str,
    marker_filename: str,
) -> dict[str, Any]:
    """Build the shared PVC cache contract for one Hugging Face model."""
    if not model_uri.startswith("hf://"):
        raise ValueError(f"Expected HF model URI, got: {model_uri}")

    cache_key = hashlib.sha256(model_uri.encode("utf-8")).hexdigest()[:10]
    pvc_name = truncate_k8s_name(
        f"{pvc_name_prefix}-{slugify_identifier(model_key, max_length=32)}-{cache_key}"
    )
    model_path = model_directory_name
    return {
        "source_uri": model_uri,
        "source_scheme": "hf",
        "cache_key": cache_key,
        "namespace": namespace,
        "pvc_name": pvc_name,
        "pvc_size": pvc_size,
        "access_mode": access_mode,
        "storage_class_name": storage_class_name,
        "model_path": model_path,
        "model_uri": f"pvc://{pvc_name}/{model_path}",
        "marker_filename": marker_filename,
        "marker_path": f"/cache/{model_path}/{marker_filename}",
        "download_job_name": truncate_k8s_name(f"{pvc_name}-download"),
        "hf_token_secret_name": None,
        "hf_token_secret_key": "token",
        "oci_image_path": None,
        "oci_registry_auth_secret_name": None,
        "oci_registry_auth_secret_key": None,
    }


def pvc_access_mode_matches(actual_modes: list[str], expected_mode: str) -> bool:
    return expected_mode in actual_modes


def annotate_model_cache_pvc(spec: dict[str, Any]) -> None:
    # Add annotations with metadata
    oc(
        "annotate",
        "persistentvolumeclaim",
        spec["pvc_name"],
        "-n",
        spec["namespace"],
        f"forge.openshift.io/model-cache-key={spec['cache_key']}",
        f"forge.openshift.io/model-source-uri={spec['source_uri']}",
        f"forge.openshift.io/model-source-scheme={spec['source_scheme']}",
        "--overwrite",
    )

    # Add labels indicating the content has been populated
    oc(
        "label",
        "persistentvolumeclaim",
        spec["pvc_name"],
        "-n",
        spec["namespace"],
        "forge.openshift.io/model-cache-populated=true",
        f"forge.openshift.io/model-cache-scheme={spec['source_scheme']}",
        "--overwrite",
    )


def model_cache_pvc_ready(spec: dict[str, Any]) -> bool:
    """Check if the model cache PVC is ready (has populated label and marker file)."""

    # Query directly for the populated label using oc command
    label_check = oc(
        "get",
        "pvc",
        spec["pvc_name"],
        "-n",
        spec["namespace"],
        "-o",
        "jsonpath={.metadata.labels.forge\\.openshift\\.io/model-cache-populated}",
        check=False,
    )

    # If command failed (PVC doesn't exist) or label is not "true", return False
    if label_check.returncode != 0 or label_check.stdout.strip() != "true":
        return False

    # Also verify with the marker file check for extra safety
    if spec["source_scheme"] == "hf":
        return _hf_cache_ready(spec)

    return True


def _hf_cache_ready(spec: dict[str, Any]) -> bool:
    pod_spec = {
        "restartPolicy": "Never",
        "containers": [
            {
                "name": "check",
                "image": "registry.access.redhat.com/ubi9/ubi-minimal:9.5",
                "command": ["test", "-f", spec["marker_path"]],
                "volumeMounts": [{"name": "cache", "mountPath": "/cache"}],
            }
        ],
        "volumes": [
            {
                "name": "cache",
                "persistentVolumeClaim": {"claimName": spec["pvc_name"]},
            }
        ],
    }

    check_result = oc(
        "run",
        f"{spec['pvc_name']}-ready-check",
        "--image=registry.access.redhat.com/ubi9/ubi-minimal:9.5",
        f"--overrides={json.dumps(pod_spec)}",
        f"-n={spec['namespace']}",
        "--restart=Never",
        "--attach",
        "--quiet",
        check=False,
        log_stdout=False,
        log_stderr=False,
    )

    # Clean up the pod
    oc(
        "delete",
        "pod",
        f"{spec['pvc_name']}-ready-check",
        "-n",
        spec["namespace"],
        "--ignore-not-found=true",
        check=False,
        log_stdout=False,
        log_stderr=False,
    )

    # Check both return code and verify the command actually ran
    if check_result.returncode == 0:
        # Double-check by listing the directory contents to verify the file exists
        rendered_override = template.render_template(
            "verify_pod_override.yaml.j2",
            {
                "model_path": spec["model_path"],
                "pvc_name": spec["pvc_name"],
            },
        )
        override_data = yaml.safe_load(rendered_override)

        verify_result = oc(
            "run",
            f"{spec['pvc_name']}-verify-check",
            "--image=registry.access.redhat.com/ubi9/ubi-minimal:9.5",
            f"--overrides={json.dumps(override_data)}",
            f"-n={spec['namespace']}",
            "--restart=Never",
            "--attach",
            "--quiet",
            check=False,
            log_stdout=True,
            log_stderr=True,
        )

        # Clean up verification pod
        oc(
            "delete",
            "pod",
            f"{spec['pvc_name']}-verify-check",
            "-n",
            spec["namespace"],
            "--ignore-not-found=true",
            check=False,
            log_stdout=False,
            log_stderr=False,
        )

        # Check if marker file is actually listed
        marker_filename = spec["marker_filename"]
        file_listed = marker_filename in verify_result.stdout if verify_result.stdout else False

        if not file_listed:
            import logging

            logger = logging.getLogger(__name__)
            logger.warning(
                f"Cache check returned success but marker file {marker_filename} not found in directory listing"
            )
            logger.info(f"Directory contents: {verify_result.stdout}")
            return False

    return check_result.returncode == 0


def load_runtime_script(name: str) -> str:
    # Load scripts from the local toolbox scripts directory
    script_path = Path(__file__).resolve().parent / "scripts" / name
    return script_path.read_text(encoding="utf-8")


def render_model_cache_pvc(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": spec["pvc_name"],
            "namespace": spec["namespace"],
        },
        "spec": {
            "accessModes": [spec["access_mode"]],
            "resources": {"requests": {"storage": spec["pvc_size"]}},
            **(
                {"storageClassName": spec["storage_class_name"]}
                if spec["storage_class_name"]
                else {}
            ),
        },
    }


def render_hf_model_cache_job(
    args, spec: dict[str, Any], hf_secret_name: str | None = None
) -> dict[str, Any]:
    common_env = [
        {"name": "MODEL_SOURCE", "value": spec["source_uri"]},
        {"name": "MODEL_TARGET_DIR", "value": f"/cache/{spec['model_path']}"},
        {"name": "MARKER_FILE", "value": spec["marker_path"]},
        {"name": "CACHE_KEY", "value": spec["cache_key"]},
    ]

    volume_mounts = [{"name": "cache", "mountPath": "/cache"}]
    volumes = [
        {
            "name": "cache",
            "persistentVolumeClaim": {"claimName": spec["pvc_name"]},
        }
    ]

    # Add HF token secret if available (either passed or from spec)
    effective_secret_name = hf_secret_name or spec["hf_token_secret_name"]
    if effective_secret_name:
        common_env.append(
            {
                "name": "HF_TOKEN_FILE",
                "value": "/secrets/hf-token/token",  # Use 'token' key for vault-created secrets
            }
        )
        volume_mounts.append(
            {
                "name": "hf-token",
                "mountPath": "/secrets/hf-token",
                "readOnly": True,
            }
        )
        volumes.append(
            {
                "name": "hf-token",
                "secret": {"secretName": effective_secret_name},
            }
        )

    command = load_runtime_script("download_hf_model.sh")

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": spec["download_job_name"],
            "namespace": spec["namespace"],
        },
        "spec": {
            "ttlSecondsAfterFinished": 300,  # Auto-cleanup completed jobs after 5 minutes
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "download",
                            "image": args.downloader_image,
                            "imagePullPolicy": args.pod_image_pull_policy,
                            "command": ["/bin/bash", "-c", command],
                            "env": common_env,
                            "volumeMounts": volume_mounts,
                        }
                    ],
                    "volumes": volumes,
                }
            },
        },
    }


def job_pod_names(job_name: str, namespace: str) -> list[str]:
    """Get the names of pods created by a Job.

    Args:
        job_name: Job name
        namespace: Namespace

    Returns:
        List of pod names
    """
    payload = oc_get_json(
        "pods",
        namespace=namespace,
        selector=f"job-name={job_name}",
        ignore_not_found=True,
    )
    if not payload:
        return []
    return [item["metadata"]["name"] for item in payload.get("items", [])]
