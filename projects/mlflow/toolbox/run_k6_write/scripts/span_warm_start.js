import {
  createExperimentPool,
  experimentForVu,
  logMeasurementStart,
  runSpanSequence,
  summary,
  SUMMARY_TREND_STATS,
  workspaceForVu,
  vusScenarioOptions,
} from './_common.js';

export const options = {
  summaryTrendStats: SUMMARY_TREND_STATS,
  scenarios: {
    span_warm_start: {
      ...vusScenarioOptions(),
    },
  },
};

// Create trace_info first, then send one-span OTLP requests for an existing
// trace. This isolates the warm span-ingestion path from trace creation.
export function setup() {
  return createExperimentPool();
}

export default function (data) {
  logMeasurementStart();
  runSpanSequence({
    warm: true,
    experimentId: experimentForVu(data),
    workspace: workspaceForVu(data),
  });
}

export function handleSummary(data) {
  return summary(data);
}

export { logMeasurementEnd as teardown } from './_common.js';
