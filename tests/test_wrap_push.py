"""
Tests for /wrap push-before-archive behavior (#353).

The /wrap command must call palinode_push before palinode_session_end so that
local commits are on the remote before the session is archived.  These tests
assert the structural contract — no live git or MCP calls needed.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent


def _wrap_md_content() -> str:
    path = REPO_ROOT / ".claude" / "commands" / "wrap.md"
    if not path.exists():
        pytest.skip("dev-only .claude wrap command is not shipped in the public repo")
    return path.read_text()


def _wrap_command_body_from_init() -> str:
    from palinode.cli.init import WRAP_COMMAND_BODY
    return WRAP_COMMAND_BODY


# ── Installed wrap.md ────────────────────────────────────────────────────────


def test_wrap_md_calls_palinode_push_before_session_end():
    """wrap.md must instruct the agent to call palinode_push before palinode_session_end."""
    content = _wrap_md_content()
    push_pos = content.find("palinode_push")
    session_end_pos = content.find("palinode_session_end")
    assert push_pos != -1, "wrap.md must mention palinode_push (#353)"
    assert session_end_pos != -1, "wrap.md must mention palinode_session_end"
    assert push_pos < session_end_pos, (
        "palinode_push must appear before palinode_session_end in wrap.md (#353) — "
        f"push at char {push_pos}, session_end at char {session_end_pos}"
    )


def test_wrap_md_handles_no_remote_gracefully():
    """wrap.md must tell the agent to skip gracefully when there is no remote."""
    content = _wrap_md_content()
    # Must mention the no-remote case explicitly
    assert "no remote" in content.lower() or "no upstream" in content.lower(), (
        "wrap.md must document graceful skip when no remote is configured (#353)"
    )


def test_wrap_md_surfaces_non_remote_push_failures():
    """wrap.md must tell the agent to surface push failures, not silently swallow them."""
    content = _wrap_md_content()
    # Must mention surfacing errors to Paul
    has_error_surface = (
        "print the error" in content
        or "surface" in content
        or "ask Paul" in content
        or "abort" in content
    )
    assert has_error_surface, (
        "wrap.md must instruct the agent to surface push failures to Paul (#353)"
    )


# ── init.py WRAP_COMMAND_BODY mirror ─────────────────────────────────────────


def test_init_py_wrap_body_calls_push_before_session_end():
    """init.py WRAP_COMMAND_BODY must mirror wrap.md: push before session-end."""
    body = _wrap_command_body_from_init()
    push_pos = body.find("palinode_push")
    session_end_pos = body.find("palinode_session_end")
    assert push_pos != -1, "init.py WRAP_COMMAND_BODY must mention palinode_push (#353)"
    assert session_end_pos != -1, "init.py WRAP_COMMAND_BODY must mention palinode_session_end"
    assert push_pos < session_end_pos, (
        "palinode_push must appear before palinode_session_end in WRAP_COMMAND_BODY (#353)"
    )


def test_wrap_md_and_init_py_body_are_consistent():
    """Installed wrap.md and init.py WRAP_COMMAND_BODY must have the same push semantics.

    They don't have to be byte-for-byte identical (the installed file gets
    variable substitution when init runs) but both must include palinode_push
    in the same relative order with respect to palinode_session_end.
    """
    installed = _wrap_md_content()
    scaffold = _wrap_command_body_from_init()

    # Both must mention push before session-end
    for content, label in [(installed, "wrap.md"), (scaffold, "WRAP_COMMAND_BODY")]:
        pp = content.find("palinode_push")
        sp = content.find("palinode_session_end")
        assert pp != -1 and sp != -1 and pp < sp, (
            f"{label}: palinode_push must precede palinode_session_end (#353)"
        )
