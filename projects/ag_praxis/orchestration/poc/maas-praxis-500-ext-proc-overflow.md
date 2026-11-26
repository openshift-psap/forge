# maas/praxis: HTTP 500 storm at high concurrency — investigation record

Investigation, 2026-11-12/13, cluster `kpouget-llmd.ibm.rhperfscale.org`.
Three distinct findings; the first two are confirmed by direct experiment, the third by
live measurement during a failing run.

**Severity note**: the plain `maas` endpoint (praxis disabled) is no longer a supported
setup — `maas/praxis` is the only supported path through the MaaS gateway. Everything
documented here therefore limits the supported path itself: on this build, the praxis
path serves roughly 20–40 concurrent users before the auth stage collapses (see Finding
4). Finding 5 chased the TokenReview-throttling explanation (RHAI-48802) and **refuted
it by measurement**: the benchmark adds zero Kubernetes reviews — the collapse is
authorino's per-request CPU cost on one connection-pinned pod (Findings 3/4, confirmed
by Prometheus in Finding 5).

---

## Finding 1: the praxis ext_proc concurrency budget (default: 8)

### Symptom

The ag_praxis `maas/praxis` endpoint returns almost 100% HTTP 500 at 2048 concurrent
users, while the plain `maas` and `svc` endpoints pass. The failures are **instant**
(13–50 ms TTFB), **empty-bodied**, with no `server:`/`x-envoy-upstream-service-time`
headers. An unauthenticated curl against the same URL returns *either* the empty 500
*or* a proper 401 (`x-ext-auth-reason`) — i.e. **when the 500 happens, authentication is
never checked**: the request dies in the Envoy filter chain before `ext_authz`.

### Evidence

Envoy access log, `%RESPONSE_CODE_DETAILS%`:

```
"POST /forge-praxis/sim-llm-maas/v1/responses HTTP/2" 500 -
  ext_proc_error_gRPC_error_14{upstream_connect_error_or_disconnect/reset_before_headers.
  _reset_reason:_overflow{upstream_reset_before_response_started{overflow}}}
  - "-" 0 0 0 ...
```

`ext_proc_error` (an external processing filter failed) + `gRPC_error_14` (UNAVAILABLE)
+ `reset_reason: overflow` (the cluster's circuit breaker tripped) + `0 0 0` (nothing
reached an upstream). Envoy generates its own empty 500 local reply — hence no body and
auth never running.

### Root cause

