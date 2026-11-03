from projects.rhaiis.postprocess.agent import _build_prompt, markdown_to_html


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
