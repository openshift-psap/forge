"""
Generate HTML index pages for Caliper reports.

The index mirrors the completion notification layout: report entries come from
the visualize step of the postprocess status (ReportFile dicts carrying
scope/source metadata) and are grouped by test scope, then by report source.

Paths are resolved against the step base and only kept when the file exists
on disk, so stale metadata never produces dead links.
"""

import html
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ReportEntry:
    """A single report file listed in the index."""

    title: str
    path: Path  # absolute path
    scope: str  # "global" or test node name, e.g. "000__maas_praxis"
    source: str  # report source, e.g. "guidellm", "prometheus_metrics"


def generate_caliper_reports_index(
    status: dict[str, Any], output_dir: Path, index_filename: str = "index.html"
) -> Path | None:
    """
    Generate an HTML index page with links to all Caliper reports.

    Args:
        status: Caliper postprocess status dict with steps information
        output_dir: Directory where reports are located and where index will be written
        index_filename: Name of the index file to generate (default: "index.html")

    Returns:
        Path to the generated index file, or None if no reports found
    """
    output_dir = Path(output_dir).resolve()
    index_path = output_dir / index_filename

    entries = _collect_report_entries(status, output_dir, index_filename)
    if not entries:
        logger.info("No HTML reports found, skipping index generation")
        return None

    html_content = _generate_index_html_content(entries, output_dir, status)

    try:
        index_path.write_text(html_content, encoding="utf-8")
    except OSError as e:
        logger.warning("Failed to write reports index to %s: %s", index_path, e)
        return None

    logger.info("Generated Caliper reports index at %s", index_path)
    return index_path


def _collect_report_entries(
    status: dict[str, Any], output_dir: Path, index_filename: str
) -> list[_ReportEntry]:
    """Collect report entries from the visualize steps of the postprocess status."""
    entries_by_path: dict[Path, _ReportEntry] = {}

    for entry in _entries_from_status(status, output_dir):
        if entry.path.name != index_filename:
            entries_by_path.setdefault(entry.path, entry)

    return list(entries_by_path.values())


def _entries_from_status(status: dict[str, Any], output_dir: Path) -> list[_ReportEntry]:
    """Extract report entries from the visualize steps of the postprocess status."""
    base_dir = _base_directory(status, output_dir)

    entries = []
    for step in status.get("steps", []):
        if not isinstance(step, dict):
            continue
        step_data = step.get("visualize")
        if not isinstance(step_data, dict):
            continue
        entries.extend(_entries_from_visualize_step(step_data, base_dir, output_dir))
    return entries


def _entries_from_visualize_step(
    step_data: dict[str, Any], base_dir: Path, output_dir: Path
) -> list[_ReportEntry]:
    """Extract report entries from a visualize step result dict.

    ReportFile paths are relative to the step output_dir, itself relative to
    the postprocess base directory.
    """
    step_output_dir = _resolve_path(step_data.get("output_dir"), base_dir) or output_dir

    entries = []
    for report in step_data.get("output_files") or []:
        if not isinstance(report, dict) or not report.get("path"):
            continue
        path = _resolve_path(report["path"], step_output_dir)
        if path is None or not path.is_file():
            continue
        entries.append(
            _ReportEntry(
                title=report.get("title") or path.stem,
                path=path,
                scope=report.get("scope") or "global",
                source=report.get("source") or "unknown",
            )
        )
    return entries


def _group_entries(
    entries: list[_ReportEntry],
) -> list[tuple[str, dict[str, list[_ReportEntry]]]]:
    """Group entries by scope then by source, in status order.

    Scopes are sorted with "global" first, then test scopes alphabetically —
    the same ordering as the completion notification.
    """
    scope_groups: dict[str, dict[str, list[_ReportEntry]]] = {}
    for entry in entries:
        scope_groups.setdefault(entry.scope, {}).setdefault(entry.source, []).append(entry)

    sorted_scopes = sorted(scope_groups, key=lambda s: ("0" if s == "global" else "1", s))
    return [(scope, scope_groups[scope]) for scope in sorted_scopes]


