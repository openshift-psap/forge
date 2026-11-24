import csv
from io import StringIO
from unittest import TestCase

from projects.mlflow.toolbox.k6_metrics import _read_stats_csv, _write_stats_csv


class TestK6LatencyReporting(TestCase):
    def test_read_preserves_percentiles_and_distinguishes_missing_values_from_zero(self):
        summary = {
            "metrics": {
                "mlflow_read_duration": {"values": {"min": 0, "p(99)": 19.5}},
                "mlflow_read_requests": {"values": {"count": 100}},
                "http_req_duration": {"values": {"p(99)": 18.5}},
            }
        }

        (row,) = csv.DictReader(StringIO(_read_stats_csv(summary, "get_trace")))
        self.assertEqual(row["P99 Response Time"], "19.5")
        self.assertEqual(row["HTTP P99 Response Time"], "18.5")
        self.assertEqual(row["Min Response Time"], "0")
        self.assertEqual(row["P95 Response Time"], "")
        self.assertEqual(row["HTTP P95 Waiting Time"], "")
        self.assertEqual(row["Failure Count"], "0")

    def test_write_reports_endpoint_p99_only_when_samples_exist(self):
        summary = {
            "metrics": {
                "trace_metadata_latency": {"values": {"p(99)": 0}},
                "span_requests": {"values": {"count": 100}},
                "span_latency": {"values": {"p(99)": 44.5}},
                "http_req_duration": {"values": {"p(99)": 35}},
            }
        }

        metadata, span = csv.DictReader(StringIO(_write_stats_csv(summary)))
        self.assertEqual(metadata["P99 Response Time"], "")
        self.assertEqual(span["P99 Response Time"], "44.5")
        self.assertEqual(span["HTTP P99 Response Time"], "")
        self.assertEqual(span["Request Count"], "100")
