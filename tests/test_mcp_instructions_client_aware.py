"""The MCP ``instructions`` must not tell a client to call a tool that will
refuse it.

``instructions`` is the first thing the server says to any client, and it opened
with "call palinode_session_init for project context" for *every* client — while
``auto_inject.harnesses_disabled`` defaults to ``["claude-code"]``, so Claude
Code's first tool call of every session was answered with "auto-inject is
suppressed for this client". One wasted round-trip per session, and the server
asked for it.

The handshake carries ``clientInfo.name`` — the same field the suppression
decision already reads — so the promise is now made only to clients that can
collect on it. Both protocol eras are exercised: the ``initialize`` handshake
(``mode="legacy"``) and the 2026-era per-request envelope, where ``initialize``
is gone and the same ``instructions`` field rides ``server/discover``
(``mode="auto"``). The wiring differs between them, so a test of only one proves
only half.
"""
from __future__ import annotations

from types import SimpleNamespace

import anyio
import pytest
from mcp.client.client import Client
from mcp.types import Implementation

from palinode import mcp as pmcp
from palinode.core.config import config


@pytest.fixture(autouse=True)
def default_auto_inject_policy(monkeypatch):
    """Pin the policy the assertions assume — a developer's own
    ``palinode.config.yaml`` is loaded into ``config`` at import time."""
    monkeypatch.setattr(config.auto_inject, "enabled", True)
    monkeypatch.setattr(config.auto_inject, "harnesses_disabled", ["claude-code"])


async def _instructions_seen_by(mode: str, client_name: str) -> str:
    """Connect a real client to the real server and report what it was told."""
    async with Client(
        server=pmcp.server,
        client_info=Implementation(name=client_name, version="1.0"),
        mode=mode,
    ) as client:
        return client.instructions or ""


async def _instructions_and_digest_reply(mode: str, client_name: str) -> tuple[str, str]:
    """As above, plus what ``palinode_session_init`` actually answers.

    The suppression branch returns before any HTTP call, so this needs no
    running API server.
    """
    async with Client(
        server=pmcp.server,
        client_info=Implementation(name=client_name, version="1.0"),
        mode=mode,
    ) as client:
        result = await client.call_tool("palinode_session_init", {})
        return client.instructions or "", result.content[0].text


# ── the invariant ────────────────────────────────────────────────────────────

def test_only_the_digest_variant_tells_the_agent_to_call_session_init():
    """Guards the wording, which is what the whole fix rests on."""
    assert "call palinode_session_init" in pmcp._SERVER_INSTRUCTIONS
    assert "call palinode_session_init" not in pmcp._SERVER_INSTRUCTIONS_NO_DIGEST
    # Both still point somewhere: an instruction that only says "no" is worse
    # than the round-trip it saves.
    assert "palinode_search" in pmcp._SERVER_INSTRUCTIONS_NO_DIGEST


def test_instructions_distinguish_explicit_capture_from_hooks_and_recap_only_sessions():
    """The handshake must not turn every connection into an unconditional write."""
    for text in (pmcp._SERVER_INSTRUCTIONS, pmcp._SERVER_INSTRUCTIONS_NO_DIGEST):
        assert (
            "When a value, ID, name, decision or plan is settled (for example a batch ID "
            "fixed or an approach agreed), save it right away with `palinode_save` as "
            '`type="Decision"`, with the value and why. Don\'t wait for the end of the session.'
        ) in text
        assert "content-bearing writes" in text
        assert "opt-in client hooks" in text
        assert "background auto-summary enrichment" in text
        assert "recall-only/no-new-information session" in text
        assert "explicit request not to save" in text


@pytest.mark.parametrize(
    "client_name,digest_promised",
    [
        ("claude-code", False),
        ("Claude-Code/2.1.0", False),  # substring match, case-insensitive
        ("claude-desktop", True),
        ("codex-cli", True),
        ("", True),  # unidentifiable clients are not suppressed
    ],
)
def test_instructions_track_the_suppression_policy(client_name, digest_promised):
    assert pmcp._digest_available_to(client_name) is digest_promised
    expected = (
        pmcp._SERVER_INSTRUCTIONS if digest_promised else pmcp._SERVER_INSTRUCTIONS_NO_DIGEST
    )
    assert pmcp._instructions_for_client(client_name) == expected


def test_the_master_switch_withdraws_the_promise_from_everyone(monkeypatch):
    """``auto_inject.enabled: false`` makes the tool refuse every client, so
    nobody should be told to call it."""
    monkeypatch.setattr(config.auto_inject, "enabled", False)
    assert pmcp._instructions_for_client("claude-desktop") == pmcp._SERVER_INSTRUCTIONS_NO_DIGEST


# ── the wiring, over the wire ────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_a_suppressed_harness_is_not_told_to_call_the_digest(mode):
    text, digest_reply = anyio.run(_instructions_and_digest_reply, mode, "claude-code")
    assert "suppressed for this client" in digest_reply, (
        "precondition: this client is supposed to be refused the digest"
    )
    assert text == pmcp._SERVER_INSTRUCTIONS_NO_DIGEST, (
        f"claude-code was told {text!r} — the server asked for a tool call it "
        "answers with a refusal"
    )


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_an_mcp_only_harness_still_gets_the_digest_sentence(mode):
    """The entry point MCP-only harnesses depend on must not regress."""
    text = anyio.run(_instructions_seen_by, mode, "claude-desktop")
    assert text == pmcp._SERVER_INSTRUCTIONS


# ── the shape on the wire ────────────────────────────────────────────────────

def test_instructions_are_never_added_to_a_result_that_had_none():
    """A server built with ``instructions_enabled: false`` stays silent: the
    rewrite swaps a present string, it does not introduce the field."""

    async def scenario():
        async def call_next(_ctx):
            return {"protocolVersion": "2025-11-25"}

        ctx = SimpleNamespace(method="initialize", params={}, session=None)
        return await pmcp._tailor_instructions(ctx, call_next)

    assert anyio.run(scenario) == {"protocolVersion": "2025-11-25"}
