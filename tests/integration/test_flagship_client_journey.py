"""Mechanical proof for the fictional flagship-client memory journey.

This is deliberately an MCP -> API -> SQLite -> markdown test, not a model
transcript.  It pins the transport evidence that both the Claude Code hook +
MCP and Codex CLI MCP + AGENTS.md walkthroughs depend on, while leaving client
lifecycle and built-in-memory observations to the recorded human trial.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient

from palinode.core.config import config


pytestmark = pytest.mark.skipif(
    not hasattr(config.search, "retrieval_mode"),
    reason="requires the v0.21 explicit lexical-retrieval configuration",
)


@pytest.fixture()
def journey_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real disposable store; no watcher, model, or user memory is used."""
    monkeypatch.setenv("PALINODE_ALLOW_FRESH_DB", "1")
    # The client is working in harbor-notes, the project the journey saves to.
    # Pinned so the scope is not whatever checkout the suite runs in: a
    # project-scoped search leaves other projects' records out.
    monkeypatch.setenv("PALINODE_PROJECT", "harbor-notes")
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.search, "retrieval_mode", "lexical")
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.git, "auto_push", False)
    for directory in ("people", "projects", "decisions", "insights", "research", "inbox", "daily"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)

    from palinode.core import store

    store.init_db()
    yield tmp_path


@pytest.fixture()
def api(journey_store: Path) -> TestClient:
    from palinode.api.server import _rate_counters, app

    _rate_counters.clear()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    _rate_counters.clear()


@pytest.fixture()
def mcp(api: TestClient, monkeypatch: pytest.MonkeyPatch):
    """Route the real MCP dispatcher through the in-process API transport."""

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.query
        if isinstance(query, bytes):
            query = query.decode("latin-1")
        path = request.url.path + (f"?{query}" if query else "")
        response = api.request(
            request.method, path, content=request.content, headers=dict(request.headers)
        )
        return httpx.Response(
            response.status_code, headers=dict(response.headers), content=response.content
        )

    from palinode import mcp as palinode_mcp

    monkeypatch.setattr(palinode_mcp, "_http_transport", httpx.MockTransport(handler))

    def call(name: str, arguments: dict) -> str:
        return asyncio.run(palinode_mcp._dispatch_tool(name, arguments))[0].text

    return call


def test_harbor_notes_correction_is_recalled_with_source_and_conflict(
    api: TestClient, journey_store: Path, mcp
) -> None:
    """Save A, correct it explicitly, then prove a new search finds B."""
    assert config.search.retrieval_mode == "lexical"
    initial = mcp("palinode_save", {
        "content": "Use SQLite for the local prototype because it runs as a single-user desktop app.",
        "type": "Decision",
        "slug": "harbor-notes-storage",
        "project": "harbor-notes",
        "source": "codex-cli",
        "title": "Harbor Notes local storage",
    })
    assert "Saved to decisions/harbor-notes-storage.md" in initial
    assert "[lexical retrieval: ready]" in initial

    # A nearby decision must remain in force through the correction.
    assert "Saved to" in mcp("palinode_save", {
        "content": "Store event timestamps in UTC.",
        "type": "Decision",
        "slug": "harbor-notes-timestamps",
        "project": "harbor-notes",
        "title": "Harbor Notes timestamps",
    })

    # The hosted-service rationale needs its own supporting record.  The
    # retired SQLite choice is preserved as lineage, not repurposed as support.
    assert "Saved to" in mcp("palinode_save", {
        "content": "The shared hosted service requires transactional coordination for concurrent writers.",
        "type": "Decision",
        "slug": "harbor-notes-concurrent-write-requirement",
        "project": "harbor-notes",
        "title": "Harbor Notes concurrent-write requirement",
    })

    # The region disagreement is deliberately recorded, not settled by the harness.
    assert "Saved to" in mcp("palinode_save", {
        "content": "Deploy the shared service in the north region.",
        "type": "Decision",
        "slug": "harbor-notes-region-north",
        "project": "harbor-notes",
        "title": "Harbor Notes north region",
    })
    assert "Saved to" in mcp("palinode_save", {
        "content": "Deploy the shared service in the south region.",
        "type": "Decision",
        "slug": "harbor-notes-region-south",
        "project": "harbor-notes",
        "title": "Harbor Notes south region",
        "contradicts": ["decisions/harbor-notes-region-north"],
    })

    corrected = mcp("palinode_save", {
        "content": "Use PostgreSQL for the shared hosted service because concurrent writers need transactional coordination.",
        "type": "Decision",
        "slug": "harbor-notes-storage-shared",
        "project": "harbor-notes",
        "source": "codex-cli",
        "title": "Harbor Notes hosted storage",
        "sources": [{
            "ref": "decisions/harbor-notes-storage.md",
            "quote": "Use SQLite for the local prototype because it runs as a single-user desktop app.",
        }, {
            "ref": "decisions/harbor-notes-concurrent-write-requirement.md",
            "quote": "The shared hosted service requires transactional coordination for concurrent writers.",
        }],
        "backed_by": ["decisions/harbor-notes-concurrent-write-requirement"],
    })
    assert "Saved to decisions/harbor-notes-storage-shared.md" in corrected

    archived = mcp("palinode_archive", {
        "file_path": "decisions/harbor-notes-storage.md",
        "reason": "Superseded by the hosted-service correction.",
        "superseded_by": "decisions/harbor-notes-storage-shared",
    })
    assert "Superseded by decisions/harbor-notes-storage-shared" in archived

    # A new search invocation over the same on-disk store finds the correction
    # through explicitly configured lexical retrieval.
    recall = mcp("palinode_search", {
        "query": "shared hosted service transactional concurrent writers",
        "threshold": 0.0,
        "resolve": "linked",
    })
    assert "decisions/harbor-notes-storage-shared.md" in recall
    assert "PostgreSQL" in recall
    assert "SQLite for the local prototype" not in recall
    assert "lexical" in recall.lower()

    evidence = mcp("palinode_read", {
        "file_path": "decisions/harbor-notes-storage-shared.md", "meta": True
    })
    assert "decisions/harbor-notes-storage.md" in evidence
    assert "decisions/harbor-notes-concurrent-write-requirement" in evidence

    digest = api.post("/context/prime", json={"project": "harbor-notes"})
    assert digest.status_code == 200, digest.text
    recent = {row["file"] for row in digest.json()["recent_decisions"]}
    assert "decisions/harbor-notes-storage-shared.md" in recent
    assert "decisions/harbor-notes-timestamps.md" in recent
    assert "decisions/harbor-notes-storage.md" not in recent

    utc_neighbor = mcp("palinode_read", {
        "file_path": "decisions/harbor-notes-timestamps.md", "meta": True
    })
    assert "Store event timestamps in UTC." in utc_neighbor

    north = mcp("palinode_read", {
        "file_path": "decisions/harbor-notes-region-north.md", "meta": True
    })
    south = mcp("palinode_read", {
        "file_path": "decisions/harbor-notes-region-south.md", "meta": True
    })
    assert "contradicts" in north
    assert "contradicts" in south
    assert journey_store.joinpath(".palinode.db").exists()
