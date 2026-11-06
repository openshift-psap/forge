"""Client for the PSAP AI agent's /v1/stream endpoint.

Sends regression context as a natural-language prompt and collects the
streamed AI response into a markdown string. Ported from model-furnace.
"""

import json
import logging
import uuid
from datetime import UTC
from html import escape
from urllib.error import URLError
from urllib.request import Request, urlopen

import markdown as md_lib

logger = logging.getLogger(__name__)

AGENT_TIMEOUT_SECONDS = 600
AGENT_HEALTH_TIMEOUT = 15
AGENT_SEVERITY_THRESHOLD = 10
_SUMMARY_METRIC_LIMIT = 5
_METRIC_UNITS = {
    "Total Throughput": "tok/s",
    "Output Throughput": "tok/s",
    "TTFT P95": "ms",
    "ITL P95": "ms",
    "Median E2E Latency": "s",
}

_REPORT_FORMAT_INSTRUCTIONS = """Write a readable Markdown report for a printable HTML page. Do not return raw
HTML, Tailwind classes, emoji, or a code fence around the report.

Start with `## At a glance` and three short blockquote paragraphs labeled
`**Measured:**`, `**Explanation:**`, and `**Next check:**`, separated by blank
quoted lines (`>`). Give the decisive
benchmark outcome, the best-supported mechanism with its evidence level
(observation, hypothesis, or confirmed), and the next validation step. If the
root cause is unconfirmed, say so in the first screenful.

Forge adds a measured-changes table from the regression data to the report.
Do not repeat its full metric inventory or list every unchanged runtime argument.
Use only the few numbers needed to explain the result, with units and the actual
measured concurrency for run-specific throughput. Label aggregate results and
the concurrency range or points they cover when known.

Follow with `## Evidence and interpretation` (one short subsection per material
contributor), `## Limits and validation`, and `## Detailed evidence` when more
log, profiler, or source detail is needed. Separate measured observations from
plausible mechanisms and confirmed causes. Preserve material conflicting
evidence and source links; put long technical inventories in detailed evidence.
Keep paragraphs short. Put each list item on its own line with a blank line
before lists. Do not use inline asterisks as list separators."""


def check_agent_connectivity(agent_url: str) -> tuple[bool, str]:
    """Verify that the agent endpoint is reachable.

    Tries a lightweight GET to the agent health URL. Returns (ok, detail).
    """
    url = agent_url
    base_url = url.rsplit("/", 2)[0]
    health_url = f"{base_url}/health"

    try:
        req = Request(health_url, method="GET")
        resp = urlopen(req, timeout=AGENT_HEALTH_TIMEOUT)  # noqa: S310
        status = resp.getcode()
        if status and status < 400:
            return True, f"Agent reachable (HTTP {status})"
        return False, f"Agent returned HTTP {status}"
    except (URLError, OSError) as e:
        return False, f"Cannot reach agent ({type(e).__name__})"
    except Exception as e:
        return False, f"Unexpected health-check error ({type(e).__name__})"


def _build_prompt(
    model: str,
    accelerator: str,
    current_version: str,
    compare_version: str,
    tp: str,
    severe_regressions: list,
    improvements: list | None = None,
) -> str:
    """Build a natural-language prompt for the agent from comparison context."""
    lines = [
        f"Analyze the performance differences between {current_version} and {compare_version} "
        f"for model {model} on {accelerator} with TP={tp}."
    ]
    if severe_regressions:
        lines.append("")
        lines.append("The following metrics regressed significantly:")
        for r in severe_regressions:
            direction = "dropped" if r["pct_diff"] < 0 else "increased"
            lines.append(
                f"- {r['metric']} ({r['profile']}): {direction} {abs(r['pct_diff']):.1f}% "
                f"({r['baseline']:.2f} -> {r['current']:.2f})"
            )
    if improvements:
        lines.append("")
        lines.append("The following metrics improved significantly:")
        for r in improvements:
            direction = "increased" if r["pct_diff"] > 0 else "decreased"
            lines.append(
                f"- {r['metric']} ({r['profile']}): {direction} {abs(r['pct_diff']):.1f}% "
                f"({r['baseline']:.2f} -> {r['current']:.2f})"
            )
    lines.append("")
    lines.append(
        f"Check if the runtime args differ between {current_version} and {compare_version} "
        f"for model {model} on {accelerator} with TP={tp}. "
    )
    lines.append("")
    lines.append(
        "Provide root cause analysis using pytorch profiler traces, vLLM logs, "
        "and vLLM source code where available."
    )
    lines.extend(("", _REPORT_FORMAT_INSTRUCTIONS))
    return "\n".join(lines)


