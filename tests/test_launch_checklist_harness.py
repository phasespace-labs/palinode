"""Regression guard: LAUNCH-CHECKLIST.md must contain the harness smoke section.

Catches accidental deletion of the release-blocking harness-smoke checkboxes
added in #346 (Phase 4 of #342).
"""
from __future__ import annotations

import re
from pathlib import Path

CHECKLIST = Path(__file__).resolve().parent.parent / "docs" / "LAUNCH-CHECKLIST.md"

# All 9 Tier 1+2 harness names that must appear as checkboxes.
TIER_1_2_HARNESSES = [
    "Claude Code",
    "Codex",
    "Generic IDE",
    "Cursor",
    "Claude Desktop",
    "Cline",
    "Zed",
    "Windsurf",
    "Continue",
]


def test_harness_smoke_section_exists() -> None:
    text = CHECKLIST.read_text()
    assert "Harness smoke" in text, (
        "LAUNCH-CHECKLIST.md is missing the 'Harness smoke' section"
    )


def test_all_tier1_tier2_checkboxes_present() -> None:
    text = CHECKLIST.read_text()
    checkbox_lines = [
        line for line in text.splitlines()
        if re.match(r"^- \[ \] .+\(Tier [12]\)", line)
    ]
    assert len(checkbox_lines) >= 9, (
        f"Expected at least 9 Tier 1+2 harness checkboxes, found {len(checkbox_lines)}"
    )
    for name in TIER_1_2_HARNESSES:
        assert any(name in line for line in checkbox_lines), (
            f"Missing checkbox for harness: {name}"
        )
