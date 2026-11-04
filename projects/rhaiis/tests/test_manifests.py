from __future__ import annotations

from projects.rhaiis.orchestration.manifests import build_inferenceservice


def _build_isvc(
    *, profiler_ranges: str | None = None, node_selector: dict[str, str] | None = None
) -> dict:
    return build_inferenceservice(
        deployment_name="test-model",
        namespace="test-namespace",
        engine="vllm",
        engine_port=8080,
        accelerator="nvidia",
        gpu_count=1,
        replicas=1,
        cpu_request="4",
        memory_request="16Gi",
        storage_source="hf",
        storage_pvc="model-pvc",
        model_id="org/model",
        profiler_ranges=profiler_ranges,
        node_selector=node_selector,
    )


def test_build_inferenceservice_adds_configured_profiler_range() -> None:
    manifest = _build_isvc(profiler_ranges="1000-1010")

    assert manifest["metadata"]["annotations"]["vllm.profiler/ranges"] == "1000-1010"


def test_build_inferenceservice_omits_profiler_range_when_not_configured() -> None:
    manifest = _build_isvc()

    assert "vllm.profiler/ranges" not in manifest["metadata"]["annotations"]


def test_build_inferenceservice_adds_configured_node_selector() -> None:
    manifest = _build_isvc(node_selector={"kubernetes.io/hostname": "gpu-node-1"})

    assert manifest["spec"]["predictor"]["nodeSelector"] == {"kubernetes.io/hostname": "gpu-node-1"}


def test_build_inferenceservice_omits_node_selector_when_not_configured() -> None:
    for node_selector in (None, {}):
        manifest = _build_isvc(node_selector=node_selector)

        assert "nodeSelector" not in manifest["spec"]["predictor"]