def build_pr_followup_prompt(
    current_version: str,
    compare_version: str,
) -> str:
    """Build a followup prompt asking the agent to identify relevant PRs."""
    return (
        "Based on your previous analysis, identify pull requests or commits "
        f"between {compare_version} and {current_version} that plausibly relate "
        "to the measured performance differences. Reuse the exact revisions "
        "and comparisons already verified in the conversation. For two upstream "
        "vLLM versions, use get_vllm_code_diff and get_vllm_pull_request as "
        "needed. For two downstream RHAIIS releases, use "
        "compare_downstream_vllm_releases with each server-reported runtime "
        "version if that comparison has not been done; check both downstream "
        "tags and their upstream bases. If an exact runtime tag is unavailable, "
        "state the limitation instead of guessing. Do not assume an upstream "
        "PR is included in a downstream build. Cite the exact "
        "tool-returned GitHub link for each verified candidate. If no PR can "
        "be tied to the compared code, say so. "
        "Return only a concise Markdown table with columns PR, Relevant change, "
        "Connection to evidence, and Evidence level. Put the most relevant PRs "
        "first and summarize other inspected PRs in one short note. Do not repeat "
        "the benchmark table, logs, or profiler findings from your previous "
        "response. A PR is a candidate explanation unless profiling or a "
        "controlled experiment confirms its performance effect. Use exact "
        "tool-returned links, no raw HTML, and no heading; Forge adds the "
        "Related Pull Requests heading."
    )


def request_agent_analysis(
    model: str,
    agent_model: str,
    accelerator: str,
    current_version: str,
    compare_version: str,
    tp: str,
    severe_regressions: list,
    agent_url: str,
    job_id: str = "",
    improvements: list | None = None,
) -> str | None:
    """Send regression context to the PSAP agent and return its analysis.

    Returns the agent's response as a markdown string, or None on failure.
    """
    url = agent_url

    prompt = _build_prompt(
        model=model,
        accelerator=accelerator,
        current_version=current_version,
        compare_version=compare_version,
        tp=tp,
        severe_regressions=severe_regressions,
        improvements=improvements,
    )

    session_key = f"forge-rhaiis-{job_id or uuid.uuid4()}"
    body = {
        "message": prompt,
        "thread_id": session_key,
        "session_id": session_key,
        "user_id": "forge-rhaiis",
        "stream_tokens": False,
        "model": agent_model,
    }

    logger.info("Requesting agent analysis for job %s", job_id)

    try:
        req = Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )
        resp = urlopen(req, timeout=AGENT_TIMEOUT_SECONDS)  # noqa: S310
        ai_content = _collect_response(resp)

        if ai_content:
            logger.info("Agent analysis received for job %s (%d chars)", job_id, len(ai_content))
        else:
            logger.warning("Agent returned empty response for job %s", job_id)

        return ai_content

    except (URLError, OSError) as e:
        logger.error("Agent request failed for job %s (%s)", job_id, type(e).__name__)
        return None
    except Exception as e:
        logger.error(
            "Unexpected error during agent analysis for job %s (%s)", job_id, type(e).__name__
        )
        return None


def send_followup(
    message: str,
    job_id: str,
    agent_url: str,
    agent_model: str,
) -> str | None:
    """Send a followup message to the agent on an existing session.

    Reuses the same thread_id/session_id as request_agent_analysis so the
    agent retains full conversation context from the initial analysis.

    Returns the agent's response as a markdown string, or None on failure.
    """
    url = agent_url

    session_key = f"forge-rhaiis-{job_id}"
    body = {
        "message": message,
        "thread_id": session_key,
        "session_id": session_key,
        "user_id": "forge-rhaiis",
        "stream_tokens": False,
        "model": agent_model,
    }

    logger.info("Sending agent followup for job %s", job_id)

    try:
        req = Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )
        resp = urlopen(req, timeout=AGENT_TIMEOUT_SECONDS)  # noqa: S310
        ai_content = _collect_response(resp)

        if ai_content:
            logger.info("Agent followup received for job %s (%d chars)", job_id, len(ai_content))
        else:
            logger.warning("Agent returned empty followup response for job %s", job_id)

        return ai_content

    except (URLError, OSError) as e:
        logger.error("Agent followup request failed for job %s (%s)", job_id, type(e).__name__)
        return None
    except Exception as e:
        logger.error(
            "Unexpected error during agent followup for job %s (%s)", job_id, type(e).__name__
        )
        return None


