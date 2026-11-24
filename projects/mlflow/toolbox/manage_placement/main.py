"""Discover NVMe-backed storage and choose portable MLflow test placement."""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from projects.core.dsl import entrypoint, execute_tasks, task
from projects.core.dsl.utils import write_json
from projects.core.dsl.utils.k8s import oc
from projects.mlflow.toolbox.apply_infrastructure.main import MINIO_PVC_GI, postgres_pvc_gi

logger = logging.getLogger(__name__)

_LABEL_PREFIX = "forge.openshift.io"
_GIB = 1024**3
_NVME_PATH = re.compile(r"(?:^|[/_-])nvme(?:[0-9]|[/_.-]|$)", re.IGNORECASE)


@dataclass
class Placement:
    """Runtime-selected nodes, storage classes, and temporary selectors."""

    storage_class_name: str
    read_data_storage_class_name: str
    storage_node: str
    compute_node: str
    node_selectors: dict[str, dict[str, str]]
    pvc_selectors: dict[str, dict[str, str]] = field(default_factory=dict)
    read_data_pvc_selector: dict[str, str] | None = None
    label_key: str = ""
    pv_label_key: str = ""
    labeled_pvs: list[str] = field(default_factory=list)
    storage_kind: str = ""
    storage_device_class: str = ""

    def metadata(self) -> dict[str, str]:
        return {
            "postgres_storage_class": self.storage_class_name,
            "read_data_storage_class": self.read_data_storage_class_name,
            "postgres_minio_node": self.storage_node,
            "mlflow_k6_node": self.compute_node,
            "storage_backend": self.storage_kind,
            "storage_device_class": self.storage_device_class,
        }

    def release(self) -> None:
        """Remove only the unique labels created for this test cell."""
        if not self.label_key:
            return
        for node in {self.storage_node, self.compute_node}:
            result = oc(
                "label",
                "node",
                node,
                f"{self.label_key}-",
                check=False,
                log_stdout=False,
            )
            if result.returncode != 0:
                logger.warning("Could not remove temporary placement label from %s", node)
        for pv in self.labeled_pvs:
            result = oc(
                "label",
                "pv",
                pv,
                f"{self.pv_label_key}-",
                check=False,
                log_stdout=False,
            )
            if result.returncode != 0:
                logger.warning("Could not remove temporary placement label from PV %s", pv)


@entrypoint
def run(
    *,
    max_pg_size_gb: int = 0,
    needs_read_data: bool = False,
    release_from: str | None = None,
):
    """Reserve storage and compute placement or release a previous reservation.

    Args:
        max_pg_size_gb: Target PostgreSQL database size in GiB
        needs_read_data: Reserve scratch storage for the k6 read workload
        release_from: Path to the placement.json from a previous invocation
    """
    return execute_tasks(locals())


@task
def manage_placement(args, ctx):
    """Label the selected nodes and volumes, or remove labels from a saved placement."""
    if args.release_from is not None:
        data = json.loads(Path(args.release_from).read_text(encoding="utf-8"))
        Placement(**data).release()
        return "Released temporary placement labels"

    placement = _discover_and_label_placement(
        max_pg_size_gb=args.max_pg_size_gb,
        needs_read_data=args.needs_read_data,
    )
    ctx.placement_file = args.artifact_dir / "placement.json"
    ctx.placement = asdict(placement)
    try:
        write_json(ctx.placement_file, ctx.placement)
    except Exception:
        placement.release()
        raise
    return f"Saved placement in {ctx.placement_file}"


