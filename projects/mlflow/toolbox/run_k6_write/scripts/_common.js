import http from 'k6/http';
import exec from 'k6/execution';
import { Counter, Trend } from 'k6/metrics';

// This is deliberately a raw server workload. It does not use the MLflow SDK.
// Metadata is sent through MLflow's StartTraceV3 JSON REST endpoint; spans are
// sent as one-span OTLP/HTTP protobuf requests.

export const BASE_URL = (
  __ENV.MLFLOW_TRACKING_URI ||
  'https://mlflow.redhat-ods-applications.svc.cluster.local:8443/mlflow'
).replace(/\/$/, '');
export const TENANT_COUNT = Number(__ENV.TENANT_COUNT || 1);
export const USERS_PER_TENANT = Number(__ENV.USERS_PER_TENANT || __ENV.USERS || 1);
export const SPAN_SIZE_BYTES = Number(__ENV.SPAN_SIZE_BYTES || 100 * 1024);
export const SPANS_PER_TRACE = Number(__ENV.SPANS_PER_TRACE || 5);
export const USERS = Number(__ENV.USERS || 1);
export const TRACES_PER_EXPERIMENT = Number(__ENV.TRACES_PER_EXPERIMENT || 1);
export const EXPERIMENT_PREFIX = __ENV.EXPERIMENT_PREFIX || 'k6-write';
export const SUMMARY_TREND_STATS = ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'];

export function vusScenarioOptions() {
  const duration = __ENV.DURATION || '60s';
  const gracefulStop = __ENV.GRACEFUL_STOP || '30s';

  if (__ENV.WARMUP_RAMP_UP === 'true') {
    return {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [{ duration, target: USERS }],
      gracefulStop,
    };
  }

  return {
    executor: 'constant-vus',
    vus: USERS,
    duration,
    gracefulStop,
  };
}

if (TENANT_COUNT < 1 || USERS_PER_TENANT < 1 || USERS !== TENANT_COUNT * USERS_PER_TENANT) {
  throw new Error('USERS must equal TENANT_COUNT * USERS_PER_TENANT, with both counts positive');
}
let serviceAccountToken = '';
try {
  serviceAccountToken = open('/var/run/secrets/kubernetes.io/serviceaccount/token').trim();
} catch (_) {
  // Local k6 runs can provide MLFLOW_TOKEN explicitly.
}
export const TOKEN = __ENV.MLFLOW_TOKEN || serviceAccountToken;

export const traceMetadataLatency = new Trend('trace_metadata_latency', true);
export const spanLatency = new Trend('span_latency', true);
export const serverBodyLatency = new Trend('server_otlp_body', true);
export const serverBodyFirstChunkLatency = new Trend('server_otlp_body_first_chunk', true);
export const serverBodyChunkDeliveryLatency = new Trend('server_otlp_body_chunk_delivery', true);
export const serverBodyJoinLatency = new Trend('server_otlp_body_join', true);
export const serverBodyReceiveWaitLatency = new Trend('server_otlp_body_receive_wait', true);
export const serverBodyChunkCount = new Trend('server_otlp_body_chunks', false);
export const serverBodyBytes = new Trend('server_otlp_body_bytes', false);
export const serverParseLatency = new Trend('server_otlp_parse', true);
export const serverConvertLatency = new Trend('server_otlp_convert', true);
export const serverExecutorQueueLatency = new Trend('server_otlp_executor_queue', true);
export const serverPrepareLatency = new Trend('server_otlp_prepare', true);
export const serverDbLatency = new Trend('server_otlp_db_session', true);
export const serverStoreLatency = new Trend('server_otlp_store', true);
export const serverTotalLatency = new Trend('server_otlp_total', true);
export const traceMetadataRequests = new Counter('trace_metadata_requests');
export const spanRequests = new Counter('span_requests');
export const traceMetadataFailures = new Counter('trace_metadata_failures');
export const spanFailures = new Counter('span_failures');
export const failures = new Counter('write_failures');

