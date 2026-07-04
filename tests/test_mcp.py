"""Tests for the MCP layer (palinode/mcp.py).

Pure-function helpers that the dispatcher delegates to. These tests don't
exercise the async tool dispatch — they cover only the logic the dispatcher
calls into, which is what changes most often and is easiest to regress.

The timeout-message tests (#416) also drive the async dispatcher directly with
a mocked slow server, since the verify-before-retry contract lives in the
dispatcher's except block, not in a pure helper alone.
"""
import httpx
import pytest

import palinode.mcp as mcp
from palinode.mcp import _coerce_str_array, _dispatch_tool, _resolve_save_type, _timeout_message


# _coerce_str_array (— JSON-encoded array args from MCP clients) ----


def test_coerce_str_array_decodes_json_array_string():
    assert _coerce_str_array('["a", "b"]') == ["a", "b"]


def test_coerce_str_array_passes_native_list_through():
    assert _coerce_str_array(["a", "b"]) == ["a", "b"]


def test_coerce_str_array_returns_none_unchanged():
    assert _coerce_str_array(None) is None


def test_coerce_str_array_returns_non_array_json_unchanged():
    # A JSON object string is not an array — leave it for downstream validation.
    assert _coerce_str_array('{"a": 1}') == '{"a": 1}'


def test_coerce_str_array_returns_invalid_json_unchanged():
    assert _coerce_str_array("not json at all") == "not json at all"


def test_coerce_str_array_handles_empty_array_string():
    assert _coerce_str_array("[]") == []


def test_coerce_str_array_preserves_inner_types():
    # Decoder preserves whatever JSON yields; validation downstream catches mismatches.
    assert _coerce_str_array("[1, 2, 3]") == [1, 2, 3]


# _resolve_save_type (— palinode_save type / ps=true shortcut) ----


def test_resolve_save_type_explicit_type():
    assert _resolve_save_type("Decision", None) == "Decision"
    assert _resolve_save_type("ProjectSnapshot", None) == "ProjectSnapshot"
    assert _resolve_save_type("Insight", False) == "Insight"


def test_resolve_save_type_ps_shortcut_only():
    assert _resolve_save_type(None, True) == "ProjectSnapshot"


def test_resolve_save_type_ps_with_redundant_matching_type():
    # ps=true + type=ProjectSnapshot is redundant but explicitly OK
    assert _resolve_save_type("ProjectSnapshot", True) == "ProjectSnapshot"


def test_resolve_save_type_ps_conflict_with_other_type():
    with pytest.raises(ValueError, match="conflicts"):
        _resolve_save_type("Decision", True)
    with pytest.raises(ValueError, match="conflicts"):
        _resolve_save_type("Insight", True)


def test_resolve_save_type_neither_specified():
    with pytest.raises(ValueError, match="must specify"):
        _resolve_save_type(None, None)
    with pytest.raises(ValueError, match="must specify"):
        _resolve_save_type(None, False)
    with pytest.raises(ValueError, match="must specify"):
        _resolve_save_type("", False)


def test_resolve_save_type_falsy_ps_treated_as_unset():
    # ps=False with a real type should pass the type through
    assert _resolve_save_type("Decision", False) == "Decision"


# _timeout_message (— verify-before-retry hint on write-path timeout) ----


def test_timeout_message_save_warns_verify_before_retry():
    msg = _timeout_message("palinode_save")
    assert "palinode_save" in msg
    assert "may have succeeded server-side" in msg
    # The actionable hint: search before retrying so you don't duplicate.
    assert "palinode_search" in msg
    assert "duplicate" in msg
    # Audit classifies write-path timeouts as errors via this prefix (mcp.py).
    assert msg.startswith("Timeout:")


def test_timeout_message_session_end_is_write_path():
    msg = _timeout_message("palinode_session_end")
    assert "palinode_session_end" in msg
    assert "palinode_search" in msg
    assert msg.startswith("Timeout:")


def test_timeout_message_read_path_keeps_plain_message():
    # Read-path tools shouldn't tell the model to dedup-check — nothing was written.
    msg = _timeout_message("palinode_search")
    assert "timed out" in msg
    assert "duplicate" not in msg
    assert "palinode_search" not in msg.replace("Error:", "")  # no self-referential hint


# async dispatcher: slow server surfaces the right message ----


async def _raise_timeout(*args, **kwargs):
    """Stand-in for a server that never answers before the request timeout."""
    raise httpx.ReadTimeout("simulated slow auto_summary (>request timeout)")


class _FakeResponse:
    status_code = 200
    text = "OK"

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_dispatch_save_timeout_surfaces_verify_hint(monkeypatch):
    monkeypatch.setattr(mcp, "_post", _raise_timeout)
    result = await _dispatch_tool(
        "palinode_save", {"content": "a distinctive phrase", "ps": True}
    )
    text = result[0].text
    assert text.startswith("Timeout:")
    assert "palinode_search" in text
    assert "duplicate" in text


@pytest.mark.asyncio
async def test_dispatch_search_timeout_keeps_plain_message(monkeypatch):
    monkeypatch.setattr(mcp, "_post", _raise_timeout)
    result = await _dispatch_tool("palinode_search", {"query": "anything"})
    text = result[0].text
    # Read path: plain timeout, no misleading dedup advice.
    assert "timed out" in text
    assert "duplicate" not in text


@pytest.mark.asyncio
async def test_dispatch_save_forwards_priority(monkeypatch):
    captured = {}

    async def fake_post(path, json=None, timeout=30.0):
        captured["path"] = path
        captured["json"] = json
        return _FakeResponse({"file_path": "/palinode/decisions/mcp-priority.md", "id": "decisions-mcp-priority"})

    monkeypatch.setattr(mcp, "_post", fake_post)
    result = await _dispatch_tool(
        "palinode_save",
        {"content": "body", "type": "Decision", "priority": 5},
    )

    assert captured["path"] == "/save"
    assert captured["json"]["priority"] == 5
    assert "Saved" in result[0].text


@pytest.mark.asyncio
async def test_dispatch_search_forwards_min_priority(monkeypatch):
    captured = {}

    async def fake_post(path, json=None, timeout=30.0):
        captured["path"] = path
        captured["json"] = json
        return _FakeResponse([])

    monkeypatch.setattr(mcp, "_post", fake_post)
    result = await _dispatch_tool(
        "palinode_search",
        {"query": "anything", "min_priority": 4},
    )

    assert captured["path"] == "/search"
    assert captured["json"]["min_priority"] == 4
    assert "No results" in result[0].text
