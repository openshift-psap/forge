"""Observe an already-created recipe without modifying any serving resources."""

import ipaddress
from urllib.parse import urlparse

from projects.core.dsl import entrypoint, execute_tasks, retry, task
from projects.core.dsl.utils.k8s import oc, oc_get_json

CONTROLLERS = {
    ("v1", "Pod"): "pods",
    ("apps/v1", "ReplicaSet"): "replicasets.apps",
    ("apps/v1", "StatefulSet"): "statefulsets.apps",
    ("apps/v1", "Deployment"): "deployments.apps",
    ("leaderworkerset.x-k8s.io/v1", "LeaderWorkerSet"): "leaderworkersets.leaderworkerset.x-k8s.io",
    ("serving.kserve.io/v1beta1", "InferenceService"): "inferenceservices.serving.kserve.io",
    ("serving.kserve.io/v1alpha2", "LLMInferenceService"): "llminferenceservices.serving.kserve.io",
}
PRIMARY_RESOURCES = {
    kind: resource
    for (_version, kind), resource in CONTROLLERS.items()
    if kind not in {"Pod", "ReplicaSet", "StatefulSet"}
}


@entrypoint
def run(
    *, namespace: str, kind: str, name: str, uid: str, timeout_seconds: int, port: int | None = None
) -> str:
    """Wait for a run-created primary and discover its endpoint; never synthesize a Service."""
    ctx = execute_tasks(locals())
    return ctx.endpoint_url


def get_primary(namespace: str, kind: str, name: str, uid: str) -> dict:
    primary = oc_get_json(PRIMARY_RESOURCES[kind], name=name, namespace=namespace)
    if primary.get("metadata", {}).get("uid") != uid:
        raise RuntimeError("Recipe primary UID changed; refusing to attribute another workload")
    return primary


@task
def wait_for_primary(args, ctx):
    if type(args.timeout_seconds) is not int or args.timeout_seconds <= 0:
        raise ValueError("Recipe readiness timeout must be a positive integer")
    get_primary(args.namespace, args.kind, args.name, args.uid)
    resource = f"{PRIMARY_RESOURCES[args.kind]}/{args.name}"
    if args.kind == "Deployment":
        command = ("rollout", "status", resource)
    else:
        condition = "Available" if args.kind == "LeaderWorkerSet" else "Ready"
        command = ("wait", resource, f"--for=condition={condition}")
    oc(
        *command,
        "-n",
        args.namespace,
        f"--timeout={args.timeout_seconds}s",
        timeout_seconds=args.timeout_seconds + 15,
        log_stdout=False,
    )
    get_primary(args.namespace, args.kind, args.name, args.uid)
    return "Run-created recipe primary is ready"


def owned_by(document: dict, uid: str, namespace: str, cache: dict | None = None) -> bool:
    """Follow verified controller UID chains; names, prefixes and labels are not ownership."""
    cache = {} if cache is None else cache
    visited = set()
    current = document
    while True:
        metadata = current.get("metadata", {})
        if metadata.get("namespace") != namespace:
            return False
        owners = [
            owner
            for owner in metadata.get("ownerReferences", [])
            if owner.get("controller") is True
        ]
        if len(owners) != 1:
            return False
        owner = owners[0]
        owner_uid = owner.get("uid")
        if owner_uid == uid:
            return True
        if not owner_uid or owner_uid in visited:
            return False
        visited.add(owner_uid)
        resource = CONTROLLERS.get((owner.get("apiVersion"), owner.get("kind")))
        if resource is None:
            return False
        if owner_uid not in cache:
            cache[owner_uid] = oc_get_json(
                resource, name=owner["name"], namespace=namespace, ignore_not_found=True
            )
        current = cache[owner_uid]
        if not current or current.get("metadata", {}).get("uid") != owner_uid:
            return False


def owned_pods(namespace: str, uid: str) -> list[dict]:
    cache = {}
    return [
        pod
        for pod in oc_get_json("pods", namespace=namespace).get("items", [])
        if owned_by(pod, uid, namespace, cache)
    ]