def _collect_response(resp) -> str | None:
    """Read the NDJSON stream and extract the final AI message content."""
    collected_tokens = []
    final_message = None

    for raw_line in resp:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line or line == "[DONE]":
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")
        content = event.get("content")

        if event_type == "token" and isinstance(content, str):
            collected_tokens.append(content)
        elif (
            event_type == "message"
            and isinstance(content, dict)
            and content.get("type") == "ai"
            and content.get("content")
        ):
            final_message = content["content"]

    if final_message:
        return final_message
    if collected_tokens:
        return "".join(collected_tokens)
    return None


def _profile_shape(profile: str) -> str:
    """Show the workload shape next to Forge's profile identifier."""
    from projects.rhaiis.postprocess.regression import PROFILE_MAP

    def short_tokens(value: int) -> str:
        return f"{value // 1000}k" if value % 1000 == 0 else str(value)

    for (input_tokens, output_tokens), name in PROFILE_MAP.items():
        if name == profile:
            return f"{short_tokens(input_tokens)}/{short_tokens(output_tokens)}"
    return profile


def _metric_table(changes: list[tuple[str, dict]]) -> str:
    rows = []
    for status, change in changes:
        profile = str(change["profile"])
        shape = _profile_shape(profile)
        profile_label = f"<strong>{escape(shape)}</strong>"
        if shape != profile:
            profile_label += f"<small>{escape(profile)}</small>"

        metric = str(change["metric"])
        unit = _METRIC_UNITS.get(metric)
        metric_label = escape(metric)
        if unit:
            metric_label += f' <span class="unit">({unit})</span>'

        rows.append(
            f"<tr><td>{profile_label}</td><td>{metric_label}</td>"
            f'<td class="number">{change["baseline"]:,.2f}</td>'
            f'<td class="number">{change["current"]:,.2f}</td>'
            f'<td><span class="change change-{status}">'
            f"{status.title()} {change['pct_diff']:+.1f}%</span></td></tr>"
        )

    return (
        '<div class="table-scroll"><table class="measurements" '
        'aria-label="Significant benchmark changes">'
        '<thead><tr><th scope="col">Workload</th><th scope="col">Metric</th>'
        '<th scope="col">Baseline</th><th scope="col">Current</th>'
        '<th scope="col">Change</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table></div>"
    )


def _metric_sections(
    regressions: list[dict] | None, improvements: list[dict] | None
) -> tuple[str, str]:
    """Render stable benchmark highlights independently of the agent model."""
    changes = [
        *(("regression", change) for change in (regressions or [])),
        *(("improvement", change) for change in (improvements or [])),
    ]
    if not changes:
        return "", ""

    changes.sort(key=lambda item: abs(item[1]["pct_diff"]), reverse=True)
    headline = changes[:_SUMMARY_METRIC_LIMIT]
    additional = changes[_SUMMARY_METRIC_LIMIT:]
    more_link = (
        f'<p class="more-changes"><a href="#additional-changes">'
        f"View {len(additional)} additional measured change(s)</a></p>"
        if additional
        else ""
    )
    overview = (
        '<section class="metric-overview" aria-labelledby="measured-changes">'
        '<h2 id="measured-changes">Measured changes</h2>'
        '<p class="section-note">Forge benchmark comparison: geometric means '
        "over matched intended concurrency levels above 1. Red and green labels "
        "describe impact; these aggregate values do not prove a root cause.</p>"
        + _metric_table(headline)
        + more_link
        + "</section>"
    )
    appendix = (
        '<section class="additional-changes" id="additional-changes">'
        "<h2>Additional measured changes</h2>" + _metric_table(additional) + "</section>"
        if additional
        else ""
    )
    return overview, appendix


def _place_overview_after_summary(body: str, overview: str) -> str:
    """Keep the model's opening conclusion above Forge's measured table."""
    if not overview:
        return body

    summary_heading = "<h2>At a glance</h2>"
    if not body.startswith(summary_heading):
        return overview + body

    next_section = body.find("<h2>", len(summary_heading))
    if next_section < 0:
        next_section = len(body)
    return body[:next_section] + overview + body[next_section:]


