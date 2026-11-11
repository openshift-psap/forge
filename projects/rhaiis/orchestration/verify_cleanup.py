#!/usr/bin/env python3
"""Verify RHAIIS target resources are gone before Fournos releases quota.

The Tekton Task runs this file in a separate step from its deployed image,
after the step that may check out PR code. Keep this module standalone.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import yaml

FOURNOS_JOB_LABEL = "fournos.dev/job-name"
OC_BINARY = "oc"
RESOURCE_TYPES = (
    "inferenceservice",
    "servingruntime",
    "job",
    "pod",
    "pvc",
    "deployment",
    "replicaset",
    "service",
    "secret",
)
MANIFESTS = {
    "inferenceservice.yaml": ("InferenceService", "inferenceservice"),
    "servingruntime.yaml": ("ServingRuntime", "servingruntime"),
    "guidellm-job.yaml": ("Job", "job"),
    "guidellm-pvc.yaml": ("PersistentVolumeClaim", "pvc"),
    "guidellm-copy-pod.yaml": ("Pod", "pod"),
}


def _oc_names(*args: str) -> list[str]:
    """Return resource names only; never expose object bodies or stderr."""
    kind = args[1] if len(args) > 1 else "resource"
    try:
        result = subprocess.run(
            [OC_BINARY, *args], capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not verify {kind} resources on the target cluster") from exc
    if result.returncode != 0:
        raise RuntimeError(f"Could not verify {kind} resources on the target cluster")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _manifest_resources(artifacts: Path) -> set[tuple[str, str, str]]:
    """Use exact identities recorded before resource creation."""
    resources: set[tuple[str, str, str]] = set()
    for path in artifacts.rglob("*.yaml"):
        expected = MANIFESTS.get(path.name)
        if path.parent.name != "src" or expected is None:
            continue
        try:
            manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
            metadata = manifest["metadata"]
            if manifest["kind"] != expected[0]:
                raise ValueError("unexpected kind")
            name, namespace = metadata["name"], metadata["namespace"]
            if not isinstance(name, str) or not isinstance(namespace, str):
                raise ValueError("invalid metadata")
        except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
            raise RuntimeError(f"Could not read {path.name} for cleanup verification") from exc
        resources.add((expected[1], namespace, name))
    return resources


def _name_matches_job(name: str, prefixes: set[str]) -> bool:
    leaf = name.rsplit("/", 1)[-1]
    return any(leaf.startswith(prefix) for prefix in prefixes)


def verify_cleanup(artifacts: Path, fjob: str, *, test_started: bool = False) -> None:
    if not artifacts.is_dir() or not fjob:
        raise ValueError("Artifact workspace and FournosJob name are required")

    resources = _manifest_resources(artifacts)
    remaining: set[str] = set()

    for kind, namespace, name in resources:
        if _oc_names("get", kind, name, "-n", namespace, "--ignore-not-found", "-o", "name"):
            remaining.add(kind)

    # Query every namespace because the target namespace is configurable.
    for kind in RESOURCE_TYPES:
        if _oc_names("get", kind, "-A", "-l", f"{FOURNOS_JOB_LABEL}={fjob}", "-o", "name"):
            remaining.add(kind)

    # The hub PipelineRun records whether 03-test started. Also use the
    # workspace directory as evidence if its TaskRun reference is delayed.
    # The hub evidence covers runs whose artifacts are unavailable.
    if not resources and (test_started or (artifacts / "03__test").is_dir()):
        raise RuntimeError(
            "RHAIIS cleanup cannot be verified: test wrote no known resource manifest"
        )

    # Generated KServe and Job descendants may not inherit the label.
    # Their parents' saved manifests give exact name prefixes to check.
    namespaces = {namespace for _, namespace, _ in resources}
    for namespace in namespaces:
        prefixes = {
            f"{name}-predictor"
            for kind, ns, name in resources
            if ns == namespace and kind == "inferenceservice"
        }
        prefixes.update(
            f"{name}-" for kind, ns, name in resources if ns == namespace and kind == "job"
        )
        for kind in RESOURCE_TYPES:
            if kind == "secret":
                continue
            names = _oc_names("get", kind, "-n", namespace, "-o", "name")
            if any(_name_matches_job(name, prefixes) for name in names):
                remaining.add(kind)

    # GuideLLM API key Secrets are derived from the saved Job name.
    for kind, namespace, name in resources:
        if kind != "job":
            continue
        if _oc_names(
            "get",
            "secret",
            f"{name}-auth-token",
            "-n",
            namespace,
            "--ignore-not-found",
            "-o",
            "name",
        ):
            remaining.add("secret")

    if remaining:
        raise RuntimeError(
            "RHAIIS remote cleanup is incomplete; remaining resource kinds: "
            + ", ".join(sorted(remaining))
        )


def main() -> None:
    global OC_BINARY
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", type=Path)
    parser.add_argument("fjob")
    parser.add_argument("oc_binary")
    parser.add_argument("test_started", choices=("true", "false"))
    args = parser.parse_args()
    if not Path(args.oc_binary).is_absolute():
        parser.error("oc_binary must be an absolute path")
    OC_BINARY = args.oc_binary
    try:
        verify_cleanup(args.artifacts, args.fjob, test_started=args.test_started == "true")
    except (RuntimeError, ValueError) as exc:
        # Keep YAML parser and oc stderr details out of the TaskRun log.
        parser.exit(1, f"RHAIIS cleanup not verified: {exc}\n")
    print(f"RHAIIS remote cleanup verified for {args.fjob}")


if __name__ == "__main__":
    main()
