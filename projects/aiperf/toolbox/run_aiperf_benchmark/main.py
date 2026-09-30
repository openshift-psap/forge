"""Run a pinned AI Perf image against an in-cluster inference endpoint."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

from projects.core.dsl import always, entrypoint, execute_tasks, retry, task
from projects.core.dsl.utils.k8s import oc, oc_apply, oc_get_json

logger = logging.getLogger(__name__)
DEFAULT_IMAGE = "quay.io/rh-ee-thibrahi/aiperf:0.12.0"
POLL_SECONDS = 10
SUMMARY_FILE = "profile_export_aiperf.json"


def build_profile_command(
    *,
    endpoint_url: str,
    model_name: str,
    tokenizer: str | None,
    endpoint_type: str,
    endpoint_path: str,
    streaming: bool,
    concurrency: int | None,
    request_rate: float | None,
    request_count: int | None,
    input_tokens: int | None,
    output_tokens: int | None,
    dataset_type: str | None,
    fixed_schedule: bool,
    fixed_schedule_auto_offset: bool,
) -> list[str]:
    """Build argv without a shell or credential-bearing command text."""
    if not endpoint_url or not model_name:
        raise ValueError("endpoint_url and model_name are required")
    parsed_url = urlsplit(endpoint_url)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.hostname:
        raise ValueError("endpoint_url must be an HTTP(S) URL")
    if parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
        raise ValueError("endpoint_url must not contain credentials or query parameters")
    if not endpoint_path.startswith("/"):
        raise ValueError("endpoint_path must start with /")
    if concurrency is not None and request_rate is not None:
        raise ValueError("Choose concurrency or request_rate, not both")
    if concurrency is None and request_rate is None and not fixed_schedule:
        raise ValueError("Set concurrency, request_rate, or fixed_schedule")
    for name, value in (
        ("concurrency", concurrency),
        ("request_count", request_count),
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
    ):
        if value is not None and value <= 0:
            raise ValueError(f"{name} must be positive")
    if request_rate is not None and request_rate <= 0:
        raise ValueError("request_rate must be positive")
    if fixed_schedule_auto_offset and not fixed_schedule:
        raise ValueError("fixed_schedule_auto_offset requires fixed_schedule")
    if dataset_type and input_tokens is not None:
        raise ValueError("input_tokens cannot be used with a trace dataset")

    command = [
        "aiperf",
        "profile",
        "--model",
        model_name,
        "--url",
        endpoint_url,
        "--endpoint-type",
        endpoint_type,
        "--endpoint",
        endpoint_path,
        "--artifact-dir",
        "/results/run",
        "--export-level",
        "summary",
        "--ui",
        "none",
    ]
    if tokenizer:
        command.extend(("--tokenizer", tokenizer))
    if streaming:
        command.append("--streaming")
    if concurrency is not None:
        command.extend(("--concurrency", str(concurrency)))
    if request_rate is not None:
        command.extend(("--request-rate", str(request_rate)))
    if request_count is not None:
        command.extend(("--request-count", str(request_count)))
    if input_tokens is not None:
        command.extend(("--synthetic-input-tokens-mean", str(input_tokens)))
    if output_tokens is not None:
        command.extend(("--output-tokens-mean", str(output_tokens)))
    if dataset_type:
        command.extend(
            ("--input-file", "/dataset/input.jsonl", "--custom-dataset-type", dataset_type)
        )
    if fixed_schedule:
        command.append("--fixed-schedule")
    if fixed_schedule_auto_offset:
        command.append("--fixed-schedule-auto-offset")
    return command


def build_job_manifest(
    *,
    name: str,
    namespace: str,
    image: str,
    pvc_name: str,
    command: list[str],
    timeout: int,
    image_pull_secret: str | None,
    dataset_url: str | None,
    dataset_sha256: str | None,
) -> dict:
    """Render a Job; dataset download is checksum verified before benchmarking."""
    volumes = [{"name": "results", "persistentVolumeClaim": {"claimName": pvc_name}}]
    mounts = [{"name": "results", "mountPath": "/results"}]
    pod_spec: dict = {
        "restartPolicy": "Never",
        "volumes": volumes,
        "containers": [
            {"name": "aiperf", "image": image, "command": command, "volumeMounts": mounts}
        ],
    }
    if image_pull_secret:
        pod_spec["imagePullSecrets"] = [{"name": image_pull_secret}]
    if dataset_url:
        volumes.append({"name": "dataset", "emptyDir": {}})
        mounts.append({"name": "dataset", "mountPath": "/dataset"})
        script = (
            "import hashlib,shutil,urllib.request\n"
            f"url={dataset_url!r}\n"
            "with urllib.request.urlopen(url, timeout=120) as response, "
            "open('/dataset/input.jsonl','wb') as target:\n"
            "  shutil.copyfileobj(response, target)\n"
            "digest=hashlib.sha256()\n"
            "with open('/dataset/input.jsonl','rb') as source:\n"
            "  for chunk in iter(lambda: source.read(1024*1024), b''):\n"
            "    digest.update(chunk)\n"
            f"assert digest.hexdigest() == {dataset_sha256!r}, 'Dataset checksum mismatch'\n"
        )
        pod_spec["initContainers"] = [
            {
                "name": "dataset",
                "image": image,
                "command": ["python3", "-c", script],
                "volumeMounts": [{"name": "dataset", "mountPath": "/dataset"}],
            }
        ]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "forge",
                "forge.openshift.io/benchmark-tool": "aiperf",
            },
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": timeout,
            "template": {
                "metadata": {"labels": {"app.kubernetes.io/name": "aiperf"}},
                "spec": pod_spec,
            },
        },
    }


@entrypoint
def run(
    *,
    endpoint_url: str,
    model_name: str,
    namespace: str,
    name: str,
    image: str = DEFAULT_IMAGE,
    timeout: int = 3600,
    pvc_size: str = "1Gi",
    pvc_storage_class: str | None = None,
    tokenizer: str | None = None,
    endpoint_type: str = "chat",
    endpoint_path: str = "/v1/chat/completions",
    streaming: bool = True,
    concurrency: int | None = None,
    request_rate: float | None = None,
    request_count: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    dataset_url: str | None = None,
    dataset_sha256: str | None = None,
    dataset_type: str | None = None,
    fixed_schedule: bool = False,
    fixed_schedule_auto_offset: bool = False,
    image_pull_secret: str | None = None,
) -> int:
    """Run AI Perf and collect summary metrics from a results PVC."""
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name) or len(name) > 55:
        raise ValueError("name must be a Kubernetes-safe name of at most 55 characters")
    if timeout <= 0 or not namespace or not image:
        raise ValueError("timeout, namespace, and image are required")
    if dataset_url:
        parsed = urlsplit(dataset_url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query:
            raise ValueError("dataset_url must be a public HTTPS URL without credentials")
        if (
            not dataset_type
            or not dataset_sha256
            or not re.fullmatch(r"[a-fA-F0-9]{64}", dataset_sha256)
        ):
            raise ValueError("dataset_type and SHA-256 digest are required for dataset_url")
    elif dataset_type or dataset_sha256:
        raise ValueError("dataset_type and dataset_sha256 require dataset_url")
    if not dataset_url and request_count is None:
        raise ValueError("request_count is required for a synthetic benchmark")
    build_profile_command(
        endpoint_url=endpoint_url,
        model_name=model_name,
        tokenizer=tokenizer,
        endpoint_type=endpoint_type,
        endpoint_path=endpoint_path,
        streaming=streaming,
        concurrency=concurrency,
        request_rate=request_rate,
        request_count=request_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        dataset_type=dataset_type,
        fixed_schedule=fixed_schedule,
        fixed_schedule_auto_offset=fixed_schedule_auto_offset,
    )
    wait_for_completion._retry_config["attempts"] = max(1, timeout // POLL_SECONDS + 1)
    execute_tasks(locals())
    return 0


@task
def create_resources(args, ctx):
    """Create the results PVC and benchmark Job."""
    ctx.pvc_name = f"{args.name}-results"
    src = args.artifact_dir / "src"
    src.mkdir(parents=True, exist_ok=True)
    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": ctx.pvc_name,
            "namespace": args.namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "forge",
                "forge.openshift.io/benchmark-tool": "aiperf",
            },
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": args.pvc_size}},
        },
    }
    if args.pvc_storage_class:
        pvc["spec"]["storageClassName"] = args.pvc_storage_class
    oc_apply(src / "aiperf-pvc.yaml", pvc)
    command = build_profile_command(
        endpoint_url=args.endpoint_url,
        model_name=args.model_name,
        tokenizer=args.tokenizer,
        endpoint_type=args.endpoint_type,
        endpoint_path=args.endpoint_path,
        streaming=args.streaming,
        concurrency=args.concurrency,
        request_rate=args.request_rate,
        request_count=args.request_count,
        input_tokens=args.input_tokens,
        output_tokens=args.output_tokens,
        dataset_type=args.dataset_type,
        fixed_schedule=args.fixed_schedule,
        fixed_schedule_auto_offset=args.fixed_schedule_auto_offset,
    )
    job = build_job_manifest(
        name=args.name,
        namespace=args.namespace,
        image=args.image,
        pvc_name=ctx.pvc_name,
        command=command,
        timeout=args.timeout,
        image_pull_secret=args.image_pull_secret,
        dataset_url=args.dataset_url,
        dataset_sha256=args.dataset_sha256,
    )
    oc_apply(src / "aiperf-job.yaml", job)
    metadata = {
        "benchmark_tool": "aiperf",
        "image": args.image,
        "model_name": args.model_name,
        "dataset_url": args.dataset_url,
        "dataset_sha256": args.dataset_sha256,
    }
    metadata_path = args.artifact_dir / "artifacts" / "aiperf_run.json"
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
    return f"Created AI Perf Job {args.name}"


@retry(attempts=361, delay=POLL_SECONDS)
@task
def wait_for_completion(args, ctx):
    """Wait for the AI Perf Job to succeed or fail."""
    job = oc_get_json("job", name=args.name, namespace=args.namespace)
    status = job.get("status", {})
    if status.get("succeeded", 0) >= 1:
        return f"AI Perf Job {args.name} completed"
    if status.get("failed", 0) >= 1 or any(
        condition.get("type") == "Failed" and condition.get("status") == "True"
        for condition in status.get("conditions", [])
    ):
        capture_job_state(args.artifact_dir, args.namespace, args.name)
        raise RuntimeError(f"AI Perf Job {args.name} failed; see captured Job logs")
    return False


def capture_job_state(artifact_dir: Path, namespace: str, name: str) -> None:
    """Capture only operational Job state, without Secret objects."""
    artifacts = artifact_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    oc(
        "get",
        "job",
        name,
        "-n",
        namespace,
        "-o",
        "yaml",
        check=False,
        log_stdout=False,
        stdout_dest=artifacts / "aiperf_job.yaml",
    )
    oc(
        "logs",
        f"job/{name}",
        "-n",
        namespace,
        "--all-containers=true",
        check=False,
        log_stdout=False,
        stdout_dest=artifacts / "aiperf_job.log",
    )


@task
def collect_results(args, ctx):
    """Read the summary through a helper Pod and validate it."""
    capture_job_state(args.artifact_dir, args.namespace, args.name)
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": f"{args.name}-copy", "namespace": args.namespace},
        "spec": {
            "restartPolicy": "Never",
            "volumes": [{"name": "results", "persistentVolumeClaim": {"claimName": ctx.pvc_name}}],
            "containers": [
                {
                    "name": "copy",
                    "image": "busybox:1.36.1",
                    "command": ["sleep", "3600"],
                    "volumeMounts": [{"name": "results", "mountPath": "/results"}],
                }
            ],
        },
    }
    benchmark_pods = oc_get_json(
        "pods",
        namespace=args.namespace,
        selector=f"batch.kubernetes.io/job-name={args.name}",
        ignore_not_found=True,
    )
    if benchmark_pods and benchmark_pods.get("items"):
        node_name = benchmark_pods["items"][0].get("spec", {}).get("nodeName")
        if node_name:
            pod["spec"]["nodeName"] = node_name
    oc_apply(args.artifact_dir / "src" / "aiperf-copy-pod.yaml", pod)
    ctx.copy_pod = pod["metadata"]["name"]
    return f"Created AI Perf copy Pod {ctx.copy_pod}"


@retry(attempts=60, delay=5)
@task
def wait_copy_pod(args, ctx):
    """Wait for the results copy Pod to become ready."""
    pod = oc_get_json("pod", name=ctx.copy_pod, namespace=args.namespace)
    if any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in pod.get("status", {}).get("conditions", [])
    ):
        return "AI Perf copy Pod ready"
    return False


@task
def extract_summary(args, ctx):
    """Extract aggregate metrics; raw inputs and responses remain on the PVC."""
    output = args.artifact_dir / "artifacts" / "aiperf" / SUMMARY_FILE
    output.parent.mkdir(parents=True, exist_ok=True)
    oc(
        "exec",
        "-n",
        args.namespace,
        ctx.copy_pod,
        "--",
        "cat",
        f"/results/run/{SUMMARY_FILE}",
        log_stdout=False,
        stdout_dest=output,
    )
    try:
        summary = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("AI Perf summary is missing or malformed") from exc
    if not isinstance(summary, dict) or "request_count" not in summary:
        raise ValueError("AI Perf summary lacks request_count")
    return f"Extracted AI Perf summary to {output}"


@always
@task
def cleanup_resources(args, ctx):
    """Remove helper resources after extracting the summary."""
    for kind, name in (
        ("pod", f"{args.name}-copy"),
        ("job", args.name),
        ("pvc", f"{args.name}-results"),
    ):
        oc("delete", kind, name, "-n", args.namespace, "--ignore-not-found=true", check=False)
    return "Removed AI Perf helper resources"