def _status_endpoint(primary: dict) -> str | None:
    status = primary.get("status", {})
    urls = []
    if primary["kind"] == "InferenceService":
        urls = [status.get("url")]
    elif primary["kind"] == "LLMInferenceService":
        address = status.get("address")
        if isinstance(address, dict) and address.get("url"):
            urls = [address["url"]]
        else:
            urls = [
                address.get("url")
                for address in status.get("addresses", [])
                if isinstance(address, dict)
            ]
    urls = list(dict.fromkeys(url for url in urls if url))
    if len(urls) > 1:
        raise ValueError("Recipe controller reports ambiguous endpoints")
    if not urls:
        return None
    url = urls[0]
    try:
        parsed = urlparse(url)
        valid = (
            isinstance(url, str)
            and parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
        )
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            valid = False
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError(
            "Recipe controller endpoint must be an unambiguous credential-free HTTP URL"
        )
    return url  # Do not append a guessed port or change the controller URL.


def _service_endpoint(service: dict, namespace: str, port: int | None) -> str | None:
    ports = service.get("spec", {}).get("ports", [])
    http = [
        entry
        for entry in ports
        if entry.get("name") in {"http", "https"} and entry.get("protocol", "TCP") == "TCP"
    ]
    candidates = http or [
        entry
        for entry in ports
        if port is not None and entry.get("port") == port and entry.get("protocol", "TCP") == "TCP"
    ]
    if not candidates:
        return None  # In particular, a portless headless LWS Service is not an HTTP endpoint.
    if (
        len(candidates) != 1
        or type(candidates[0].get("port")) is not int
        or not 1 <= candidates[0]["port"] <= 65535
    ):
        raise ValueError("Controller Service has ambiguous HTTP ports")
    selected = candidates[0]
    scheme = "https" if selected.get("name") == "https" else "http"
    return (
        f"{scheme}://{service['metadata']['name']}.{namespace}.svc.cluster.local:{selected['port']}"
    )


def _record_endpoint(args, ctx, endpoint: str) -> None:
    path = args.artifact_dir / "artifacts" / "endpoint.url"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(endpoint + "\n", encoding="utf-8")
    ctx.endpoint_url = endpoint


@retry(attempts=60, delay=10, backoff=1.0)
@task
def resolve_endpoint(args, ctx):
    primary = get_primary(args.namespace, args.kind, args.name, args.uid)
    endpoint = _status_endpoint(primary)
    if endpoint:
        _record_endpoint(args, ctx, endpoint)
        return "Resolved controller-reported recipe endpoint"
    if args.kind not in {"Deployment", "LeaderWorkerSet"}:
        services = oc_get_json("services", namespace=args.namespace).get("items", [])
        cache = {}
        endpoints = [
            endpoint
            for service in services
            if owned_by(service, args.uid, args.namespace, cache)
            if (endpoint := _service_endpoint(service, args.namespace, args.port))
        ]
        if len(endpoints) > 1:
            raise ValueError("Multiple owned HTTP Services; recipe endpoint is ambiguous")
        if endpoints:
            _record_endpoint(args, ctx, endpoints[0])
            return "Resolved ownership-verified controller Service"
        return False, "Waiting for controller-reported or owned HTTP endpoint"
    if args.port is None:
        raise ValueError("Service-free endpoint requires a source-declared unambiguous HTTP port")
    pods = owned_pods(args.namespace, args.uid)
    if args.kind == "LeaderWorkerSet":
        pods = [
            pod
            for pod in pods
            if pod.get("metadata", {})
            .get("labels", {})
            .get("leaderworkerset.sigs.k8s.io/worker-index")
            == "0"
        ]
    pods = [
        pod
        for pod in pods
        if not pod.get("metadata", {}).get("deletionTimestamp")
        and any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in pod.get("status", {}).get("conditions", [])
        )
    ]
    if not pods:
        return False, "Waiting for an owned Ready endpoint pod"
    if len(pods) != 1:
        raise ValueError("Multiple owned endpoint pods; cannot select a single recipe endpoint")
    address = pods[0].get("status", {}).get("podIP")
    if not address:
        return False, "Waiting for endpoint pod IP"
    ip = ipaddress.ip_address(address)
    host = f"[{ip}]" if ip.version == 6 else str(ip)
    _record_endpoint(args, ctx, f"http://{host}:{args.port}")
    return "Resolved owned Ready endpoint pod without creating a Service"


if __name__ == "__main__":
    run.main()
