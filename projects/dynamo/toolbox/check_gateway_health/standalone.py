#!/usr/bin/env python3
"""
Standalone Dynamo gateway health check — no Forge dependency.
Just needs: python3, oc (logged in to cluster).

Usage:
  python3 standalone.py --namespace forge-dynamo-test
  python3 standalone.py --namespace forge-dynamo-test --gateway inference-gateway --model Qwen/Qwen3-0.6B
"""

import argparse
import json
import re
import subprocess
import sys


PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"
INFO = "\033[34mINFO\033[0m"
BOLD = "\033[1m"
RESET = "\033[0m"


def oc(*args, check=False):
    r = subprocess.run(["oc"] + list(args), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"oc {' '.join(args)} failed: {r.stderr}")
    return r


def oc_json(*args):
    r = oc(*args, "-o", "json")
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def step(num, title):
    print(f"\n{'='*60}")
    print(f"  Step {num}: {title}")
    print(f"{'='*60}")


def result(tag, msg):
    print(f"  [{tag}] {msg}")


def fix(title, cmd):
    print(f"  {BOLD}Fix — {title}{RESET}")
    for line in cmd.strip().split("\n"):
        print(f"    $ {line}")


def check_gateway_pod(ns, gw):
    step(1, "Gateway proxy pod running?")

    data = oc_json("get", "pods", "-n", ns, "-l", f"gateway.networking.k8s.io/gateway-name={gw}")
    pods = (data or {}).get("items", [])

    if not pods:
        result(FAIL, "No gateway proxy pods found")
        r = oc("get", "gatewayclass", "--no-headers")
        if not r.stdout.strip():
            fix("No GatewayClass — install agentgateway",
                "helm upgrade -i agentgateway-crds oci://cr.agentgateway.dev/charts/agentgateway-crds \\\n"
                "  --create-namespace --namespace agentgateway-system --version v1.0.0\n"
                "helm upgrade -i agentgateway oci://cr.agentgateway.dev/charts/agentgateway \\\n"
                "  --namespace agentgateway-system --version v1.0.0 \\\n"
                "  --set inferenceExtension.enabled=true --wait")
        return False, None

    pod = pods[0]
    name = pod["metadata"]["name"]
    phase = pod["status"].get("phase", "?")
    cs = pod["status"].get("containerStatuses", [])
    ready = all(c.get("ready", False) for c in cs)
    restarts = sum(c.get("restartCount", 0) for c in cs)

    if phase == "Running" and ready:
        result(PASS, f"{name} Running/Ready (restarts={restarts})")
        return True, name

    if restarts > 3:
        result(FAIL, f"{name} CrashLooping (restarts={restarts})")
        logs = oc("logs", name, "-n", ns, "--previous", "--tail=30")
        lt = logs.stdout or ""
        if "too many open files" in lt.lower():
            fix("kgateway fd exhaustion — switch to agentgateway",
                "# kgateway loads ALL cluster endpoints.\n"
                "# Install agentgateway and recreate Gateway with gatewayClassName: agentgateway")
        elif "x509" in lt or "unknown authority" in lt.lower():
            fix("Istio CA mismatch",
                "# Compare fingerprints:\n"
                "oc get cm istio-ca-root-cert -n istio-system -o jsonpath='{.data.root-cert\\.pem}' | openssl x509 -noout -fingerprint\n"
                "oc get secret istio-ca-secret -n istio-system -o jsonpath='{.data.ca-cert\\.pem}' | base64 -d | openssl x509 -noout -fingerprint")
        else:
            errors = [l for l in lt.split("\n") if any(k in l.lower() for k in ["error", "fatal", "critical"])]
            for e in errors[:3]:
                result(INFO, e.strip()[:120])
        return False, name

    # Check SCC events
    ev = oc_json("get", "events", "-n", ns, "--field-selector", "reason=FailedCreate")
    for e in (ev or {}).get("items", []):
        msg = e.get("message", "")
        if "forbidden" in msg.lower() and gw in e.get("involvedObject", {}).get("name", ""):
            if "NET_BIND_SERVICE" in msg:
                result(FAIL, "SCC blocks NET_BIND_SERVICE")
                fix("Patch SCC + grant to SA",
                    f"oc patch scc dynamo-frontend-scc --type merge -p '{{\"allowedCapabilities\":[\"NET_BIND_SERVICE\"]}}'\n"
                    f"oc adm policy add-scc-to-user dynamo-frontend-scc -z {gw} -n {ns}\n"
                    f"oc rollout restart deployment {gw} -n {ns}")
            else:
                result(FAIL, f"SCC: {msg[:100]}")
                fix("Grant SCC",
                    f"oc adm policy add-scc-to-user dynamo-frontend-scc -z {gw} -n {ns}\n"
                    f"oc rollout restart deployment {gw} -n {ns}")
            return False, name

    result(FAIL, f"{name} phase={phase} ready={ready}")
    return False, name


