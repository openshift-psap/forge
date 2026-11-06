import json

from projects.rhaiis.postprocess import agent as agent_module
from projects.rhaiis.postprocess.agent import (
    _build_prompt,
    build_pr_followup_prompt,
    markdown_to_html,
    validate_pr_analysis_scope,
)


def test_markdown_to_html_renders_common_markdown() -> None:
    html = markdown_to_html(
        "### Regression summary\n\n**TTFT P95** increased.\n\n- profile1",
        job_id="job-123",
        model="example/model",
        current_version="current",
        compare_version="baseline",
    )

    assert "<h3>Regression summary</h3>" in html
    assert "<strong>TTFT P95</strong>" in html
    assert "<ul>\n<li>profile1</li>\n</ul>" in html
    assert "### Regression summary" not in html
    assert "**TTFT P95**" not in html


def test_markdown_to_html_keeps_all_measured_changes_and_escapes_html() -> None:
    regressions = [
        {
            "profile": "profile1",
            "metric": "Total Throughput",
            "baseline": 1000.0 + index,
            "current": 800.0 + index,
            "pct_diff": -20.0 - index,
        }
        for index in range(6)
    ]
    improvements = [
        {
            "profile": "profile4",
            "metric": "TTFT P95",
            "baseline": 400.0,
            "current": 300.0,
            "pct_diff": -25.0,
        }
    ]

    html = markdown_to_html(
        "## At a glance\n\n> **Measured:** Throughput fell.\n\n"
        "## Evidence and interpretation\n\n<script>alert(1)</script>",
        job_id="job-123",
        model="example/<model>",
        current_version="current",
        compare_version="baseline",
        regressions=regressions,
        improvements=improvements,
    )

    assert html.count('class="change change-regression"') == 6
    assert html.count('class="change change-improvement"') == 1
    assert "1k/1k" in html
    assert "8k/1k" in html
    assert 'href="#additional-changes"' in html
    assert "Additional measured changes" in html
    assert "<blockquote>" in html
    assert html.index("At a glance") < html.index("Measured changes")
    assert html.index("Measured changes") < html.index("Evidence and interpretation")
    assert "<script>" not in html
    assert "example/&lt;model&gt;" in html


def test_verbose_at_a_glance_is_compact_without_losing_original_detail() -> None:
    explanation = (
        "Startup logs show attention backend, KV cache, and scheduler changes "
        "that may alter batching and GPU launch overhead. "
        "The profiler has conflicting workload evidence. " * 35
    )
    html = markdown_to_html(
        "## At a glance\n\n"
        "> **Measured:** Many runtime arguments and profiler numbers are listed here.\n>\n"
        f"> **Explanation:** {explanation}\n>\n"
        "> **Next check:** Run a controlled A/B benchmark on H200 (TP=1) forcing "
        "the attention backend back to FLASH_ATTN, then inspect each scheduler metric.\n\n"
        "## Evidence and interpretation\n\nDetailed evidence follows.",
        job_id="job-123",
        model="example/model",
        current_version="current",
        compare_version="baseline",
        regressions=[
            {
                "profile": "profile7",
                "metric": "Total Throughput",
                "baseline": 16307.0,
                "current": 9125.0,
                "pct_diff": -44.0,
            },
            {
                "profile": "profile1",
                "metric": "Total Throughput",
                "baseline": 1000.0,
                "current": 865.0,
                "pct_diff": -13.5,
            },
        ],
    )

    assert "Total throughput fell 13.5–44.0% across 2 workloads" in html
    assert (
        "Changes involving attention backend, KV cache, and scheduling may have altered batching"
        in html
    )
    assert (
        "Run a controlled A/B benchmark on H200 forcing the attention backend back to FLASH_ATTN."
        in html
    )
    assert '<details class="full-summary">' in html
    assert explanation in html
    assert html.index("takeaway-list") < html.index("Measured changes")


def test_compaction_keeps_detail_when_agent_uses_h3_for_later_sections() -> None:
    html = markdown_to_html(
        "## At a glance\n\n> **Explanation:** "
        + "Detailed profiler evidence. " * 120
        + "\n\n### Evidence and interpretation\n\nThis section must stay visible.",
        job_id="job-123",
        model="example/model",
        current_version="current",
        compare_version="baseline",
    )

    assert html.index("</details>") < html.index("<h3>Evidence and interpretation</h3>")
    assert "This section must stay visible." in html


