from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml


def _load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def render_graph_deployment(
    *,
    config_dir: str | Path,
    namespace: str,
    model_name: str,
    model_slug: str,
    deployment_profile: dict[str, Any],
    model_cache: dict[str, Any],
    dynamo_config: dict[str, Any],
) -> dict[str, Any]:
    """Render a DynamoGraphDeployment manifest from config inputs."""
    template_path = Path(config_dir) / "manifests" / "dynamo-graph-deployment.yaml"
    manifest = _load_yaml(template_path)

    manifest["metadata"]["name"] = f"dynamo-{model_slug}"
    manifest["metadata"]["namespace"] = namespace
    manifest["metadata"].setdefault("labels", {})
    manifest["metadata"]["labels"].update({
        "app.kubernetes.io/managed-by": "forge",
        "forge.openshift.io/project": "dynamo",
    })

    backend_framework = deployment_profile.get("backend_framework", "vllm")
    manifest["spec"]["backendFramework"] = backend_framework

    serving_mode = deployment_profile.get("serving_mode", "aggregated")
    router_mode = deployment_profile.get("router_mode", "direct")
    runtime_image = deployment_profile["runtime_image"]
    frontend_image = deployment_profile["frontend_image"]
    tensor_parallelism = deployment_profile.get("tensor_parallelism", 1)
    vllm_args = _build_vllm_args(deployment_profile.get("vllm_args", []))
    env_vars = deployment_profile.get("env", {})
    kv_block_size = deployment_profile.get("kv_block_size", "16")
    use_dra_resources = deployment_profile.get("use_dra_resources", False)
    dra_resource_name = deployment_profile.get("dra_resource_name", "dra.llm-d.io/gpu-nic-pair")
    kv_transfer_config = deployment_profile.get("kv_transfer_config")
    kvbm_cpu_cache_gb = deployment_profile.get("kvbm_cpu_cache_gb")

    model_path = model_name
    hf_home = deployment_profile.get("hf_home", "/opt/models")

    base_env = [
        {"name": "SERVED_MODEL_NAME", "value": model_name},
        {"name": "MODEL_PATH", "value": model_path},
        {"name": "HF_HOME", "value": hf_home},
    ]
    if kvbm_cpu_cache_gb:
        base_env.append({"name": "DYN_KVBM_CPU_CACHE_GB", "value": str(kvbm_cpu_cache_gb)})
    for key, value in env_vars.items():
        base_env.append({"name": key, "value": str(value)})

    # Add KVBM kv_transfer_config to vllm args if specified
    if kv_transfer_config:
        vllm_args.extend(["--kv-transfer-config", f"'{kv_transfer_config}'"])

    common_kwargs = dict(
        deployment_profile=deployment_profile,
        runtime_image=runtime_image,
        frontend_image=frontend_image,
        model_name=model_name,
        tensor_parallelism=tensor_parallelism,
        vllm_args=vllm_args,
        base_env=base_env,
        kv_block_size=kv_block_size,
        backend_framework=backend_framework,
        router_mode=router_mode,
        use_dra_resources=use_dra_resources,
        dra_resource_name=dra_resource_name,
    )

    if serving_mode == "disaggregated":
        services = _build_disagg_services(**common_kwargs)
    else:
        services = _build_aggregated_services(**common_kwargs)

    manifest["spec"]["services"] = services
    return manifest


