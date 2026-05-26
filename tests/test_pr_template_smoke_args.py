"""Regression guard: PR template must mention TOOL_SMOKE_ARGS.

Catches accidental removal of the MCP-tool drift-guard reminder added in #346.
"""
from __future__ import annotations

from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / ".github" / "PULL_REQUEST_TEMPLATE.md"


def test_pr_template_contains_smoke_args_reminder() -> None:
    text = TEMPLATE.read_text()
    assert "TOOL_SMOKE_ARGS" in text, (
        "PR template is missing the TOOL_SMOKE_ARGS checkbox"
    )