def check_gateway_programmed(ns, gw):
    step(2, "Gateway programmed?")
    data = oc_json("get", f"gateway/{gw}", "-n", ns)
    if not data:
        result(FAIL, f"Gateway '{gw}' not found")
        return False

    conds = {c["type"]: c for c in data.get("status", {}).get("conditions", [])}
    if conds.get("Accepted", {}).get("status") != "True":
        result(FAIL, f"Not accepted: {conds.get('Accepted', {}).get('message', '?')[:80]}")
        return False
    if conds.get("Programmed", {}).get("status") == "True":
        result(PASS, f"Programmed (class={data['spec']['gatewayClassName']})")
        return True
    result(FAIL, f"Not programmed: {conds.get('Programmed', {}).get('message', '?')[:80]}")
    return False


def check_httproute(ns, gw, route_name=None):
    step(3, "HTTPRoute accepted?")
    data = oc_json("get", "httproutes", "-n", ns)
    items = (data or {}).get("items", [])
    if not items:
        result(FAIL, "No HTTPRoutes found")
        return False, None

    route = items[0] if not route_name else next((r for r in items if r["metadata"]["name"] == route_name), items[0])
    rname = route["metadata"]["name"]
    pool = None
    ok = True

    for p in route.get("status", {}).get("parents", []):
        gwn = p.get("parentRef", {}).get("name", "?")
        conds = {c["type"]: c for c in p.get("conditions", [])}
        acc = conds.get("Accepted", {})
        ref = conds.get("ResolvedRefs", {})

        if acc.get("status") != "True":
            msg = acc.get("message", "?")
            result(FAIL, f"'{rname}' NOT accepted by '{gwn}': {msg[:80]}")
            if "namespace" in msg.lower():
                fix("Namespace restriction", f"oc get gateway {gwn} -o jsonpath='{{.spec.listeners[0].allowedRoutes.namespaces}}'")
            ok = False
        elif ref.get("status") != "True":
            result(FAIL, f"'{rname}' refs unresolved on '{gwn}': {ref.get('message','?')[:80]}")
            if "InferencePool" in ref.get("message", ""):
                fix("Pool not found", f"oc get inferencepools -n {ns}\noc get dynamographdeployment -n {ns}")
            ok = False
        else:
            result(PASS, f"'{rname}' accepted by '{gwn}'")

    for rule in route["spec"].get("rules", []):
        for ref in rule.get("backendRefs", []):
            if ref.get("kind") == "InferencePool":
                pool = ref["name"]
    return ok, pool


def check_epp(ns):
    step(4, "EPP running?")
    data = oc_json("get", "pods", "-n", ns, "-l", "nvidia.com/dynamo-component-type=epp")
    pods = (data or {}).get("items", [])
    if not pods:
        result(FAIL, "No EPP pods")
        return False

    pod = pods[0]
    name = pod["metadata"]["name"]
    cs = pod["status"].get("containerStatuses", [])
    ready = all(c.get("ready", False) for c in cs)

    if pod["status"].get("phase") == "Running" and ready:
        result(PASS, f"{name} Running/Ready")
        return True

    result(WARN, f"{name} phase={pod['status'].get('phase','?')} ready={ready}")

    workers = oc_json("get", "pods", "-n", ns, "-l", "nvidia.com/dynamo-component-class=worker")
    w_items = (workers or {}).get("items", [])
    w_ready = [w for w in w_items if all(c.get("ready", False) for c in w["status"].get("containerStatuses", []))]
    result(INFO, f"Workers: {len(w_ready)}/{len(w_items)} ready — EPP waits for worker discovery")
    return len(w_ready) > 0