The praxis ext_proc filters (`envoy.filters.http.ext_proc.ipp-pre` / `.ipp`, seen in the
gateway's `config_dump`) target dedicated synthetic clusters `payload-pre-processing-extproc`
and `payload-processing-extproc`. Those clusters are defined — with a deliberate, commented
concurrency budget — in the vendored EnvoyFilter overlay
`ai-gateway-controller/config/manifests/praxis-extproc/overlays/odh/envoy-filter.yaml`
(upstream: opendatahub-io/praxis-extproc):

```yaml
circuit_breakers:
  thresholds:
    - max_connections: 8        # "Default ExtProc concurrency budget
      max_pending_requests: 8   #  (paired with max_body_bytes 10 MiB)."
      max_requests: 8
http2_protocol_options:
  max_concurrent_streams: 8
```

These are synthetic clusters (bare names, no Istio service host), so **DestinationRules
cannot reach them** — the `payload-*` DestinationRules apply to the unrelated
`outbound|9004||...` service clusters.

### Proof

Concurrency sweep on the praxis endpoint: **8 users succeed, 16 fail.** The breaking
point equals the configured budget exactly. Corollaries:

- Throughput is pinned at `8 / extproc_latency` (~160 req/s observed) regardless of user
  count — the flat 800–1200-user results were all saturated at the same ceiling, not scaling.
- Above the budget, failure is instant while successes are slow, so a closed-loop load
  generator immediately retries → attempt rate explodes → the failure fraction snaps to
  ~100% (positive feedback).
- The budget is **gateway-global** (clusters live in `openshift-ingress`), shared across
  all tenants/routes, and survives namespace redeploys.

### Fix

The controller supports a per-tenant annotation (read from the MaasTenantConfig), which
writes one value into all four limits on all three ext_proc clusters:

```sh
oc annotate maastenantconfig default-tenant -n models-as-a-service \
    maas.opendatahub.io/experimental-extproc-max-requests=1024 --overwrite
```

Caveats:

- **Ceiling is 1024** (`MaxExtProcMaxRequests`). A 2048-user run cannot be configured to
  pass; 1024 users is the hard maximum on this path.
- **Out-of-range values silently fall back to 8** (not clamped to 1024) — always verify
  with `show-active-rules.sh` after annotating.
- Verify: `./show-active-rules.sh payload` → all limits read 1024.

After raising to 1024, the 500s at 2048 users changed signature (see Finding 3): the
ext_proc overflow was gone, and the bottleneck moved to authentication.

---

## Finding 2: token metering is broken on /v1/responses (100% of successful requests)

### Evidence

During a window with **zero** 500s, the gateway log showed exactly, per successful
request: 3 × `Missing json property: /model`, 1 × `Missing json property:
/usage/total_tokens`, 1 × `Task failed`. 18372 failing requests in another window
contributed **zero** additional warnings — the shim's metering runs only on successful
responses and fails on every one of them, deterministically.

### Root cause

The kuadrant wasm-shim config (embedded in the gateway's wasm filter, visible in
`config_dump`) declares:

```json
"metrics.labels.model": "responseBodyJSON(\"/model\")"
```

and the token-rate-limit usage extraction reads `/usage/total_tokens` the same way: the
shim parses the **response body as a single JSON document**. guidellm uses
`request_format: /v1/responses` (workloads.yaml) with SSE streaming, so the body is a
sequence of `data: {...}` events — no single JSON document, so every extraction fails.

The `TokenRateLimitPolicy` (generated from `MaaSSubscription.tokenRateLimits`,
e.g. `maas-trlp-sim-llm-maas`) reports `Enforced`, but its counters
(`auth.identity.selected_subscription_id`) are fed by the usage extraction, so nothing is
metered.

### Consequence

Token-based rate limiting fails **open**: requests return 200, no quota is counted or
enforced. No client-visible error — it survives green benchmark runs unnoticed. Any run
meant to characterize MaaS with token quotas is not exercising the quota path.

### Related upstream work

ai-gateway-controller PRs #249/#250 (merged 2026-11-10, *after* the tested build) change
`response_body_mode` BUFFERED→STREAMED and add the
`maas.opendatahub.io/experimental-extproc-response-body-mode` annotation. They do **not**
touch the concurrency budget or the JSON-path extraction, so neither fixes this. The
tested build predates them (config_dump showed `response_body_mode: BUFFERED`).

---

## Finding 3: with the budget raised, the bottleneck moves to authorino (auth fails closed)

After annotating the budget to 1024 and re-running 2048 users, the 500s returned with a
**different** signature: empty details, ~1.5 KB in / 23 B out, **~800 ms** duration —
paired with `kuadrant_wasm_shim: gRPC status code is not OK` in the gateway log.

The wasm shim config names the policy:

```json
"services": {
  "auth-service":            {"failureMode": "deny", "timeout": "200ms", "type": "auth"},
  "ratelimit-check-service": {"failureMode": "allow", "timeout": "100ms"},
  ...
}
```

Auth **fails closed at 200 ms**; rate limiting fails open. Live measurements during the
failing run:

- Gateway: **~540 RPS** (closed-loop retry on instant failures).
- authorino: **1 replica, no resource requests/limits, 5.6 cores** — CPU-saturated
  (limitador idle at 1m; the failing gRPC is auth, not rate limiting).
- authorino log: **30,490 CEL errors in ~15 min** — `has(auth.identity.subscription_info.
  costCenter)` etc. failing with `no such key` on every request, each logged at error
  level with a full stack trace.

Why the CEL fails: the shim's `requestData` metric labels
(`metrics.labels.cost_center`, `..._organization_id`, `..._subscription`) read
`auth.identity.subscription_info.*` / `auth.identity.selected_subscription`. Those keys
are only set by the AuthConfig's *response*-phase `dynamicMetadata.identity.json` — they
do not exist in `auth.identity` while metric labels are evaluated, so every evaluation
errors. The expressions live in the shim config, not in any CR on the cluster.

Where the shim config comes from (traced): the EnvoyFilter
`kuadrant-maas-default-gateway` in `openshift-ingress` (owned by the Gateway, labeled
`kuadrant.io/wasm: "true"`) carries the full wasm-shim config, including the
`requestData.metrics.labels.*` block with MaaS-specific literals
(`tenant_name`/`tenant_namespace` = "models-as-a-service"). It is generated by the **RHCL
kuadrant-operator** (`registry.redhat.io/rhcl-1/rhcl-rhel9-operator`), which also serves
the shim binary itself (`plugin.wasm` from its `:8082` service, pinned by sha256 in the
same EnvoyFilter). Neither the expressions nor the `requestData` schema exist in
upstream kuadrant-operator or Kuadrant/wasm-shim `main` (checked: `internal/wasm/types.go`
has no RequestData type; wasm-shim `configuration.rs` parses no metrics.labels) — this is
RHCL-downstream code, so the fix belongs in the Red Hat forks. The intended design is
per-tenant usage metering metrics (cost_center / organization_id / subscription / model
labels, paired with the TRLP token reporting) — but the label expressions were written
against the shim's post-auth view of `auth.identity` (where the response-phase
dynamicMetadata has been merged — the same view the TRLP counters successfully use) while
authorino evaluates them mid-pipeline against its own `auth.identity` (authentication
output only). One phrase, two meanings, across the gRPC boundary.

Why authorino is expensive at 540 RPS even with cached metadata/authz (ttl 60): the
`subscription-info` metadata **cache key** is itself a multi-hundred-character nested
CEL expression (regexes included) that must be evaluated on *every request* to perform
the cache lookup; add the shim's five metric-label CELs per config evaluation, the
massive identity-JSON response CELs, and OPA. All per request.

