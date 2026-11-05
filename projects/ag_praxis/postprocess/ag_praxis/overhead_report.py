from __future__ import annotations

import html as html_module
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from projects.caliper.engine.model import UnifiedResultRecord
from projects.caliper.postprocess.helpers.visualization_utils import (
    create_report_filename,
    create_report_title_display,
)
from projects.caliper.postprocess.prometheus.report import assemble_report

logger = logging.getLogger(__name__)

ENDPOINT_ORDER = ["svc", "maas", "maas/praxis"]


def _merge_replica_curves(
    concurrencies: list[float], latencies: list[float]
) -> tuple[list[float], list[float]]:
    """Merge duplicate concurrency levels from replicas by averaging latencies."""
    if not concurrencies:
        return [], []

    grouped: dict[float, list[float]] = {}
    for conc, lat in zip(concurrencies, latencies):
        if conc not in grouped:
            grouped[conc] = []
        grouped[conc].append(lat)

    merged_conc = sorted(grouped.keys())
    merged_lat = [sum(grouped[c]) / len(grouped[c]) for c in merged_conc]
    return merged_conc, merged_lat


def _parse_duration_ms(value: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ms|s|us)", str(value).strip())
    if not match:
        raise ValueError(f"Cannot parse duration: {value!r}")

    number = float(match.group(1))
    unit = match.group(2)
    if unit == "ms":
        return number
    if unit == "s":
        return number * 1000
    if unit == "us":
        return number / 1000

    raise ValueError(f"Unknown duration unit in {value!r}")


def _extract_sim_config(config_path: Path) -> dict[str, Any] | None:
    try:
        config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Failed to read config from %s", config_path, exc_info=True)
        return None

    if not isinstance(config_data, dict):
        return None

    sim = (config_data.get("platform") or {}).get("sim") or {}
    ttft_raw = sim.get("time_to_first_token")
    itl_raw = sim.get("inter_token_latency")
    if not ttft_raw or not itl_raw:
        return None

    test_cfg = config_data.get("test") or {}
    payload_name = test_cfg.get("payload", "default")

    workloads = config_data.get("workloads") or {}
    payloads = workloads.get("payloads") or {}
    payload = payloads.get(payload_name)
    if not payload:
        return None

    try:
        ttft_ms = _parse_duration_ms(ttft_raw)
        itl_ms = _parse_duration_ms(itl_raw)
    except ValueError:
        logger.warning("Failed to parse sim durations: ttft=%r, itl=%r", ttft_raw, itl_raw)
        return None

    prompt_tokens = payload["prompt_tokens"]
    output_tokens = payload["output_tokens"]
    simulated_ms = output_tokens * itl_ms + ttft_ms

    return {
        "ttft_ms": ttft_ms,
        "itl_ms": itl_ms,
        "ttft_raw": ttft_raw,
        "itl_raw": itl_raw,
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "payload_name": payload_name,
        "simulated_ms": simulated_ms,
    }


def _find_config_yaml(record: UnifiedResultRecord) -> Path | None:
    test_base = Path(record.test_base_path)
    config_path = test_base / "config.yaml"
    if config_path.is_file():
        return config_path
    return None


def _format_ms(value: float) -> str:
    if abs(value) >= 100:
        return f"{value:,.1f}ms"
    if abs(value) >= 1:
        return f"{value:.2f}ms"
    return f"{value:.3f}ms"


def _format_pct(value: float) -> str:
    return f"{value:.1f}%"


def _render_formula_section(sim_config: dict[str, Any]) -> str:
    prompt_tokens = sim_config["prompt_tokens"]
    output_tokens = sim_config["output_tokens"]
    itl_ms = sim_config["itl_ms"]
    ttft_ms = sim_config["ttft_ms"]
    simulated_ms = sim_config["simulated_ms"]
    payload_name = sim_config["payload_name"]

    formula = (
        f"{payload_name} ({prompt_tokens:,}pt / {output_tokens:,}ot): "
        f"{output_tokens:,} tokens x {itl_ms}ms ITL + {ttft_ms}ms TTFT = "
        f"{_format_ms(simulated_ms)}"
    )

    return (
        f'<div class="metric-section">'
        f"<h2>Simulator Configuration</h2>"
        f"<p>Simulated response time = output_tokens &times; ITL + TTFT</p>"
        f'<p style="font-family: monospace; font-size: 1.1em;">'
        f"{html_module.escape(formula)}</p>"
        f"</div>"
    )


