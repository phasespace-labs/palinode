"""Recorded delivery lookup against live markdown and a real SQLite store."""
import json

import pytest
from click.testing import CliRunner

from palinode.core import store
from palinode.core.config import config
from palinode.core.retrieval_log import RetrievalLogger
from palinode.core.session_deliveries import session_deliveries


@pytest.fixture
def memory(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / "index.db"))
    store.init_db()
    (tmp_path / "note.md").write_text("---\nvisibility: shared\n---\nA note\n")
    logger = RetrievalLogger(str(tmp_path))
    from palinode.api.routers import git_history
    monkeypatch.setattr(git_history, "_retrieval_logger", logger)
    return tmp_path, logger


def test_delivery_shapes_and_legacy(memory):
    root, logger = memory
    logger.record_file_read("./note.md", source="palinode_read", mode="explicit", session_id="alpha")
    logger.record_file_read(str(root / "note.md"), source="palinode_read", mode="explicit", session_id="alpha")
    logger.record_file_read("note.md", source="palinode_read", mode="explicit")
    for source in ("resolve", "prime"):
        logger.record_public_receipt({
            "bundle_id": source + "12345", "evaluated_at": "2026-10-01T00:00:00Z",
            "supplied": [{"ref": "note"}, {"ref": "note"}, {"ref": "other"}],
            "lineage": [{"origin": "named-only"}],
        }, source=source, session_id="beta")
    logger.record_trigger_fires([{"id": "trigger", "memory_file": "note.md"}], session_id="gamma")
    result = session_deliveries("note.md", str(root))
    assert [(r["session_id"], r["delivery_count"]) for r in result["sessions"]] == [
        ("alpha", 2), ("beta", 2), ("gamma", 1),
    ]
    assert result["deliveries_without_session_id"] == 1
    assert result["sessions"][1]["sources"] == ["resolve", "prime"]
    assert "other" not in json.dumps(result)
    assert not session_deliveries("named-only.md", str(root))["sessions"]
    assert not session_deliveries("missing.md", str(root))["sessions"]
    (root / "note.md").write_text("---\nvisibility: private\n---\nSecret\n")
    hidden = session_deliveries("note.md", str(root))
    assert hidden["sessions"] == []
    assert hidden["deliveries_without_session_id"] == 0


def test_api_combinations_and_paths(memory):
    root, logger = memory
    from fastapi import HTTPException
    from palinode.api.routers.git_history import blame_api
    logger.record_file_read("note.md", source="read", mode="explicit", session_id="alpha")
    result = blame_api("note.md", search="note", claims=True, sessions=True)
    assert set(result) == {"blame", "claims", "sessions"}
    assert result["sessions"]["sessions"][0]["session_id"] == "alpha"
    assert set(blame_api("note.md")) == {"blame"}
    (root / "escape.md").symlink_to(root.parent / "outside.md")
    for unsafe in ("../outside.md", "escape.md", "/outside.md", "bad\x00.md"):
        with pytest.raises(HTTPException) as exc:
            blame_api(unsafe, sessions=True)
        assert exc.value.status_code in (400, 403)


def test_cli_client_and_output(memory, monkeypatch):
    from palinode.cli import git
    from palinode.cli._format import OutputFormat
    from palinode.api.routers.git_history import blame_api
    monkeypatch.setattr(git.api_client, "blame", blame_api)
    monkeypatch.setattr(git, "get_default_format", lambda: OutputFormat.JSON)
    result = CliRunner().invoke(git.blame, ["--sessions", "--claims", "--search", "note", "note.md"])
    assert result.exit_code == 0, result.output
    assert set(json.loads(result.output)) == {"blame", "claims", "sessions"}


def test_cli_api_client_forwards_sessions(memory):
    import httpx
    from palinode.cli._api import api_client

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"blame": "line", "sessions": {"sessions": []}})

    original = api_client.client
    try:
        api_client.client = httpx.Client(transport=httpx.MockTransport(handle), base_url=original.base_url)
        result = api_client.blame("note.md", "needle", claims=True, sessions=True)
        assert result["sessions"] == {"sessions": []}
        assert dict(requests[0].url.params) == {"search": "needle", "claims": "true", "sessions": "true"}
    finally:
        api_client.client.close()
        api_client.client = original


def test_receipt_rows_count_once_and_scan_limit(memory, monkeypatch):
    root, logger = memory
    from palinode.core.retrieval_log import RetrievalEvent
    from palinode.core import session_deliveries as module

    for _ in range(2):
        logger.record(RetrievalEvent(
            timestamp="2026-10-01T00:00:00Z", file_path="note.md", chunk_id=None,
            mode="explicit", source="search", query=None, rank=0, score=1,
            session_id="alpha", bundle_id="12345678",
        ))
    assert session_deliveries("note.md", str(root))["sessions"][0]["delivery_count"] == 1
    monkeypatch.setattr(module, "MAX_LINES_SCANNED", 1)
    assert session_deliveries("note.md", str(root))["scan_truncated"] is True


def test_human_output_explains_incomplete_history(memory, monkeypatch):
    from palinode.cli import git
    from palinode.cli._format import OutputFormat
    from palinode.api.routers.git_history import blame_api

    monkeypatch.setattr(git.api_client, "blame", blame_api)
    monkeypatch.setattr(git, "get_default_format", lambda: OutputFormat.TEXT)
    result = CliRunner().invoke(git.blame, ["--sessions", "note.md"])
    assert result.exit_code == 0, result.output
    assert "No visible recorded sessions" in result.output
    assert "not use or causal influence" in result.output
    assert "retention" in result.output


def test_project_visibility_filters_before_aggregation(memory):
    root, logger = memory
    from palinode.core.scope import ScopeChain

    (root / "note.md").write_text("---\nentities: [project/alpha]\n---\nA note\n")
    logger.record_file_read("note.md", source="read", mode="explicit", session_id="hidden")
    assert session_deliveries("note.md", str(root), chain=ScopeChain(project="beta"))["sessions"] == []
    assert session_deliveries("note.md", str(root), chain=ScopeChain(project="alpha"))["sessions"][0]["session_id"] == "hidden"
