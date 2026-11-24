"""Per-test MaaS gateway 500-cliff report.

Explains the HTTP-500 cliff of the maas/praxis endpoint for one test: the
observed platform configuration (ext_proc budget, identity metrics -- from the
captured cluster state, never from the harness configuration) next to the
gateway telemetry of that test's Prometheus capture, and a computed verdict of
which failure regime dominated.

The two failure regimes (maas-praxis-500-ext-proc-overflow.md):

- ext_proc overflow: Envoy sheds requests with an instant (< 1 ms) empty 500
  before auth when the ext_proc circuit-breaker budget is exhausted. Every
  in-flight streaming request holds an ext_proc stream for its full response,
  so the admission ceiling is budget / stream-duration. These 500s attribute to
  destination_workload="unknown" (no upstream host was ever selected).
- auth deadline: authorino cannot answer within the kuadrant wasm shim's
  fail-closed auth deadline (~2 s) -- 500s pinned just under the deadline,
  authorino CPU saturated on (typically) one connection-pinned pod.
"""

from __future__ import annotations

import html as html_module
from pathlib import Path

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from projects.ag_praxis.postprocess.ag_praxis.auth_saturation_report import (
    _find_authorino_cpu,
    _find_metric,
    _window,
)
from projects.ag_praxis.postprocess.ag_praxis.k8s_state import PraxisK8sState
from projects.caliper.postprocess.helpers.visualization_utils import (
    compute_report_stem,
    create_report_filename,
    create_report_title_display,
    save_figure,
)
from projects.caliper.postprocess.prometheus.parser import (
    CaptureGroupResult,
    MetricTimeSeries,
)
from projects.caliper.postprocess.prometheus.report import assemble_report

REPORT_TITLE = "Gateway 500 Cliff"

# 500s at or below this duration (ms) are Envoy-local instant rejections -- the
# ext_proc overflow signature. The auth fail-closed deadline sits at ~2 s.
INSTANT_BUCKET_LE = "1.0"

# A 500 fraction below this means the run never left the clean regime.
CLEAN_500_FRACTION = 0.02

PANEL_TITLES = (
    "Gateway request rate by response code",
    "Request rate by response code × destination workload",
    "Request duration by response code (log scale)",
    "In-flight ext_proc streams vs budget",
    "Authorino CPU per pod",
)


def generate_500_cliff_report(
    group_results: list[CaptureGroupResult],
    k8s_state: PraxisK8sState | None,
    output_dir: str | Path,
    report_number: int | None = None,
) -> Path | None:
    """Compose the 500-cliff view for one test node.

    One figure, five stacked panels sharing the benchmark time axis (request
    rates by code; the code × destination split where destination_workload
    "unknown" marks Envoy-local replies; duration quantiles by code; in-flight
    ext_proc streams against the effective budget; authorino CPU per pod), plus
    the observed platform state from the captured K8s artifacts, derived
    indicators, and a verdict naming the dominant failure regime.
    """
    requests = _find_metric(group_results, "istio_requests_by_code")
    by_destination = _find_metric(group_results, "istio_requests_by_code_destination")
    p50 = _find_metric(group_results, "istio_request_duration_p50_by_code")
    p95 = _find_metric(group_results, "istio_request_duration_p95_by_code")
    buckets = _find_metric(group_results, "istio_500_duration_bucket_rate")
    inflight = _find_metric(group_results, "praxis_extproc_streams_inflight")
    stream_duration = _find_metric(group_results, "praxis_extproc_stream_duration_avg")
    authorino_cpu = _find_authorino_cpu(group_results)

    has_series = any(m and m.series for m in (requests, by_destination, inflight))
    if k8s_state is None and not has_series:
        return None

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    stats = _compute_stats(
        requests, by_destination, buckets, p50, p95, stream_duration, authorino_cpu, k8s_state
    )

    display_title = create_report_title_display(REPORT_TITLE)
    report_stem = compute_report_stem(REPORT_TITLE, report_number)
    report_dir = output_dir / report_stem
    report_dir.mkdir(parents=True, exist_ok=True)

    fig = _build_figure(requests, by_destination, p50, p95, inflight, authorino_cpu, stats)
    save_figure(fig, report_dir, "500_cliff", height=1800)
    inline_plot = fig.to_html(include_plotlyjs=False, full_html=False)

    sections = [
        '<div class="metric-section">',
        f"<h2>Combined view ({_window(group_results)})</h2>",
        inline_plot,
        _render_indicators(stats),
        _render_verdict(stats),
        _render_reading_guide(),
        "</div>",
    ]
    if k8s_state is not None:
        sections.insert(1, _render_k8s_state(k8s_state))

    report_html = assemble_report(display_title, sections)
    report_path = output_dir / create_report_filename(REPORT_TITLE, report_number)
    report_path.write_text(report_html, encoding="utf-8")
    return report_path