def check_extproc(ns, gw, gw_pod):
    step(5, "Gateway → EPP (ext_proc)?")
    svc = f"inference-gateway.{ns}.svc.cluster.local"
    r = oc("run", "gw-chk", "-n", ns, "--rm", "-i", "--restart=Never",
           "--image=curlimages/curl:8.11.1", "--",
           "curl", "-s", "--max-time", "10", f"http://{svc}/v1/models")
    resp = r.stdout or ""

    if r.returncode == 0 and "data" in resp:
        try:
            models = [m.get("id", "?") for m in json.loads(resp).get("data", [])]
            result(PASS, f"ext_proc working — models: {', '.join(models)}")
        except Exception:
            result(PASS, "ext_proc working")
        return True

    if r.returncode == 0 and resp == "":
        result(FAIL, "500 empty body — Istio sidecar intercepting ext_proc")
        if gw_pod:
            pd = oc_json("get", f"pod/{gw_pod}", "-n", ns)
            if pd:
                cnames = [c["name"] for c in pd["spec"]["containers"]]
                if "istio-proxy" in cnames:
                    fix("Exclude sidecar from gateway proxy",
                        f"# Add annotation sidecar.istio.io/inject: 'false' to gateway proxy pod template")
        return False

    result(FAIL, f"Gateway unreachable (exit={r.returncode})")
    if resp:
        result(INFO, resp[:100])
    return False


def check_smoke(ns, model):
    step(6, "Inference smoke test")
    svc = f"inference-gateway.{ns}.svc.cluster.local"
    payload = json.dumps({"model": model, "prompt": "test", "max_tokens": 5, "temperature": 0})
    r = oc("run", "smoke-chk", "-n", ns, "--rm", "-i", "--restart=Never",
           "--image=curlimages/curl:8.11.1", "--",
           "curl", "-s", "--max-time", "30", "-X", "POST",
           f"http://{svc}/v1/completions",
           "-H", "Content-Type: application/json", "-d", payload)
    resp = r.stdout or ""
    if r.returncode == 0 and resp:
        # Check for success patterns even if JSON is truncated by oc run
        if '"choices"' in resp:
            try:
                data = json.loads(resp)
                tokens = data.get("usage", {}).get("total_tokens", "?")
            except json.JSONDecodeError:
                tokens = "?"
            result(PASS, f"Inference OK — {tokens} tokens")
            return True
        try:
            data = json.loads(resp)
            if "Worker ID required" in data.get("message", ""):
                result(FAIL, "EPP not setting routing headers — check InferencePool endpointPickerRef")
                return False
            if "RoutingFailed" in data.get("message", ""):
                result(FAIL, "EPP can't find eligible workers")
                fix("Check discovery", f"oc logs -n {ns} -l nvidia.com/dynamo-component-type=epp --tail=20")
                return False
            result(FAIL, data.get("message", resp[:120]))
            return False
        except json.JSONDecodeError:
            result(FAIL, f"Unparseable response: {resp[:120]}")
            return False
    result(FAIL, f"Smoke failed (exit={r.returncode}): {resp[:100]}")
    return False


def main():
    p = argparse.ArgumentParser(description="Dynamo Gateway Stack Health Check")
    p.add_argument("--namespace", "-n", required=True)
    p.add_argument("--gateway", "-g", default="inference-gateway")
    p.add_argument("--httproute", default=None)
    p.add_argument("--model", "-m", default="Qwen/Qwen3-0.6B")
    p.add_argument("--skip-smoke", action="store_true")
    args = p.parse_args()

    print(f"\n{BOLD}Dynamo Gateway Health Check{RESET}")
    print(f"  namespace: {args.namespace}  gateway: {args.gateway}  model: {args.model}\n")

    ok, gw_pod = check_gateway_pod(args.namespace, args.gateway)
    if not ok:
        print(f"\n{'='*60}\n  [{FAIL}] Stopped at Step 1\n{'='*60}")
        sys.exit(1)

    if not check_gateway_programmed(args.namespace, args.gateway):
        print(f"\n{'='*60}\n  [{FAIL}] Stopped at Step 2\n{'='*60}")
        sys.exit(1)

    route_ok, pool = check_httproute(args.namespace, args.gateway, args.httproute)
    if not route_ok:
        print(f"\n{'='*60}\n  [{FAIL}] Stopped at Step 3\n{'='*60}")
        sys.exit(1)

    if not check_epp(args.namespace):
        print(f"\n{'='*60}\n  [{FAIL}] Stopped at Step 4\n{'='*60}")
        sys.exit(1)

    if not check_extproc(args.namespace, args.gateway, gw_pod):
        print(f"\n{'='*60}\n  [{FAIL}] Stopped at Step 5\n{'='*60}")
        sys.exit(1)

    if not args.skip_smoke:
        if not check_smoke(args.namespace, args.model):
            print(f"\n{'='*60}\n  [{FAIL}] Stopped at Step 6\n{'='*60}")
            sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  [{PASS}] All checks passed — stack is healthy")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