def _build_overhead_table(
    rate: int,
    sim_config: dict[str, Any],
    latencies_by_endpoint: dict[str, float],
) -> str:
    simulated_ms = sim_config["simulated_ms"]
    endpoints = [ep for ep in ENDPOINT_ORDER if ep in latencies_by_endpoint]

    if not endpoints:
        return ""

    svc_latency = latencies_by_endpoint.get("svc")
    maas_latency = latencies_by_endpoint.get("maas")

    header_cells = "".join(f"<th>{html_module.escape(ep)}</th>" for ep in endpoints)

    rows: list[str] = []

    # Row 1: Simulated response time
    cells = "".join(f"<td>{_format_ms(simulated_ms)}</td>" for _ in endpoints)
    rows.append(f"<tr><td>1</td><td>Simulated response time (configured)</td>{cells}</tr>")

    # Row 2: Captured response time (measured)
    cells = ""
    for ep in endpoints:
        val = latencies_by_endpoint[ep]
        cells += f"<td>{_format_ms(val)}</td>"
    rows.append(f"<tr><td>2</td><td>Captured response time (measured median)</td>{cells}</tr>")

    # Row 3: Transport + sim processing overhead [(2)-(1)]
    transport: dict[str, float] = {}
    cells = ""
    for ep in endpoints:
        val = latencies_by_endpoint[ep] - simulated_ms
        transport[ep] = val
        cells += f"<td>{_format_ms(val)}</td>"
    rows.append(f"<tr><td>3</td><td>Transport + sim processing overhead [(2)-(1)]</td>{cells}</tr>")

    # Row 4: Gateway overhead vs svc [(2_ep)-(2_svc)]
    cells = ""
    for ep in endpoints:
        if ep == "svc" or svc_latency is None:
            cells += "<td>&mdash;</td>"
        else:
            val = latencies_by_endpoint[ep] - svc_latency
            cells += f"<td>{_format_ms(val)}</td>"
    rows.append(
        f"<tr><td>4</td><td>Overhead vs svc [(2<sub>ep</sub>)-(2<sub>svc</sub>)]</td>{cells}</tr>"
    )

    # Row 5: Praxis overhead vs maas [(2_praxis)-(2_maas)]
    cells = ""
    for ep in endpoints:
        if ep != "maas/praxis" or maas_latency is None:
            cells += "<td>&mdash;</td>"
        else:
            val = latencies_by_endpoint[ep] - maas_latency
            cells += f"<td>{_format_ms(val)}</td>"
    rows.append(
        f"<tr><td>5</td>"
        f"<td>Praxis overhead vs maas [(2<sub>praxis</sub>)-(2<sub>maas</sub>)]</td>{cells}</tr>"
    )

    # Row 6: Overhead vs svc as % of processing time [(4)/(3)]
    cells = ""
    for ep in endpoints:
        if ep == "svc" or svc_latency is None:
            cells += "<td>&mdash;</td>"
        else:
            overhead = latencies_by_endpoint[ep] - svc_latency
            proc = transport.get(ep, 0)
            if abs(proc) < 0.001:
                cells += "<td>&mdash;</td>"
            else:
                cells += f"<td>{_format_pct(overhead / proc * 100)}</td>"
    rows.append(
        f"<tr><td>6</td><td>Overhead vs svc as % of processing time [(4)/(3)]</td>{cells}</tr>"
    )

    # Row 7: Praxis overhead as % of processing time [(5)/(3_maas)]
    cells = ""
    for ep in endpoints:
        if ep != "maas/praxis" or maas_latency is None:
            cells += "<td>&mdash;</td>"
        else:
            overhead = latencies_by_endpoint[ep] - maas_latency
            proc = transport.get("maas", 0)
            if abs(proc) < 0.001:
                cells += "<td>&mdash;</td>"
            else:
                cells += f"<td>{_format_pct(overhead / proc * 100)}</td>"
    rows.append(
        f"<tr><td>7</td>"
        f"<td>Praxis overhead vs maas as % of processing time [(5)/(3<sub>maas</sub>)]</td>"
        f"{cells}</tr>"
    )

    rows_html = "\n".join(rows)

    return (
        f'<div class="metric-section">'
        f"<h2>Concurrency: {rate} streams</h2>"
        f'<table class="overhead-table">'
        f"<thead><tr><th>#</th><th>Metric</th>{header_cells}</tr></thead>"
        f"<tbody>{rows_html}</tbody>"
        f"</table>"
        f"</div>"
    )


