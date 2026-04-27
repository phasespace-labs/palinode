"""
Tests for palinode_doctor and palinode_doctor_deep MCP tools.

Covers:
  - Both tools are registered in list_tools()
  - palinode_doctor dispatches GET /doctor?fast=true
  - palinode_doctor_deep dispatches GET /doctor?canary=true
  - Both return JSON-serializable results (valid JSON text content)
  - Both handle API errors gracefully

Uses monkeypatching of the internal _get helper (async) so no live API server
is required.  The tool list assertions test the MCP registry directly.
"""
from __future__ import annotations

import json
from typing import Any
from unittest import mock

import httpx
import pytest

import palinode.mcp as mcp_module
from palinode.mcp import _dispatch_tool, list_tools


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_doctor_response(
    *,
    status_code: int = 200,
    results: list[dict] | None = None,
    fast: bool = False,
    canary: bool = False,
) -> mock.AsyncMock:
    """Build an async mock that behaves like a successful httpx.Response."""
    if results is None:
        results = [
            {
                "name": "memory_dir_exists",
                "severity": "critical",
                "passed": True,
                "message": "Memory directory exists",
                "remediation": None,
            }
        ]
    payload = {
        "results": results,
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r["passed"]),
            "failed": sum(1 for r in results if not r["passed"]),
        },
        "params": {"fast": fast, "canary": canary},
    }
    resp = mock.MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.text = json.dumps(payload)
    return resp


def _fake_error_response(status_code: int = 500, text: str = "internal error") -> mock.MagicMock:
    resp = mock.MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    return resp


async def _run_tool(name: str, arguments: dict[str, Any] | None = None) -> str:
    """Run a single tool through _dispatch_tool and return the first text item."""
    result = await _dispatch_tool(name, arguments or {})
    assert result, "dispatch returned empty list"
    return result[0].text


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_palinode_doctor_registered() -> None:
    """palinode_doctor must appear in list_tools()."""
    tools = await list_tools()
    names = {t.name for t in tools}
    assert "palinode_doctor" in names, (
        f"palinode_doctor not in tool list; found: {sorted(names)}"
    )


@pytest.mark.asyncio
async def test_palinode_doctor_deep_registered() -> None:
    """palinode_doctor_deep must appear in list_tools()."""
    tools = await list_tools()
    names = {t.name for t in tools}
    assert "palinode_doctor_deep" in names, (
        f"palinode_doctor_deep not in tool list; found: {sorted(names)}"
    )


@pytest.mark.asyncio
async def test_both_doctor_tools_have_descriptions() -> None:
    """Both tools must have non-empty descriptions."""
    tools = await list_tools()
    by_name = {t.name: t for t in tools}
    for tool_name in ("palinode_doctor", "palinode_doctor_deep"):
        assert tool_name in by_name
        assert by_name[tool_name].description, f"{tool_name} has empty description"


# ---------------------------------------------------------------------------
# palinode_doctor dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_palinode_doctor_calls_fast_endpoint(monkeypatch) -> None:
    """palinode_doctor must hit /doctor with fast=true."""
    captured_params: list[dict] = []

    async def fake_get(path: str, params: dict | None = None, timeout: float = 30.0):
        captured_params.append({"path": path, "params": params or {}})
        return _fake_doctor_response(fast=True)

    monkeypatch.setattr(mcp_module, "_get", fake_get)
    await _run_tool("palinode_doctor")

    assert len(captured_params) == 1
    assert captured_params[0]["path"] == "/doctor"
    assert captured_params[0]["params"].get("fast") == "true"


@pytest.mark.asyncio
async def test_palinode_doctor_returns_json_serializable(monkeypatch) -> None:
    """palinode_doctor must return valid JSON text."""
    async def fake_get(path, params=None, timeout=30.0):
        return _fake_doctor_response(fast=True)

    monkeypatch.setattr(mcp_module, "_get", fake_get)
    text = await _run_tool("palinode_doctor")

    parsed = json.loads(text)
    assert "results" in parsed
    assert "summary" in parsed


@pytest.mark.asyncio
async def test_palinode_doctor_result_shape(monkeypatch) -> None:
    """The returned JSON must have results + summary with correct counts."""
    async def fake_get(path, params=None, timeout=30.0):
        return _fake_doctor_response(fast=True)

    monkeypatch.setattr(mcp_module, "_get", fake_get)
    text = await _run_tool("palinode_doctor")

    parsed = json.loads(text)
    summary = parsed["summary"]
    assert "total" in summary
    assert "passed" in summary
    assert "failed" in summary
    assert summary["total"] == len(parsed["results"])


