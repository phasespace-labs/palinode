"""
Tests for the /wrap push contract (#353, #378).

The canonical source of the /wrap command is the ``WRAP_COMMAND_BODY`` constant
in ``palinode.cli.init`` — it renders into whichever surface a consumer installs
(a personal/project skill via ``palinode init --skills``, or the legacy
``.claude/commands/wrap.md`` via ``--slash``). This repo no longer commits its
own ``.claude/commands/wrap.md`` copy (#378/#474, ADR-011 update), so these tests
assert the structural contract against the constant — no live git or MCP calls
needed, and no dependency on a checked-in command file that could drift.
"""
from __future__ import annotations

import pytest


def _wrap_body() -> str:
    from palinode.cli.init import WRAP_COMMAND_BODY
    return WRAP_COMMAND_BODY


# ── push prior work BEFORE archiving ───────────────────────────────────


def test_wrap_body_calls_palinode_push_before_session_end():
    """WRAP_COMMAND_BODY must call palinode_push before palinode_session_end (#353)."""
    body = _wrap_body()
    push_pos = body.find("palinode_push")
    session_end_pos = body.find("palinode_session_end")
    assert push_pos != -1, "wrap body must mention palinode_push (#353)"
    assert session_end_pos != -1, "wrap body must mention palinode_session_end"
    assert push_pos < session_end_pos, (
        "palinode_push must appear before palinode_session_end (#353) — "
        f"push at char {push_pos}, session_end at char {session_end_pos}"
    )


def test_wrap_body_handles_no_remote_gracefully():
    """WRAP_COMMAND_BODY must tell the agent to skip gracefully with no remote (#353)."""
    body = _wrap_body().lower()
    assert "no remote" in body or "no upstream" in body, (
        "wrap body must document graceful skip when no remote is configured (#353)"
    )


def test_wrap_body_surfaces_non_remote_push_failures():
    """WRAP_COMMAND_BODY must instruct the agent to surface push failures (#353)."""
    body = _wrap_body()
    has_error_surface = (
        "print the error" in body
        or "surface" in body
        or "ask the user" in body
        or "ask Alice" in body
        or "abort" in body
    )
    assert has_error_surface, (
        "wrap body must instruct the agent to surface push failures, not swallow them (#353)"
    )


# ── session_end ships its own note (push: true) ────────────────────────


def test_wrap_session_end_pushes_its_own_note():
    """session_end must be invoked with push: true so the note ships in one call.

    #353 added push-before-archive (sync prior commits). #378 originally added a
    SECOND trailing palinode_push because session_end committed the daily note
    WITHOUT pushing it (config.git.auto_push defaults to False), so a
    push-before-only flow left the wrap's own note unpushed — the final session
    before a gap never reached the remote.

    The trailing push was a forgettable third prose step. It is now folded into
    session_end via the `push` parameter (#378 follow-up): `session_end(push=true)`
    commits AND pushes the note in one call, making the note-ship a structural
    property of the tool rather than something the agent must remember. This test
    guards that the wrap body actually passes push: true.
    """
    body = _wrap_body()
    assert body.find("palinode_session_end") != -1, "wrap body must call palinode_session_end"
    lowered = body.lower()
    assert ("push: true" in lowered) or ("push=true" in lowered) or ('"push": true' in lowered), (
        "palinode_session_end must be invoked with push: true so the committed "
        "session note is shipped in the same call (#378)"
    )