def generate_overhead_report(
    records: list[UnifiedResultRecord],
    output_dir: str | Path,
    report_number: int | None = None,
) -> Path | None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    records_by_endpoint: dict[str, UnifiedResultRecord] = {}
    for record in records:
        ep = record.distinguishing_labels.get("endpoint")
        if not ep:
            continue
        records_by_endpoint[ep] = record

    if not records_by_endpoint:
        logger.info("No endpoint-labeled records found for overhead report")
        return None

    sim_config = None
    for record in records:
        config_path = _find_config_yaml(record)
        if config_path:
            sim_config = _extract_sim_config(config_path)
            if sim_config:
                break

    if not sim_config:
        logger.warning("Could not extract simulator config for overhead report")
        return None

    # Build merged curves per endpoint (averaging replicas at the same concurrency)
    merged_by_endpoint: dict[str, dict[str, list[float]]] = {}
    for ep in ENDPOINT_ORDER:
        record = records_by_endpoint.get(ep)
        if not record:
            continue
        ep_curves = record.metrics.get("performance_curves", {})
        raw_conc = ep_curves.get("intended_concurrency", [])
        raw_lat = ep_curves.get("request_latency_median", [])
        m_conc, m_lat = _merge_replica_curves(raw_conc, raw_lat)
        if m_conc:
            merged_by_endpoint[ep] = {"concurrency": m_conc, "latency": m_lat}

    # Determine the set of concurrency levels from the merged data
    all_concurrency: set[float] = set()
    for data in merged_by_endpoint.values():
        all_concurrency.update(data["concurrency"])
    concurrency_levels = sorted(all_concurrency)

    if not concurrency_levels:
        logger.warning("No concurrency levels found in performance curves")
        return None

    sections: list[str] = []
    sections.append(_render_formula_section(sim_config))

    for rate in concurrency_levels:
        latencies_by_endpoint: dict[str, float] = {}

        for ep in ENDPOINT_ORDER:
            ep_data = merged_by_endpoint.get(ep)
            if not ep_data:
                continue
            try:
                idx = ep_data["concurrency"].index(rate)
            except ValueError:
                continue
            latencies_by_endpoint[ep] = ep_data["latency"][idx] * 1000

        table_html = _build_overhead_table(int(rate), sim_config, latencies_by_endpoint)
        if table_html:
            sections.append(table_html)

    if len(sections) <= 1:
        logger.info("No overhead tables generated (no matching concurrency data)")
        return None

    report_title = "Overhead Analysis"
    display_title = create_report_title_display(report_title, report_number)

    report_html = assemble_report(display_title, sections)

    report_html = report_html.replace(
        "</style>",
        ".overhead-table { border-collapse: collapse; font-size: 0.95em; width: 100%; margin-top: 12px; }\n"
        ".overhead-table th, .overhead-table td { border: 1px solid #ddd; padding: 8px 12px; text-align: left; }\n"
        ".overhead-table th { background: #f5f5f5; font-weight: bold; }\n"
        ".overhead-table td:first-child { width: 30px; text-align: center; color: #888; }\n"
        "</style>",
    )

    report_dir_name = "overhead_analysis"
    if report_number is not None:
        report_dir_name = f"report_{report_number:02d}_{report_dir_name}"

    report_filename = create_report_filename(report_dir_name, report_number, report_title, "html")
    report_path = output_dir / report_filename
    report_path.write_text(report_html, encoding="utf-8")

    return report_path
