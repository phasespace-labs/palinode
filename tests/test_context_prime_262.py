"""#262 (ADR-012 Layer 4) — session-start context digest across surfaces.

Covers the core digest builder (scope resolution precedence, project
filtering with no cross-project bleed, bounded output, no-project
degradation), the REST /context/prime endpoint (including the exact call
shape the shipped SessionStart hook POSTs), the MCP session-init tool
(schema, threading, harness suppression policy, initialize instructions),
and the CLI prime command.

Real files + tmp_path; no DB, no embeds — the digest is frontmatter-only by
design.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

from palinode.api.server import app
from palinode.core.config import config
from palinode.core.context_prime import (
    MAX_CORE_MEMORIES,
    MAX_RECENT_DECISIONS,
    MAX_RECENT_SNAPSHOTS,
    build_context_digest,
    format_context_digest,
    resolve_project,
)

client = TestClient(app)


@pytest.fixture
def mock_memory_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    yield tmp_path


def _seed(memory_dir, rel, meta, body="body"):
    p = memory_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\n{yaml.safe_dump(meta, default_flow_style=False)}---\n\n{body}\n",
        encoding="utf-8",
    )
    return p


# ── scope resolution ─────────────────────────────────────────────────────────


def test_resolve_project_explicit_wins(monkeypatch):
    assert resolve_project(cwd="/x/other", project="palinode") == "project/palinode"
    assert resolve_project(project="org/custom") == "org/custom"


def test_resolve_project_cwd_map_then_autodetect(monkeypatch):
    monkeypatch.setattr(config.context, "project_map", {"mydir": "mapped-proj"})
    monkeypatch.setattr(config.context, "auto_detect", True)
    assert resolve_project(cwd="/home/u/mydir") == "project/mapped-proj"
    assert resolve_project(cwd="/home/u/otherdir") == "project/otherdir"


def test_resolve_project_never_guesses(monkeypatch):
    monkeypatch.setattr(config.context, "auto_detect", False)
    assert resolve_project(cwd="/home/u/somedir") is None
    assert resolve_project() is None


# ── digest builder ───────────────────────────────────────────────────────────


def test_digest_core_only_when_no_project(mock_memory_dir, monkeypatch):
    monkeypatch.setattr(config.context, "auto_detect", False)
    _seed(mock_memory_dir, "insights/core-one.md",
          {"type": "Insight", "core": True, "title": "Core one"})
    _seed(mock_memory_dir, "decisions/proj-dec.md",
          {"type": "Decision", "entities": ["project/alpha"], "title": "Alpha dec"})
    digest = build_context_digest()
    assert digest["project"] is None
    assert [r["file"] for r in digest["core_memories"]] == ["insights/core-one.md"]
    assert digest["recent_decisions"] == []
    assert digest["open_action_items"] == []
    assert digest["recent_snapshots"] == []
    assert "_palinode_hint" in digest


def test_digest_project_scoped_no_bleed(mock_memory_dir):
    """Two projects in one store — only the resolved project's rows appear."""
    _seed(mock_memory_dir, "decisions/alpha-dec.md",
          {"type": "Decision", "entities": ["project/alpha"], "title": "Alpha decision"})
    _seed(mock_memory_dir, "decisions/beta-dec.md",
          {"type": "Decision", "entities": ["project/beta"], "title": "Beta decision"})
    _seed(mock_memory_dir, "inbox/alpha-todo.md",
          {"type": "ActionItem", "entities": ["project/alpha"], "title": "Alpha todo"})
    _seed(mock_memory_dir, "inbox/beta-todo.md",
          {"type": "ActionItem", "entities": ["project/beta"], "title": "Beta todo"})
    _seed(mock_memory_dir, "projects/alpha-snap.md",
          {"type": "ProjectSnapshot", "entities": ["project/alpha"], "title": "Alpha snap"})
    _seed(mock_memory_dir, "projects/beta-snap.md",
          {"type": "ProjectSnapshot", "entities": ["project/beta"], "title": "Beta snap"})
    digest = build_context_digest(project="alpha")
    assert digest["project"] == "project/alpha"
    assert [r["file"] for r in digest["recent_decisions"]] == ["decisions/alpha-dec.md"]
    assert [r["file"] for r in digest["open_action_items"]] == ["inbox/alpha-todo.md"]
    assert [r["file"] for r in digest["recent_snapshots"]] == ["projects/alpha-snap.md"]