def _discover_and_label_placement(
    *,
    max_pg_size_gb: int,
    needs_read_data: bool,
) -> Placement:
    """Find an NVMe storage node and a distinct compute node, then label them.

    LVMS device selectors and node status are preferred because they identify
    the physical devices behind a TopoLVM StorageClass. Local Storage Operator
    PV paths are used as a fallback when they identify available NVMe devices.
    """
    storage_classes = _get_items("storageclass")
    nodes = _get_items("nodes")
    pvs = _get_items("pv")
    if not storage_classes:
        raise RuntimeError("The cluster has no StorageClasses")
    if not nodes:
        raise RuntimeError("Could not discover Kubernetes nodes")

    postgres_pvc_size = postgres_pvc_gi(max_pg_size_gb)
    read_pvc_gi = 1
    worker_nodes = _eligible_worker_nodes(nodes)
    if len(worker_nodes) < 2:
        raise RuntimeError("MLflow placement requires two distinct Ready, schedulable worker nodes")

    lvm_groups = _get_items("lvmvolumegroup")
    lvm_statuses = _get_items("lvmvolumegroupnodestatus")
    selected = _select_lvms_placement(
        storage_classes=storage_classes,
        nodes=worker_nodes,
        groups=lvm_groups,
        statuses=lvm_statuses,
        pvs=pvs,
        postgres_pvc_gi=postgres_pvc_size,
        minio_pvc_gi=MINIO_PVC_GI,
        read_pvc_gi=read_pvc_gi,
        needs_read_data=needs_read_data,
    )
    if selected is None:
        selected = _select_lso_placement(
            storage_classes=storage_classes,
            nodes=worker_nodes,
            pvs=pvs,
            postgres_pvc_gi=postgres_pvc_size,
            minio_pvc_gi=MINIO_PVC_GI,
            read_pvc_gi=read_pvc_gi,
            needs_read_data=needs_read_data,
        )
    if selected is None:
        raise RuntimeError(
            "No usable NVMe-backed StorageClass and node placement were found. "
            "The cluster must expose NVMe through an LVMS device selector/status "
            "or an available Local PV whose path identifies NVMe."
        )

    token = uuid.uuid4().hex[:12]
    label_key = f"{_LABEL_PREFIX}/mlflow-placement-{token}"
    storage_value = "postgres-minio"
    compute_value = "mlflow-k6"
    storage_selector = {label_key: storage_value}
    compute_selector = {label_key: compute_value}
    node_selectors = {
        "minio": storage_selector,
        "postgresql": storage_selector,
        "minio_bucket": storage_selector,
        "mlflow": compute_selector,
        "k6": compute_selector,
    }

    pv_selectors: dict[str, dict[str, str]] = {}
    read_data_pv_selector = None
    pv_label_key = ""
    labeled_pvs: list[str] = []
    if selected["postgres_pv"]:
        pv_label_key = f"{_LABEL_PREFIX}/mlflow-pv-{token}"
        assignments = {
            selected["postgres_pv"]: ("postgres", "postgresql-pvc"),
            selected["minio_pv"]: ("minio", "minio-pvc"),
        }
        if selected.get("read_data_pv"):
            assignments[selected["read_data_pv"]] = ("read-data", "read-data")
        try:
            for pv_name, (value, claim_name) in assignments.items():
                oc(
                    "label",
                    "pv",
                    pv_name,
                    f"{pv_label_key}={value}",
                    "--overwrite",
                    log_stdout=False,
                )
                labeled_pvs.append(pv_name)
                selector = {pv_label_key: value}
                if claim_name == "read-data":
                    read_data_pv_selector = selector
                else:
                    pv_selectors[claim_name] = selector
        except Exception:
            _remove_labels(
                label_key,
                selected["storage_node"],
                selected["compute_node"],
                labeled_pvs,
                pv_label_key=pv_label_key,
            )
            raise

    placement = Placement(
        storage_class_name=selected["storage_class_name"],
        read_data_storage_class_name=selected["read_data_storage_class_name"],
        storage_node=selected["storage_node"],
        compute_node=selected["compute_node"],
        node_selectors=node_selectors,
        pvc_selectors=pv_selectors,
        read_data_pvc_selector=read_data_pv_selector,
        label_key=label_key,
        pv_label_key=pv_label_key,
        labeled_pvs=labeled_pvs,
        storage_kind=selected["storage_kind"],
        storage_device_class=selected.get("device_class", ""),
    )
    try:
        oc(
            "label",
            "node",
            placement.storage_node,
            f"{label_key}={storage_value}",
            "--overwrite",
            log_stdout=False,
        )
        oc(
            "label",
            "node",
            placement.compute_node,
            f"{label_key}={compute_value}",
            "--overwrite",
            log_stdout=False,
        )
    except Exception:
        placement.release()
        raise

    logger.info(
        "Selected PostgreSQL/MinIO node %s (StorageClass %s, backend=%s, "
        "device_class=%s) and MLflow/k6 node %s",
        placement.storage_node,
        placement.storage_class_name,
        placement.storage_kind,
        placement.storage_device_class or "local PV",
        placement.compute_node,
    )
    return placement