const SERVER_TIMING_HEADERS = [
  ['X-MLflow-Otlp-Body-Ms', serverBodyLatency],
  ['X-MLflow-Otlp-Body-First-Chunk-Ms', serverBodyFirstChunkLatency],
  ['X-MLflow-Otlp-Body-Chunk-Delivery-Ms', serverBodyChunkDeliveryLatency],
  ['X-MLflow-Otlp-Body-Join-Ms', serverBodyJoinLatency],
  ['X-MLflow-Otlp-Body-Receive-Wait-Ms', serverBodyReceiveWaitLatency],
  ['X-MLflow-Otlp-Body-Chunks', serverBodyChunkCount],
  ['X-MLflow-Otlp-Body-Bytes', serverBodyBytes],
  ['X-MLflow-Otlp-Parse-Ms', serverParseLatency],
  ['X-MLflow-Otlp-Convert-Ms', serverConvertLatency],
  ['X-MLflow-Otlp-Queue-Ms', serverExecutorQueueLatency],
  ['X-MLflow-Otlp-Prepare-Ms', serverPrepareLatency],
  ['X-MLflow-Otlp-Db-Ms', serverDbLatency],
  ['X-MLflow-Otlp-Store-Ms', serverStoreLatency],
  ['X-MLflow-Otlp-Total-Ms', serverTotalLatency],
];

function addServerTimings(response) {
  for (const [header, trend] of SERVER_TIMING_HEADERS) {
    const key = Object.keys(response.headers).find((name) => name.toLowerCase() === header.toLowerCase());
    if (key) {
      const value = Number(response.headers[key]);
      if (Number.isFinite(value)) trend.add(value);
    }
  }
}

function text(value) {
  return new TextEncoder().encode(String(value));
}

function concat(...parts) {
  const length = parts.reduce((total, part) => total + part.length, 0);
  const result = new Uint8Array(length);
  let offset = 0;
  for (const part of parts) {
    result.set(part, offset);
    offset += part.length;
  }
  return result;
}

function varint(value) {
  let number = Number(value);
  const bytes = [];
  do {
    let byte = number % 128;
    number = Math.floor(number / 128);
    if (number > 0) byte |= 0x80;
    bytes.push(byte);
  } while (number > 0);
  return Uint8Array.from(bytes);
}

function fieldBytes(field, value) {
  return concat(varint((field << 3) | 2), varint(value.length), value);
}

function fieldString(field, value) {
  return fieldBytes(field, text(value));
}

function fieldVarint(field, value) {
  return concat(varint(field << 3), varint(value));
}

function fieldFixed32(field, value) {
  return concat(
    varint((field << 3) | 5),
    Uint8Array.from([
      value & 0xff,
      (value >>> 8) & 0xff,
      (value >>> 16) & 0xff,
      (value >>> 24) & 0xff,
    ]),
  );
}

function fixed64(value) {
  // OTLP timestamps are fixed64. Avoid BigInt so this runs on stock k6/Goja.
  const base = 4294967296;
  const low = Math.floor(value % base);
  const high = Math.floor(value / base);
  return Uint8Array.from([
    low & 0xff, (low >>> 8) & 0xff, (low >>> 16) & 0xff, (low >>> 24) & 0xff,
    high & 0xff, (high >>> 8) & 0xff, (high >>> 16) & 0xff, (high >>> 24) & 0xff,
  ]);
}

function fieldFixed64(field, value) {
  return concat(varint((field << 3) | 1), fixed64(value));
}