def test_pr_followup_scope_rejects_another_workload() -> None:
    prompt = build_pr_followup_prompt(
        "current",
        "baseline",
        job_id="job-123",
        model="google/gemma-4-12B-it",
        accelerator="H200",
        tp="1",
    )
    scope = "Scope: job=job-123; model=google/gemma-4-12B-it; accelerator=H200; TP=1"
    assert scope in prompt

    good, issue = validate_pr_analysis_scope(
        f"{scope}\n\nFor google/gemma-4-12B-it at TP=1, see this PR table.\n"
        "| PR | Relevant change | Connection to evidence | Evidence level |",
        job_id="job-123",
        model="google/gemma-4-12B-it",
        accelerator="H200",
        tp="1",
    )
    assert good is not None and issue is None

    wrong, issue = validate_pr_analysis_scope(
        f"{scope}\n\nFor google/gemma-4-12B-it at TP=1, see this PR. "
        "This TP=4 H200 Llama run improved throughput.",
        job_id="job-123",
        model="google/gemma-4-12B-it",
        accelerator="H200",
        tp="1",
    )
    assert wrong is None
    assert issue == "PR analysis refers to a different TP"

    wrong, issue = validate_pr_analysis_scope(
        "Scope: job=other-job; model=google/gemma-4-12B-it; accelerator=H200; TP=1\nPRs",
        job_id="job-123",
        model="google/gemma-4-12B-it",
        accelerator="H200",
        tp="1",
    )
    assert wrong is None
    assert issue == "missing or mismatched job/model/TP scope"


def test_pr_table_renders_as_readable_cards_with_short_links() -> None:
    pr_analysis = (
        "| PR | Relevant change | Connection to evidence | Evidence level |\n"
        "| --- | --- | --- | --- |\n"
        "| [vllm-project/vllm#53183](https://github.com/vllm-project/vllm/pull/53183) "
        "| Enables Model Runner V2 by default. More code detail follows. "
        "| Matches the server log transition. The runtime impact needs an A/B test. "
        "| Candidate explanation; verified in source, not isolated in a test. |\n\n"
        "Other inspected changes were unrelated to this workload."
    )
    html = markdown_to_html(
        "## At a glance\n\n> **Measured:** Throughput fell.",
        job_id="job-123",
        model="example/model",
        current_version="current",
        compare_version="baseline",
        pr_analysis=pr_analysis,
    )

    assert '<article class="pr-card">' in html
    assert ">vllm #53183</a>" in html
    assert "<strong>Why it matters:</strong> Matches the server log transition." in html
    assert '<details class="pr-details">' in html
    assert "More code detail follows" in html
    assert '<details class="pr-extra">' in html
    assert "Other inspected changes were unrelated" in html


def test_analysis_and_followup_share_report_thread_but_keep_job_session(monkeypatch) -> None:
    sent = []

    def fake_urlopen(request, *, timeout):
        sent.append(json.loads(request.data))
        return [b'{"type":"message","content":{"type":"ai","content":"ready"}}\n']

    monkeypatch.setattr(agent_module, "urlopen", fake_urlopen)
    thread_id = "forge-rhaiis-unique-report"
    assert (
        agent_module.request_agent_analysis(
            model="google/gemma-4-12B-it",
            agent_model="gemini-4-argon",
            accelerator="H200",
            current_version="current",
            compare_version="baseline",
            tp="1",
            severe_regressions=[],
            agent_url="https://example.test/v1/stream",
            job_id="job-123",
            thread_id=thread_id,
        )
        == "ready"
    )
    assert (
        agent_module.send_followup(
            message="Find PRs",
            job_id="job-123",
            agent_url="https://example.test/v1/stream",
            agent_model="gemini-4-argon",
            thread_id=thread_id,
        )
        == "ready"
    )

    assert len(sent) == 2
    assert [body["thread_id"] for body in sent] == [thread_id, thread_id]
    assert [body["session_id"] for body in sent] == [
        "forge-rhaiis-job-123",
        "forge-rhaiis-job-123",
    ]


def _prompt_kwargs(**overrides):
    kwargs = {
        "model": "example/model",
        "accelerator": "H200",
        "current_version": "current",
        "compare_version": "baseline",
        "tp": "1",
        "severe_regressions": [],
        "improvements": None,
    }
    kwargs.update(overrides)
    return kwargs


def test_build_prompt_regressions_only() -> None:
    prompt = _build_prompt(
        **_prompt_kwargs(
            severe_regressions=[
                {
                    "metric": "TTFT P95",
                    "profile": "profile1",
                    "pct_diff": -15.2,
                    "baseline": 10.0,
                    "current": 8.48,
                }
            ],
        )
    )

    assert "Analyze the performance differences" in prompt
    assert "regressed significantly" in prompt
    assert "improved significantly" not in prompt


def test_build_prompt_improvements_only() -> None:
    prompt = _build_prompt(
        **_prompt_kwargs(
            improvements=[
                {
                    "metric": "Output Throughput",
                    "profile": "profile1",
                    "pct_diff": 86.6,
                    "baseline": 100.0,
                    "current": 186.6,
                }
            ],
        )
    )

    assert "Analyze the performance differences" in prompt
    assert "regressed significantly" not in prompt
    assert "improved significantly" in prompt
    assert "Output Throughput" in prompt
