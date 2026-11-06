"""Client for the PSAP AI agent's /v1/stream endpoint.

Sends regression context as a natural-language prompt and collects the
streamed AI response into a markdown string. Ported from model-furnace.
"""

import json
import logging
import re
from datetime import UTC
from html import escape, unescape
from urllib.error import URLError
from urllib.parse import urlparse
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

Start with `## At a glance` and three one-sentence blockquote paragraphs labeled
`**Measured:**`, `**Explanation:**`, and `**Next check:**`, separated by blank
quoted lines (`>`). Keep each paragraph under 30 words. Give the decisive
benchmark outcome, the best-supported mechanism with its evidence level
(observation, hypothesis, or confirmed), and the next validation step. If the
root cause is unconfirmed, say so in the first screenful. Do not put UUIDs,
source paths, configuration inventories, or profiler timings in this summary.

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
    *,
    job_id: str,
    model: str,
    accelerator: str,
    tp: str,
) -> str:
    """Build a followup prompt asking the agent to identify relevant PRs."""
    scope_line = _pr_scope_line(job_id, model, accelerator, tp)
    return (
        f"Continue only this job: {scope_line}. The PR analysis must apply to "
        "this exact model, accelerator, and tensor-parallel size. Ignore evidence "
        "from other jobs, models, or TP values. If your current context does not "
        "match, return `Scope mismatch` and stop. Based on your previous analysis, "
        "identify pull requests or commits "
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
        f"Start your answer with this exact line: {scope_line}\n"
        "Then return a Markdown table with columns PR, Relevant change, "
        "Connection to evidence, and Evidence level. List at most five PRs, most "
        "relevant first. Keep each cell to one short sentence, with no long code "
        "paths, commit SHAs, or repeated metric inventories. Summarize other "
        f"inspected PRs in one short note. Include {model} and TP={tp} together "
        "in one Connection to evidence cell (or in the no-PR statement) so "
        "the report can verify its scope. "
        "Do not repeat "
        "the benchmark table, logs, or profiler findings from your previous "
        "response. A PR is a candidate explanation unless profiling or a "
        "controlled experiment confirms its performance effect. Use exact "
        "tool-returned links, no raw HTML, and no heading; Forge adds the "
        "Related Pull Requests heading."
    )


def _pr_scope_line(job_id: str, model: str, accelerator: str, tp: str) -> str:
    return f"Scope: job={job_id}; model={model}; accelerator={accelerator}; TP={tp}"