# ── derived indicators ─────────────────────────────────────────────────────


def _compute_stats(
    requests,
    by_destination,
    buckets,
    p50,
    p95,
    stream_duration,
    authorino_cpu,
    k8s_state,
) -> dict:
    vals_200 = _code_values(requests, "200")
    vals_500 = _code_values(requests, "500")
    mean_200 = _mean(vals_200)
    mean_500 = _mean(vals_500)

    frac_500 = None
    if requests:
        # Total gateway traffic counts every response code in the capture
        # window (4xx, 503 from the model workload, ...), not just 200 and 500.
        per_code: dict[str, list[float]] = {}
        for series in requests.series:
            per_code.setdefault(series.labels.get("response_code", "?"), []).extend(series.values)
        total = sum(_mean(values) or 0 for values in per_code.values())
        if total:
            # mean_500 is None when no 500 ever occurred (the sum-by-code query
            # returns no series for an absent code): that is a fraction of 0.0,
            # not a crash — the report must generate for clean runs too.
            frac_500 = (mean_500 or 0) / total

    unknown_share = None
    if by_destination:
        unknown = [
            v
            for s in by_destination.series
            if s.labels.get("response_code") == "500"
            and s.labels.get("destination_workload") == "unknown"
            for v in s.values
        ]
        total_500 = [
            v
            for s in by_destination.series
            if s.labels.get("response_code") == "500"
            for v in s.values
        ]
        mean_unknown = _mean(unknown)
        mean_total = _mean(total_500)
        if mean_unknown is not None and mean_total:
            unknown_share = mean_unknown / mean_total

    instant_frac = None
    if buckets:
        below = _bucket_mean(buckets, INSTANT_BUCKET_LE)
        total = _bucket_mean(buckets, "+Inf")
        if below is not None and total:
            instant_frac = below / total

    budget = budget_source = None
    if k8s_state is not None:
        budget = k8s_state.effective_budget()
        if budget is not None:
            budget_source = "effective (gateway Envoy config_dump)"
        else:
            budget = k8s_state.extproc_max_requests.get("annotation")
            if budget is not None:
                budget_source = "annotated (effective value not captured)"

    duration = (
        _mean([v for s in stream_duration.series for v in s.values]) if stream_duration else None
    )
    predicted_ceiling = budget / duration if budget and duration else None

    authorino_peak = None
    if authorino_cpu:
        per_pod = [max(s.values) for s in authorino_cpu.series if s.values]
        if per_pod:
            authorino_peak = max(per_pod)

    return {
        "peak_200": max(vals_200, default=None),
        "peak_500": max(vals_500, default=None),
        "mean_200": mean_200,
        "frac_500": frac_500,
        "unknown_share": unknown_share,
        "instant_frac": instant_frac,
        "p50_500": max(_code_values(p50, "500"), default=None) if p50 else None,
        "p95_500": max(_code_values(p95, "500"), default=None) if p95 else None,
        "p50_200": _mean(_code_values(p50, "200")) if p50 else None,
        "budget": budget,
        "budget_source": budget_source,
        "stream_duration": duration,
        "predicted_ceiling": predicted_ceiling,
        "authorino_peak": authorino_peak,
        "identity_metrics": _identity_metrics_value(k8s_state),
    }


def _identity_metrics_value(k8s_state) -> bool | None:
    if k8s_state is None:
        return None
    policies = k8s_state.identity_metrics.get("policies") or {}
    values = {info.get("identity_metrics") for info in policies.values()}
    if len(values) == 1:
        return values.pop()
    return None


def _code_values(metric: MetricTimeSeries | None, code: str) -> list[float]:
    if metric is None:
        return []
    return [v for s in metric.series if s.labels.get("response_code") == code for v in s.values]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _bucket_mean(buckets: MetricTimeSeries, le: str) -> float | None:
    values = [v for s in buckets.series if s.labels.get("le") == le for v in s.values]
    return _mean(values)


