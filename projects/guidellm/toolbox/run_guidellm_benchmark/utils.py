"""
Utilities for the GuideLL-M benchmark toolbox module.
"""

from __future__ import annotations

import json as _json
import re
import shlex
from dataclasses import dataclass
from typing import Any

import yaml

from projects.core.dsl import template


@dataclass(frozen=True)
class GuideLLMRun:
    rate: str | None
    label: str
    args: list[str]


def _sanitize_rate_label(rate: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", rate).strip("._-")
    return sanitized or "rate"


def _format_expression_value(value: float | int) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _evaluate_rate_expression(expression: str, rate: str) -> str:
    rate_value = float(rate)
    normalized = expression.strip()

    if normalized == "rate":
        return _format_expression_value(rate_value)

    left_multiply = re.fullmatch(r"(\d+)\s*\*\s*rate", normalized)
    if left_multiply:
        return _format_expression_value(int(left_multiply.group(1)) * rate_value)

    right_multiply = re.fullmatch(r"rate\s*\*\s*(\d+)", normalized)
    if right_multiply:
        return _format_expression_value(rate_value * int(right_multiply.group(1)))

    raise ValueError(
        f"Unsupported rate expression: {expression}. "
        "Supported forms are 'rate', 'N*rate', and 'rate*N'."
    )


def _substitute_rate_expressions(value: str, rate: str) -> str:
    return re.sub(
        r"\{([^{}]+)\}",
        lambda match: _evaluate_rate_expression(match.group(1), rate),
        value,
    )


def _has_rate_expressions(guidellm_args: list[str]) -> bool:
    return any(re.search(r"\{[^{}]*\brate\b[^{}]*\}", arg) for arg in guidellm_args)


def expand_guidellm_runs(guidellm_args: list[str]) -> list[GuideLLMRun]:
    has_rate_expressions = _has_rate_expressions(guidellm_args)
    rate_arg = next((arg for arg in guidellm_args if arg.startswith("--rate=")), None)
    if not rate_arg:
        if has_rate_expressions:
            raise ValueError(
                "Rate-based expressions require a '--rate=' argument. "
                "Found '{rate}'-style placeholders without any rate values."
            )
        return [GuideLLMRun(rate=None, label="default", args=list(guidellm_args))]
    if not has_rate_expressions:
        return [GuideLLMRun(rate=None, label="default", args=list(guidellm_args))]

    rate_values = [value.strip() for value in rate_arg.split("=", 1)[1].split(",") if value.strip()]
    if not rate_values:
        raise ValueError(
            "Argument '--rate' must include at least one non-empty value "
            "when rate-based expressions are used."
        )

    runs: list[GuideLLMRun] = []
    for rate in rate_values:
        run_args: list[str] = []
        for arg in guidellm_args:
            if arg.startswith("--rate="):
                run_args.append(f"--rate={rate}")
                continue
            run_args.append(_substitute_rate_expressions(arg, rate))

        runs.append(
            GuideLLMRun(
                rate=rate,
                label=f"rate-{_sanitize_rate_label(rate)}",
                args=run_args,
            )
        )

    return runs


def build_guidellm_args(benchmark: dict[str, object]) -> list[str]:
    guidellm_args: list[str] = []
    benchmark_args = benchmark.get("args", {})
    if benchmark_args:
        for key, value in benchmark_args.items():
            cli_key = key.replace("_", "-")
            if isinstance(value, list):
                rendered_value = ",".join(str(item) for item in value)
            else:
                rendered_value = str(value)
            guidellm_args.append(f"--{cli_key}={rendered_value}")

    if "rate" in benchmark and "rate" not in benchmark_args:
        guidellm_args.append(f"--rate={benchmark['rate']}")

    if not any(arg.startswith("--outputs=") for arg in guidellm_args):
        guidellm_args.append(f"--outputs={benchmark.get('outputs', 'json')}")

    return guidellm_args


_RATE_KEY_BY_PROFILE = {
    "concurrent": "streams",
    "sweep": "sweep_size",
    "throughput": "max_concurrency",
}


_FILE_KIND_BY_EXT = {
    ".json": "json_file",
    ".jsonl": "json_file",
    ".csv": "csv_file",
    ".parquet": "parquet_file",
    ".arrow": "arrow_file",
    ".txt": "text_file",
    ".hdf5": "hdf5_file",
    ".h5": "hdf5_file",
    ".db": "db_file",
    ".tar": "tar_file",
}


def _convert_data_spec(data_spec: str) -> str:
    """Convert a ``--data`` value to GuideLLM ``kind=…`` format if needed."""
    if data_spec.startswith("kind="):
        return data_spec

    for ext, kind in _FILE_KIND_BY_EXT.items():
        if data_spec.endswith(ext):
            return f"kind={kind},path={data_spec}"

    if "prompt_tokens" in data_spec or "output_tokens" in data_spec:
        return f"kind=synthetic_text,{data_spec}"

    if "/" in data_spec and "=" not in data_spec:
        return f"kind=huggingface,source={data_spec}"

    return f"kind=synthetic_text,{data_spec}"


def _build_run_args(endpoint_url: str, old_args: list[str]) -> list[str]:
    """Transform config-derived CLI args into ``guidellm run`` arguments."""
    backend_type = "openai_http"
    model = None
    data_spec = None
    rate_type = "concurrent"
    rates_str = None
    max_seconds = None
    max_requests = None
    rampup = None
    warmup = None
    processor = None
    passthrough: list[str] = []

    for arg in old_args:
        key, _, val = arg.partition("=")
        if key == "--backend-type":
            backend_type = val
        elif key == "--rate-type":
            rate_type = val
        elif key == "--model":
            model = val
        elif key == "--data":
            data_spec = val
        elif key == "--rate":
            rates_str = val
        elif key == "--max-seconds":
            max_seconds = val
        elif key == "--max-requests":
            max_requests = val
        elif key == "--rampup":
            rampup = val
        elif key == "--warmup":
            warmup = val
        elif key in ("--processor", "--processor-args"):
            if key == "--processor":
                processor = val
        elif key in ("--outputs", "--output-dir"):
            pass
        else:
            passthrough.append(arg)

    new_args: list[str] = []

    backend_spec = f"kind={backend_type},target={endpoint_url}"
    if model:
        backend_spec += f",model={model}"
    new_args.append(f"--backend={backend_spec}")

    if data_spec:
        new_args.append(f"--data={_convert_data_spec(data_spec)}")

    rate_key = _RATE_KEY_BY_PROFILE.get(rate_type, "rate")
    if rates_str:
        rate_values = [v.strip() for v in rates_str.split(",") if v.strip()]
        if len(rate_values) == 1:
            profile_spec = f"kind={rate_type},{rate_key}={rate_values[0]}"
            if warmup:
                profile_spec += f",warmup={warmup}"
            if rampup:
                profile_spec += f",rampup_duration={rampup}"
            new_args.append(f"--profile={profile_spec}")
        else:
            parsed_rates: list[int | float] = []
            for r in rate_values:
                f = float(r)
                parsed_rates.append(int(f) if f == int(f) else f)
            profile_dict: dict = {
                "kind": rate_type,
                rate_key: parsed_rates,
            }
            if warmup:
                f = float(warmup)
                profile_dict["warmup"] = int(f) if f == int(f) else f
            if rampup:
                f = float(rampup)
                profile_dict["rampup_duration"] = int(f) if f == int(f) else f
            new_args.append(f"--profile={_json.dumps(profile_dict)}")
    else:
        profile_spec = f"kind={rate_type}"
        if warmup:
            profile_spec += f",warmup={warmup}"
        if rampup:
            profile_spec += f",rampup_duration={rampup}"
        new_args.append(f"--profile={profile_spec}")

    if max_seconds:
        new_args.append(f"--constraint=kind=max_duration,seconds={max_seconds}")
    if max_requests:
        new_args.append(f"--constraint=kind=max_requests,count={max_requests}")

    new_args.append("--output=kind=json,path=/results/benchmarks.json")

    if processor:
        new_args.append(f"--tokenizer=kind=huggingface_auto,model={processor}")

    new_args.extend(passthrough)
    return new_args


def _build_multi_run_script(*, endpoint_url: str, runs: list[GuideLLMRun]) -> str:
    """Shell script for multiple GuideLLM runs (rate-expression expansion)."""
    lines = ["set -euo pipefail", "mkdir -p /results"]
    for run in runs:
        run_args = _build_run_args(endpoint_url, run.args)
        output_path = f"/results/benchmarks-{run.label}.json"
        filtered = [a for a in run_args if not a.startswith("--output=")]
        filtered.append(f"--output=kind=json,path={output_path}")
        command = ["/opt/app-root/bin/guidellm", "run", *filtered]
        lines.append(shlex.join(command))
    return "\n".join(lines)


def render_guidellm_pvc_from_parts(
    *,
    namespace: str,
    name: str,
    pvc_size: str,
    pvc_storage_class: str | None = None,
    owner_reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Render a GuideLL-M PVC manifest from individual components.

    Args:
        namespace: Target namespace
        name: Name of the benchmark job and PVC
        pvc_size: Size of the PVC
        pvc_storage_class: Optional storage class name for the PVC
        owner_reference: Optional owner reference to set (e.g., for job ownership)

    Returns:
        PVC manifest as dict
    """
    rendered_yaml = template.render_template(
        "guidellm_pvc.yaml.j2",
        {
            "namespace": namespace,
            "name": name,
            "pvc_size": pvc_size,
            "pvc_storage_class": pvc_storage_class,
        },
    )
    manifest = yaml.safe_load(rendered_yaml)

    # Add owner reference if provided
    if owner_reference:
        manifest["metadata"]["ownerReferences"] = [owner_reference]

    return manifest


def render_guidellm_job_from_parts(
    *,
    namespace: str,
    name: str,
    image: str,
    endpoint_url: str,
    guidellm_args: list[str],
    timeout_seconds: int,
    hf_token_secret: str = "",
    fs_group: int | None = None,
) -> dict[str, Any]:
    """Render a GuideLL-M job manifest from individual components.

    Args:
        namespace: Target namespace
        name: Name of the benchmark job
        image: Container image for GuideLLM
        endpoint_url: Gateway endpoint URL
        guidellm_args: Additional arguments for GuideLLM
        timeout_seconds: Active deadline for the Kubernetes Job
        hf_token_secret: Name of the K8s secret containing HF_TOKEN. If empty, HF_TOKEN is not injected.
        fs_group: If set, adds a pod-level securityContext.fsGroup to ensure
            the PVC is writable by the container. Needed on clusters where the
            CSI driver provisions volumes with root-only permissions.

    Returns:
        Job manifest as dict
    """
    runs = expand_guidellm_runs(guidellm_args)
    rendered_yaml = template.render_template(
        "guidellm_job.yaml.j2",
        {
            "namespace": namespace,
            "name": name,
            "image": image,
            "hf_token_secret": hf_token_secret,
            "fs_group": fs_group,
        },
    )
    manifest = yaml.safe_load(rendered_yaml)
    manifest["spec"]["activeDeadlineSeconds"] = timeout_seconds
    container = manifest["spec"]["template"]["spec"]["containers"][0]

    if len(runs) == 1 and runs[0].rate is None:
        container["command"] = ["/opt/app-root/bin/guidellm"]
        container["args"] = [
            "run",
            *_build_run_args(endpoint_url, runs[0].args),
        ]
        return manifest

    container["command"] = ["/bin/sh", "-lc"]
    container["args"] = [_build_multi_run_script(endpoint_url=endpoint_url, runs=runs)]
    return manifest


def render_guidellm_shared_volume_job_from_parts(
    *,
    namespace: str,
    name: str,
    image: str,
    endpoint_url: str,
    guidellm_args: list[str],
    timeout_seconds: int,
    hf_token_secret: str = "",
    fs_group: int | None = None,
) -> dict[str, Any]:
    """Render a GuideLL-M job manifest with shared volume (main + sidecar containers).

    Args:
        namespace: Target namespace
        name: Name of the benchmark job
        image: Container image for GuideLLM
        endpoint_url: Gateway endpoint URL
        guidellm_args: Additional arguments for GuideLLM
        timeout_seconds: Active deadline for the Kubernetes Job
        hf_token_secret: Name of the K8s secret containing HF_TOKEN. If empty, HF_TOKEN is not injected.
        fs_group: If set, adds a pod-level securityContext.fsGroup to ensure
            the shared volume is writable by both containers.

    Returns:
        Job manifest as dict with main and sidecar containers
    """
    runs = expand_guidellm_runs(guidellm_args)
    rendered_yaml = template.render_template(
        "guidellm_shared_volume_job.yaml.j2",
        {
            "namespace": namespace,
            "name": name,
            "image": image,
            "hf_token_secret": hf_token_secret,
            "fs_group": fs_group,
        },
    )
    manifest = yaml.safe_load(rendered_yaml)
    manifest["spec"]["activeDeadlineSeconds"] = timeout_seconds

    if len(runs) == 1 and runs[0].rate is None:
        run_args = _build_run_args(endpoint_url, runs[0].args)
        cmd = shlex.join(["/opt/app-root/bin/guidellm", "run", *run_args])
        main_script = "\n".join(
            [
                "set -euo pipefail",
                "mkdir -p /results",
                cmd,
            ]
        )
    else:
        main_script = _build_multi_run_script(endpoint_url=endpoint_url, runs=runs)

    manifest["spec"]["template"]["spec"]["containers"][0]["command"] = ["/bin/sh", "-c"]
    manifest["spec"]["template"]["spec"]["containers"][0]["args"] = [main_script]
    return manifest


def render_guidellm_copy_pod_from_parts(
    *,
    namespace: str,
    name: str,
    pvc_size: str,
    node_name: str | None = None,
) -> dict[str, Any]:
    """Render a GuideLL-M copy pod manifest from individual components.

    Args:
        namespace: Target namespace
        name: Name of the benchmark job (used for copy pod naming)
        pvc_size: Size of the PVC (not used directly, but kept for interface consistency)
        node_name: Optional node name to pin the pod to

    Returns:
        Pod manifest as dict
    """
    rendered_yaml = template.render_template(
        "guidellm_copy_pod.yaml.j2",
        {
            "namespace": namespace,
            "name": name,
            "node_name": node_name,
        },
    )
    return yaml.safe_load(rendered_yaml)
