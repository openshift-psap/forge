#!/usr/bin/env python3

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path

import yaml

from projects.core.dsl import entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import oc, oc_apply, oc_get_json

logger = logging.getLogger("TOOLBOX")

STARTUP_FAILURE_REASONS = {
    "CrashLoopBackOff",
    "CreateContainerConfigError",
    "CreateContainerError",
    "ErrImagePull",
    "ImagePullBackOff",
    "InvalidImageName",
    "RunContainerError",
}


@entrypoint
def run(
    *,
    namespace: str,
    service_manifest_path: str,
    leader_worker_set_manifest_path: str,
    pod_selector: str,
    timeout_seconds: int,
) -> str:
    """Deploy a Service and LeaderWorkerSet, then wait for availability."""
    ctx = execute_tasks(locals())
    if not getattr(ctx, "endpoint_url", None):
        raise RuntimeError("Failed to resolve the LeaderWorkerSet endpoint")
    return ctx.endpoint_url


@task
def load_and_validate_manifests(args, ctx):
    if not args.namespace or not args.pod_selector:
        raise ValueError("namespace and pod_selector are required")
    if type(args.timeout_seconds) is not int or args.timeout_seconds <= 0:
        raise ValueError("LWS readiness timeout must be a positive integer")

    service = _load_yaml(Path(args.service_manifest_path))
    lws = _load_yaml(Path(args.leader_worker_set_manifest_path))
    if not isinstance(service, dict) or service.get("kind") != "Service":
        raise ValueError("service_manifest_path must contain a Service")
    if not isinstance(lws, dict) or lws.get("kind") != "LeaderWorkerSet":
        raise ValueError("leader_worker_set_manifest_path must contain a LeaderWorkerSet")

    service_metadata = service.get("metadata")
    lws_metadata = lws.get("metadata")
    if (
        not isinstance(service_metadata, dict)
        or not isinstance(service_metadata.get("name"), str)
        or not service_metadata["name"]
    ):
        raise ValueError("Service manifest must define metadata.name")
    if (
        not isinstance(lws_metadata, dict)
        or not isinstance(lws_metadata.get("name"), str)
        or not lws_metadata["name"]
    ):
        raise ValueError("LeaderWorkerSet manifest must define metadata.name")
    service_spec = service.get("spec")
    ports = service_spec.get("ports") if isinstance(service_spec, dict) else None
    if (
        not isinstance(ports, list)
        or not ports
        or not isinstance(ports[0], dict)
        or type(ports[0].get("port")) is not int
        or not 1 <= ports[0]["port"] <= 65535
    ):
        raise ValueError("Service manifest must define a valid spec.ports[0].port")

    service_metadata["namespace"] = args.namespace
    lws_metadata["namespace"] = args.namespace
    ctx.service = service
    ctx.lws = lws
    ctx.service_name = service_metadata["name"]
    ctx.lws_name = lws_metadata["name"]
    ctx.port = ports[0]["port"]
    return f"Validated Service {ctx.service_name} and LeaderWorkerSet {ctx.lws_name}"


@task
def apply_service(args, ctx):
    oc_apply(Path(args.service_manifest_path), ctx.service)
    return f"Applied Service {ctx.service_name}"


@task
def apply_leader_worker_set(args, ctx):
    oc_apply(Path(args.leader_worker_set_manifest_path), ctx.lws)
    return f"Applied LeaderWorkerSet {ctx.lws_name}"


