"""Presentation-only concurrency series from explicitly identified sweep members."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class KpiSweep:
    """One workload invocation; member runs differ only in concurrency/run label.

    The orchestration-owned sweep_id is persisted in Caliper labels. This is
    a transient HTML reporting group, never an exported KPI/test record.
    """

    sweep_id: str
    labels: dict[str, str] = field(default_factory=dict)
    members: dict[str, float] = field(default_factory=dict)
    curves: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.sweep_id, str) or not self.sweep_id.strip():
            raise ValueError("sweep_id must be a nonempty string")

    def add_test(self, test: dict[str, Any]) -> None:
        try:
            concurrency = float(test["labels"].get("concurrency"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid concurrency in sweep {self.sweep_id}") from exc
        if not math.isfinite(concurrency) or concurrency <= 0:
            raise ValueError(f"Invalid concurrency in sweep {self.sweep_id}")
        labels = {
            key: str(value)
            for key, value in test["labels"].items()
            if key not in ("concurrency", "run_label")
        }
        if self.members and labels != self.labels:
            raise ValueError(f"Conflicting labels in sweep {self.sweep_id}")
        if test["run_id"] in self.members or concurrency in self.members.values():
            raise ValueError(f"Duplicate concurrency in sweep {self.sweep_id}")
        self.labels = labels
        self.members[test["run_id"]] = concurrency

        for row in test.get("kpis", []):
            if row.get("is_curve") or row.get("value") is None:
                continue
            value = row["value"]
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
            ):
                raise ValueError(f"Invalid scalar value in sweep {self.sweep_id}")
            kpi_id = row["kpi_id"]
            name = row.get("name") or kpi_id
            unit = row.get("unit", "")
            if kpi_id not in self.curves:
                self.curves[kpi_id] = {
                    "kpi_id": f"{kpi_id}_vs_concurrency",
                    "name": f"{name} vs concurrency",
                    "values": [],
                    "is_curve": True,
                    "unit": "",
                    "x_unit": "requests",
                    "x_help": "Configured concurrency",
                    "y_unit": unit,
                    "y_help": f"{name} ({unit})" if unit else name,
                }
            elif unit != self.curves[kpi_id]["y_unit"]:
                raise ValueError(f"Conflicting units in sweep {self.sweep_id}")
            self.curves[kpi_id]["values"].append([concurrency, value])

    def report_test(self) -> dict[str, Any]:
        source_run_ids = sorted(self.members, key=self.members.__getitem__)
        for curve in self.curves.values():
            curve["values"].sort(key=lambda point: point[0])
        return {
            "run_id": f"sweep/{self.sweep_id}",
            "labels": dict(self.labels),
            "metadata": {"source_run_ids": source_run_ids},
            "kpis": list(self.curves.values()),
        }


def build_sweep_report_tests(tests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive chart series without modifying source KPIs or inferring membership."""
    sweeps: dict[str, KpiSweep] = {}
    for test in tests:
        sweep_id = test.get("labels", {}).get("sweep_id")
        if sweep_id is None or not any(not kpi.get("is_curve") for kpi in test.get("kpis", [])):
            continue
        sweep = KpiSweep(sweep_id)
        sweeps.setdefault(sweep_id, sweep).add_test(test)
    return [sweep.report_test() for sweep in sweeps.values()]
