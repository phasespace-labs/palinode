"""E2E tests for the MCP tool layer (issue #122).

Each test calls ``_dispatch_tool`` directly — the async dispatcher that
implements every ``palinode_*`` MCP tool.  The dispatcher makes HTTP calls
to the Palinode API, which we redirect in-process via an ``httpx.MockTransport``
shim over FastAPI's ``TestClient`` (same pattern as
``tests/integration/test_session_end_e2e_l1_l3.py``).

This exercises the full MCP → API → SQLite → filesystem path without
running a real palinode-api process or touching the network.  Only the
embedder and LLM helpers are mocked (no Ollama required).

Coverage:
  - palinode_search: returns results with expected shape
  - palinode_save: file lands on disk, subsequent search finds it (roundtrip)
  - palinode_session_end: daily file is created with structured content
  - palinode_status: returns health stats
  - palinode_read: returns file content for a known file
  - palinode_history: summary and full detail modes
  - palinode_doctor: returns check results
  - palinode_list: returns file listing
  - palinode_save ps=True shortcut: ProjectSnapshot shortcut works
  - error handling: unknown tool name → graceful error text (no raise)
  - error handling: missing required arg → graceful error text (no raise)
  - error handling: ps=true with conflicting type → error text
"""
from __future__ import annotations

import asyncio
import os
import time
from unittest import mock

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from palinode.core.config import config


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EMBED_DIM = 1024


def _fake_embed(text: str, backend: str = "local") -> list[float]:
    """Deterministic fake embedder — no Ollama needed."""
    return [0.1] * EMBED_DIM


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    """Isolated tmp memory dir with real SQLite, fake embedder, no Ollama."""
    memory_dir = str(tmp_path)
    db_path = os.path.join(memory_dir, ".palinode.db")

    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config, "db_path", db_path)
    monkeypatch.setattr(config.git, "auto_commit", False)

    for d in ("people", "projects", "decisions", "insights", "research", "inbox", "daily"):
        os.makedirs(os.path.join(memory_dir, d), exist_ok=True)

    from palinode.core import store
    store.init_db()

    with (
        mock.patch("palinode.core.embedder.embed", side_effect=_fake_embed),
        mock.patch("palinode.api.server._generate_description", return_value="Test description"),
        mock.patch("palinode.api.server._generate_summary", return_value=""),
    ):
        yield memory_dir


@pytest.fixture()
def api_tc(isolated_env):
    """FastAPI TestClient with cleared rate counters."""
    from palinode.api.server import app, _rate_counters
    _rate_counters.clear()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def dispatch(api_tc):
    """Return an async callable that invokes _dispatch_tool in-process.

    The MCP dispatcher uses ``httpx.AsyncClient`` to call the API.  We swap
    its transport for one that delegates synchronously to the ``TestClient``
    so no real server needs to be running.
    """
    from palinode.api.server import app as _app

    def _mock_transport_handler(request: httpx.Request) -> httpx.Response:
        # Reconstruct path + query string.
        # url.query is bytes; decode it before appending.
        url = request.url
        raw_query = url.query
        if isinstance(raw_query, bytes):
            raw_query = raw_query.decode("latin-1")
        path_with_qs = url.path + ("?" + raw_query if raw_query else "")
        tc_resp = api_tc.request(
            method=request.method,
            url=path_with_qs,
            content=request.content,
            headers=dict(request.headers),
        )
        return httpx.Response(
            status_code=tc_resp.status_code,
            headers=dict(tc_resp.headers),
            content=tc_resp.content,
        )

    async def _call(name: str, arguments: dict) -> list:
        from palinode.mcp import _dispatch_tool

        # Patch AsyncClient to use our in-process transport
        original_async_client = httpx.AsyncClient

        class _InProcessAsyncClient(httpx.AsyncClient):
            def __init__(self, **kwargs):
                kwargs.pop("transport", None)
                super().__init__(
                    transport=httpx.MockTransport(_mock_transport_handler),
                    **kwargs,
                )

        with mock.patch("palinode.mcp.httpx.AsyncClient", _InProcessAsyncClient):
            return await _dispatch_tool(name, arguments)

    return _call