# ── figure ─────────────────────────────────────────────────────────────────


def _build_figure(requests, by_destination, p50, p95, inflight, authorino_cpu, stats) -> go.Figure:
    fig = make_subplots(
        rows=5,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
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
                    showlegend=(row <= 2),
                ),
                row=row,
                col=1,
            )

    add_series(1, requests, lambda s: f"HTTP {s.labels.get('response_code', '?')}")

    def destination_name(series):
        code = series.labels.get("response_code", "?")
        workload = series.labels.get("destination_workload", "?")
        if workload == "unknown":
            return f"HTTP {code} → unknown (Envoy-local)"
        return f"HTTP {code} → {workload}"

    add_series(2, by_destination, destination_name)
    add_series(3, p50, lambda s: f"{s.labels.get('response_code', '?')} p50")
    add_series(3, p95, lambda s: f"{s.labels.get('response_code', '?')} p95", dash="dot")
    add_series(4, inflight, lambda s: "in-flight ext_proc streams")

    budget = stats.get("budget")
    if budget:
        fig.add_hline(
            y=budget,
            row=4,
            col=1,
            line_dash="dash",
            line_color="red",
            annotation_text=f"budget {budget}",
        )

    add_series(5, authorino_cpu, lambda s: s.labels.get("pod", "?").removeprefix("authorino-"))

    fig.update_yaxes(title_text="req/s", row=1, col=1)
    fig.update_yaxes(title_text="req/s", row=2, col=1)
    fig.update_yaxes(title_text="ms", row=3, col=1, type="log")
    fig.update_yaxes(title_text="streams", row=4, col=1)
    fig.update_yaxes(title_text="cores", row=5, col=1)
    fig.update_layout(height=1800, hovermode="x unified")
    return fig


# ── HTML sections ──────────────────────────────────────────────────────────


def _render_k8s_state(state: PraxisK8sState) -> str:
    rows: list[tuple[str, str]] = []

    ext = state.extproc_max_requests
    annotation = ext.get("annotation")
    if annotation is not None:
        annotation_txt = str(annotation)
    elif ext.get("annotation_invalid") is not None:
        annotation_txt = f"invalid value {ext['annotation_invalid']!r}"
    else:
        annotation_txt = "absent → operator default (8)"
    rows.append(("ExtProc budget — annotated", annotation_txt))
    rows.append(("ExtProc budget — effective", _format_effective_budget(ext)))
    if ext.get("consistent") is False:
        rows.append(
            (
                "Budget consistency",
                "MISMATCH — the gateway did not enforce the annotated budget "
                "(silent out-of-range fallback to the operator default, or a "
                "reverted patch). Trust the effective value.",
            )
        )

    policies = state.identity_metrics.get("policies") or {}
    for name, info in sorted(policies.items()):
        value = info.get("identity_metrics")
        rows.append(
            (
                f"Identity metrics — AuthPolicy {name}",
                f"{'on' if value else 'off'} ({info.get('filters', '?')} filter(s))",
            )
        )
    authconfigs = state.identity_metrics.get("authconfigs")
    if authconfigs is None:
        rows.append(("Identity metrics — generated AuthConfigs", "not captured"))
    else:
        value = authconfigs.get("identity_metrics")
        total = authconfigs.get("total_filters", 0)
        if value is None:
            authconfigs_txt = f"inconsistent across {total} filters"
        else:
            authconfigs_txt = f"{'on' if value else 'off'} ({total} filters)"
        rows.append(("Identity metrics — generated AuthConfigs", authconfigs_txt))

    if state.authorino:
        replicas = state.authorino.get("replicas")
        cache = state.authorino.get("evaluator_cache_size")
        authorino_txt = f"replicas={replicas if replicas is not None else 'default (1)'}"
        if cache is not None:
            authorino_txt += f", evaluatorCacheSize={cache}"
        rows.append(("Authorino", authorino_txt))

    rows.append(
        (
            "Source",
            "captured cluster state (praxis_state_summary.json) — not the harness configuration",
        )
    )

    return (
        "<h2>Observed platform state</h2>"
        '<table class="variables-table"><tr><th>Setting</th><th>Observed value</th></tr>'
        + "\n".join(
            f"<tr><td><b>{html_module.escape(k)}</b></td><td>{html_module.escape(v)}</td></tr>"
            for k, v in rows
        )
        + "</table>"
    )


