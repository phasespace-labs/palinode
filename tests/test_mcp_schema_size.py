"""MCP schema semantics guard and token-surface measurement."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from palinode.mcp import CORE_TOOL_NAMES, MCP_SEARCH_LIMIT_MAX, list_tools


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "mcp_schema_semantics.json"


def _param_semantics(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    props = schema.get("properties", {}) or {}
    projected: dict[str, dict[str, Any]] = {}
    for name in sorted(props):
        prop = props[name]
        param: dict[str, Any] = {}
        if "enum" in prop:
            param["enum"] = prop["enum"]
        items = prop.get("items")
        if isinstance(items, dict) and "enum" in items:
            param["items_enum"] = items["enum"]
        projected[name] = param
    return projected


async def _schema_semantics() -> dict[str, dict[str, Any]]:
    tools = await list_tools()
    return {
        tool.name: {
            "description": tool.description,
            "required": tool.input_schema.get("required", []),
            "params": _param_semantics(tool.input_schema),
        }
        for tool in sorted(tools, key=lambda item: item.name)
    }


@pytest.mark.asyncio
async def test_search_limit_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """``palinode_search.limit`` must carry a ``maximum``.

    ``full=true`` caps each result body at ``_FULL_CONTENT_HARD_CAP`` but has no
    aggregate ceiling, so ``limit`` is what decides how large a single tool
    result can get. Unbounded, one call can return ~200 KB — and tool results
    are never prompt-cacheable on first appearance and stay resident in
    ``messages`` for the rest of the session.

    Capping the *count* is deliberate and is not truncation: no memory is cut
    mid-body, so a returned memory is always whole. An aggregate output cap
    would truncate, and a truncated memory is indistinguishable from a complete
    one at the point of use — the failure mode
    ``tests/test_mcp_schema_size_budget.py`` exists to prevent.
    """
    monkeypatch.setenv("PALINODE_MCP_SURFACE", "full")
    tools = {tool.name: tool for tool in await list_tools()}
    limit = tools["palinode_search"].input_schema["properties"]["limit"]

    assert "maximum" in limit, (
        "palinode_search.limit has no `maximum`. With full=true capped per "
        "result but not in aggregate, an unbounded limit makes one call's "
        "context cost unbounded too."
    )
    assert limit["maximum"] == MCP_SEARCH_LIMIT_MAX
    # ADR-022: the server owns the default, so the schema advertises none. An
    # omitted limit gets the server's, which out of the box must fit the cap.
    assert "default" not in limit
    from palinode.core.config import SearchConfig

    out_of_box = SearchConfig().default_limit
    assert out_of_box <= limit["maximum"], (
        f"the server's default limit ({out_of_box}) exceeds MCP's maximum "
        f"({limit['maximum']}): an omitted limit would return more than the cap."
    )


@pytest.mark.asyncio
async def test_mcp_schema_semantics_match_golden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PALINODE_MCP_SURFACE", "full")
    expected = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert await _schema_semantics() == expected


@pytest.mark.asyncio
async def test_mcp_schema_token_estimate_report(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PALINODE_MCP_SURFACE", "full")
    tools = await list_tools()
    estimates = [
        (tool.name, len(json.dumps(tool.input_schema, sort_keys=True)) / 4)
        for tool in sorted(tools, key=lambda item: item.name)
    ]
    total = sum(estimate for _, estimate in estimates)

    with capsys.disabled():
        print("\nMCP inputSchema estimated tokens (chars/4):")
        for name, estimate in estimates:
            print(f"{name}: {estimate:.1f}")
        print(f"TOTAL: {total:.1f}")


def _schema_tokens(tools: list[Any]) -> float:
    return sum(len(tool.model_dump_json()) / 4 for tool in tools)


@pytest.mark.asyncio
async def test_mcp_core_surface_is_strict_slim_subset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_core_names = {
        "palinode_session_init",
        "palinode_save",
        "palinode_search",
        "palinode_read",
        "palinode_session_end",
        "palinode_status",
        "palinode_push",
        "palinode_list",
        "palinode_entities",
        "palinode_trigger",
        "palinode_ingest",
        "palinode_doctor",
    }
    assert CORE_TOOL_NAMES == expected_core_names

    monkeypatch.setenv("PALINODE_MCP_SURFACE", "full")
    full_tools = await list_tools()
    full_names = {tool.name for tool in full_tools}

    monkeypatch.setenv("PALINODE_MCP_SURFACE", "core")
    core_tools = await list_tools()
    core_names = {tool.name for tool in core_tools}

    assert core_names == expected_core_names
    assert core_names < full_names
    assert _schema_tokens(core_tools) <= 0.55 * _schema_tokens(full_tools)