def _select_lvms_placement(
    *,
    storage_classes: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    statuses: list[dict[str, Any]],
    pvs: list[dict[str, Any]],
    postgres_pvc_gi: int,
    minio_pvc_gi: int,
    read_pvc_gi: int,
    needs_read_data: bool,
) -> dict[str, Any] | None:
    if not groups:
        return None

    defaults = [
        sc
        for sc in storage_classes
        if sc.get("metadata", {})
        .get("annotations", {})
        .get("storageclass.kubernetes.io/is-default-class")
        == "true"
    ]
    default_sc = defaults[-1] if defaults else None
    needed_bytes = (postgres_pvc_gi + minio_pvc_gi) * _GIB
    candidates: list[dict[str, Any]] = []

    for group in groups:
        if group.get("metadata", {}).get("deletionTimestamp"):
            continue
        device_class = group.get("metadata", {}).get("name", "")
        if not device_class:
            continue
        selector = group.get("spec", {}).get("deviceSelector", {})
        configured_paths = (selector.get("paths") or []) + (selector.get("optionalPaths") or [])
        configured_nvme = bool(configured_paths) and all(
            _is_nvme_path(path) for path in configured_paths
        )

        sc = _storage_class_for_device_class(storage_classes, device_class)
        if sc is None or sc.get("provisioner") != "topolvm.io":
            continue
        if sc.get("volumeBindingMode") != "WaitForFirstConsumer":
            continue

        vg_selector = group.get("spec", {}).get("nodeSelector", {})
        status_by_node = _lvm_nodes_for_class(statuses, device_class)
        for node in nodes:
            name = node.get("metadata", {}).get("name")
            if not name or not _matches_node_selector(node, vg_selector):
                continue
            capacity = _topolvm_capacity(node, device_class)
            if capacity is None or capacity < needed_bytes:
                continue

            status = status_by_node.get(name)
            if status is not None:
                if status.get("status") != "Ready":
                    continue
                devices = status.get("devices", [])
                if not devices or not all(_is_nvme_path(path) for path in devices):
                    continue
            elif not configured_nvme or capacity is None:
                continue

            candidates.append(
                {
                    "storage_class_name": sc["metadata"]["name"],
                    "storage_node": name,
                    "storage_score": _node_score(node),
                    "storage_capacity": capacity or 0,
                    "device_class": device_class,
                    "storage_kind": "topolvm",
                    "postgres_pv": None,
                    "minio_pv": None,
                    "read_data_pv": None,
                    "read_data_storage_class_name": (
                        default_sc.get("metadata", {}).get("name")
                        if needs_read_data and default_sc
                        else sc["metadata"]["name"]
                    ),
                }
            )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: (item["storage_score"], item["storage_capacity"], item["storage_node"]),
        reverse=True,
    )
    for candidate in candidates:
        read_classes = [candidate["read_data_storage_class_name"]]
        if candidate["storage_class_name"] not in read_classes:
            read_classes.append(candidate["storage_class_name"])
        for read_class in read_classes:
            compute_nodes = [
                node
                for node in nodes
                if node.get("metadata", {}).get("name") != candidate["storage_node"]
                and (
                    not needs_read_data
                    or _supports_read_storage_class(
                        node=node,
                        storage_class_name=read_class,
                        storage_classes=storage_classes,
                        groups=groups,
                        statuses=statuses,
                        pvs=pvs,
                        minimum_bytes=read_pvc_gi * _GIB,
                    )
                )
            ]
            if compute_nodes:
                compute_nodes.sort(
                    key=lambda node: (_node_score(node), node["metadata"]["name"]),
                    reverse=True,
                )
                candidate["read_data_storage_class_name"] = read_class
                candidate["compute_node"] = compute_nodes[0]["metadata"]["name"]
                return candidate
    return None