def test_digest_excludes_closed_action_items(mock_memory_dir):
    _seed(mock_memory_dir, "inbox/open.md",
          {"type": "ActionItem", "entities": ["project/p"], "title": "Open"})
    _seed(mock_memory_dir, "inbox/done.md",
          {"type": "ActionItem", "entities": ["project/p"], "status": "done", "title": "Done"})
    _seed(mock_memory_dir, "inbox/archived.md",
          {"type": "ActionItem", "entities": ["project/p"], "status": "archived", "title": "Old"})
    digest = build_context_digest(project="p")
    assert [r["file"] for r in digest["open_action_items"]] == ["inbox/open.md"]


def test_digest_is_bounded(mock_memory_dir):
    for i in range(MAX_CORE_MEMORIES + 5):
        _seed(mock_memory_dir, f"insights/core-{i:02d}.md",
              {"type": "Insight", "core": True, "title": f"Core {i}"})
    for i in range(MAX_RECENT_DECISIONS + 5):
        _seed(mock_memory_dir, f"decisions/dec-{i:02d}.md",
              {"type": "Decision", "entities": ["project/p"], "title": f"Dec {i}"})
    digest = build_context_digest(project="p")
    assert len(digest["core_memories"]) == MAX_CORE_MEMORIES
    assert len(digest["recent_decisions"]) == MAX_RECENT_DECISIONS


def test_digest_surfaces_snapshots_newest_first(mock_memory_dir):
    """ProjectSnapshots for the resolved project surface, most-recent first."""
    old = _seed(mock_memory_dir, "projects/session-end-old.md",
                {"type": "ProjectSnapshot", "entities": ["project/p"], "title": "Old wrap"})
    new = _seed(mock_memory_dir, "projects/session-end-new.md",
                {"type": "ProjectSnapshot", "entities": ["project/p"], "title": "New wrap"})
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    digest = build_context_digest(project="p")
    assert [r["file"] for r in digest["recent_snapshots"]] == [
        "projects/session-end-new.md",
        "projects/session-end-old.md",
    ]


def test_digest_snapshots_bounded(mock_memory_dir):
    n = MAX_RECENT_SNAPSHOTS + 2
    for i in range(n):
        p = _seed(mock_memory_dir, f"projects/snap-{i:02d}.md",
                  {"type": "ProjectSnapshot", "entities": ["project/p"], "title": f"Snap {i}"})
        os.utime(p, (1000 + i, 1000 + i))
    digest = build_context_digest(project="p")
    # Bounded to the cap, and it keeps the newest ones in newest-first order.
    assert [r["file"] for r in digest["recent_snapshots"]] == [
        f"projects/snap-{i:02d}.md"
        for i in range(n - 1, n - 1 - MAX_RECENT_SNAPSHOTS, -1)
    ]


def test_digest_snapshots_empty_when_none(mock_memory_dir):
    """A resolved project with decisions but no snapshots yields an empty section."""
    _seed(mock_memory_dir, "decisions/d.md",
          {"type": "Decision", "entities": ["project/p"], "title": "D"})
    digest = build_context_digest(project="p")
    assert digest["recent_snapshots"] == []


def test_digest_snapshots_ignores_status_log(mock_memory_dir):
    """The append-only projects/<p>-status.md log is not a ProjectSnapshot and
    must not surface (selection is by type, not by the projects/ path)."""
    _seed(mock_memory_dir, "projects/p-status.md",
          {"entities": ["project/p"], "title": "p status log"})
    digest = build_context_digest(project="p")
    assert digest["recent_snapshots"] == []


def test_digest_skips_daily_and_archive(mock_memory_dir, monkeypatch):
    monkeypatch.setattr(config.context, "auto_detect", False)
    _seed(mock_memory_dir, "daily/2026-07-18.md", {"core": True, "title": "Daily"})
    _seed(mock_memory_dir, "archive/old.md", {"core": True, "title": "Old"})
    digest = build_context_digest()
    assert digest["core_memories"] == []


def test_format_digest_renders_sections_and_hint(mock_memory_dir):
    _seed(mock_memory_dir, "decisions/d.md",
          {"type": "Decision", "entities": ["project/p"], "title": "The decision",
           "description": "why it was made"})
    text = format_context_digest(build_context_digest(project="p"))
    assert "Session context: project/p" in text
    assert "The decision — why it was made" in text
    assert "palinode_search" in text  # the hint


def test_format_digest_renders_snapshot_section(mock_memory_dir):
    _seed(mock_memory_dir, "projects/wrap.md",
          {"type": "ProjectSnapshot", "entities": ["project/p"],
           "title": "Where I left off", "description": "wired the renderer"})
    text = format_context_digest(build_context_digest(project="p"))
    assert "### Recent snapshots" in text
    assert "Where I left off — wired the renderer" in text
    assert "[projects/wrap.md]" in text