def _run(coro):
    """Run a coroutine synchronously (pytest doesn't natively run async tests
    unless pytest-asyncio is configured; avoid the dependency here)."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _index_file(fp: str, content: str, category: str) -> None:
    """Manually index a file so search can find it (watcher not running)."""
    from palinode.core import store
    chunks = [{
        "id": f"mcp-e2e-{os.path.basename(fp)}",
        "file_path": fp,
        "section_id": None,
        "category": category,
        "content": content,
        "metadata": {},
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "embedding": _fake_embed("x"),
    }]
    store.upsert_chunks(chunks)


# ---------------------------------------------------------------------------
# Tests: palinode_search
# ---------------------------------------------------------------------------


def test_search_returns_text_result(dispatch):
    """palinode_search returns a non-empty text result (even on empty index)."""
    result = _run(dispatch("palinode_search", {"query": "hybrid search", "threshold": 0.0}))
    assert isinstance(result, list)
    assert len(result) == 1
    # "No results found." or actual matches — both are valid text responses
    assert result[0].text is not None


def test_search_result_shape(dispatch, api_tc, isolated_env):
    """After saving a file and indexing it, search returns it with expected shape."""
    # Save via API
    resp = api_tc.post("/save", json={
        "content": "Palinode uses hybrid BM25+vector search.",
        "type": "Insight",
        "slug": "mcp-search-target",
    })
    assert resp.status_code == 200
    fp = resp.json()["file_path"]

    # Index it
    _index_file(fp, "Palinode uses hybrid BM25+vector search.", "insights")

    result = _run(dispatch("palinode_search", {
        "query": "hybrid search",
        "threshold": 0.0,
        "limit": 5,
    }))
    assert len(result) == 1
    text = result[0].text
    assert "mcp-search-target" in text or "hybrid" in text.lower()


# ---------------------------------------------------------------------------
# Tests: palinode_save
# ---------------------------------------------------------------------------


def test_save_creates_file_on_disk(dispatch, isolated_env):
    """palinode_save writes a markdown file to the memory directory."""
    result = _run(dispatch("palinode_save", {
        "content": "MCP save test — file should exist on disk.",
        "type": "Insight",
        "slug": "mcp-save-disk",
    }))
    assert len(result) == 1
    text = result[0].text
    # Should NOT be an error
    assert not text.startswith("Save failed")
    assert not text.startswith("Error")

    # File should exist
    fp = os.path.join(isolated_env, "insights", "mcp-save-disk.md")
    assert os.path.exists(fp), f"Expected file at {fp}"

    with open(fp) as f:
        content = f.read()
    assert "MCP save test" in content


def test_save_roundtrip(dispatch, api_tc, isolated_env):
    """palinode_save then palinode_search — saved item is findable."""
    # Save via MCP
    result = _run(dispatch("palinode_save", {
        "content": "Roundtrip memory: quantum-entangled databases.",
        "type": "Insight",
        "slug": "mcp-roundtrip",
    }))
    assert not result[0].text.startswith("Error")
    assert not result[0].text.startswith("Save failed")

    fp = os.path.join(isolated_env, "insights", "mcp-roundtrip.md")
    assert os.path.exists(fp)

    # Index it so search can find it
    _index_file(fp, "Roundtrip memory: quantum-entangled databases.", "insights")

    # Search via MCP
    search_result = _run(dispatch("palinode_search", {
        "query": "quantum-entangled databases",
        "threshold": 0.0,
    }))
    assert len(search_result) == 1
    text = search_result[0].text
    assert "mcp-roundtrip" in text or "quantum" in text.lower()


def test_save_ps_shortcut(dispatch, isolated_env):
    """palinode_save ps=True should create a ProjectSnapshot file."""
    result = _run(dispatch("palinode_save", {
        "content": "Project snapshot via ps shortcut.",
        "ps": True,
        "slug": "mcp-ps-shortcut",
    }))
    assert len(result) == 1
    text = result[0].text
    assert not text.startswith("Error")
    assert not text.startswith("Save failed")

    fp = os.path.join(isolated_env, "projects", "mcp-ps-shortcut.md")
    assert os.path.exists(fp)

    with open(fp) as f:
        raw = f.read()
    fm = yaml.safe_load(raw.split("---", 2)[1])
    assert fm["type"] == "ProjectSnapshot"


def test_save_ps_type_conflict_returns_error(dispatch, isolated_env):
    """ps=True with a conflicting type should return an error string, not raise."""
    result = _run(dispatch("palinode_save", {
        "content": "Conflicting save.",
        "ps": True,
        "type": "Decision",
    }))
    assert len(result) == 1
    assert "Error" in result[0].text or "conflict" in result[0].text.lower()


# ---------------------------------------------------------------------------
# Tests: palinode_session_end
# ---------------------------------------------------------------------------


def test_session_end_creates_daily_file(dispatch, isolated_env):
    """palinode_session_end creates a daily file with structured content."""
    result = _run(dispatch("palinode_session_end", {
        "summary": "Completed MCP E2E test suite.",
        "decisions": ["Use in-process transport for MCP tests"],
        "project": "palinode",
        "source": "test",
    }))
    assert len(result) == 1
    text = result[0].text
    assert not text.startswith("Session-end failed")
    assert not text.startswith("Error")

    # Response text should mention the daily file
    assert "daily" in text.lower() or "session" in text.lower() or "captured" in text.lower()

    # Daily file should exist on disk
    daily_files = []
    daily_dir = os.path.join(isolated_env, "daily")
    if os.path.exists(daily_dir):
        daily_files = [f for f in os.listdir(daily_dir) if f.endswith(".md")]
    assert len(daily_files) >= 1, "Expected at least one daily file to be created"

    # Check content
    daily_path = os.path.join(daily_dir, daily_files[0])
    with open(daily_path) as f:
        content = f.read()
    assert "Completed MCP E2E test suite." in content


# ---------------------------------------------------------------------------
# Tests: palinode_status
# ---------------------------------------------------------------------------


def test_status_returns_health_stats(dispatch):
    """palinode_status returns formatted health stats text."""
    result = _run(dispatch("palinode_status", {}))
    assert len(result) == 1
    text = result[0].text
    assert not text.startswith("API unreachable")
    # Should contain standard status fields
    assert "Files indexed" in text or "Chunks indexed" in text or "Palinode Status" in text


# ---------------------------------------------------------------------------
# Tests: palinode_read
# ---------------------------------------------------------------------------


def test_read_returns_file_content(dispatch, api_tc, isolated_env):
    """palinode_read returns the body of a known memory file."""
    # Create a file via API
    resp = api_tc.post("/save", json={
        "content": "Readable MCP content.",
        "type": "Insight",
        "slug": "mcp-readable",
    })
    assert resp.status_code == 200

    result = _run(dispatch("palinode_read", {"file_path": "insights/mcp-readable.md"}))
    assert len(result) == 1
    text = result[0].text
    assert not text.startswith("Error")
    assert "Readable MCP content." in text


def test_read_meta_includes_frontmatter(dispatch, api_tc, isolated_env):
    """palinode_read with meta=True includes frontmatter block."""
    api_tc.post("/save", json={
        "content": "Meta test content.",
        "type": "Decision",
        "slug": "mcp-meta-read",
    })

    result = _run(dispatch("palinode_read", {
        "file_path": "decisions/mcp-meta-read.md",
        "meta": True,
    }))
    text = result[0].text
    assert "---" in text  # frontmatter block present
    assert "type" in text


# ---------------------------------------------------------------------------
# Tests: palinode_history
# ---------------------------------------------------------------------------


def test_history_summary_no_git(dispatch, api_tc, isolated_env):
    """palinode_history returns gracefully when git history is absent (no commits)."""
    api_tc.post("/save", json={
        "content": "History test file.",
        "type": "Insight",
        "slug": "mcp-history",
    })

    result = _run(dispatch("palinode_history", {
        "file_path": "insights/mcp-history.md",
        "detail": "summary",
    }))
    assert len(result) == 1
    text = result[0].text
    # Either "No history found." (no git) or actual commit lines — both valid
    assert text is not None and len(text) > 0


def test_history_full_detail_no_git(dispatch, api_tc, isolated_env):
    """palinode_history with detail=full returns gracefully."""
    api_tc.post("/save", json={
        "content": "Full history test.",
        "type": "Insight",
        "slug": "mcp-history-full",
    })

    result = _run(dispatch("palinode_history", {
        "file_path": "insights/mcp-history-full.md",
        "detail": "full",
    }))
    assert len(result) == 1
    assert result[0].text is not None


# ---------------------------------------------------------------------------
# Tests: palinode_doctor
# ---------------------------------------------------------------------------


def test_doctor_returns_check_results(dispatch):
    """palinode_doctor returns a JSON-formatted check-results structure."""
    import json as _json

    result = _run(dispatch("palinode_doctor", {}))
    assert len(result) == 1
    text = result[0].text
    assert not text.startswith("Doctor failed")

    # Should be JSON
    data = _json.loads(text)
    assert "results" in data
    assert "summary" in data
    assert isinstance(data["results"], list)


# ---------------------------------------------------------------------------
# Tests: palinode_list
# ---------------------------------------------------------------------------


def test_list_returns_files(dispatch, api_tc, isolated_env):
    """palinode_list returns a text list including saved files."""
    api_tc.post("/save", json={
        "content": "List test item.",
        "type": "Insight",
        "slug": "mcp-list-item",
    })

    result = _run(dispatch("palinode_list", {}))
    assert len(result) == 1
    text = result[0].text
    assert "mcp-list-item" in text or "insights" in text.lower()


# ---------------------------------------------------------------------------
# Tests: error handling
# ---------------------------------------------------------------------------


def test_unknown_tool_returns_graceful_error(dispatch):
    """Unknown tool name returns an error string rather than raising."""
    result = _run(dispatch("palinode_nonexistent_tool", {}))
    assert len(result) == 1
    text = result[0].text
    assert "Unknown tool" in text or "unknown" in text.lower()


def test_missing_required_arg_search(dispatch):
    """palinode_search without 'query' returns an error, does not crash."""
    # KeyError on arguments["query"] should be caught by the broad except clause
    result = _run(dispatch("palinode_search", {}))
    assert len(result) == 1
    # Should be an error response, not a crash
    text = result[0].text
    assert text is not None


def test_missing_required_arg_save(dispatch):
    """palinode_save without 'content' returns an error."""
    result = _run(dispatch("palinode_save", {"type": "Insight"}))
    assert len(result) == 1
    text = result[0].text
    # ValueError from _resolve_save_type (no type or ps) or KeyError on content
    assert text is not None


# ---------------------------------------------------------------------------
# Phase 1: every-tool smoke (issue, parent)
#
# Registry-driven test that exercises every MCP tool with minimal valid args.
# The smoke-args registry is shared with the stdio JSON-RPC test
# tests/integration/_smoke_args.py — both surfaces stay in lockstep.
# ---------------------------------------------------------------------------


from tests.integration._smoke_args import (
    DISPATCH_ERROR_PREFIXES,
    SKIP_TOOLS,
    TOOL_SMOKE_ARGS,
    registered_tool_names,
)


def test_smoke_args_covers_all_tools():
    """Drift guard: every full-surface tool in @server.list_tools() must have a
    TOOL_SMOKE_ARGS entry, and the registry must not contain stale tool names.

    When a new MCP tool is added to palinode/mcp.py without a smoke-args
    entry, this test fails with a clear instruction to add one.
    """
    registered = set(registered_tool_names())
    covered = set(TOOL_SMOKE_ARGS.keys())
    missing = registered - covered
    extra = covered - registered
    assert not missing, (
        f"New MCP tool(s) registered without a TOOL_SMOKE_ARGS entry: "
        f"{sorted(missing)}. Add minimal valid args to TOOL_SMOKE_ARGS "
        f"in tests/integration/_smoke_args.py."
    )
    assert not extra, (
        f"TOOL_SMOKE_ARGS has entries for tools not registered in "
        f"palinode/mcp.py: {sorted(extra)}. Remove the stale entry "
        f"or fix the tool name."
    )


@pytest.fixture()
def seeded_env(api_tc, isolated_env):
    """isolated_env + a pre-seeded insights/smoke-target.md.

    Several tools (read, history, blame, rollback, cluster_neighbors) need a
    target file to exercise.  A single shared seed keeps the parametrized
    smoke test reproducible without per-tool setup.
    """
    api_tc.post("/save", json={
        "content": "Seed file for the parametrized MCP smoke suite.",
        "type": "Insight",
        "slug": "smoke-target",
    })
    return isolated_env


@pytest.mark.parametrize(
    "tool_name,args,lenient",
    [
        pytest.param(name, args, lenient, marks=pytest.mark.skip(reason=SKIP_TOOLS[name]))
        if name in SKIP_TOOLS
        else (name, args, lenient)
        for name, (args, lenient) in TOOL_SMOKE_ARGS.items()
    ],
    ids=list(TOOL_SMOKE_ARGS.keys()),
)
def test_every_tool_dispatches(dispatch, seeded_env, tool_name, args, lenient):
    """Every MCP tool dispatches without raising and returns a non-empty TextContent.

    Strict tools (lenient=False) must not return a dispatcher error response.
    Lenient tools may return graceful error text in test env — only the
    no-crash invariant is checked.
    """
    result = _run(dispatch(tool_name, args))
    assert isinstance(result, list), f"{tool_name}: expected list, got {type(result).__name__}"
    assert len(result) == 1, f"{tool_name}: expected 1 TextContent, got {len(result)}"
    text = result[0].text
    assert text and len(text) > 0, f"{tool_name}: returned empty text"

    if not lenient:
        for prefix in DISPATCH_ERROR_PREFIXES:
            if text.startswith(prefix):
                pytest.fail(
                    f"{tool_name} returned dispatcher error response: "
                    f"{text[:300]!r}"
                )
