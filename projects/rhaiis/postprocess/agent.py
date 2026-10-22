"""Client for the PSAP AI agent's /v1/stream endpoint.

Sends regression context as a natural-language prompt and collects the
streamed AI response into a markdown string. Ported from model-furnace.
"""

import json
import logging
import uuid
from datetime import UTC
from urllib.error import URLError
from urllib.request import Request, urlopen

import markdown as md_lib

logger = logging.getLogger(__name__)

AGENT_TIMEOUT_SECONDS = 600
AGENT_HEALTH_TIMEOUT = 15
AGENT_SEVERITY_THRESHOLD = 10


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
    """Build a natural-language prompt for the agent from regression context."""
    lines = [
        f"Analyze the performance regression between {current_version} and {compare_version} "
        f"for model {model} on {accelerator} with TP={tp}.",
        "",
        "The following metrics regressed significantly (>10%):",
    ]
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
    return "\n".join(lines)


def build_pr_followup_prompt(
    current_version: str,
    compare_version: str,
) -> str:
    """Build a followup prompt asking the agent to identify relevant PRs."""
    return (
        "Based on your previous analysis, use the compare_vllm_versions and "
        "get_vllm_pull_request tools to identify which specific pull requests "
        f"between vLLM {compare_version} and {current_version} most likely "
        "explain the performance differences you found. For each PR you "
        "identify, include its GitHub link and a brief explanation of why "
        "it's relevant to the regressions or improvements you observed."
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


def markdown_to_html(
    md_text: str,
    job_id: str,
    model: str,
    current_version: str,
    compare_version: str,
) -> str:
    """Convert markdown analysis to a self-contained HTML page."""
    body = md_lib.markdown(md_text, extensions=["tables", "fenced_code"])

    from datetime import datetime

    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Analysis — {job_id}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         max-width: 900px; margin: 0 auto; padding: 2rem; color: #1a1a1a; line-height: 1.6; }}
  h1 {{ font-size: 1.5rem; border-bottom: 2px solid #e2e2e2; padding-bottom: .5rem; }}
  h2 {{ font-size: 1.25rem; margin-top: 2rem; }}
  .meta {{ color: #555; font-size: 0.9rem; margin-bottom: 2rem; }}
  .meta span {{ margin-right: 1.5rem; }}
  pre {{ background: #f5f5f5; padding: 1rem; border-radius: 6px; overflow-x: auto; }}
  code {{ background: #f0f0f0; padding: 0.15em 0.3em; border-radius: 3px; font-size: 0.9em; }}
  pre code {{ background: none; padding: 0; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
  th, td {{ border: 1px solid #ddd; padding: 0.5rem 0.75rem; text-align: left; }}
  th {{ background: #f5f5f5; }}
</style>
</head>
<body>
<h1>Regression Analysis Report</h1>
<div class="meta">
  <span><strong>Job:</strong> {job_id}</span>
  <span><strong>Model:</strong> {model}</span>
  <span><strong>Versions:</strong> {current_version} vs {compare_version}</span>
  <span><strong>Generated:</strong> {timestamp}</span>
</div>
{body}
</body>
</html>"""