@task
def wait_for_leader_worker_set(args, ctx):
    deadline = time.monotonic() + args.timeout_seconds
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        wait_seconds = max(1, min(30, int(remaining)))
        result = oc(
            "wait",
            f"leaderworkerset/{ctx.lws_name}",
            "-n",
            args.namespace,
            "--for=condition=Available",
            f"--timeout={wait_seconds}s",
            timeout_seconds=wait_seconds + 15,
            check=False,
            log_stdout=False,
            log_stderr=False,
        )
        if result.returncode == 0:
            return f"LeaderWorkerSet {ctx.lws_name} is Available"

        stderr = result.stderr or ""
        if "timed out" not in stderr.lower():
            raise RuntimeError(
                f"Waiting for LeaderWorkerSet {ctx.lws_name} failed: {stderr.strip()}"
            )

        try:
            pods = oc_get_json(
                "pods",
                namespace=args.namespace,
                selector=args.pod_selector,
            )
        except (ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            logger.warning(
                "Could not inspect pods for LeaderWorkerSet %s during readiness polling; retrying: %s",
                ctx.lws_name,
                exc,
            )
            continue

        for pod in (pods or {}).get("items", []):
            pod_name = pod.get("metadata", {}).get("name", "unknown")
            status = pod.get("status", {})
            container_statuses = (status.get("initContainerStatuses") or []) + (
                status.get("containerStatuses") or []
            )
            for container in container_statuses:
                reason = container.get("state", {}).get("waiting", {}).get("reason")
                if reason in STARTUP_FAILURE_REASONS:
                    raise RuntimeError(
                        f"LeaderWorkerSet {ctx.lws_name} pod {pod_name} "
                        f"container {container.get('name', 'unknown')} is {reason}; "
                        "see captured pod logs"
                    )

    raise RuntimeError(
        f"LeaderWorkerSet {ctx.lws_name} did not become Available within {args.timeout_seconds}s"
    )


@task
def resolve_endpoint(args, ctx):
    ctx.endpoint_url = f"http://{ctx.service_name}.{args.namespace}.svc.cluster.local:{ctx.port}"
    return f"Resolved LeaderWorkerSet endpoint {ctx.endpoint_url}"


def attach_model_cache_pvc(lws: dict, volume_name: str, pvc_name: str) -> None:
    templates = lws.get("spec", {}).get("leaderWorkerTemplate")
    if not isinstance(templates, dict):
        raise ValueError("LeaderWorkerSet must define spec.leaderWorkerTemplate")

    pod_templates = [templates.get("workerTemplate")]
    if templates.get("leaderTemplate") is not None:
        pod_templates.append(templates["leaderTemplate"])

    for pod_template in pod_templates:
        if not isinstance(pod_template, dict) or not isinstance(pod_template.get("spec"), dict):
            raise ValueError("LeaderWorkerSet pod templates must define spec")
        volumes = pod_template["spec"].get("volumes", [])
        if not isinstance(volumes, list):
            raise ValueError("LeaderWorkerSet pod spec.volumes must be a list")
        matches = [volume for volume in volumes if volume.get("name") == volume_name]
        if len(matches) != 1:
            raise ValueError(
                f"LeaderWorkerSet must define exactly one {volume_name!r} model volume"
            )
        matches[0].clear()
        matches[0].update({"name": volume_name, "persistentVolumeClaim": {"claimName": pvc_name}})


def replace_resource_name(manifest: dict, source: str, target: str) -> None:
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?/[A-Za-z0-9][-A-Za-z0-9_.]*", target):
        raise ValueError(f"Invalid extended resource name: {target!r}")
    replacements = 0

    def visit(value):
        nonlocal replacements
        if isinstance(value, list):
            for child in value:
                visit(child)
            return
        if not isinstance(value, dict):
            return

        resources = value.get("resources")
        if isinstance(resources, dict):
            for section in ("requests", "limits"):
                quantities = resources.get(section)
                if isinstance(quantities, dict) and source in quantities:
                    if target in quantities and target != source:
                        raise ValueError(f"Resource block already defines {target}")
                    quantities[target] = quantities.pop(source)
                    replacements += 1
        for child in value.values():
            visit(child)

    visit(manifest)
    if not replacements:
        raise ValueError(f"Recipe does not request resource {source!r}")
    logger.info("Replaced %s with %s in %d resource entries", source, target, replacements)


def _load_yaml(path: Path):
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)