def _select_lso_placement(
    *,
    storage_classes: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    pvs: list[dict[str, Any]],
    postgres_pvc_gi: int,
    minio_pvc_gi: int,
    read_pvc_gi: int,
    needs_read_data: bool,
) -> dict[str, Any] | None:
    by_class_node: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for pv in pvs:
        if pv.get("status", {}).get("phase") != "Available":
            continue
        local_path = pv.get("spec", {}).get("local", {}).get("path", "")
        if not _is_nvme_path(local_path):
            continue
        node_name = _pv_node_name(pv)
        sc_name = pv.get("spec", {}).get("storageClassName")
        if not node_name or not sc_name:
            continue
        sc = next(
            (item for item in storage_classes if item.get("metadata", {}).get("name") == sc_name),
            None,
        )
        if sc is None:
            continue
        by_class_node.setdefault((sc_name, node_name), []).append(pv)

    storage_options: list[dict[str, Any]] = []
    node_by_name = {node["metadata"]["name"]: node for node in nodes}
    for (sc_name, node_name), local_pvs in by_class_node.items():
        node = node_by_name.get(node_name)
        if node is None:
            continue
        pg_pvs = [pv for pv in local_pvs if _pv_capacity_bytes(pv) >= postgres_pvc_gi * _GIB]
        minio_pvs = [pv for pv in local_pvs if _pv_capacity_bytes(pv) >= minio_pvc_gi * _GIB]
        pair = next(
            (
                (pg, mini)
                for pg in pg_pvs
                for mini in minio_pvs
                if pg["metadata"]["name"] != mini["metadata"]["name"]
            ),
            None,
        )
        if pair is None:
            continue
        storage_options.append(
            {
                "storage_class_name": sc_name,
                "storage_node": node_name,
                "storage_score": _node_score(node),
                "storage_capacity": _pv_capacity_bytes(pair[0]) + _pv_capacity_bytes(pair[1]),
                "storage_kind": "local-storage-operator",
                "postgres_pv": pair[0]["metadata"]["name"],
                "minio_pv": pair[1]["metadata"]["name"],
                "read_data_pv": None,
                "read_data_storage_class_name": sc_name,
            }
        )

    storage_options.sort(
        key=lambda item: (item["storage_score"], item["storage_capacity"], item["storage_node"]),
        reverse=True,
    )
    for candidate in storage_options:
        compute_options = [
            node for node in nodes if node["metadata"]["name"] != candidate["storage_node"]
        ]
        if needs_read_data:
            read_pvs = [
                pv
                for pv in pvs
                if pv.get("status", {}).get("phase") == "Available"
                and _pv_capacity_bytes(pv) >= read_pvc_gi * _GIB
                and _pv_node_name(pv) != candidate["storage_node"]
                and pv.get("spec", {}).get("storageClassName")
                and any(
                    sc.get("metadata", {}).get("name") == pv["spec"]["storageClassName"]
                    for sc in storage_classes
                )
            ]
            pv_by_node = {_pv_node_name(pv): pv for pv in read_pvs if _pv_node_name(pv)}
            compute_options = [
                node for node in compute_options if node["metadata"]["name"] in pv_by_node
            ]
            if not compute_options:
                continue
            compute_options.sort(
                key=lambda node: (_node_score(node), node["metadata"]["name"]), reverse=True
            )
            compute_node = compute_options[0]["metadata"]["name"]
            read_pv = pv_by_node[compute_node]
            candidate["read_data_pv"] = read_pv["metadata"]["name"]
            candidate["read_data_storage_class_name"] = read_pv["spec"]["storageClassName"]
        else:
            compute_options.sort(
                key=lambda node: (_node_score(node), node["metadata"]["name"]), reverse=True
            )
            compute_node = compute_options[0]["metadata"]["name"] if compute_options else None
        if compute_node:
            candidate["compute_node"] = compute_node
            return candidate
    return None