def validate_pr_analysis_scope(
    response: str, *, job_id: str, model: str, accelerator: str, tp: str
) -> tuple[str | None, str | None]:
    """Keep another job's PR analysis out of this report."""
    lines = response.strip().splitlines()
    expected = _pr_scope_line(job_id, model, accelerator, tp)
    if not lines or lines[0].strip() != expected:
        return None, "missing or mismatched job/model/TP scope"

    analysis = "\n".join(lines[1:]).strip()
    if not analysis:
        return None, "empty PR analysis"
    if model.lower() not in analysis.lower() or f"TP={tp}" not in analysis:
        return None, "PR analysis lacks the current model and TP evidence"

    for match in re.finditer(r"\bTP\s*=\s*(\d+)\b", analysis, flags=re.IGNORECASE):
        if match.group(1) != tp:
            return None, "PR analysis refers to a different TP"

    return analysis, None


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
    *,
    thread_id: str,
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

    session_id = f"forge-rhaiis-{job_id}" if job_id else thread_id
    body = {
        "message": prompt,
        "thread_id": thread_id,
        "session_id": session_id,
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
    *,
    thread_id: str,
) -> str | None:
    """Send a followup message to the agent on an existing session.

    Reuses the analysis thread so the agent retains its conversation context.
    The session ID still groups both traces under the Forge job.

    Returns the agent's response as a markdown string, or None on failure.
    """
    url = agent_url

    session_id = f"forge-rhaiis-{job_id}" if job_id else thread_id
    body = {
        "message": message,
        "thread_id": thread_id,
        "session_id": session_id,
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
        '<thead><tr><th scope="col">Shape</th><th scope="col">Metric</th>'
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


def _plain_text(fragment: str) -> str:
    """Remove rendered Markdown tags for short, escaped report takeaways."""
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _metric_sentence(change: dict) -> str:
    direction = "fell" if change["pct_diff"] < 0 else "rose"
    shape = _profile_shape(str(change["profile"]))
    return f"{change['metric']} {direction} {abs(change['pct_diff']):.1f}% on {shape}."


def _measured_takeaway(regressions: list[dict], improvements: list[dict]) -> str:
    throughput = [change for change in regressions if change["metric"] == "Total Throughput"]
    workloads = {str(change["profile"]) for change in throughput}
    if len(workloads) > 1:
        worst = max(throughput, key=lambda change: abs(change["pct_diff"]))
        low = min(abs(change["pct_diff"]) for change in throughput)
        high = abs(worst["pct_diff"])
        measured = (
            f"Total throughput fell {low:.1f}–{high:.1f}% across {len(workloads)} "
            f"workloads; the largest drop was on {_profile_shape(str(worst['profile']))}."
        )
    elif throughput:
        measured = _metric_sentence(max(throughput, key=lambda item: abs(item["pct_diff"])))
    elif regressions:
        measured = _metric_sentence(max(regressions, key=lambda item: abs(item["pct_diff"])))
    else:
        measured = ""

    if improvements:
        best = max(improvements, key=lambda item: abs(item["pct_diff"]))
        improvement = _metric_sentence(best)
        measured = f"{measured} Improvement: {improvement}" if measured else improvement

    return measured or "The benchmark results are listed in Measured changes below."


def _explanation_takeaway(original: str) -> str:
    if original and len(original.split()) <= 35:
        return original

    lower = original.lower()
    topics = [
        label
        for keyword, label in (
            ("attention", "attention backend"),
            ("kv cache", "KV cache"),
            ("schedul", "scheduling"),
            ("cuda graph", "CUDA graphs"),
            ("gpu kernel", "GPU kernels"),
            ("network", "networking"),
        )
        if keyword in lower
    ][:3]
    if topics:
        subject = topics[0] if len(topics) == 1 else f"{', '.join(topics[:-1])}, and {topics[-1]}"
        if "batch" in lower and ("launch" in lower or "dispatch" in lower):
            return (
                f"Changes involving {subject} may have altered batching and GPU launch "
                "behavior. Their effect on throughput is not isolated."
            )
        return (
            f"Possible factors involve {subject}. "
            "The detailed evidence below separates observed changes from unconfirmed causes."
        )
    return (
        "The detailed evidence below explains the proposed mechanism and what remains unconfirmed."
    )


def _first_clause(original: str) -> str:
    depth = 0
    for index, character in enumerate(original):
        if character in "([":
            depth += 1
        elif character in ")]":
            depth = max(0, depth - 1)
        elif character in ",;" and depth == 0:
            return original[:index].strip()
    return original.strip()


def _first_action(original: str) -> str:
    action = re.sub(r"\([^()]*\)|\[[^\[\]]*\]", "", _first_clause(original))
    action = re.sub(r"\s+", " ", action).strip().rstrip(".:")
    if 4 <= len(action.split()) <= 32:
        return f"{action}."
    return "Follow the validation plan below to test the proposed mechanism."


def _summary_field(section: str, name: str) -> str:
    match = re.search(
        rf"<strong>\s*{re.escape(name)}:\s*</strong>(.*?)(?:</p>|$)",
        section,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return _plain_text(match.group(1)) if match else ""


def _compact_at_a_glance(body: str, regressions: list[dict], improvements: list[dict]) -> str:
    """Keep a verbose model summary available without letting it dominate page one."""
    heading = "<h2>At a glance</h2>"
    start = body.find(heading)
    if start < 0:
        return body

    content_start = start + len(heading)
    next_heading = body.find("<h2>", content_start)
    if next_heading >= 0:
        end = next_heading
    else:
        quote_end = body.find("</blockquote>", content_start)
        if quote_end < 0:
            return body
        end = quote_end + len("</blockquote>")
    original = body[content_start:end]
    if len(_plain_text(original).split()) <= 95:
        return body

    measured = _measured_takeaway(regressions, improvements)
    explanation = _explanation_takeaway(_summary_field(original, "Explanation"))
    next_check = _first_action(_summary_field(original, "Next check"))
    cards = "".join(
        f'<div class="takeaway"><strong>{label}</strong><p>{escape(value)}</p></div>'
        for label, value in (
            ("Measured", measured),
            ("Explanation", explanation),
            ("Next check", next_check),
        )
    )
    compact = (
        f'<div class="takeaway-list">{cards}</div>'
        '<details class="full-summary"><summary>Read the full agent summary</summary>'
        f"{original}</details>"
    )
    return body[:content_start] + compact + body[end:]


def _pr_preview(fragment: str) -> str:
    plain = _plain_text(fragment)
    clause = _first_clause(plain)
    if 6 <= len(clause.split()) <= 36:
        ending = "." if clause == plain else "…"
        return clause.rstrip(".:") + ending
    if len(plain.split()) <= 36:
        return plain
    return " ".join(plain.split()[:36]).rstrip(".:") + "…"


def _pr_label_and_link(cell: str) -> tuple[str, str]:
    match = re.search(r'<a\s+href="([^"]+)"', cell)
    if match:
        url = unescape(match.group(1))
        parsed = urlparse(url)
        parts = parsed.path.strip("/").split("/")
        if parsed.scheme == "https" and parsed.hostname == "github.com" and len(parts) >= 4:
            repo = parts[1]
            if parts[2] == "pull" and parts[3].isdigit():
                return f"{repo} #{parts[3]}", url
            if parts[2] == "commit":
                return f"{repo} commit {parts[3][:8]}", url
    return _plain_text(cell)[:60], ""


def _render_pr_section(pr_analysis: str | None, pr_warning: str | None) -> str:
    if pr_warning:
        return (
            '<section class="pr-section"><h2>Related Pull Requests</h2>'
            f'<p class="pr-warning">{escape(pr_warning)}</p></section>'
        )
    if not pr_analysis:
        return ""

    rendered = md_lib.markdown(
        pr_analysis.replace("<", "&lt;"), extensions=["tables", "fenced_code", "sane_lists"]
    )
    table = re.search(r"<table>(.*?)<tbody>(.*?)</tbody>\s*</table>", rendered, re.DOTALL)
    if not table:
        return f'<section class="pr-section"><h2>Related Pull Requests</h2>{rendered}</section>'
    headers = [
        _plain_text(cell).lower()
        for cell in re.findall(r"<th[^>]*>(.*?)</th>", table.group(1), re.DOTALL)
    ]
    if headers != ["pr", "relevant change", "connection to evidence", "evidence level"]:
        return f'<section class="pr-section"><h2>Related Pull Requests</h2>{rendered}</section>'

    cards = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table.group(2), re.DOTALL):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
        if len(cells) != 4:
            return f'<section class="pr-section"><h2>Related Pull Requests</h2>{rendered}</section>'
        pr_cell, change, connection, evidence = cells
        label, link = _pr_label_and_link(pr_cell)
        title = (
            f'<a href="{escape(link, quote=True)}">{escape(label)}</a>' if link else escape(label)
        )
        status = "Candidate" if "candidate" in _plain_text(evidence).lower() else "Evidence noted"
        cards.append(
            '<article class="pr-card">'
            f'<h3>{title}<span class="pr-status">{status}</span></h3>'
            f"<p><strong>Change:</strong> {escape(_pr_preview(change))}</p>"
            f"<p><strong>Why it matters:</strong> {escape(_pr_preview(connection))}</p>"
            f'<p class="pr-evidence"><strong>Evidence:</strong> {escape(_pr_preview(evidence))}</p>'
            '<details class="pr-details"><summary>Full PR evidence</summary>'
            f"<p><strong>Change:</strong> {change}</p>"
            f"<p><strong>Connection:</strong> {connection}</p>"
            f"<p><strong>Evidence level:</strong> {evidence}</p>"
            "</details></article>"
        )

    note = rendered[table.end() :].strip()
    extra = (
        f'<details class="pr-extra"><summary>Other inspected changes</summary>{note}</details>'
        if note
        else ""
    )
    return (
        '<section class="pr-section"><h2>Related Pull Requests</h2>'
        '<p class="section-note">Source evidence shows what changed; a controlled test '
        "is needed to establish each change's performance effect.</p>"
        + "".join(cards)
        + extra
        + "</section>"
    )


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
  .takeaway-list { display: grid; gap: .6rem; margin: .8rem 0; }
  .takeaway {
    border-left: 4px solid #2563eb; padding: .55rem .8rem;
    background: #f8fafc;
  }
  .takeaway:nth-child(2) { border-left-color: #d97706; }
  .takeaway:nth-child(3) { border-left-color: #15803d; }
  .takeaway p { margin: .15rem 0 0; }
  .full-summary, .pr-details, .pr-extra {
    margin: .6rem 0; color: #475569; font-size: .9rem;
  }
  summary { cursor: pointer; color: #1d4ed8; }
  .table-scroll { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; margin: .8rem 0 1.2rem; }
  .measurements { font-size: .89rem; }
  .measurements th:first-child { min-width: 4rem; white-space: nowrap; }
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
  .pr-section { margin-top: 1.8rem; }
  .pr-section > h2 { border-bottom: 1px solid #dbe4ef; padding-bottom: .3rem; }
  .pr-card {
    border: 1px solid #dbe4ef; border-left: 4px solid #2563eb;
    border-radius: 6px; margin: .75rem 0; padding: .65rem .9rem;
    break-inside: avoid;
  }
  .pr-card h3 { display: flex; flex-wrap: wrap; gap: .5rem; margin: 0 0 .4rem; }
  .pr-card p { margin: .3rem 0; }
  .pr-evidence { color: #475569; font-size: .9rem; }
  .pr-status {
    border: 1px solid #f59e0b; border-radius: 999px;
    padding: .05rem .45rem; color: #92400e; background: #fffbeb;
    font-size: .76rem; font-weight: 700;
  }
  .pr-warning {
    border-left: 4px solid #b45309; padding: .7rem .9rem;
    background: #fffbeb;
  }
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
    tr, .meta, blockquote, .takeaway, .pr-card { break-inside: avoid; }
    h1, h2, h3 { break-after: avoid; }
    p, li { orphans: 3; widows: 3; }
    pre { white-space: pre-wrap; overflow-wrap: anywhere; }
    .full-summary, .pr-details, .pr-extra { display: none; }
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
    pr_analysis: str | None = None,
    pr_warning: str | None = None,
) -> str:
    """Convert markdown analysis to a self-contained HTML page."""
    # The agent supplies Markdown, not trusted HTML. Escaping '<' prevents raw
    # tags from passing through Python-Markdown while preserving blockquotes.
    body = md_lib.markdown(
        md_text.replace("<", "&lt;"),
        extensions=["tables", "fenced_code", "sane_lists"],
    )
    body = _compact_at_a_glance(body, regressions or [], improvements or [])
    overview, additional = _metric_sections(regressions, improvements)
    body = _place_overview_after_summary(body, overview)
    pr_section = _render_pr_section(pr_analysis, pr_warning)

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
{pr_section}
{additional}
</main>
</body>
</html>"""
