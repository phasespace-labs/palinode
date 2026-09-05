"""Tests for the MCP layer (palinode/mcp.py).

Pure-function helpers that the dispatcher delegates to. These tests don't
exercise the async tool dispatch — they cover only the logic the dispatcher
calls into, which is what changes most often and is easiest to regress.

The timeout-message tests also drive the async dispatcher directly with
a mocked slow server, since the verify-before-retry contract lives in the
dispatcher's except block, not in a pure helper alone.
"""
import os

import httpx
import pytest

import palinode.mcp as mcp
from palinode.core.config import config
from palinode.mcp import (
    _coerce_str_array,
    _dispatch_tool,
    _rel_path_from,
    _resolve_save_type,
    _timeout_message,
)


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


# _rel_path_from (— server-computed rel_path, with a config-derived fallback
# for an older API server that doesn't send it yet; replaces the hardcoded
# "/palinode/" rsplit that silently failed on any custom-named memory_dir) ----


def test_rel_path_from_prefers_server_computed_rel_path():
    payload = {"file_path": "/anything/at/all.md", "rel_path": "decisions/at-all.md"}
    assert _rel_path_from(payload) == "decisions/at-all.md"


def test_rel_path_from_falls_back_when_rel_path_missing(monkeypatch, tmp_path):
    # Simulates an older API server that hasn't started sending rel_path yet.
    # memory_dir deliberately has no "palinode" substring — the exact shape
    # the old hardcoded-literal split failed on.
    memory_dir = tmp_path / "second-brain"
    memory_dir.mkdir()
    monkeypatch.setattr(config, "memory_dir", str(memory_dir))

    abs_path = os.path.join(str(memory_dir), "decisions", "no-rel-path.md")
    payload = {"file_path": abs_path}
    assert _rel_path_from(payload) == os.path.join("decisions", "no-rel-path.md")


def test_rel_path_from_uses_custom_key_for_topic_coverage_best_match():
    payload = {"best_match": "/wherever/best.md", "rel_path": "insights/best.md"}
    assert _rel_path_from(payload, key="best_match") == "insights/best.md"


def test_rel_path_from_empty_payload_returns_empty_string():
    assert _rel_path_from({}) == ""


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


# ── rel_path parity: MCP tool text must render the API's rel_path, never a
# raw absolute path, for a memory_dir with no "/palinode/" substring — the
# regression coverage for the seven hardcoded-literal call sites ────────────


_NO_PALINODE_ABS_PATH = "/Users/x/Documents/second-brain/decisions/target.md"
_NO_PALINODE_REL_PATH = "decisions/target.md"


@pytest.mark.asyncio
async def test_dispatch_search_renders_server_rel_path(monkeypatch):
    async def fake_post(path, json=None, timeout=30.0):
        return _FakeResponse([
            {
                "file_path": _NO_PALINODE_ABS_PATH,
                "rel_path": _NO_PALINODE_REL_PATH,
                "score": 0.9,
                "snippet": "target content",
            }
        ])

    monkeypatch.setattr(mcp, "_post", fake_post)
    result = await _dispatch_tool("palinode_search", {"query": "anything"})
    text = result[0].text
    assert _NO_PALINODE_REL_PATH in text
    assert _NO_PALINODE_ABS_PATH not in text


@pytest.mark.asyncio
async def test_dispatch_save_renders_server_rel_path(monkeypatch):
    async def fake_post(path, json=None, timeout=30.0):
        return _FakeResponse({
            "file_path": _NO_PALINODE_ABS_PATH,
            "rel_path": _NO_PALINODE_REL_PATH,
            "id": "decisions-target",
        })

    monkeypatch.setattr(mcp, "_post", fake_post)
    result = await _dispatch_tool(
        "palinode_save", {"content": "body", "type": "Decision"}
    )
    text = result[0].text
    assert _NO_PALINODE_REL_PATH in text
    assert _NO_PALINODE_ABS_PATH not in text


@pytest.mark.parametrize(
    ("save_outcome", "disambiguated_from", "expected"),
    [
        ("replaced", None, "Saved to decisions/target.md (replaced)"),
        (
            "disambiguated",
            "target",
            "Saved to decisions/target.md (disambiguated from target)",
        ),
    ],
)
@pytest.mark.asyncio
async def test_dispatch_save_reports_outcome(
    monkeypatch, save_outcome, disambiguated_from, expected
):
    async def fake_post(path, json=None, timeout=30.0):
        return _FakeResponse({
            "file_path": _NO_PALINODE_ABS_PATH,
            "rel_path": _NO_PALINODE_REL_PATH,
            "id": "decisions-target",
            "save_outcome": save_outcome,
            "disambiguated_from": disambiguated_from,
        })

    monkeypatch.setattr(mcp, "_post", fake_post)
    result = await _dispatch_tool(
        "palinode_save", {"content": "body", "type": "Decision"}
    )

    assert result[0].text == expected