def _supports_read_storage_class(
    *,
    node: dict[str, Any],
    storage_class_name: str,
    storage_classes: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    statuses: list[dict[str, Any]],
    pvs: list[dict[str, Any]],
    minimum_bytes: int,
) -> bool:
    sc = next(
        (
            item
            for item in storage_classes
            if item.get("metadata", {}).get("name") == storage_class_name
        ),
        None,
    )
    if sc is None:
        return False
    if sc.get("provisioner") == "topolvm.io":
        device_class = _device_class_from_sc(sc)
        if not device_class:
            return False
        capacity = _topolvm_capacity(node, device_class)
        if capacity is None or capacity < minimum_bytes:
            return False
        status = _lvm_nodes_for_class(statuses, device_class).get(node["metadata"]["name"])
        if status is not None:
            return status.get("status") == "Ready" and bool(status.get("devices"))
        group = next(
            (item for item in groups if item.get("metadata", {}).get("name") == device_class), None
        )
        return (
            group is not None
            and capacity is not None
            and _matches_node_selector(node, group.get("spec", {}).get("nodeSelector", {}))
        )
    if sc.get("provisioner") == "kubernetes.io/no-provisioner":
        return any(
            pv.get("status", {}).get("phase") == "Available"
            and pv.get("spec", {}).get("storageClassName") == storage_class_name
            and _pv_node_name(pv) == node["metadata"]["name"]
            and _pv_capacity_bytes(pv) >= minimum_bytes
            for pv in pvs
        )
    return True


def _storage_class_for_device_class(
    storage_classes: list[dict[str, Any]], device_class: str
) -> dict[str, Any] | None:
    for sc in storage_classes:
        if sc.get("provisioner") != "topolvm.io":
            continue
        if _device_class_from_sc(sc) == device_class:
            return sc
    return None


def _device_class_from_sc(storage_class: dict[str, Any]) -> str | None:
    parameters = storage_class.get("parameters", {}) or {}
    value = parameters.get("topolvm.io/device-class")
    if value:
        return value
    name = storage_class.get("metadata", {}).get("name", "")
    return name.removeprefix("lvms-") if name.startswith("lvms-") else None


def _lvm_nodes_for_class(
    statuses: list[dict[str, Any]], device_class: str
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in statuses:
        if item.get("metadata", {}).get("deletionTimestamp"):
            continue
        node = item.get("metadata", {}).get("name")
        for status in item.get("spec", {}).get("nodeStatus", []):
            if status.get("name") == device_class:
                result[node] = status
    return result


def _eligible_worker_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for node in nodes:
        metadata = node.get("metadata", {})
        labels = metadata.get("labels", {})
        conditions = node.get("status", {}).get("conditions", [])
        ready = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in conditions
        )
        if not ready or node.get("spec", {}).get("unschedulable"):
            continue
        if "node-role.kubernetes.io/worker" not in labels:
            continue
        blocked_taints = [
            taint
            for taint in node.get("spec", {}).get("taints", [])
            if taint.get("effect") in {"NoSchedule", "NoExecute"}
            and taint.get("key")
            not in {
                "node.kubernetes.io/not-ready",
                "node.kubernetes.io/unreachable",
            }
        ]
        if blocked_taints:
            continue
        result.append(node)
    return result


def _matches_node_selector(node: dict[str, Any], selector: dict[str, Any]) -> bool:
    if not selector:
        return True
    labels = node.get("metadata", {}).get("labels", {})
    terms = selector.get("nodeSelectorTerms", [])
    if not terms:
        return True
    for term in terms:
        if not _match_requirements(labels, term.get("matchExpressions", [])):
            continue
        fields = {"metadata.name": node.get("metadata", {}).get("name", "")}
        if _match_requirements(fields, term.get("matchFields", [])):
            return True
    return False


