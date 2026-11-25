import { sleep } from 'k6';
import {
  createExperimentPool,
  experimentForVu,
  logMeasurementStart,
  newTrace,
  writeTraceMetadata,
  workspaceForVu,
  summary,
  SUMMARY_TREND_STATS,
  vusScenarioOptions,
} from './_common.js';

export const options = {
  summaryTrendStats: SUMMARY_TREND_STATS,
  scenarios: {
    trace_metadata: {
      ...vusScenarioOptions(),
    },
  },
};

export function setup() {
  return createExperimentPool();
}

export default function (data) {
  logMeasurementStart();
  const trace = newTrace();
  trace.experimentId = experimentForVu(data);
  trace.workspace = workspaceForVu(data);
  writeTraceMetadata(trace);
  sleep(Number(__ENV.THINK_TIME_SECONDS || 0));
}

export function handleSummary(data) {
  return summary(data);
}

export { logMeasurementEnd as teardown } from './_common.js';