### Status

Not yet mitigated. Candidate levers, in order of practicality:

1. Scale authorino (replicas / resource requests) — it currently has none.
2. Reduce the per-request CEL cost (upstream: the cache-key expression, the metric-label
   expressions referencing not-yet-existing identity keys).
3. Raise the shim's 200 ms auth timeout — trades latency for failure rate.

---

## Finding 4: scaling authorino does not help — auth traffic pins to one pod

After scaling authorino to 3 replicas (supported: `spec.replicas` on the Authorino CR),
a 256-user run still failed **100%** (9,209 × 500, zero successful auth calls — the auth
cluster's `rq_success` counter did not move). The reason, from the gateway's live cluster
stats (`pilot-agent request GET clusters`):

```
kuadrant-auth-service::172.30.49.1:50051::cx_active::2      ← all auth on 2 connections
kuadrant-auth-service::172.30.49.1:50051::cx_total::6       ← 6 ever created
kuadrant-auth-service::172.30.49.1:50051::rq_total::131955
kuadrant-auth-service::172.30.49.1:50051::rq_timeout::113561   ← 86% time out
kuadrant-auth-service::172.30.49.1:50051::rq_success::1679     ← 1% succeed
```

- All auth traffic rides **2 long-lived HTTP/2 connections**, each conntrack-pinned to
  whichever authorino pod answered when it opened. During the failing run: one replica at
  **2514m CPU**, the other two at **1m — idle**. Two thirds of the capacity added by
  scaling has never received a request.
- `rq_active` plateaus at ~205 — just under the per-connection stream limit a Go gRPC
  server advertiates (~250) — so Envoy never opens a third connection that could reach
  the idle pods.
- With one pod authenticating at ~40–80 RPS (Finding 3's CEL cost) and the shim's 200 ms
  fail-closed deadline, the **startup burst** of N concurrent users must be absorbed in
  200 ms: the cliff sits around **N ≈ 20–40**. Above it, closed-loop retry amplification
  turns partial failure into ~100% failure (256 users → ~660 RPS of attempts, all timing
  out). 8 users: 224/224 auth calls succeed; 256 users: 0.

### Attempted fix: max_concurrent_streams — wrong lever

The first attempt capped `http2_protocol_options.max_concurrent_streams: 16` on the
`kuadrant-auth-service` cluster, intending to force Envoy to open many connections and
spread across all replicas. Two problems:

1. **Wrong lever**: `max_concurrent_streams` caps streams per connection, so Envoy opens
   more connections — but they still go through the same ClusterIP and kube-proxy can
   land them on the same pod. The right lever is `max_requests_per_connection`, which
   forces connections to **close and reopen** after N requests, giving kube-proxy
   repeated chances to route to a different pod.

2. **Wrong field**: the operator ADDs the cluster with the legacy top-level
   `http2_protocol_options: {}`, but Envoy may internally represent HTTP/2 config under
   `typed_extension_protocol_options`. A MERGE targeting the wrong field is silently
   ignored — the patch never appeared in `config_dump`.

3. **MERGE-on-ADD limitation**: every EnvoyFilter MERGE path to this cluster was tested
   and failed:
   - MERGE by cluster name: silent no-op.
   - MERGE by cluster service (mirroring the operator's own ADD match): also silent no-op.
   - Direct edit of the operator's EnvoyFilter (`kuadrant-auth-maas-default-gateway`):
     reverted immediately by the kuadrant-operator's reconcile.
   - No connection knobs on the `Kuadrant` CR (only mtls and log levels).

### Revised approach: max_requests_per_connection

Updated [`auth-connection-spread.yaml`](auth-connection-spread.yaml) to use
`max_requests_per_connection: 100` instead. This is a top-level cluster field (not
nested in protocol options), so it avoids the field-mismatch issue. If it lands in
`config_dump`, watch `cx_total` climb to confirm connections are cycling and load is
spreading across pods.

If the MERGE-on-ADD limitation still prevents the patch from landing, the fix must come
from upstream.

### Upstream fix (RHCL 1.5+)

The proper fix is making the Authorino authorization service **headless**
(`clusterIP: None`), so STRICT_DNS returns one A record per pod IP and Envoy gets one
upstream host per replica — no kube-proxy involvement at all. This is implemented in
kuadrant-operator PR #2281, expected in RHCL 1.5.

**Caveat**: a regression was reported in MaaS — reconciling the service on upgrades by
deleting and recreating it breaks custom annotations used by MaaS. Status unknown.

For 1.4.x, the remaining levers are:
1. `max_requests_per_connection` via EnvoyFilter (if the MERGE reaches the cluster).
2. Scale authorino (useful only once connections actually spread).
3. Reduce the per-request CEL cost (upstream).
4. Raise the shim's 200 ms auth timeout — trades latency for failure rate.

### Consequences

- Scaling authorino replicas is useless while connections pin to one pod.
- A gateway pod restart re-pins at most 2 connections → at most 2 pods, randomly.
- On this build the supported path serves ~20–40 concurrent users; the precise cliff is
  worth measuring (16/32/64 sweep) for the report and for benchmark envelopes.

---

## Finding 5: the TokenReview-throttle theory — refuted by measurement; the auth cliff is authorino-internal (RHAI-48802, CONNLINK-2020)

The maas path is guarded by `openshift-ingress/maas-gateway-auth` (AuthPolicy,
generated by maas-controller, owned by the maas-default-gateway Gateway; fetched
2026-11-22, generation 46 — `AuthPolicy.maas-gateway-auth.yaml`). Its structure:

- `sk-oai-` API keys authenticate via the plain selector + the `apiKeyValidation`
  HTTP call to maas-api + OPA — **no Kubernetes API call on that leg**.
- **Five rules already carry `cache` stanzas (ttl 60)**: `auth-valid`,
  `require-group-membership`, `subscription-valid` (authorization) and
  `apiKeyValidation`, `subscription-info` (metadata) — shipped by maas-controller
  (policy recreated 2026-11-18, after the Finding 3/4 investigation). Whether they
  actually store anything depends on the entry-size limit below.
- The remaining Kubernetes review, `authentication/openshift-identities`, is an
  **ungated `kubernetesTokenReview`** (priority 2, no `when`): it sits in the
  evaluation path of every request, not just OpenShift-token bearers.

A cluster review (2026-11-23, `show` mode — [`show.txt`](show.txt)) adds two facts:

- **`openshift-ingress/openshift-ai-inference-authn`** — the serving AuthPolicy of
  RHAI-48802 — is also on this cluster, with exactly the recipe's **five uncached
  review rules**: `kubernetes-user` (TokenReview) + `inference-access`,
  `inference-access-delegate`, `model-access-path`, `model-access-path-delegate`
  (SubjectAccessReviews). These are "the five AuthPolicy cache changes". None of
  its translations appear among the generated AuthConfigs — check its status
  conditions before counting on that hop being enforced.
- **kuadrant-system holds ~18 hash-named AuthConfigs mirroring `maas-gateway-auth`**
  — kuadrant's generated translations (one per attached route), the effective
  configs authorino actually evaluates. They must be patched via the AuthPolicy,
  never directly.

Two failure modes, both established by the AI-grid campaign on the same RHCL build:

1. **TokenReview client-side throttling** (RHAI-48802, Blocker): authorino's
   Kubernetes client is capped at **5 requests/s, burst 10, per pod**. Whenever
   reviews fire per request, past that budget they queue for seconds behind
   `Waited for ... due to client-side throttling`, the kuadrant wasm shim fails auth
   **closed at 200 ms**, and the gateway returns the empty ~800 ms 500s of Findings
   3/4 — regardless of replica count. Finding 4's numbers looked like a fit at
   first (1,679 successful auths out of 131,955 attempts ≈ a ~5/s budget), but the
   measurement verdict below shows `openshift-identities` is **not** evaluated for
   `sk-oai-` bearers at all — that success ratio was the one-pod CPU ceiling, not
   a review budget.
2. **Silent cache no-store** (CONNLINK-2020, Major): authorino's evaluator cache
   (freecache) silently refuses entries larger than **1/1024 of its size** — at the
   default 1 MB that is 1 kB. The `apiKeyValidation` and `auth-valid` cache keys
   embed the raw API key; if it exceeds ~1 kB, **all five caches store nothing**,
   every request pays the full auth pipeline, and neither authorino's logs nor its
   metrics reveal it.

### Workaround

1. Add a cache to every uncached review rule in both AuthPolicies: the
   `openshift-identities` TokenReview of `maas-gateway-auth` and the five review
   rules of `openshift-ai-inference-authn` — TokenReview caches keyed on the
   Authorization header, SubjectAccessReview caches on the Authorization header
   **and the request path** (SAR decisions depend on the reviewed resource — a
   credential-only key would serve one model's approval to another). ttl 60,
   matching the style of the existing caches.
2. `spec.evaluatorCacheSize: 3` on the Authorino CR.

In RHAI-48802's campaign the equivalent change (cached reviews + evaluatorCacheSize
3) took a 150 RPS run from 7,368/9,000 successes (1,285 × 401, 347 × 500, median
1,774 ms) to 9,000/9,000, p50 164 ms — and the path then ran clean to 5,000 RPS
(599,999/600,000, p99 248 ms).

Apply with [`auth-review-caching.sh`](auth-review-caching.sh) (`show` / `apply` /
`revert`, plus a `rate` mode that samples the apiserver TokenReview and
SubjectAccessReview rates — it also settles which hop actually fires on this
cluster — and garbage-token/denied-request safety checks that the caches are keyed
per credential). Caveats:

- maas-gateway-auth is generated — a reconcile can revert the patch; the script is
  idempotent, re-run it after MaaS reconciles and before each campaign. The merged
  3.6 fixes (red-hat-data-services/odh-model-controller#1869, odh-gitops#69)
  address the grid's SA-token path through an opt-in certificate path; they do not
  cover this policy's API-key traffic, so the manual patch remains the lever here.
- Authorino memory grows with the bigger cache (observed 1.7–2.3 GiB/pod before,
  4.3–5.3 GiB after — with 199 AuthConfigs; far fewer here, expect less). Authorino
  runs without requests/limits on this cluster (Finding 3) — watch node memory.
- Auth still costs authorino CPU per request (RHAI-48802: ~6 ms even cached): the
  CEL cost of Finding 3 remains, and Finding 4's connection pinning still caps a
  single pod's share. Only the throttling waits and repeated reviews disappear.
- With the workloads' single shared API key, auth is exercised once per TTL — fine
  for inference benchmarking, wrong for any auth-focused run (same caveat class as
  Finding 2's quota note).

### Status

**Theory refuted by measurement** — see the verdict below. The review caches and
`evaluatorCacheSize` change nothing on this path; the live levers are Finding 3/4's.

**Lever 1 measured (2026-11-23, 16:11 run, identity-metrics off, verified held):**
authorino CPU per successful request dropped 97 → **68 ms (−30 %)** — a partial
win, not the 3–8× hoped: the `metrics` flag gates only part of the `metricLabels`
work; the filter's payload CELs still evaluate. But the run still failed ~65 %
(~26k 500s vs ~14k 200s) because **authorino was no longer the binding
constraint** — the ext_proc concurrency budget was: every in-flight streaming
request holds an ext_proc stream for its full ~5–7.5 s response (sim model:
30 ms inter-token latency), so admission ceiling ≈ budget / duration ≈
256 / 6.5 s ≈ **40 rps** — matching the observed 28–48 rps of successes. Beyond
it, Finding 1's overflow signature: instant sub-ms 500s, no response flag, never
reaching auth (the auth cluster saw only ~15k calls ≈ the successes; 99.7 % of
them succeeded). The morning runs showed the same instant regime *plus* deadline
kills because authorino was slower then. Note the annotation is harness-set
(`platform.yaml: praxis.extproc_max_requests: 256`, max 1024): raising it to 1024
moves the ceiling to ~140–160 rps, at which point authorino CPU (one pinned pod)
becomes the next ceiling — the levers stack.

### Measurement verdict (Prometheus/thanos, Nov 20 window, queried 2026-11-23)

The kube-apiserver was healthy all window (18 5xx = background noise, **0 APF
rejections**, max inflight 57, p99 at the quiet-baseline profile) — which is what
client-side throttling would also look like, so it settled nothing. What did:

- **Reviews: the benchmark added zero.** TokenReviews served in the window:
  **4,307** on Nov 20 vs **4,296** (Nov 19) and **4,115** (Nov 21) in the same
  hours — identical to quiet days. SubjectAccessReviews: **23,621** vs 24,133 /
  23,923 — pure background (kube-scheduler alone: 254). So `openshift-identities`
  is never evaluated for `sk-oai-` bearers — the plain `api-keys` selector at
  priority 0 succeeds and authorino stops there — and the serving-hop policy
  (`openshift-ai-inference-authn`) is not in this path, consistent with its absence
  from the generated AuthConfigs. **RHAI-48802's recipe does not transfer to the
  API-key path.**
- **The auth stage is authorino-internal CPU.** Two authorino replicas ran: one
  pinned at **11.6 cores** (avg 3.6), the second mostly idle (max 2.3, avg 0.57) —
  Finding 4's connection pinning, still active. Quiet-day authorino CPU: ~0.003.
- **maas-api idle** (max 0.05 cores) — the apiKeyValidation/subscription-info
  backends are not the cost; it is authorino's own per-request evaluation (Finding
  3's CEL cache-key expressions, evaluated on every request even on cache hits,
  plus OPA and the response-phase CEL).
- **The failures die at a ~2 s auth deadline**: 500 p50 **1.78 s**, p95 **2.48 s**
  (the 2 s era of the shim's auth timeout — not the 200 ms one; successful requests
  are slow for model-side reasons, 200 p50 7.5 s).
- **Rates**: successes peak at **~50 rps** (the one-pod auth ceiling); the 500s came
  in two concentrated bursts — ~187 rps early and **~454 rps** mid-window (≈ the
  whole 261,445) — with clean running in between.
- **No resource limits — and none were the wall**: authorino runs BestEffort
  (`resources={}`, no historical request/limit series). Its 11.6 cores was its own
  saturation level on a 15.5-core worker with ~4 cores still free — a process bound
  (≈230 ms CPU per auth), not a cgroup or node wall. Both authorino replicas, both
  gateway pods and the four sim pods all scheduled onto that same node
  (`worker-1-9qkdm`).

### Profile verdict (pprof CPU profile, 2026-11-23 capture)

A 60 s pprof capture ([`authorino-saturation-capture.sh`](authorino-saturation-capture.sh))
during a live failing window (16 rps of 200s, 1,673 rps of 500s, authorino at
3.09 cores / 78 goroutines, per-successful-auth CPU ≈ 193 ms — consistent with
Nov 20's ~230 ms). The profile pins the cost precisely:

- **`AuthPipeline.evaluateAuthConfig`: 84.7 % of all CPU** — the request path, not
  the controller.
- **`metricLabels`: 71.8 % of all CPU.** Of that: **41 % is `cel.NewExpression` —
  CEL expressions are COMPILED on the request path, every evaluation** (`cel.Compile`
  alone is 29.5 % of total CPU: NewEnv + Parse + Check + Program per call);
  **42 % is `ResolveFor`**, which converts the auth JSON document to CEL input via
  **`protojson.Unmarshal` (JSON→protobuf Struct) per evaluation — 38.9 % of total
  CPU**; 16 % is `GetAuthorizationJSON` (building that JSON document per request).
- The allocation churn behind it: `mallocgc` 24.6 % cum + GC/scan/write-barrier
  machinery.
- **CEL evaluation and OPA are minor** — Finding 3's "CEL cost" is actually
  **per-request recompilation + JSON↔Struct conversion**, dominated by metric-label
  evaluation. The `identity` response filter of `maas-gateway-auth` carries
  `metrics: true` — its ~12 label expressions (the giant nested group/subscription
  CELs) are what `metricLabels` evaluates per request.
- Pod churn during the capture (three replacements 11:04–11:19 UTC) was deliberate:
  no OOM (max 1.3 GiB), no node pressure, no evictions.

A second capture 20 minutes later (heavier failing window: 754–1,325 rps of 500s,
~5 cores burned, pod freshly restarted — cold caches) reproduced the anatomy with
even sharper shares — **`metricLabels` 87.8 %, `cel.Compile` 42.2 %,
`AuthJsonToCel`/protojson 31.9 %** — and added the Envoy-side and collapse view:

- **Envoy auth cluster** (`kuadrant-auth-service`, from `pilot-agent GET clusters`):
  `cx_total` = **2** (connection pinning, Finding 4, on every observation),
  `rq_active` peaks at **238** — the admission ceiling ≈ 2 connections × per-conn
  stream limit; of admitted auth requests **96–99 % time out** (28,250 timeouts vs
  150 successes on that gateway pod), `cx_connect_fail` = 0 (the endpoint is
  healthy — the failures are deadline kills, not connectivity).
- **Congestive collapse inside authorino**: goroutines 78 → **2,734** during the
  window, ~5 cores burned, ~1 rps completing. Arithmetic: ~119 rps admitted
  (238 in-flight / 2 s deadline) × ~42 ms CPU each (5 cores / 119 rps) — every
  request is cancelled at the deadline with ~20 % of the ~190 ms of CPU it needs,
  so essentially nothing completes. Warm pods (Nov 20) completed ~50 rps; the
  freshly restarted pod never even warms its evaluation caches, because the
  request that would populate a cache entry cannot complete either — vicious
  cycle. Post-load, the pileup drains to 76 controller goroutines with zero
  request handlers, `rq_active` = 0.

### Revised levers (in order of practicality)

1. **Set `metrics: false` on the `identity` response filter of `maas-gateway-auth`**
   — a one-field patch to the generated AuthPolicy that removes part of the
   `metricLabels` work (72–88 % of authorino CPU attribution, per the pprof
   profiles; measured effect: authorino CPU per successful auth **97 → 68 ms,
   −38 %**). The metering labels it feeds are broken on the tested path anyway
   (Finding 2: /v1/responses streaming defeats the JSON-path extraction), so
   nothing of value is lost. **Harness knob since 2026-11-23**:
   `platform.praxis.identity_metrics` (default `false`), applied and verified by
   `enable_maas_praxis` (patch + AuthConfig propagation check, fails loudly if
   reverted); also usable as a matrix `deployment_level` dimension for A/B.
   The manual [`auth-identity-metrics.sh`](auth-identity-metrics.sh) remains for
   ad-hoc use. Verify effects with the `report_auth_saturation` caliper report —
   "CPU per successful request" is the before/after indicator.

   What the filter is (from the generated AuthConfig,
   `response.success.dynamicMetadata.identity`): a `metrics: true` JSON filter
   with **11 CEL properties** — `groups` (1,139 chars), `groups_str` (1,151),
   `keyId`, `keyName`, `selected_subscription`, `selected_subscription_id`,
   `selected_subscription_key` (749), `subscription_error`,
   `subscription_error_message`, `subscription_info`, `userid` — gated to model
   paths. Its output is the dynamic metadata the kuadrant wasm shim reads as
   per-request usage-metering labels. Per request, per property, authorino:
   rebuilds the auth JSON input document (`GetAuthorizationJSON`), **compiles**
   the expression (`cel.NewExpression`/`Compile` — 42 % of all CPU:
   NewEnv+Parse+Check+Program), converts the JSON to a protobuf Struct
   (`AuthJsonToCel`/protojson — 32 %), evaluates — and most evaluations **fail
   and log** (Finding 3's 30k CEL errors: `subscription_info` keys don't exist
   at label-evaluation time), so the labels are largely empty anyway. Note
   authorino exports **no** `authorino_*` metrics on :8080 — the label values
   reach the shim, not authorino's own metrics endpoint.

   Honest uncertainty: if `metrics: false` proves inert (the CELs still running
   as filter payload rather than as metric labels), the bigger hammer is
   removing the identity filter from the generated policy outright — same
   revert caveats, and it definitely deletes the work. The re-profile after the
   flip decides which world we are in.
2. Spread the gateway→authorino connections across replicas —
   `max_requests_per_connection` via EnvoyFilter (Finding 4's open question), or
   the RHCL 1.5 headless-authorino fix (kuadrant-operator PR #2281).
3. Scale authorino — **not** by adding limits (it was never limited; 11.6 cores was
   its own bound with ~4 node cores free). Extra replicas only pay off combined with
   (2) and with node spreading: both replicas co-located on one 15.5-core worker
   together with the gateway and the sim pods.
4. Upstream (RHCL/authorino): **stop compiling CEL expressions on the request path**
   (29.5 % of CPU is `cel.Compile`) and stop the per-evaluation JSON→Struct
   conversion (`AuthJsonToCel`/protojson, 38.9 %). Compiled-once expressions and a
   reusable typed input would cut the per-request cost several-fold regardless of
   policy size.
5. Raise the shim's auth deadline (currently ~2 s) — trades latency for failure
   rate.

---

## Campaign measurement (2026-11-23, 18:19–19:56 UTC): the ITL sweep vs the admission ceiling

The `sim_inter_token_latency` preset (4 rounds = ITL 5/15/30/60 ms, sweep [128, 256,
512, 1024] concurrent users over 4 guidellm replicas, budget 256, identity metrics
off) ran as four clean repetitions. Every number below is from thanos-querier over
the campaign window.

**Telemetry marker:** in `istio_requests_total` at the gateway, the 500s and the
200s split perfectly by `destination_workload`: all 200s attribute to
`sim-llm-maas-kserve`, all 500s to `unknown` (`response_flags="-"`). Workload
attribution requires an upstream host; Envoy-local replies never select one, while
`destination_service_name` still resolves from the route's cluster. So
`destination_workload="unknown"` isolates gateway-local failures in one label —
useful as a PromQL filter for the overflow/deadline regimes.

**Per-level outcome (each of the 4 rounds, all consistent):**

| users | 200s        | 500s       |
|-------|-------------|------------|
| 128   | 4.2k–5.2k   | 0          |
| 256   | 4.8k–10.8k  | 0–1.3k     |
| 512   | 148–690     | 18k–22k    |
| 1024  | 19–661      | 30k–39k    |

Window totals: 56,006 × 200, 224,458 × 500. The 500 latency is **bimodal**:
88.6 % under 0.5 ms (Finding 1's instant overflow) and 11.4 % in the 1–2.5 s band
(Finding 5's ~2 s auth-deadline kills); only ~56 requests overall exceeded 2.5 s.

**The cliff sits exactly at the admission ceiling** `budget / duration` (256 /
avg-200-latency, confirming the preset's predicted table — measured avg 200
latency per round: 2.58 s / 3.49 s / 5.19 s / 8.83 s for 5/15/30/60 ms):

- 5 ms round (ceiling ~160 rps): 256 users ≈ 99 rps demand → mostly clean
  (~11 % 500s); 512 users → collapse.
- 60 ms round (ceiling ~29 rps): 256 users → 25 rps observed, zero 500s — just
  under; 512 users → collapse.

Above the ceiling the instant-rejection regime plus closed-loop retry
amplification snaps success to ~0–1 %, as documented.

**Component state during the window:**

- ext_proc pods idle (max 0.18 cores) — the overflow is Envoy's circuit breaker,
  not ext_proc server CPU.
- authorino peaked at 10.2 cores on one pod (connection pinning, Finding 4),
  memory max 2.5 GiB. Pods were replaced (same ReplicaSet, pod deletions) at
  18:45, 19:05 and 19:27 UTC — each right after a collapse tail, consistent with
  unwedging the saturated cold-cache pod.
- The whole gateway/authorino/limitador/payload stack was pod-replaced at
  19:27 UTC, between rounds 3 and 4 (the old gateway pod failing readiness 503
  before being killed). The RHCL/kuadrant operators were reinstalled ~19:52 UTC,
  at the very end of the window.

## Finding 6: the extproc metrics exist on the pods but never reach thanos (2026-11-24)

`verify_podmonitors` was rewritten from a kube-burner probe to a direct
thanos-querier query (route + short-lived `oc create token prometheus-k8s` +
curl, the token on stdin so it never hits command text or logs). The honest
run of the check surfaced a subtle trap: **the pods serve the metrics** (an
`oc exec` curl of `localhost:9090/metrics` returns the `praxis_extproc_*`
families — this is where a manual copy-paste "from the cluster" comes from),
and the Prometheus target even shows `up=1` — **yet the series have zero
samples in thanos** (`count_over_time` over any window is empty, including
windows where up=1).

The mechanism, from the target states and up history:

1. **payload-pre-processing serves 401 on /metrics — always down.** The
   plugins ConfigMap (`openshift-ingress/payload-processing-plugins`, managed
   by ai-gateway-controller from the vendored praxis-extproc overlay) sets
   `metrics_auth: enabled: true` in `pre-extproc.yaml` but `enabled: false`
   in `extproc.yaml`. Anonymous scrapes get 401; a random bearer gets 500
   (the server tries to validate it). The platform's own
   `payload-processing-metrics` PodMonitor is equally down.
2. **payload-processing serves 200 with a blank Content-Type — down exactly
   when it matters.** The extproc metrics server (Rust) sends no
   Content-Type, and Prometheus 3 (OCP 4.21) refuses: "non-compliant scrape
   target sending blank Content-Type and no fallback_scrape_protocol
   specified for target". The observed `up=1` windows (e.g. 09:15–09:21 UTC,
   2026-11-24) are the idle-pod artifact: while no ext_proc stream has ever
   been processed the exporter's body is empty, and an empty body scrapes
   "successfully" with zero samples. The moment real series exist, the blank
   Content-Type makes every scrape fail — so the busier the pod, the less of
   it Prometheus sees.
3. The natural fix — `fallbackScrapeProtocol: PrometheusText0.0.4` on the
   PodMonitor — **does not exist in OCP 4.21's PodMonitor CRD** (the API
   server silently prunes the field), so there is no harness-side lever
   through the PodMonitor.

Consequences: the `praxis_extproc` capture group and the 500-cliff report's
ext_proc stream panels are empty on this cluster until upstream fixes either
cause (extproc sends a proper Content-Type, or `metrics_auth` is disabled in
the overlay, or OCP's prometheus-operator grows `fallbackScrapeProtocol`).
Interim harness-side option, if those series are needed before the upstream
fix: capture the raw `/metrics` output of the payload pods directly in
`capture_maas_praxis` via `oc exec` (works today, bypassing Prometheus —
cumulative counters only, no rates). The upstream report:
odh-praxis-extproc metrics endpoint is unscrapable by the system Prometheus
on OCP 4.21 — blank Content-Type (both pods) plus `metrics_auth` on the
pre-processing pod.

The gateway PodMonitor (`praxis-gateway-envoy`) is healthy — `envoy_server_live`
scrapes fine — so `verify_podmonitors` distinguishes the two.

## Method notes (traps that cost time — do not repeat)

- **`%UPSTREAM_CLUSTER%` in an Envoy access log names the route's cluster, not the
  ext_proc cluster.** The logged `inference-pool-ip` cluster is the backend (endpoints =
  sim pods, selected via `override_host`/`x-gateway-destination-endpoint`); the ext_proc
  clusters are named nowhere in the log. Get them from `config_dump` → the ext_proc
  filters' `grpc_service.envoy_grpc.cluster_name`.
- **Istio's `stats_matcher` means most Envoy stats do not exist at all** — not "read
  zero". Empty output from `/stats` or `/stats/prometheus` is not evidence of absence.
  `config_dump` and access logs are unaffected.
- **Envoy admin is on `localhost:15000`** (Istio-managed gateway; 19000 is Envoy
  Gateway's port). No `curl` in the istio-proxy container — use
  `pilot-agent request GET <path>`.
- **`oc logs -l <selector>` defaults to `--tail=10`** — the hot-path capture was silently
  discarding the access log that held the answer. Fixed with `--tail=-1` in
  `capture_maas_praxis`.
- **A DestinationRule replaces a cluster's whole trafficPolicy; it does not patch it.**
  A rule setting only `connectionPool` drops the previous `tls` settings → plaintext
  backend → `503 UC` + `tls: first record does not look like a TLS handshake`. Istio
  records the owning config in each cluster's metadata (`config:` field) — the fastest
  way to find which rule produced a cluster. Rules in the *client's* namespace take
  precedence over the service's namespace.
- **Line-based grep over YAML misses wrapped expressions.** The failing CEL was found
  nowhere by `grep` because YAML folds long strings; search parsed JSON instead.
- **Prometheus histogram `le` labels are formatted with a decimal** (`2500.0`, not
  `2500`) — a `le=~"2500"` regex silently matches nothing. And API response values
  are JSON *strings*; `tonumber` them before any arithmetic in jq.

## Related files

- [`capture-500-details.sh`](capture-500-details.sh) — reproduce from inside a guidellm pod.
- [`ext-proc-circuit-breakers.yaml`](ext-proc-circuit-breakers.yaml) — the budget fix.
- [`auth-review-caching.sh`](auth-review-caching.sh) — the TokenReview caching fix (Finding 5).
- [`authorino-saturation-capture.sh`](authorino-saturation-capture.sh) — confirm authorino CPU saturation during a load run: pprof CPU profile (authorino serves pprof on :8083), Go runtime metrics incl. scheduler latency (:8080, not scraped by cluster Prometheus), Envoy auth-cluster stats, cAdvisor CPU + gateway rates.
- [`auth-identity-metrics.sh`](auth-identity-metrics.sh) — lever 1 below: flip `metrics` on the identity response filter of maas-gateway-auth (off = removes the `metricLabels` work, 72–88 % of authorino CPU).
- [`show-active-rules.sh`](show-active-rules.sh) — candidates vs. effective Envoy config.
