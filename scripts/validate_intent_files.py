#!/usr/bin/env python3
"""Validate *.intent.md files.

Ensures each intent file contains the required Markdown sections
(## Core Intent, ## Side Effects & State, ## Constraints, ## Verification Hooks).

Usage:
    scripts/validate_intent_files.py file1.intent.md [file2.intent.md ...]
"""

import re
import sys
from pathlib import Path

REQUIRED_SECTIONS = {
    "Public API Surface",
    "Core Intent",
    "Side Effects & State",
    "Constraints",
    "Verification Hooks",
}

SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def validate_intent_file(filepath: Path) -> list[str]:
    errors = []

    try:
        content = filepath.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"cannot read file: {exc}"]

    sections = SECTION_RE.findall(content)
    present = set(sections)

    dupes = [s for s in sections if sections.count(s) > 1]
    for section in sorted(set(dupes)):
        errors.append(f'duplicate section: "## {section}"')

    missing = REQUIRED_SECTIONS - present
    unexpected = present - REQUIRED_SECTIONS

    for section in sorted(missing):
        errors.append(f'missing required section: "## {section}"')

    for section in sorted(unexpected):
        errors.append(
            f'unexpected section: "## {section}" (allowed: {", ".join(sorted(REQUIRED_SECTIONS))})'
        )

    return errors


def main() -> int:
    if len(sys.argv) < 2:
        return 0

    failed = 0
    for arg in sys.argv[1:]:
        filepath = Path(arg)
        errors = validate_intent_file(filepath)
        for error in errors:
            print(f"{filepath}: {error}", file=sys.stderr)
        if errors:
            failed += 1

    if failed:
        print(
            f"\n{failed} of {len(sys.argv) - 1} intent file(s) failed validation",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
