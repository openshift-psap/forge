#!/bin/bash
set -euo pipefail

MODEL_NS="forge-praxis"
TENANT_NS="models-as-a-service"
LLMISVC_NAME="sim-llm"
MODELREF_NAME="sim-llm"
MODEL_NAME="simulated-llama-3"

CLUSTER_DOMAIN=$(oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}')
GATEWAY_URL="https://maas.${CLUSTER_DOMAIN}"

deploy() {
    echo "=== Creating namespace ${MODEL_NS} ==="
    oc create namespace "${MODEL_NS}" --dry-run=client -o yaml | oc apply -f -

    echo "=== Deploying LLMInferenceService ==="
    oc apply -n "${MODEL_NS}" -f - <<'EOF'
apiVersion: serving.kserve.io/v1alpha1
kind: LLMInferenceService
metadata:
  name: sim-llm
  labels:
    maas.io/gateway-name: maas-gateway
  annotations:
    maas.io/enabled: "true"
    serving.kserve.io/enable-external-route: "true"
spec:
  runtime: llm-d-sim-runtime
  router:
    route:
      http: {}
    scheduler: {}
    gateway:
      refs:
      - name: maas-default-gateway
        namespace: openshift-ingress
  model:
    name: simulated-llama-3
    uri: hf://simulated-llama-3
  storageInitializer:
    enabled: false
  template:
    containers:
      - name: main
        image: ghcr.io/llm-d/llm-d-inference-sim:latest
        command: ["/app/llm-d-inference-sim"]
        args:
        - "--port=8000"
        - "--model=simulated-llama-3"
        - "--max-num-seqs"
        - "2048"
        - "--max-model-len"
        - "8192"
        - "--time-to-first-token"
        - "800ms"
        - "--inter-token-latency"
        - "30ms"
        - "--mode"
        - "random"
        - "--logtostderr=true"
        ports:
        - containerPort: 8000
          name: http1
          protocol: TCP
        readinessProbe:
          httpGet:
            path: /health
            port: 8000
            scheme: HTTP
          initialDelaySeconds: 2
          periodSeconds: 5
        startupProbe:
          failureThreshold: 60
          httpGet:
            path: /health
            port: 8000
            scheme: HTTP
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
            scheme: HTTP
          initialDelaySeconds: 5
          periodSeconds: 10
EOF

    echo "=== Deploying MaaSModelRef ==="
    oc apply -n "${MODEL_NS}" -f - <<EOF
apiVersion: maas.opendatahub.io/v1alpha1
kind: MaaSModelRef
metadata:
  name: ${MODELREF_NAME}
spec:
  modelRef:
    kind: LLMInferenceService
    name: ${LLMISVC_NAME}
EOF

    echo "=== Deploying MaaSAuthPolicy ==="
    oc apply -n "${TENANT_NS}" -f - <<EOF
apiVersion: maas.opendatahub.io/v1alpha1
kind: MaaSAuthPolicy
metadata:
  name: simulator-access
spec:
  modelRefs:
    - name: ${MODELREF_NAME}
      namespace: ${MODEL_NS}
  subjects:
    groups:
      - name: system:authenticated
    users: []
EOF

    echo "=== Deploying MaaSSubscription ==="
    oc apply -n "${TENANT_NS}" -f - <<EOF
apiVersion: maas.opendatahub.io/v1alpha1
kind: MaaSSubscription
metadata:
  name: sim-llm-subscription
spec:
  owner:
    groups:
      - name: system:authenticated
    users: []
  modelRefs:
    - name: ${MODELREF_NAME}
      namespace: ${MODEL_NS}
      tokenRateLimits:
        - limit: 100
          window: 1m
  priority: 10
EOF

    echo "=== Waiting for LLMInferenceService to be ready ==="
    oc wait --for=condition=Ready llminferenceservice/${LLMISVC_NAME} -n "${MODEL_NS}" --timeout=120s

    echo "=== Deploying DestinationRule (disable TLS to backend) ==="
    POOL_SVC=$(oc get svc -n "${MODEL_NS}" -l internal.istio.io/service-semantics=inferencepool \
      -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
    if [ -n "${POOL_SVC}" ]; then
        oc apply -n "${MODEL_NS}" -f - <<EOF
apiVersion: networking.istio.io/v1
kind: DestinationRule
metadata:
  name: ${LLMISVC_NAME}-plaintext
spec:
  host: ${POOL_SVC}.${MODEL_NS}.svc.cluster.local
  trafficPolicy:
    tls:
      mode: DISABLE
EOF
    else
        echo "  WARNING: InferencePool service not found, skipping DestinationRule"
    fi

    echo "=== Waiting for MaaSModelRef to be ready ==="
    for i in $(seq 1 30); do
        phase=$(oc get maasmodelref "${MODELREF_NAME}" -n "${MODEL_NS}" -o jsonpath='{.status.phase}' 2>/dev/null || echo "")
        if [ "$phase" = "Ready" ]; then
            echo "MaaSModelRef is Ready"
            break
        fi
        echo "  phase=${phase:-unknown}, waiting... ($i/30)"
        sleep 4
    done

    echo ""
    echo "=== Deploy complete ==="
    oc get maasmodelref "${MODELREF_NAME}" -n "${MODEL_NS}" -o wide
}

test_inference() {
    echo "=== Creating API key ==="
    token=$(oc create token default -n "${MODEL_NS}")

    API_KEY_RESPONSE=$(curl -sS -k -X POST "${GATEWAY_URL}/maas-api/v1/api-keys" \
      -H "Authorization: Bearer ${token}" \
      -H "Content-Type: application/json" \
      -d '{"name": "test-key", "expiresIn": "1h", "ephemeral": true}')

    API_KEY=$(echo "${API_KEY_RESPONSE}" | jq -r .key)
    if [ -z "${API_KEY}" ] || [ "${API_KEY}" = "null" ]; then
        echo "ERROR: Failed to create API key"
        echo "${API_KEY_RESPONSE}" | jq .
        return 1
    fi
    echo "API key created: ${API_KEY:0:10}..."

    echo ""
    echo "=== Listing models ==="
    curl -sS -k -H "Authorization: Bearer ${API_KEY}" \
      "${GATEWAY_URL}/maas-api/v1/models" | jq '.data[] | {id, ready}'

    echo ""
    echo "=== Testing path-based inference ==="
    curl -sS -k -H "Authorization: Bearer ${API_KEY}" \
      -H "Content-Type: application/json" \
      -d "{\"model\": \"${MODEL_NAME}\", \"messages\": [{\"role\": \"user\", \"content\": \"Hello\"}]}" \
      "${GATEWAY_URL}/${MODEL_NS}/${LLMISVC_NAME}/v1/chat/completions" | jq .

    echo ""
    echo "=== Testing body-based inference ==="
    MODEL_ID=$(curl -sS -k -H "Authorization: Bearer ${API_KEY}" \
      "${GATEWAY_URL}/maas-api/v1/models" | jq -re '.data[0].id')
    curl -sS -k -H "Authorization: Bearer ${API_KEY}" \
      -H "Content-Type: application/json" \
      -d "{\"model\": \"${MODEL_ID}\", \"messages\": [{\"role\": \"user\", \"content\": \"Hello\"}]}" \
      "${GATEWAY_URL}/v1/chat/completions" | jq .
}

cleanup() {
    echo "=== Cleaning up ==="
    oc delete maassubscription sim-llm-subscription -n "${TENANT_NS}" --ignore-not-found
    oc delete maasauthpolicy simulator-access -n "${TENANT_NS}" --ignore-not-found
    oc delete maasmodelref "${MODELREF_NAME}" -n "${MODEL_NS}" --ignore-not-found
    oc delete destinationrule "${LLMISVC_NAME}-plaintext" -n "${MODEL_NS}" --ignore-not-found
    oc delete llminferenceservice "${LLMISVC_NAME}" -n "${MODEL_NS}" --ignore-not-found
    echo "=== Cleanup complete ==="
}

status() {
    echo "=== Status ==="
    echo "--- LLMInferenceService ---"
    oc get llminferenceservice -n "${MODEL_NS}" 2>/dev/null || echo "(none)"
    echo "--- MaaSModelRef ---"
    oc get maasmodelref -n "${MODEL_NS}" -o wide 2>/dev/null || echo "(none)"
    echo "--- MaaSAuthPolicy ---"
    oc get maasauthpolicy -n "${TENANT_NS}" 2>/dev/null || echo "(none)"
    echo "--- MaaSSubscription ---"
    oc get maassubscription -n "${TENANT_NS}" 2>/dev/null || echo "(none)"
}

case "${1:-help}" in
    deploy)  deploy ;;
    test)    test_inference ;;
    cleanup) cleanup ;;
    status)  status ;;
    *)
        echo "Usage: $0 {deploy|test|cleanup|status}"
        exit 1
        ;;
esac
