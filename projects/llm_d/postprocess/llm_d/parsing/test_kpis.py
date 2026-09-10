"""Tests for KPI parsing functionality."""

from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from projects.caliper.engine.model import UnifiedRunModel
from projects.llm_d.postprocess.llm_d.parsing.kpis import GuideLLMKpiHandler


class TestGuideLLMKpiHandler(unittest.TestCase):
    """Test cases for GuideLLM KPI handler coordinate conversion."""

    def test_coordinate_conversion_preserves_integer_types(self):
        """Test that coordinate conversion preserves original numeric types without unnecessary float conversion."""

        # Create mock data with large integer X values and mixed numeric types
        mock_record = Mock()
        mock_record.test_base_path = "test_path"
        mock_record.metrics = {
            "performance_curves": {"latency": [(1000000, 0.5), (2000000, 1.0), (3000000, 1.5)]},
            "request_rate": [100, 200, 300],
            "product_version": "v1.0",
            "cluster": "test-cluster",
            "model_name": "test-model",
            "benchmark_key": "test",
        }

        # Create a mock curve KPI function that returns various numeric types
        def mock_curve_kpi_func(record):
            """Mock curve KPI function that returns coordinates with large integers and floats."""
            return [
                (1000000, 0.5),  # Large int X, float Y
                (3000000, 1.5),  # Large int X, float Y
                (2000000, 1),  # Large int X, int Y
                (500000, 0.25),  # Medium int X, float Y
                (4000000, 2.0),  # Large int X, float Y
            ]

        # Add KPI metadata attributes to the mock function
        mock_curve_kpi_func._kpi_unit = "ms"
        mock_curve_kpi_func._kpi_x_unit = "req/s"
        mock_curve_kpi_func._kpi_x_help = "Request rate"
        mock_curve_kpi_func._kpi_y_unit = "ms"
        mock_curve_kpi_func._kpi_y_help = "Latency"

        # Create unified model with mock record
        model = Mock(spec=UnifiedRunModel)
        model.unified_result_records = [mock_record]

        # Mock the KPI functions and dependencies
        mock_kpi_functions = {"test_latency": mock_curve_kpi_func}

        with patch(
            "projects.llm_d.postprocess.llm_d.parsing.kpis.get_kpi_functions"
        ) as mock_get_kpi_functions:
            with patch(
                "projects.llm_d.postprocess.llm_d.parsing.kpis.is_curve_kpi"
            ) as mock_is_curve_kpi:
                mock_get_kpi_functions.return_value = mock_kpi_functions
                mock_is_curve_kpi.return_value = True

                # Call the compute_kpis method
                kpi_records, status = GuideLLMKpiHandler.compute_kpis(model)

        # Verify we got a KPI record
        assert len(kpi_records) == 1
        kpi_record = kpi_records[0]

        # Verify the coordinate types are preserved
        values = kpi_record.values
        assert len(values) == 5

        # Verify the values are sorted by X coordinate
        expected_sorted_values = [
            [500000, 0.25],  # Sorted by X value
            [1000000, 0.5],
            [2000000, 1],  # Integer Y value preserved
            [3000000, 1.5],
            [4000000, 2.0],
        ]

        assert values == expected_sorted_values

        # Verify that integer types are preserved (not converted to float)
        # The large integer X values should remain as integers
        assert isinstance(values[0][0], int)  # 500000
        assert isinstance(values[1][0], int)  # 1000000
        assert isinstance(values[2][0], int)  # 2000000
        assert isinstance(values[2][1], int)  # 1 (integer Y value preserved)
        assert isinstance(values[3][0], int)  # 3000000
        assert isinstance(values[4][0], int)  # 4000000

        # Verify that float types are preserved
        assert isinstance(values[0][1], float)  # 0.25
        assert isinstance(values[1][1], float)  # 0.5
        assert isinstance(values[3][1], float)  # 1.5
        assert isinstance(values[4][1], float)  # 2.0

        # Verify serialization preserves types
        serialized = json.dumps(values)
        deserialized = json.loads(serialized)

        # After JSON round-trip, integers should remain integers (as long as they're within safe range)
        # Note: JSON preserves integers up to JavaScript's safe integer limit
        for i, (x, y) in enumerate(deserialized):
            original_x, original_y = values[i]
            # Large integers should be preserved in JSON
            assert x == original_x
            assert y == original_y

    def test_coordinate_conversion_handles_empty_values(self):
        """Test that coordinate conversion handles empty or null values correctly."""

        mock_record = Mock()
        mock_record.test_base_path = "test_path"
        mock_record.metrics = {
            "performance_curves": {"latency": []},
            "request_rate": [100],
            "product_version": "v1.0",
            "cluster": "test-cluster",
            "model_name": "test-model",
            "benchmark_key": "test",
        }

        def mock_empty_kpi_func(record):
            """Mock KPI function that returns empty values."""
            return []

        mock_empty_kpi_func._kpi_unit = "ms"
        mock_empty_kpi_func._kpi_x_unit = "req/s"
        mock_empty_kpi_func._kpi_x_help = "Request rate"
        mock_empty_kpi_func._kpi_y_unit = "ms"
        mock_empty_kpi_func._kpi_y_help = "Latency"

        model = Mock(spec=UnifiedRunModel)
        model.unified_result_records = [mock_record]

        mock_kpi_functions = {"test_empty": mock_empty_kpi_func}

        with patch(
            "projects.llm_d.postprocess.llm_d.parsing.kpis.get_kpi_functions"
        ) as mock_get_kpi_functions:
            with patch(
                "projects.llm_d.postprocess.llm_d.parsing.kpis.is_curve_kpi"
            ) as mock_is_curve_kpi:
                mock_get_kpi_functions.return_value = mock_kpi_functions
                mock_is_curve_kpi.return_value = True

                kpi_records, status = GuideLLMKpiHandler.compute_kpis(model)

        # Empty values should result in no KPI records being created
        assert len(kpi_records) == 0

    def test_coordinate_sorting_with_large_integers(self):
        """Test that coordinate sorting works correctly with large integer values."""

        mock_record = Mock()
        mock_record.test_base_path = "test_path"
        mock_record.metrics = {
            "performance_curves": {"latency": []},
            "request_rate": [100],
            "product_version": "v1.0",
            "cluster": "test-cluster",
            "model_name": "test-model",
            "benchmark_key": "test",
        }

        def mock_unsorted_kpi_func(record):
            """Mock KPI function with unsorted large integer coordinates."""
            return [
                (9999999999, 5.0),  # Very large X value
                (1, 1.0),  # Small X value
                (1000000000, 3.0),  # Large X value
                (500, 2.0),  # Medium X value
            ]

        mock_unsorted_kpi_func._kpi_unit = "ms"
        mock_unsorted_kpi_func._kpi_x_unit = "req/s"
        mock_unsorted_kpi_func._kpi_x_help = "Request rate"
        mock_unsorted_kpi_func._kpi_y_unit = "ms"
        mock_unsorted_kpi_func._kpi_y_help = "Latency"

        model = Mock(spec=UnifiedRunModel)
        model.unified_result_records = [mock_record]

        mock_kpi_functions = {"test_sort": mock_unsorted_kpi_func}

        with patch(
            "projects.llm_d.postprocess.llm_d.parsing.kpis.get_kpi_functions"
        ) as mock_get_kpi_functions:
            with patch(
                "projects.llm_d.postprocess.llm_d.parsing.kpis.is_curve_kpi"
            ) as mock_is_curve_kpi:
                mock_get_kpi_functions.return_value = mock_kpi_functions
                mock_is_curve_kpi.return_value = True

                kpi_records, status = GuideLLMKpiHandler.compute_kpis(model)

        assert len(kpi_records) == 1
        values = kpi_records[0].values

        # Verify coordinates are sorted by X value (first element)
        expected_sorted = [
            [1, 1.0],
            [500, 2.0],
            [1000000000, 3.0],
            [9999999999, 5.0],
        ]

        assert values == expected_sorted

        # Verify large integers are preserved as integers
        assert isinstance(values[2][0], int)  # 1000000000
        assert isinstance(values[3][0], int)  # 9999999999

        # Verify the large integer values are exactly what we expect
        assert values[2][0] == 1000000000
        assert values[3][0] == 9999999999
