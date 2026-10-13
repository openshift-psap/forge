from projects.rhaiis.postprocess.agent import markdown_to_html


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