def _build_aggregated_services(
    *,
    deployment_profile: dict[str, Any],
    runtime_image: str,
    frontend_image: str,
    model_name: str,
    tensor_parallelism: int,
    vllm_args: list[str],
    base_env: list[dict[str, str]],
    kv_block_size: str,
    backend_framework: str,
    router_mode: str = "direct",
    use_dra_resources: bool = False,
    dra_resource_name: str = "dra.llm-d.io/gpu-nic-pair",
) -> dict[str, Any]:
    """Build services spec for aggregated (single-pool) mode."""
    replicas = deployment_profile.get("replicas", 1)
    router_args = deployment_profile.get("router_args", [])

    worker_cmd = (
        f"python3 -m dynamo.{backend_framework} "
        f"--model $MODEL_PATH "
        f"--served-model-name $SERVED_MODEL_NAME "
        f"--tensor-parallel-size {tensor_parallelism} "
        f"--data-parallel-size 1 "
        + " ".join(vllm_args)
        + " --kv-events-config '{\"enable_kv_cache_events\":true}'"
        + f" --block-size {kv_block_size}"
    )

    services: dict[str, Any] = {}

    if router_mode == "kv":
        # Standalone frontend with KV-aware router (no EPP)
        frontend_args = ["--router-mode", "kv", "--router-reset-states"] + router_args
        services["Frontend"] = {
            "componentType": "frontend",
            "replicas": 1,
            "extraPodSpec": {
                "mainContainer": {
                    "image": runtime_image,
                    "workingDir": "/workspace",
                    "command": ["python3", "-m", "dynamo.frontend"],
                    "args": frontend_args,
                    "env": [{"name": "HF_HOME", "value": base_env[2]["value"]}],
                },
            },
            "resources": {
                "requests": {"cpu": deployment_profile.get("frontend_cpu", "4")},
                "limits": {"cpu": deployment_profile.get("frontend_cpu", "4")},
            },
        }
    else:
        # EPP-based routing (production pattern)
        services["Epp"] = _build_epp_service(
            frontend_image=frontend_image,
            model_name=model_name,
            kv_block_size=kv_block_size,
        )

    worker_sidecar_args = ["-m", "dynamo.frontend", "--router-mode", router_mode]
    if router_mode == "kv":
        worker_sidecar_args = ["-m", "dynamo.frontend", "--router-mode", "direct"]

    services["VllmWorker"] = {
        "componentType": "worker",
        "volumeMounts": [
            {"name": "model-cache", "mountPoint": base_env[2]["value"]},
        ],
        "sharedMemory": {"size": "2Gi"},
        "frontendSidecar": {
            "image": runtime_image,
            "args": worker_sidecar_args,
        },
        "extraPodSpec": {
            "mainContainer": {
                "env": copy.deepcopy(base_env),
                "args": [worker_cmd],
                "command": ["/bin/sh", "-c"],
                "image": runtime_image,
                "workingDir": f"/workspace/examples/backends/{backend_framework}",
            },
        },
        "replicas": replicas,
        "resources": _build_gpu_resources(
            tensor_parallelism,
            use_dra=use_dra_resources,
            dra_resource_name=dra_resource_name,
        ),
    }

    return services