def _scope_display(scope: str) -> str:
    """Display name of a report scope."""
    return "Global" if scope == "global" else f"Test {scope}"


def _source_display(source: str) -> str:
    """Display name of a report source."""
    return source.replace("_", " ").title()


def _base_directory(status: dict[str, Any], output_dir: Path) -> Path:
    """Base directory the postprocess status paths are relative to."""
    base_directory = status.get("base_directory")
    if base_directory:
        path = Path(base_directory)
        if path.is_absolute():
            return path
    return output_dir


def _resolve_path(path_str: Any, base: Path | None) -> Path | None:
    """Resolve a possibly-relative path against a base directory."""
    if not path_str:
        return None

    path = Path(str(path_str))
    if path.is_absolute():
        return path
    if base is None:
        return None
    return base / path


def _link_target(entry: _ReportEntry, output_dir: Path) -> str:
    """Relative link target of an entry from the index location."""
    try:
        return str(entry.path.relative_to(output_dir))
    except ValueError:
        return os.path.relpath(entry.path, output_dir)


def _generate_index_html_content(
    entries: list[_ReportEntry], output_dir: Path, status: dict[str, Any]
) -> str:
    """Generate the HTML content for the reports index."""
    test_phase = status.get("test_phase", {})
    test_status = test_phase.get("phase", "UNKNOWN")
    final_status = status.get("final_status", "unknown")
    status_class = "success" if "failed" not in str(final_status) else "error"

    html_parts = [
        "<!DOCTYPE html>",
        "<html>",
        "<head>",
        "    <meta charset='utf-8'>",
        "    <title>Caliper Reports Index</title>",
        "    <style>",
        "        body { font-family: Arial, sans-serif; margin: 40px; }",
        "        h1 { color: #333; border-bottom: 2px solid #333; }",
        "        h2 { color: #666; margin-top: 30px; }",
        "        h3 { color: #888; margin-top: 20px; }",
        "        ul { list-style-type: disc; padding-left: 20px; }",
        "        li { margin: 8px 0; }",
        "        a { text-decoration: none; color: #0066cc; }",
        "        .status { padding: 10px; border-radius: 5px; margin: 20px 0; }",
        "        .success { background-color: #d4edda; border: 1px solid #c3e6cb; }",
        "        .warning { background-color: #fff3cd; border: 1px solid #ffeaa7; }",
        "        .error { background-color: #f8d7da; border: 1px solid #f5c6cb; }",
        "        .report .path { color: #999; font-size: 0.85em; }",
        "    </style>",
        "</head>",
        "<body>",
        "    <h1>Caliper Reports Index</h1>",
        f"    <div class='status {status_class}'>",
        f"        <strong>Test Status:</strong> {html.escape(str(test_status))}<br>",
        f"        <strong>Final Status:</strong> {html.escape(str(final_status))}",
        "    </div>",
    ]

    for scope, source_groups in _group_entries(entries):
        html_parts.append(f"    <h2>{html.escape(_scope_display(scope))}</h2>")
        for source, reports in source_groups.items():
            html_parts.append(f"    <h3>{html.escape(_source_display(source))}</h3>")
            html_parts.append("    <ul>")
            for entry in reports:
                rel_path = _link_target(entry, output_dir)
                html_parts.append(
                    f"        <li class='report'><a href='{html.escape(rel_path)}'>"
                    f"{html.escape(entry.title)}</a> "
                    f"<span class='path'>({html.escape(rel_path)})</span></li>"
                )
            html_parts.append("    </ul>")

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html_parts.extend(
        [
            "    <hr>",
            f"    <p><small>Generated on {timestamp} by FORGE Caliper</small></p>",
            "</body>",
            "</html>",
        ]
    )

    return "\n".join(html_parts) + "\n"
