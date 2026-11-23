from __future__ import annotations

import html as html_module
from dataclasses import replace
from pathlib import Path

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from projects.caliper.postprocess.helpers.visualization_utils import (
    compute_report_stem,
    create_report_filename,
    create_report_title_display,
    save_figure,
)
from projects.caliper.postprocess.prometheus.parser import CaptureGroupResult, MetricTimeSeries
from projects.caliper.postprocess.prometheus.report import assemble_report

REPORT_TITLE = "Auth Path Saturation"

# metric name -> (capture group file, series label filter)
AUTHORINO_POD_PREFIX = "authorino"

PANEL_TITLES = (
    "Gateway request rate by response code",
    "Authorino CPU per pod",
    "Request duration by response code",
    "Kubernetes API review rates",
)


def generate_auth_saturation_report(
    group_results: list[CaptureGroupResult],
    output_dir: str | Path,
    report_number: int | None = None,
) -> Path | None:
    """Compose the authorino saturation view for one test node.

    One figure, four stacked panels sharing the benchmark time axis:
      1. gateway request rate by response code — the failure storm vs successes
      2. authorino CPU per pod — the saturation plateau (and idle replicas)
      3. request duration p50/p95 by response code — 500s pinned at the auth
         deadline while 200s carry model time
      4. Kubernetes API review rates — flat at background level while the
         gateway fails proves the cost is authorino-internal, not Kubernetes
         review throttling (RHAI-48802 does not transfer)

    Plus a summary table of derived indicators: peak rates, peak authorino
    cores, the 500-deadline value, per-successful-request CPU, and the review
    verdict.
    """
    requests = _find_metric(group_results, "istio_requests_by_code")
    authorino_cpu = _find_authorino_cpu(group_results)
    p50 = _find_metric(group_results, "istio_request_duration_p50_by_code")
    p95 = _find_metric(group_results, "istio_request_duration_p95_by_code")
    reviews = [
        m
        for name in ("apiserver_tokenreviews_rate", "apiserver_subjectaccessreviews_rate")
        if (m := _find_metric(group_results, name))
    ]
    apf = _find_metric(group_results, "apiserver_flowcontrol_rejected_rate")

    if not requests and not authorino_cpu:
        return None

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    display_title = create_report_title_display(REPORT_TITLE)
    report_stem = compute_report_stem(REPORT_TITLE, report_number)
    report_dir = output_dir / report_stem
    report_dir.mkdir(parents=True, exist_ok=True)

    fig = _build_figure(requests, authorino_cpu, p50, p95, reviews, apf)
    save_figure(fig, report_dir, "auth_saturation", height=1600)
    inline_plot = fig.to_html(include_plotlyjs=False, full_html=False)

    sections = [
        '<div class="metric-section">',
        f"<h2>Combined view ({_window(group_results)})</h2>",
        inline_plot,
        _render_summary(requests, authorino_cpu, p50, p95, reviews),
        _render_reading_guide(),
        "</div>",
    ]

    report_html = assemble_report(display_title, sections)
    report_path = output_dir / create_report_filename(REPORT_TITLE, report_number)
    report_path.write_text(report_html, encoding="utf-8")
    return report_path


def _find_metric(
    group_results: list[CaptureGroupResult], metric_name: str
) -> MetricTimeSeries | None:
    for group in group_results:
        for metric in group.metrics:
            if metric.metric_name == metric_name:
                return metric
    return None


def _find_authorino_cpu(group_results: list[CaptureGroupResult]) -> MetricTimeSeries | None:
    """Authorino pods out of the per-pod CPU captures.

    Several capture groups define cpu_usage_by_pod (load-generator, sim,
    ingress, payload, kuadrant) — scan them all and keep the authorino series.
    """
    for group in group_results:
        for metric in group.metrics:
            if metric.metric_name != "cpu_usage_by_pod":
                continue
            series = [
                s
                for s in metric.series
                if s.labels.get("pod", "").startswith(AUTHORINO_POD_PREFIX)
                and not s.labels.get("pod", "").startswith("authorino-operator")
            ]
            if series:
                return replace(metric, series=series, description="Authorino CPU usage per pod")
    return None


def _build_figure(requests, authorino_cpu, p50, p95, reviews, apf) -> go.Figure:
    fig = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.05,
        subplot_titles=PANEL_TITLES,
    )

    def add_series(row, metric, name_fn, *, dash=None):
        if metric is None:
            return
        for series in metric.series:
            fig.add_trace(
                go.Scatter(
                    x=series.timestamps,
                    y=series.values,
                    name=name_fn(series),
                    line={"dash": dash} if dash else None,
                    legendgroup=name_fn(series),
                    showlegend=(row == 1),
                ),
                row=row,
                col=1,
            )

    add_series(1, requests, lambda s: f"HTTP {s.labels.get('response_code', '?')}")
    add_series(2, authorino_cpu, lambda s: s.labels.get("pod", "?").removeprefix("authorino-"))
    add_series(3, p50, lambda s: f"{s.labels.get('response_code', '?')} p50")
    add_series(3, p95, lambda s: f"{s.labels.get('response_code', '?')} p95", dash="dot")
    for metric in reviews:
        add_series(4, metric, lambda s, m=metric: m.metric_name.removeprefix("apiserver_"))
    add_series(4, apf, lambda s: "APF rejected")

    fig.update_yaxes(title_text="req/s", row=1, col=1)
    fig.update_yaxes(title_text="cores", row=2, col=1)
    fig.update_yaxes(title_text="ms", row=3, col=1)
    fig.update_yaxes(title_text="req/s", row=4, col=1)
    fig.update_layout(height=1600, hovermode="x unified")
    return fig