def _format_effective_budget(ext: dict) -> str:
    effective = ext.get("effective") or {}
    payload = {name: data for name, data in effective.items() if "payload" in name}
    if not payload:
        if not effective:
            return "not captured"
        return f"no payload ext_proc clusters (found: {', '.join(sorted(effective))})"

    parts = []
    for name, data in sorted(payload.items()):
        values = sorted(
            {
                e.get("max_requests")
                for e in data.get("limits", [])
                if e.get("max_requests") is not None
            },
            key=str,
        )
        part = f"{name}: {'/'.join(str(v) for v in values) or 'no limits'}"
        if data.get("threshold_entries", 0) > 1:
            part += f" ({data['threshold_entries']} threshold entries — botched merge)"
        parts.append(part)
    return "; ".join(parts)


def _render_indicators(stats: dict) -> str:
    rows: list[tuple[str, str]] = []

    if stats.get("peak_200") is not None:
        rows.append(("Peak 200 rate (observed success ceiling)", f"{stats['peak_200']:.0f} req/s"))
    if stats.get("peak_500") is not None:
        rows.append(("Peak 500 rate", f"{stats['peak_500']:.0f} req/s"))
    if stats.get("frac_500") is not None:
        rows.append(("500 fraction of gateway traffic", f"{stats['frac_500'] * 100:.1f} %"))
    if stats.get("unknown_share") is not None:
        rows.append(
            (
                "500s attributed to destination_workload=unknown",
                f"{stats['unknown_share'] * 100:.1f} % — Envoy-local replies, "
                "no upstream host selected",
            )
        )
    if stats.get("instant_frac") is not None:
        rows.append(
            (
                "Instant 500s (< 1 ms)",
                f"{stats['instant_frac'] * 100:.1f} % of 500s — ext_proc overflow signature",
            )
        )

    if stats.get("p50_500") is not None or stats.get("p95_500") is not None:
        p50_txt = f"{stats['p50_500']:.1f} ms" if stats.get("p50_500") is not None else "n/a"
        p95_txt = f"{stats['p95_500']:.0f} ms" if stats.get("p95_500") is not None else "n/a"
        rows.append(("500 latency p50 / p95 (peak)", f"{p50_txt} / {p95_txt}"))

    if stats.get("budget") is not None:
        rows.append(("ExtProc budget", f"{stats['budget']} ({stats.get('budget_source')})"))
    if stats.get("stream_duration") is not None:
        rows.append(("Mean ext_proc stream duration", f"{stats['stream_duration']:.1f} s"))
    if stats.get("predicted_ceiling") is not None:
        rows.append(
            (
                "Predicted admission ceiling (budget / duration)",
                f"{stats['predicted_ceiling']:.0f} req/s",
            )
        )
    if stats.get("authorino_peak") is not None:
        rows.append(("Peak authorino CPU (one pod)", f"{stats['authorino_peak']:.1f} cores"))

    if not rows:
        return ""
    return (
        "<h2>Derived indicators</h2>"
        '<table class="variables-table"><tr><th>Indicator</th><th>Value</th></tr>'
        + "\n".join(
            f"<tr><td><b>{html_module.escape(k)}</b></td><td>{html_module.escape(v)}</td></tr>"
            for k, v in rows
        )
        + "</table>"
    )


def _render_verdict(stats: dict) -> str:
    label, explanation = _classify(stats)
    return f"<h2>Verdict: {html_module.escape(label)}</h2><p>{html_module.escape(explanation)}</p>"