@pytest.mark.asyncio
async def test_palinode_doctor_api_error_returns_error_text(monkeypatch) -> None:
    """On API error, palinode_doctor must return an error string."""
    async def fake_get(path, params=None, timeout=30.0):
        return _fake_error_response(status_code=500, text="boom")

    monkeypatch.setattr(mcp_module, "_get", fake_get)
    text = await _run_tool("palinode_doctor")

    assert "Doctor failed" in text or "boom" in text


# ---------------------------------------------------------------------------
# palinode_doctor_deep dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_palinode_doctor_deep_calls_canary_endpoint(monkeypatch) -> None:
    """palinode_doctor_deep must hit /doctor with canary=true."""
    captured_params: list[dict] = []

    async def fake_get(path: str, params: dict | None = None, timeout: float = 30.0):
        captured_params.append({"path": path, "params": params or {}})
        return _fake_doctor_response(canary=True)

    monkeypatch.setattr(mcp_module, "_get", fake_get)
    await _run_tool("palinode_doctor_deep")

    assert len(captured_params) == 1
    assert captured_params[0]["path"] == "/doctor"
    assert captured_params[0]["params"].get("canary") == "true"


@pytest.mark.asyncio
async def test_palinode_doctor_deep_returns_json_serializable(monkeypatch) -> None:
    """palinode_doctor_deep must return valid JSON text."""
    async def fake_get(path, params=None, timeout=30.0):
        return _fake_doctor_response(canary=True)

    monkeypatch.setattr(mcp_module, "_get", fake_get)
    text = await _run_tool("palinode_doctor_deep")

    parsed = json.loads(text)
    assert "results" in parsed
    assert "summary" in parsed


@pytest.mark.asyncio
async def test_palinode_doctor_deep_result_shape(monkeypatch) -> None:
    """The returned JSON must have results + summary with correct counts."""
    async def fake_get(path, params=None, timeout=30.0):
        return _fake_doctor_response(canary=True)

    monkeypatch.setattr(mcp_module, "_get", fake_get)
    text = await _run_tool("palinode_doctor_deep")

    parsed = json.loads(text)
    summary = parsed["summary"]
    assert "total" in summary
    assert "passed" in summary
    assert "failed" in summary


@pytest.mark.asyncio
async def test_palinode_doctor_deep_api_error_returns_error_text(monkeypatch) -> None:
    """On API error, palinode_doctor_deep must return an error string."""
    async def fake_get(path, params=None, timeout=30.0):
        return _fake_error_response(status_code=503, text="unavailable")

    monkeypatch.setattr(mcp_module, "_get", fake_get)
    text = await _run_tool("palinode_doctor_deep")

    assert "Doctor" in text or "unavailable" in text


# ---------------------------------------------------------------------------
# Timeout values
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_palinode_doctor_uses_short_timeout(monkeypatch) -> None:
    """palinode_doctor must use a timeout <= 15s (fast target)."""
    captured_timeouts: list[float] = []

    async def fake_get(path, params=None, timeout=30.0):
        captured_timeouts.append(timeout)
        return _fake_doctor_response(fast=True)

    monkeypatch.setattr(mcp_module, "_get", fake_get)
    await _run_tool("palinode_doctor")

    assert captured_timeouts[0] <= 15.0, (
        f"palinode_doctor timeout too high: {captured_timeouts[0]}s"
    )


@pytest.mark.asyncio
async def test_palinode_doctor_deep_uses_longer_timeout(monkeypatch) -> None:
    """palinode_doctor_deep must use a timeout >= 30s (deep checks can take 10-15s)."""
    captured_timeouts: list[float] = []

    async def fake_get(path, params=None, timeout=30.0):
        captured_timeouts.append(timeout)
        return _fake_doctor_response(canary=True)

    monkeypatch.setattr(mcp_module, "_get", fake_get)
    await _run_tool("palinode_doctor_deep")

    assert captured_timeouts[0] >= 30.0, (
        f"palinode_doctor_deep timeout too short: {captured_timeouts[0]}s"
    )