def _window(group_results: list[CaptureGroupResult]) -> str:
    starts = [m.series[0].timestamps[0] for g in group_results for m in g.metrics if m.series]
    ends = [m.series[-1].timestamps[-1] for g in group_results for m in g.metrics if m.series]
    if not starts or not ends:
        return "no data"
    fmt = "%H:%M:%S"
    return f"{min(starts).strftime(fmt)} – {max(ends).strftime(fmt)} UTC"


def _series_stat(metric: MetricTimeSeries | None, agg) -> float | None:
    if metric is None:
        return None
    values = [v for s in metric.series for v in s.values]
    return agg(values) if values else None


def _render_summary(requests, authorino_cpu, p50, p95, reviews) -> str:
    def code_series(metric, code):
        if metric is None:
            return []
        return [v for s in metric.series if s.labels.get("response_code") == code for v in s.values]

    rows: list[tuple[str, str]] = []

    peak_500 = _series_stat(requests, max) if requests else None
    peak_500 = max(code_series(requests, "500"), default=None) or peak_500
    if peak_500 is not None:
        rows.append(("Peak gateway 500 rate", f"{peak_500:.0f} req/s"))

    mean_200 = None
    if requests and (vals := code_series(requests, "200")):
        mean_200 = sum(vals) / len(vals)
        rows.append(("Mean gateway 200 rate", f"{mean_200:.1f} req/s"))

    if authorino_cpu:
        per_pod_peak = {
            s.labels.get("pod", "?"): max(s.values) for s in authorino_cpu.series if s.values
        }
        for pod, peak in sorted(per_pod_peak.items()):
            rows.append((f"Peak CPU — {pod.removeprefix('authorino-')}", f"{peak:.1f} cores"))
        total_mean = sum(sum(s.values) / len(s.values) for s in authorino_cpu.series if s.values)
        rows.append(("Mean authorino CPU (all pods)", f"{total_mean:.1f} cores"))
        if mean_200 and mean_200 > 0.5:
            rows.append(
                ("CPU per successful request", f"{total_mean / mean_200 * 1000:.0f} ms/auth")
            )

    p50_500 = max(code_series(p50, "500"), default=None) if p50 else None
    p95_500 = max(code_series(p95, "500"), default=None) if p95 else None
    if p50_500 is not None or p95_500 is not None:
        p50_txt = f"{p50_500:.0f} ms" if p50_500 is not None else "n/a"
        p95_txt = f"{p95_500:.0f} ms" if p95_500 is not None else "n/a"
        rows.append(
            (
                "500 latency p50 / p95 (peak)",
                f"{p50_txt} / {p95_txt} — two regimes: instant rejections (overflow, "
                "never reach auth) + deadline kills (auth fail-closed timeout)",
            )
        )

    if reviews:
        # Quiet-day background on the reference cluster is ~0.5 TokenReview/s plus
        # ~2.5 SubjectAccessReview/s (~3/s combined). "Tracking the load" means the
        # review rate rises by orders of magnitude with the request rate.
        review_peak = sum(_series_stat(m, max) or 0 for m in reviews)
        verdict = (
            "flat at background level — no Kubernetes reviews fired with the load; "
            "the auth cost is authorino-internal (see Finding 5)"
            if review_peak < 10
            else "tracks the load — Kubernetes reviews are firing per request (RHAI-48802 mode)"
        )
        rows.append(("Peak TokenReview+SAR rate", f"{review_peak:.1f} req/s — {verdict}"))

    if not rows:
        return ""
    body = "\n".join(
        f"<tr><td><b>{html_module.escape(k)}</b></td><td>{html_module.escape(v)}</td></tr>"
        for k, v in rows
    )
    return (
        "<h2>Derived indicators</h2>"
        '<table class="variables-table"><tr><th>Indicator</th><th>Value</th></tr>'
        f"{body}</table>"
    )


def _render_reading_guide() -> str:
    return (
        "<h2>How to read it</h2>"
        "<p>Saturation (Findings 3–5 of the maas/praxis 500 investigation): the 500 rate "
        "explodes while authorino CPU plateaus (one pinned pod, idle replicas); 500 "
        "latency splits in two regimes — instant rejections from Envoy's auth-cluster "
        "overflow (never reach authorino) and deadline kills at the auth fail-closed "
        "timeout (~2 s) — while 200 latency carries model time; and the review rates "
        "stay flat at background — the bottleneck is authorino's per-request CPU (CEL "
        "recompilation + JSON conversion), not the Kubernetes API.</p>"
        "<p>Not visible here (not in Prometheus): Envoy's auth-cluster admission ceiling "
        "(2 pinned connections, ~238 in-flight) and authorino's in-process goroutine "
        "pileup — capture those live with "
        "orchestration/poc/authorino-saturation-capture.sh.</p>"
    )