_REPORT_STYLE = """
  :root { color-scheme: light; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    max-width: 960px; margin: 0 auto; padding: 2rem;
    color: #1e293b; line-height: 1.55; background: #fff;
  }
  h1 { font-size: 1.6rem; margin: 0 0 .7rem; color: #172554; }
  h2 { font-size: 1.25rem; margin: 1.8rem 0 .65rem; color: #1e3a8a; }
  h3 { font-size: 1.06rem; margin: 1.25rem 0 .45rem; color: #334155; }
  p, li { margin: .45rem 0; }
  ul, ol { padding-left: 1.5rem; }
  li > p { margin: .25rem 0; }
  .meta {
    display: flex; flex-wrap: wrap; gap: .35rem 1.25rem;
    border-top: 1px solid #cbd5e1; padding-top: .85rem;
    color: #475569; font-size: .9rem;
  }
  .meta span { overflow-wrap: anywhere; }
  .metric-overview {
    margin: 1.6rem 0; padding: 1rem 1.2rem;
    border: 1px solid #bfdbfe; border-left: 5px solid #2563eb;
    border-radius: 8px; background: #f8fbff;
  }
  .metric-overview h2 { margin: 0 0 .4rem; }
  .section-note { color: #475569; font-size: .88rem; margin: .35rem 0 .85rem; }
  .more-changes { font-size: .9rem; margin: .8rem 0 0; }
  .table-scroll { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; margin: .8rem 0 1.2rem; }
  .measurements { font-size: .89rem; }
  th, td {
    border-bottom: 1px solid #dbe4ef; padding: .58rem .65rem;
    text-align: left; vertical-align: top; overflow-wrap: anywhere;
  }
  th { background: #eaf2ff; color: #1e3a5f; font-weight: 700; }
  tbody tr:nth-child(even) { background: #f8fafc; }
  td.number { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
  td small { display: block; color: #64748b; }
  .unit { color: #64748b; font-size: .9em; }
  .change {
    display: inline-block; border: 1px solid currentColor;
    border-radius: 999px; padding: .1rem .5rem;
    font-weight: 700; white-space: nowrap;
  }
  .change-regression { color: #9f1239; background: #fff1f2; }
  .change-improvement { color: #166534; background: #f0fdf4; }
  .analysis-body > h2 { border-bottom: 1px solid #dbe4ef; padding-bottom: .3rem; }
  .analysis-body blockquote {
    margin: .8rem 0 1.2rem; padding: .6rem 1rem;
    border-left: 4px solid #2563eb; background: #eff6ff;
  }
  .analysis-body blockquote p { margin: .35rem 0; }
  pre {
    background: #f1f5f9; padding: .9rem; border-radius: 6px;
    overflow-x: auto; font-size: .85rem;
  }
  code { background: #f1f5f9; padding: .12em .28em; border-radius: 3px; font-size: .88em; }
  pre code { background: none; padding: 0; }
  a { color: #1d4ed8; overflow-wrap: anywhere; }
  .additional-changes { margin-top: 2rem; }
  @media (max-width: 700px) {
    body { padding: 1rem; }
    .metric-overview { padding: .8rem; }
    table { min-width: 590px; }
  }
  @page { size: letter; margin: .65in; }
  @media print {
    body {
      max-width: none; padding: 0; font-size: 11pt;
      -webkit-print-color-adjust: exact; print-color-adjust: exact;
    }
    .table-scroll { overflow: visible; }
    table { min-width: 0; font-size: 9.5pt; }
    thead { display: table-header-group; }
    tr, .meta, blockquote { break-inside: avoid; }
    h1, h2, h3 { break-after: avoid; }
    p, li { orphans: 3; widows: 3; }
    pre { white-space: pre-wrap; overflow-wrap: anywhere; }
  }
"""


def markdown_to_html(
    md_text: str,
    job_id: str,
    model: str,
    current_version: str,
    compare_version: str,
    *,
    regressions: list[dict] | None = None,
    improvements: list[dict] | None = None,
) -> str:
    """Convert markdown analysis to a self-contained HTML page."""
    # The agent supplies Markdown, not trusted HTML. Escaping '<' prevents raw
    # tags from passing through Python-Markdown while preserving blockquotes.
    body = md_lib.markdown(
        md_text.replace("<", "&lt;"),
        extensions=["tables", "fenced_code", "sane_lists"],
    )
    overview, additional = _metric_sections(regressions, improvements)
    body = _place_overview_after_summary(body, overview)

    from datetime import datetime

    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    safe_job_id = escape(job_id)
    safe_model = escape(model)
    safe_current = escape(current_version)
    safe_compare = escape(compare_version)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Analysis — {safe_job_id}</title>
<style>{_REPORT_STYLE}</style>
</head>
<body>
<h1>Regression Analysis Report</h1>
<div class="meta">
  <span><strong>Job:</strong> {safe_job_id}</span>
  <span><strong>Model:</strong> {safe_model}</span>
  <span><strong>Versions:</strong> {safe_current} vs {safe_compare}</span>
  <span><strong>Generated:</strong> {timestamp}</span>
</div>
<main>
<article class="analysis-body">{body}</article>
{additional}
</main>
</body>
</html>"""
