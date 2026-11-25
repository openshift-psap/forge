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
    span_cold_start: {
      ...vusScenarioOptions(),
    },
  },
};

// No explicit start_trace call. The first OTLP span uses a new trace ID, so
// MLflow exercises log_spans()'s missing-trace creation path.
export function setup() {
  return createExperimentPool();
}

export default function (data) {
  logMeasurementStart();
  runSpanSequence({
    warm: false,
    experimentId: experimentForVu(data),
    workspace: workspaceForVu(data),
  });
}

export function handleSummary(data) {
  return summary(data);
}

export { logMeasurementEnd as teardown } from './_common.js';
