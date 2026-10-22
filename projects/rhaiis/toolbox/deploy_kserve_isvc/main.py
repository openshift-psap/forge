#!/usr/bin/env python3

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from projects.core.dsl import (
    entrypoint,
    execute_tasks,
    task,
)
from projects.core.dsl.utils.k8s import oc, oc_resource_exists


@entrypoint
def run(
    *,
    namespace: str,
    servingruntime_file: str,
    inferenceservice_file: str,
):
    """Deploy a KServe InferenceService from pre-rendered YAML files.

    Args:
        namespace: Target namespace for the deployment.
        servingruntime_file: Path to a ServingRuntime YAML file.
        inferenceservice_file: Path to an InferenceService YAML file.
    """
    return execute_tasks(locals())


@task
def capture_source_files(args, context):
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    context.servingruntime_src = src_dir / Path(args.servingruntime_file).name
    shutil.copy2(args.servingruntime_file, context.servingruntime_src)

    context.inferenceservice_src = src_dir / Path(args.inferenceservice_file).name
    shutil.copy2(args.inferenceservice_file, context.inferenceservice_src)

    return f"Copied source files to {src_dir}"


@task
def ensure_namespace(args, context):
    if oc_resource_exists("namespace", args.namespace):
        return f"Namespace {args.namespace} exists"
    oc("create", "namespace", args.namespace)
    return f"Created namespace {args.namespace}"


@task
def create_servingruntime(args, context):
    oc("create", "-f", str(context.servingruntime_src))
    with open(context.servingruntime_src) as f:
        name = yaml.safe_load(f).get("metadata", {}).get("name", "")
    return f"Created ServingRuntime {name}"


@task
def create_inferenceservice(args, context):
    oc("create", "-f", str(context.inferenceservice_src))
    with open(context.inferenceservice_src) as f:
        name = yaml.safe_load(f).get("metadata", {}).get("name", "")
    return f"Created InferenceService {name}"


if __name__ == "__main__":
    run.main()