def _match_requirements(labels: dict[str, str], requirements: list[dict[str, Any]]) -> bool:
    for requirement in requirements:
        key = requirement.get("key", "")
        operator = requirement.get("operator", "")
        values = set(requirement.get("values", []))
        present = key in labels
        value = labels.get(key)
        if operator == "In" and (not present or value not in values):
            return False
        if operator == "NotIn" and present and value in values:
            return False
        if operator == "Exists" and not present:
            return False
        if operator == "DoesNotExist" and present:
            return False
        if operator in {"Gt", "Lt"}:
            try:
                numeric_value = int(value)
                threshold = int(next(iter(values)))
            except (TypeError, ValueError, StopIteration):
                return False
            if operator == "Gt" and numeric_value <= threshold:
                return False
            if operator == "Lt" and numeric_value >= threshold:
                return False
        if operator not in {"In", "NotIn", "Exists", "DoesNotExist", "Gt", "Lt"}:
            return False
    return True


def _topolvm_capacity(node: dict[str, Any], device_class: str) -> int | None:
    value = (
        node.get("metadata", {}).get("annotations", {}).get(f"capacity.topolvm.io/{device_class}")
    )
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _pv_node_name(pv: dict[str, Any]) -> str | None:
    terms = (
        pv.get("spec", {}).get("nodeAffinity", {}).get("required", {}).get("nodeSelectorTerms", [])
    )
    for term in terms:
        for requirement in term.get("matchExpressions", []):
            if (
                requirement.get("key") == "kubernetes.io/hostname"
                and requirement.get("operator") == "In"
            ):
                values = requirement.get("values", [])
                if values:
                    return values[0]
    return None


def _pv_capacity_bytes(pv: dict[str, Any]) -> int:
    size = pv.get("spec", {}).get("capacity", {}).get("storage", "0")
    return int(_parse_quantity(size, kind="bytes"))


def _node_score(node: dict[str, Any]) -> int:
    allocatable = node.get("status", {}).get("allocatable", {})
    cpu_milli = int(_parse_quantity(allocatable.get("cpu", "0"), kind="cpu") * 1000)
    memory_bytes = int(_parse_quantity(allocatable.get("memory", "0"), kind="bytes"))
    return cpu_milli * 1_000_000 + memory_bytes


def _parse_quantity(value: str, *, kind: str) -> Decimal:
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))([a-zA-Z]*)", str(value))
    if not match:
        raise ValueError(f"Unsupported Kubernetes quantity: {value!r}")
    try:
        amount = Decimal(match.group(1))
    except InvalidOperation as exc:
        raise ValueError(f"Unsupported Kubernetes quantity: {value!r}") from exc
    suffix = match.group(2)
    if kind == "cpu":
        factors = {"": Decimal(1), "n": Decimal("1e-9"), "u": Decimal("1e-6"), "m": Decimal("1e-3")}
        if suffix not in factors:
            raise ValueError(f"Unsupported CPU quantity: {value!r}")
        return amount * factors[suffix]

    binary = {
        f"{prefix}i": 1024**power
        for power, prefix in enumerate(("", "K", "M", "G", "T", "P", "E"))
        if prefix
    }
    decimal = {
        prefix: 1000**power
        for power, prefix in enumerate(("", "k", "M", "G", "T", "P", "E"))
        if prefix
    }
    if suffix in binary:
        return amount * binary[suffix]
    if suffix in decimal:
        return amount * decimal[suffix]
    if suffix:
        raise ValueError(f"Unsupported storage quantity: {value!r}")
    return amount


def _is_nvme_path(path: str) -> bool:
    return bool(_NVME_PATH.search(str(path)))


def _get_items(resource: str) -> list[dict[str, Any]]:
    result = oc("get", resource, "-A", "-o", "json", check=False, log_stdout=False)
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not decode cluster response for {resource}") from exc
    return value.get("items", []) if isinstance(value, dict) else []


def _remove_labels(
    node_label_key: str,
    storage_node: str,
    compute_node: str,
    pvs: list[str],
    *,
    pv_label_key: str,
) -> None:
    for node in {storage_node, compute_node}:
        oc("label", "node", node, f"{node_label_key}-", check=False, log_stdout=False)
    for pv in pvs:
        oc("label", "pv", pv, f"{pv_label_key}-", check=False, log_stdout=False)


if __name__ == "__main__":
    run.main()