def _build_disagg_services(
    *,
    deployment_profile: dict[str, Any],
    runtime_image: str,
    frontend_image: str,
    model_name: str,
    tensor_parallelism: int,
    vllm_args: list[str],
    base_env: list[dict[str, str]],
    kv_block_size: str,
    backend_framework: str,
    router_mode: str = "direct",
    use_dra_resources: bool = False,
    dra_resource_name: str = "dra.llm-d.io/gpu-nic-pair",
) -> dict[str, Any]:
    """Build services spec for disaggregated (prefill/decode) mode."""
    prefill_replicas = deployment_profile.get("prefill_replicas", 1)
    decode_replicas = deployment_profile.get("decode_replicas", 2)

    common_args = " ".join(vllm_args)
    base_cmd = (
        f"python3 -m dynamo.{backend_framework} "
        f"--model $MODEL_PATH "
        f"--served-model-name $SERVED_MODEL_NAME "
        f"--tensor-parallel-size {tensor_parallelism} "
        f"--data-parallel-size 1 "
        f"{common_args} "
        f"--kv-events-config '{{\"enable_kv_cache_events\":true}}' "
        f"--block-size {kv_block_size}"
    )

    prefill_cmd = f"{base_cmd} --disaggregation-mode prefill"
    decode_cmd = f"{base_cmd} --disaggregation-mode decode"

    def _worker_service(name: str, cmd: str, replicas: int) -> dict[str, Any]:
        return {
            "componentType": "worker",
            "volumeMounts": [
                {"name": "model-cache", "mountPoint": base_env[2]["value"]},
            ],
            "sharedMemory": {"size": "2Gi"},
            "frontendSidecar": {
                "image": runtime_image,
                "args": ["-m", "dynamo.frontend", "--router-mode", "direct"],
            },
            "extraPodSpec": {
                "mainContainer": {
                    "env": copy.deepcopy(base_env),
                    "args": [cmd],
                    "command": ["/bin/sh", "-c"],
                    "image": runtime_image,
                    "workingDir": f"/workspace/examples/backends/{backend_framework}",
                },
            },
            "replicas": replicas,
            "resources": _build_gpu_resources(
                tensor_parallelism,
                use_dra=use_dra_resources,
                dra_resource_name=dra_resource_name,
            ),
        }

    return {
        "Epp": _build_epp_service(
            frontend_image=frontend_image,
            model_name=model_name,
            kv_block_size=kv_block_size,
        ),
        "VllmPrefillWorker": _worker_service("prefill", prefill_cmd, prefill_replicas),
        "VllmDecodeWorker": _worker_service("decode", decode_cmd, decode_replicas),
    }


def _build_epp_service(
    *,
    frontend_image: str,
    model_name: str,
    kv_block_size: str,
) -> dict[str, Any]:
    """Build the EPP (Endpoint Picker Plugin) service."""
    return {
        "componentType": "epp",
        "replicas": 1,
        "extraPodSpec": {
            "mainContainer": {
                "image": frontend_image,
                "env": [
                    {"name": "DYN_KV_CACHE_BLOCK_SIZE", "value": kv_block_size},
                    {"name": "DYN_MODEL_NAME", "value": model_name},
                    {"name": "DYN_DECODE_FALLBACK", "value": "true"},
                ],
            },
        },
        "eppConfig": {
            "config": {
                "plugins": [
                    {"type": "disagg-profile-handler"},
                    {
                        "name": "decode-filter",
                        "type": "label-filter",
                        "parameters": {
                            "label": "nvidia.com/dynamo-sub-component-type",
                            "validValues": ["decode"],
                            "allowsNoLabel": True,
                        },
                    },
                    {"name": "picker", "type": "max-score-picker"},
                    {"name": "dyn-decode", "type": "dyn-decode-scorer"},
                ],
                "schedulingProfiles": [
                    {
                        "name": "decode",
                        "plugins": [
                            {"pluginRef": "decode-filter", "weight": 1},
                            {"pluginRef": "dyn-decode", "weight": 1},
                            {"pluginRef": "picker", "weight": 1},
                        ],
                    },
                ],
            },
        },
    }


def _build_gpu_resources(
    tensor_parallelism: int,
    *,
    use_dra: bool = False,
    dra_resource_name: str = "dra.llm-d.io/gpu-nic-pair",
) -> dict[str, Any]:
    gpu_str = str(tensor_parallelism)
    if use_dra:
        return {
            "limits": {"custom": {dra_resource_name: gpu_str}},
            "requests": {"custom": {dra_resource_name: gpu_str}},
        }
    return {
        "limits": {"gpu": gpu_str},
        "requests": {"gpu": gpu_str},
    }


def _build_vllm_args(vllm_args: dict[str, Any] | list[str]) -> list[str]:
    if isinstance(vllm_args, list):
        return [str(arg) for arg in vllm_args]

    rendered_args: list[str] = []
    for key, value in vllm_args.items():
        cli_key = key.replace("_", "-")
        if isinstance(value, bool):
            if value:
                rendered_args.append(f"--{cli_key}")
            continue
        rendered_args.append(f"--{cli_key}={value}")
    return rendered_args
