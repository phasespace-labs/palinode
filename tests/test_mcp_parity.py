"""MCP-surface parity for the ADR-015 write-semantics params (#479).

ADR-010 parity requires the MCP surface to expose — and forward to the API — the
same capabilities as the REST/CLI surfaces. The write-semantics params
(`update_policy` on save, `include_telemetry` on search) had no MCP-level test,
so a renamed/dropped MCP arg or a key-name typo in `mcp.py` would ship green
(the gap the swarm review flagged).

Two layers per param:
  - schema parity: `list_tools()` declares the param (catches a dropped/renamed
    schema field or a broken enum).
  - threading parity: `_dispatch_tool` forwards the param into the JSON body
    posted to the API (catches a typo'd body key or a missing forward).

The MCP layer is a thin async httpx wrapper over the REST API, so we mock
`mcp._post` to CAPTURE the forwarded body — no live server needed (mirrors the
timeout tests in test_mcp.py).
"""
from __future__ import annotations

from typing import Any

import pytest

import palinode.mcp as mcp
from palinode.mcp import _dispatch_tool, list_tools


class _FakeResp:
    """Minimal stand-in for an httpx.Response — only what the dispatch reads."""

    status_code = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture()
def captured_post(monkeypatch):
    """Replace ``mcp._post`` with a capture that records (path, body) and
    returns a benign 200. The dispatch reads response fields only via ``.get()``,
    so a minimal payload suffices for both /save and /search."""
    captured: dict[str, Any] = {}

    async def _fake_post(path, json=None, timeout=30.0):
        captured["path"] = path
        captured["body"] = json or {}
        payload = {"results": []} if path == "/search" else {"file_path": "/p/insights/x.md"}
        return _FakeResp(payload)

    monkeypatch.setattr(mcp, "_post", _fake_post)
    return captured


# ── schema parity: the params are declared on the tools ──────────────────────

@pytest.mark.asyncio
async def test_save_schema_declares_update_policy():
    tools = {t.name: t for t in await list_tools()}
    props = tools["palinode_save"].inputSchema["properties"]
    assert "update_policy" in props, "palinode_save schema dropped update_policy"
    assert set(props["update_policy"].get("enum", [])) >= {"append", "replace"}, (
        "update_policy enum must offer append + replace"
    )


@pytest.mark.asyncio
async def test_search_schema_declares_include_telemetry():
    tools = {t.name: t for t in await list_tools()}
    props = tools["palinode_search"].inputSchema["properties"]
    assert "include_telemetry" in props, "palinode_search schema dropped include_telemetry"
    assert props["include_telemetry"].get("type") == "boolean"


# ── threading parity: the params reach the API body ──────────────────────────

@pytest.mark.asyncio
async def test_save_forwards_update_policy_to_api(captured_post):
    await _dispatch_tool("palinode_save", {
        "content": "living infra state",
        "type": "ActionItem",
        "slug": "infra",
        "update_policy": "replace",
    })
    assert captured_post["path"] == "/save"
    assert captured_post["body"].get("update_policy") == "replace"


@pytest.mark.asyncio
async def test_save_omits_update_policy_when_absent(captured_post):
    await _dispatch_tool("palinode_save", {
        "content": "episodic note",
        "type": "Insight",
    })
    assert "update_policy" not in captured_post["body"]


@pytest.mark.asyncio
async def test_search_forwards_include_telemetry_to_api(captured_post):
    await _dispatch_tool("palinode_search", {
        "query": "uptime",
        "include_telemetry": True,
    })
    assert captured_post["path"] == "/search"
    assert captured_post["body"].get("include_telemetry") is True


@pytest.mark.asyncio
async def test_search_omits_include_telemetry_by_default(captured_post):
    await _dispatch_tool("palinode_search", {"query": "uptime"})
    assert "include_telemetry" not in captured_post["body"]