def test_format_digest_no_project_label(monkeypatch, mock_memory_dir):
    monkeypatch.setattr(config.context, "auto_detect", False)
    text = format_context_digest(build_context_digest())
    assert "no project resolved" in text


# ── REST endpoint ────────────────────────────────────────────────────────────


def test_context_prime_endpoint_hook_shape(mock_memory_dir):
    """The exact body the shipped SessionStart hook POSTs must succeed."""
    res = client.post(
        "/context/prime",
        json={"cwd": "/home/u/someproj", "session_id": "abc-123"},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["project"] == "project/someproj"
    assert "_palinode_hint" in data


def test_context_prime_endpoint_empty_body(mock_memory_dir, monkeypatch):
    monkeypatch.setattr(config.context, "auto_detect", False)
    res = client.post("/context/prime", json={})
    assert res.status_code == 200, res.text
    assert res.json()["project"] is None


# ── MCP surface ──────────────────────────────────────────────────────────────


class _FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_mcp_schema_declares_session_init():
    from palinode.mcp import list_tools

    tools = {t.name: t for t in await list_tools()}
    assert "palinode_session_init" in tools
    props = tools["palinode_session_init"].inputSchema["properties"]
    assert "cwd" in props and "project" in props


@pytest.mark.asyncio
async def test_mcp_session_init_threads_to_api(monkeypatch):
    import palinode.mcp as mcp

    captured = {}

    async def _fake_post(path, json=None, timeout=30.0):
        captured["path"] = path
        captured["body"] = json or {}
        return _FakeResp({"project": "project/p", "core_memories": [],
                          "recent_decisions": [], "open_action_items": [],
                          "_palinode_hint": "hint"})

    monkeypatch.setattr(mcp, "_post", _fake_post)
    result = await mcp._dispatch_tool(
        "palinode_session_init", {"cwd": "/x/proj"}
    )
    assert captured["path"] == "/context/prime"
    assert captured["body"] == {"cwd": "/x/proj"}
    assert "Session context: project/p" in result[0].text


@pytest.mark.asyncio
async def test_mcp_session_init_defaults_cwd(monkeypatch):
    import os as _os

    import palinode.mcp as mcp

    captured = {}

    async def _fake_post(path, json=None, timeout=30.0):
        captured["body"] = json or {}
        return _FakeResp({"project": None, "core_memories": [],
                          "recent_decisions": [], "open_action_items": [],
                          "_palinode_hint": "hint"})

    monkeypatch.setattr(mcp, "_post", _fake_post)
    await mcp._dispatch_tool("palinode_session_init", {})
    assert captured["body"] == {"cwd": _os.getcwd()}


@pytest.mark.asyncio
async def test_mcp_session_init_respects_master_switch(monkeypatch):
    import palinode.mcp as mcp

    monkeypatch.setattr(config.auto_inject, "enabled", False)
    result = await mcp._dispatch_tool("palinode_session_init", {})
    assert "disabled" in result[0].text


def test_harness_suppression_policy(monkeypatch):
    from palinode.mcp import _auto_inject_suppressed_for

    monkeypatch.setattr(config.auto_inject, "harnesses_disabled", ["claude-code"])
    assert _auto_inject_suppressed_for("claude-code") is True
    assert _auto_inject_suppressed_for("Claude-Code CLI") is True
    assert _auto_inject_suppressed_for("claude-desktop") is False
    assert _auto_inject_suppressed_for("codex") is False
    # unidentifiable client is NOT suppressed (explicit invocation, not push)
    assert _auto_inject_suppressed_for("") is False


def test_initialize_instructions_present():
    """The MCP initialize response carries the content-free memory contract."""
    from palinode.mcp import server

    assert server.instructions
    assert "palinode_session_init" in server.instructions
    assert "palinode_search" in server.instructions


# ── CLI surface ──────────────────────────────────────────────────────────────


def test_cli_prime_renders_digest():
    import importlib

    from click.testing import CliRunner

    from palinode.cli import _api

    prime_mod = importlib.import_module("palinode.cli.prime")

    class _Client:
        def post(self, path, json=None, timeout=None):
            class _R:
                status_code = 200

                def raise_for_status(self):
                    pass

                def json(self):
                    return {"project": "project/p", "core_memories": [
                        {"file": "insights/x.md", "summary": "X"}],
                        "recent_decisions": [], "open_action_items": [],
                        "_palinode_hint": "hint"}

            return _R()

    fake = _api.PalinodeAPI.__new__(_api.PalinodeAPI)
    fake.client = _Client()
    with patch.object(prime_mod, "api_client", fake):
        result = CliRunner().invoke(prime_mod.prime, ["--format", "text"])
    assert result.exit_code == 0, result.output
    assert "Session context: project/p" in result.output
    assert "insights/x.md" in result.output