@pytest.mark.asyncio
async def test_dispatch_ingest_renders_server_rel_path(monkeypatch):
    async def fake_post(path, json=None, timeout=60.0):
        return _FakeResponse({
            "status": "success",
            "file_path": _NO_PALINODE_ABS_PATH,
            "rel_path": _NO_PALINODE_REL_PATH,
        })

    monkeypatch.setattr(mcp, "_post", fake_post)
    result = await _dispatch_tool("palinode_ingest", {"url": "https://example.com/x"})
    text = result[0].text
    assert _NO_PALINODE_REL_PATH in text
    assert _NO_PALINODE_ABS_PATH not in text


@pytest.mark.asyncio
async def test_dispatch_dedup_suggest_renders_server_rel_path(monkeypatch):
    async def fake_post(path, json=None, timeout=60.0):
        return _FakeResponse([
            {
                "file_path": _NO_PALINODE_ABS_PATH,
                "rel_path": _NO_PALINODE_REL_PATH,
                "similarity": 0.95,
                "snippet": "near-duplicate content",
            }
        ])

    monkeypatch.setattr(mcp, "_post", fake_post)
    result = await _dispatch_tool("palinode_dedup_suggest", {"content": "draft"})
    text = result[0].text
    assert _NO_PALINODE_REL_PATH in text
    assert _NO_PALINODE_ABS_PATH not in text


@pytest.mark.asyncio
async def test_dispatch_orphan_repair_renders_server_rel_path(monkeypatch):
    async def fake_post(path, json=None, timeout=60.0):
        return _FakeResponse([
            {
                "file_path": _NO_PALINODE_ABS_PATH,
                "rel_path": _NO_PALINODE_REL_PATH,
                "similarity": 0.8,
                "snippet": "related content",
            }
        ])

    monkeypatch.setattr(mcp, "_post", fake_post)
    result = await _dispatch_tool("palinode_orphan_repair", {"broken_link": "target"})
    text = result[0].text
    assert _NO_PALINODE_REL_PATH in text
    assert _NO_PALINODE_ABS_PATH not in text


@pytest.mark.asyncio
async def test_dispatch_cluster_neighbors_renders_server_rel_path(monkeypatch):
    async def fake_post(path, json=None, timeout=60.0):
        return _FakeResponse([
            {
                "file_path": _NO_PALINODE_ABS_PATH,
                "rel_path": _NO_PALINODE_REL_PATH,
                "similarity": 0.85,
                "snippet": "neighbour content",
            }
        ])

    monkeypatch.setattr(mcp, "_post", fake_post)
    result = await _dispatch_tool(
        "palinode_cluster_neighbors", {"file_path": "decisions/source.md"}
    )
    text = result[0].text
    assert _NO_PALINODE_REL_PATH in text
    assert _NO_PALINODE_ABS_PATH not in text


@pytest.mark.asyncio
async def test_dispatch_topic_coverage_renders_server_rel_path(monkeypatch):
    async def fake_post(path, json=None, timeout=60.0):
        return _FakeResponse({
            "covered": True,
            "best_match": _NO_PALINODE_ABS_PATH,
            "rel_path": _NO_PALINODE_REL_PATH,
            "similarity": 0.9,
        })

    monkeypatch.setattr(mcp, "_post", fake_post)
    result = await _dispatch_tool("palinode_topic_coverage", {"query": "some topic"})
    text = result[0].text
    assert _NO_PALINODE_REL_PATH in text
    assert _NO_PALINODE_ABS_PATH not in text


@pytest.mark.asyncio
async def test_dispatch_search_falls_back_to_config_memory_dir_without_rel_path(
    monkeypatch, tmp_path
):
    """Older API server that hasn't started sending rel_path yet: MCP still
    relativizes client-side, using config.memory_dir rather than any
    hardcoded literal — for a memory_dir with no "palinode" substring."""
    memory_dir = tmp_path / "second-brain"
    memory_dir.mkdir()
    monkeypatch.setattr(config, "memory_dir", str(memory_dir))

    abs_path = os.path.join(str(memory_dir), "decisions", "legacy.md")

    async def fake_post(path, json=None, timeout=30.0):
        return _FakeResponse([
            {"file_path": abs_path, "score": 0.5, "snippet": "legacy content"}
        ])

    monkeypatch.setattr(mcp, "_post", fake_post)
    result = await _dispatch_tool("palinode_search", {"query": "anything"})
    text = result[0].text
    assert os.path.join("decisions", "legacy.md") in text
    assert abs_path not in text
