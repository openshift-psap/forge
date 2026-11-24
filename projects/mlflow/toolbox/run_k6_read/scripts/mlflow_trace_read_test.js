import http from 'k6/http';
import { check } from 'k6';
import exec from 'k6/execution';
import { Counter, Trend } from 'k6/metrics';

const BASE_URL = (__ENV.MLFLOW_TRACKING_URI || '').replace(/\/$/, '');
const OPERATION = __ENV.READ_OPERATION || 'get_trace';
const VUS = parseInt(__ENV.USERS || '1', 10);
const TENANT_COUNT = parseInt(__ENV.TENANT_COUNT || '1', 10);
const USERS_PER_TENANT = parseInt(__ENV.USERS_PER_TENANT || String(VUS), 10);
const DURATION = __ENV.DURATION || '120s';
const TRACE_SELECTION_SEED = parseInt(__ENV.TRACE_SELECTION_SEED || '1', 10);
const TENANT_TRACE_DATA = JSON.parse(open('/trace-data/trace_data.json')).tenants;
const TOKEN = open('/var/run/secrets/kubernetes.io/serviceaccount/token').trim();

if (TENANT_COUNT < 1 || USERS_PER_TENANT < 1 || VUS !== TENANT_COUNT * USERS_PER_TENANT) {
    throw new Error('USERS must equal TENANT_COUNT * USERS_PER_TENANT, with both counts positive');
}
if (TENANT_TRACE_DATA.length !== TENANT_COUNT) {
    throw new Error('tenant trace data count must match TENANT_COUNT');
}

const operationDuration = new Trend('mlflow_read_duration', true);
const operationRequests = new Counter('mlflow_read_requests');
const operationFailures = new Counter('mlflow_read_failures');

export const options = {
    summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'],
    scenarios: {
        read: {
            executor: 'constant-vus',
            vus: VUS,
            duration: DURATION,
            gracefulStop: '0s',
        },
    },
};

function tenantPool() {
    const tenantIndex = Math.floor((__VU - 1) / USERS_PER_TENANT);
    const pool = TENANT_TRACE_DATA[tenantIndex];
    if (!pool || !pool.workspace) {
        throw new Error(`No trace pool for VU ${__VU}`);
    }
    return pool;
}

function headers(workspace) {
    return {
        Authorization: `Bearer ${TOKEN}`,
        'X-MLFLOW-WORKSPACE': workspace,
    };
}

function createRandom(seed) {
    let state = seed >>> 0;
    return function () {
        state = (state + 0x6D2B79F5) | 0;
        let value = state;
        value = Math.imul(value ^ (value >>> 15), value | 1);
        value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
        return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
    };
}

let traceRandom;

function target(pool) {
    if (!pool.trace_ids || pool.trace_ids.length === 0) {
        throw new Error(`tenant ${pool.workspace} has no trace IDs`);
    }
    if (!traceRandom) {
        // Give each VU a deterministic, distinct stream for this job's seed.
        traceRandom = createRandom((TRACE_SELECTION_SEED + __VU) >>> 0);
    }
    // Uniform sampling with replacement spreads reads across the full seeded
    // trace pool instead of walking a sequential prefix of trace IDs.
    return pool.trace_ids[Math.floor(traceRandom() * pool.trace_ids.length)];
}

function request(method, url, workspace, body = null) {
    const response = http.request(method, url, body, {
        headers: { ...headers(workspace), 'Content-Type': 'application/json' },
        // Keep trace IDs out of k6's URL metric tag. Otherwise each unique
        // trace creates a new time series during a large read run.
        tags: {
            operation: OPERATION,
            workspace,
            name: OPERATION === 'get_trace'
                ? '/api/3.0/mlflow/traces/get'
                : '/api/3.0/mlflow/traces/search',
        },
    });
    const ok = check(response, { 'response is successful': r => r.status >= 200 && r.status < 300 });
    operationDuration.add(response.timings.duration);
    operationRequests.add(1);
    if (!ok) operationFailures.add(1);
    return response;
}

export default function () {
    if (exec.scenario.iterationInInstance === 0) {
        console.info(`MLFLOW_K6_SCENARIO_START_MS=${exec.scenario.startTime}`);
    }
    const pool = tenantPool();

    if (OPERATION === 'get_trace') {
        const traceId = target(pool);
        request(
            'GET',
            `${BASE_URL}/api/3.0/mlflow/traces/get?trace_id=${encodeURIComponent(traceId)}`,
            pool.workspace,
        );
        return;
    }

    if (OPERATION === 'search_traces') {
        request(
            'POST',
            `${BASE_URL}/api/3.0/mlflow/traces/search`,
            pool.workspace,
            JSON.stringify({
                experiment_ids: pool.experiment_ids,
                filter: __ENV.SEARCH_FILTER || "trace.status = 'OK'",
                max_results: parseInt(__ENV.SEARCH_MAX_RESULTS || '100', 10),
                order_by: [__ENV.SEARCH_ORDER_BY || 'timestamp_ms DESC'],
                include_spans: false,
            }),
        );
        return;
    }

    throw new Error(`Unsupported READ_OPERATION: ${OPERATION}`);
}

export function teardown() {
    console.info(`MLFLOW_K6_SCENARIO_END_MS=${Date.now()}`);
}

export function handleSummary(data) {
    return {
        stdout: `===K6_SUMMARY===${JSON.stringify(data)}===K6_SUMMARY_END===`,
    };
}