function hexBytes(hex) {
  const result = new Uint8Array(hex.length / 2);
  for (let i = 0; i < result.length; i += 1) {
    result[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return result;
}

function randomHex(bytes) {
  let result = '';
  while (result.length < bytes * 2) {
    result += Math.floor(Math.random() * 0x100000000).toString(16).padStart(8, '0');
  }
  return result.slice(0, bytes * 2);
}

export function newTrace() {
  const traceId = randomHex(16);
  return {
    traceId,
    rootSpanId: randomHex(8),
    vu: __VU,
    iteration: __ITER,
  };
}

function anyString(value) {
  return fieldString(1, value);
}

function attribute(key, value) {
  return concat(fieldString(1, key), fieldBytes(2, anyString(value)));
}

function spanPayload(size) {
  // Put the bulk in an MLflow span input attribute so it follows the normal
  // span-content persistence path. The resulting serialized protobuf is a bit
  // larger than size because of protobuf and attribute framing overhead.
  return 'x'.repeat(Math.max(0, size));
}

export function startTraceJson(trace, experimentId) {
  const nowMs = Date.now();
  return JSON.stringify({
    trace: {
      trace_info: {
        trace_id: trace.traceId,
        trace_location: {
          type: 'MLFLOW_EXPERIMENT',
          mlflow_experiment: { experiment_id: experimentId },
        },
        request_time: {
          seconds: Math.floor(nowMs / 1000),
          nanos: (nowMs % 1000) * 1000000,
        },
        state: 'IN_PROGRESS',
      },
    },
  });
}

export function otlpSpan(trace, index, size, options = {}) {
  const includeAttributes = options.includeAttributes !== false;
  const includeStatus = options.includeStatus !== false;
  const includeKind = options.includeKind !== false;
  const includeFlags = options.includeFlags !== false;
  const includeEmptyResource = options.includeEmptyResource === true;
  const includeEmptyScope = options.includeEmptyScope === true;
  const nowNs = options.startTimeNs ?? Date.now() * 1000000;
  const spanId = index === 0 ? trace.rootSpanId : randomHex(8);
  const parent = index === 0 ? null : trace.rootSpanId;
  let span = concat(
    fieldBytes(1, hexBytes(trace.traceId)),
    fieldBytes(2, hexBytes(spanId)),
    ...(parent ? [fieldBytes(4, hexBytes(parent))] : []),
    fieldString(5, `k6-span-${index}`),
    ...(includeKind ? [fieldVarint(6, 1)] : []), // SPAN_KIND_INTERNAL
    fieldFixed64(7, nowNs),
    fieldFixed64(8, nowNs + 1000000),
    ...(includeAttributes
      ? [fieldBytes(9, attribute('mlflow.spanInputs', spanPayload(size)))]
      : []),
    ...(includeStatus && index === 0 ? [fieldBytes(15, fieldVarint(3, 1))] : []), // root status: OK
    ...(includeFlags ? [fieldFixed32(16, 1)] : []), // sampled flag
  );

  const scope = includeEmptyScope ? fieldBytes(1, new Uint8Array(0)) : new Uint8Array(0);
  // scopeSpans is already a fully tagged (field 2) ScopeSpans entry for
  // ResourceSpans.scope_spans — do not wrap it in another fieldBytes(2, ...).
  const scopeSpans = fieldBytes(2, concat(scope, fieldBytes(2, span)));
  const resource = includeEmptyResource ? fieldBytes(1, new Uint8Array(0)) : new Uint8Array(0);
  const resourceSpans = concat(resource, scopeSpans);
  return fieldBytes(1, resourceSpans).buffer;
}

function authHeaders(contentType, experimentId, workspace) {
  const headers = {
    'Content-Type': contentType,
    'X-MLFLOW-WORKSPACE': workspace,
  };
  if (experimentId) headers['x-mlflow-experiment-id'] = experimentId;
  if (TOKEN) headers.Authorization = `Bearer ${TOKEN}`;
  return headers;
}

function metadataUrl() {
  // StartTraceV3 is a tracking REST API endpoint, unlike OTLP which is served
  // directly at /v1/traces. With a tracking URI ending in /mlflow this becomes
  // /mlflow/api/3.0/mlflow/traces.
  return `${BASE_URL}/api/3.0/mlflow/traces`;
}

export function otlpUrl() {
  // MLflow 3.15.2 exposes the OTLP route at /v1/traces. Starting in MLflow 3.16,
  // native FastAPI routes honor --static-prefix, so with --static-prefix=/mlflow
  // OTLP must be reached at /mlflow/v1/traces. An explicit path keeps both layouts
  // usable without changing the legacy default.
  const configuredPath = (__ENV.MLFLOW_OTLP_PATH || '').trim();
  if (configuredPath) {
    if (
      !configuredPath.startsWith('/') ||
      configuredPath.startsWith('//') ||
      /[?#\s]/.test(configuredPath)
    ) {
      throw new Error('MLFLOW_OTLP_PATH must be an absolute path without query or fragment');
    }
    const origin = BASE_URL.match(/^(https?:\/\/[^/]+)/i);
    if (!origin) {
      throw new Error('MLFLOW_TRACKING_URI must be an absolute HTTP(S) URL');
    }
    return `${origin[1]}${configuredPath}`;
  }

  // Preserve the existing default for MLflow 3.15.2 and earlier deployments.
  // The internal service route exposes OTLP at /v1/traces; the public tracking
  // route commonly keeps the /mlflow prefix from MLFLOW_TRACKING_URI.
  if (BASE_URL.includes('.svc') && BASE_URL.endsWith('/mlflow')) {
    return BASE_URL.slice(0, -'/mlflow'.length) + '/v1/traces';
  }
  return `${BASE_URL}/v1/traces`;
}

export function writeTraceMetadata(trace) {
  const response = http.post(metadataUrl(), startTraceJson(trace, trace.experimentId), {
    headers: authHeaders('application/json', null, trace.workspace),
    tags: { operation: 'trace_metadata', payload: 'metadata' },
  });
  traceMetadataLatency.add(response.timings.duration);
  traceMetadataRequests.add(1);
  if (response.status < 200 || response.status >= 300) {
    traceMetadataFailures.add(1);
    failures.add(1);
  }
  return response;
}

export function writeSpan(trace, index, size) {
  const response = http.post(otlpUrl(), otlpSpan(trace, index, size), {
    headers: authHeaders('application/x-protobuf', trace.experimentId, trace.workspace),
    tags: { operation: 'span', span_size_bytes: String(size) },
  });
  spanLatency.add(response.timings.duration);
  addServerTimings(response);
  spanRequests.add(1);
  if (response.status < 200 || response.status >= 300) {
    spanFailures.add(1);
    failures.add(1);
  }
  return response;
}

export function runSpanSequence({ warm, experimentId, workspace }) {
  const trace = newTrace();
  trace.experimentId = experimentId;
  trace.workspace = workspace;
  if (warm) writeTraceMetadata(trace);
  for (let index = 0; index < SPANS_PER_TRACE; index += 1) {
    writeSpan(trace, index, SPAN_SIZE_BYTES);
  }
}

function experimentCreateUrl() {
  return `${BASE_URL}/api/2.0/mlflow/experiments/create`;
}

export function createExperimentPool() {
  const runSuffix = `${Date.now()}-${Math.floor(Math.random() * 1000000)}`;
  const tenants = [];

  for (let tenantIndex = 0; tenantIndex < TENANT_COUNT; tenantIndex += 1) {
    const workspace = workspaceForTenant(tenantIndex);
    const experimentCount = Math.ceil(USERS_PER_TENANT / TRACES_PER_EXPERIMENT);
    const experimentIds = [];
    for (let index = 0; index < experimentCount; index += 1) {
      const response = http.post(
        experimentCreateUrl(),
        JSON.stringify({
          name: `${EXPERIMENT_PREFIX}-${runSuffix}-t${tenantIndex + 1}-${index}`,
        }),
        {
          headers: authHeaders('application/json', null, workspace),
          tags: { phase: 'experiment_setup' },
        },
      );
      if (response.status < 200 || response.status >= 300) {
        throw new Error(`Experiment creation failed (${response.status}): ${response.body}`);
      }
      const body = JSON.parse(response.body);
      if (!body.experiment_id) {
        throw new Error(`Experiment creation returned no experiment_id: ${response.body}`);
      }
      experimentIds.push(String(body.experiment_id));
    }
    tenants.push({ workspace, experimentIds });
  }

  return {
    tenants,
    usersPerTenant: USERS_PER_TENANT,
    tracesPerExperiment: TRACES_PER_EXPERIMENT,
  };
}

export function experimentForVu(data) {
  const tenantIndex = Math.min(
    Math.floor((__VU - 1) / data.usersPerTenant),
    data.tenants.length - 1,
  );
  const tenantVu = (__VU - 1) % data.usersPerTenant;
  const experimentIndex = Math.min(
    Math.floor(tenantVu / data.tracesPerExperiment),
    data.tenants[tenantIndex].experimentIds.length - 1,
  );
  return data.tenants[tenantIndex].experimentIds[experimentIndex];
}

export function workspaceForVu(data) {
  const tenantIndex = Math.min(
    Math.floor((__VU - 1) / data.usersPerTenant),
    data.tenants.length - 1,
  );
  return data.tenants[tenantIndex].workspace;
}

function workspaceForTenant(tenantIndex) {
  return `tenant-${tenantIndex + 1}`;
}

export function summary(data) {
  return { stdout: `===K6_SUMMARY===${JSON.stringify(data)}===K6_SUMMARY_END===` };
}

export function logMeasurementStart() {
  if (exec.scenario.iterationInInstance === 0) {
    console.info(`MLFLOW_K6_SCENARIO_START_MS=${exec.scenario.startTime}`);
  }
}

export function logMeasurementEnd() {
  console.info(`MLFLOW_K6_SCENARIO_END_MS=${Date.now()}`);
}