def _classify(stats: dict) -> tuple[str, str]:
    """Name the dominant failure regime and explain it with the computed numbers."""
    frac_500 = stats.get("frac_500")
    if frac_500 is None:
        return "no gateway telemetry", (
            "No gateway request metrics were captured for this test — only the "
            "platform state above could be reported."
        )
    if frac_500 < CLEAN_500_FRACTION:
        return "clean", (
            f"Only {frac_500 * 100:.1f} % of gateway traffic failed with 500s: the run "
            "stayed under the admission ceiling. " + _ceiling_sentence(stats)
        )

    instant = stats.get("instant_frac")

    parts = [
        f"{frac_500 * 100:.1f} % of gateway traffic failed with 500s",
        _ceiling_sentence(stats),
    ]

    if instant is None:
        # Without the duration-bucket distribution the dominant regime stays
        # unknown: the peak p50 is a display metric, not a run-wide fraction.
        label = "500 failures"
        parts.append(
            "The duration distribution of the 500s was not captured, so the "
            "dominant failure regime (ext_proc overflow vs auth deadline) "
            "cannot be determined."
        )
    elif instant >= 0.8:
        label = "ext_proc overflow"
        parts.append(
            f"{instant * 100:.0f} % of the 500s were instant (< 1 ms) Envoy-local rejections "
            "(destination_workload=unknown): the ext_proc circuit-breaker budget was "
            "exhausted, requests were shed before auth."
        )
    elif instant <= 0.2:
        label = "auth deadline"
        p95 = stats.get("p95_500")
        deadline_txt = f", p95 {p95:.0f} ms" if p95 is not None else ""
        parts.append(
            f"Only {instant * 100:.0f} % of the 500s were instant; the rest were slow"
            f"{deadline_txt} — pinned at the kuadrant shim's fail-closed auth deadline: "
            "authorino could not answer in time. " + _authorino_sentence(stats)
        )
    else:
        label = "mixed: ext_proc overflow + auth deadline"
        parts.append(
            f"{instant * 100:.0f} % of the 500s were instant Envoy-local rejections "
            "(ext_proc overflow), the rest were slow — pinned at the auth fail-closed "
            "deadline (authorino falling behind). " + _authorino_sentence(stats)
        )

    if stats.get("unknown_share") is not None and stats["unknown_share"] < 0.9:
        parts.append(
            f"Note: only {stats['unknown_share'] * 100:.0f} % of the 500s are "
            "destination_workload=unknown — part of the failures came from the model "
            "workload itself, not the gateway."
        )

    return label, " ".join(part for part in parts if part)


def _ceiling_sentence(stats: dict) -> str:
    budget = stats.get("budget")
    duration = stats.get("stream_duration")
    predicted = stats.get("predicted_ceiling")
    observed = stats.get("peak_200")

    if budget is None:
        return "The effective ext_proc budget was not captured, so the admission ceiling cannot be computed."
    if predicted is None:
        return f"The ext_proc budget was {budget}, but the stream duration was not captured, so the admission ceiling cannot be computed."

    sentence = (
        f"Every in-flight streaming request holds an ext_proc stream for its full "
        f"response, so the admission ceiling is budget / duration = {budget} / "
        f"{duration:.1f} s ≈ {predicted:.0f} req/s"
    )
    if observed is not None:
        sentence += f"; the observed peak success rate was {observed:.0f} req/s"
    return sentence + "."


def _authorino_sentence(stats: dict) -> str:
    peak = stats.get("authorino_peak")
    identity = stats.get("identity_metrics")
    if peak is None:
        return "Authorino CPU was not captured."
    sentence = f"Authorino CPU peaked at {peak:.1f} cores on one pod (connection pinning keeps replicas idle)."
    if identity is False:
        sentence += " Identity metrics were off (authorino's cheapest configuration)."
    elif identity is True:
        sentence += (
            " Identity metrics were ON — authorino re-compiles the filter's CEL label "
            "expressions per request (~72-88 % of its CPU in pprof profiles); turning "
            "them off is the cheapest lever."
        )
    return sentence


def _render_reading_guide() -> str:
    return (
        "<h2>How to read it</h2>"
        "<p>The 500 cliff of the maas/praxis path is a stack of two constraints "
        "(maas-praxis-500-ext-proc-overflow.md). Every in-flight streaming request "
        "holds an ext_proc stream for its full response, so the gateway admits at "
        "most budget / response-duration concurrent streams; above that ceiling "
        "Envoy sheds requests with an instant, empty 500 before auth — visible as "
        'destination_workload="unknown" in the code × destination panel. Below the '
        "ext_proc ceiling, the next wall is authentication: the kuadrant wasm shim "
        "fails auth closed at its deadline (~2 s) when authorino cannot keep up — "
        "slow 500s pinned just under the deadline, authorino CPU saturated on one "
        "connection-pinned pod. Closed-loop retry amplification then turns partial "
        "failure into near-total failure.</p>"
        "<p>The platform state table is read from the captured cluster artifacts "
        "(capture_maas_praxis), not from the harness configuration: an out-of-range "
        "budget annotation silently falls back to the operator default, and "
        "reconciles can revert patches — what the gateway enforced is what "
        "matters.</p>"
    )
