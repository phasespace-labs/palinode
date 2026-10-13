"""Supported service-free retrieval, real SQLite/files/git, and mode migration."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from palinode.api import server
from palinode.core import embedder, store
from palinode.core.config import SearchConfig, config, load_config
from palinode.core.retrieval import diagnostics
from palinode.indexer import reconcile
from palinode.indexer.index_file import index_file
from palinode.indexer.watcher import PalinodeHandler


@pytest.fixture()
def lexical(tmp_path, monkeypatch):
    monkeypatch.setattr(config.search, "retrieval_mode", "lexical")
    monkeypatch.setattr(config.git, "auto_commit", True)
    monkeypatch.setattr(config.capture.cross_refs, "enabled", False)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for key, value in [("user.email", "fixture@example.invalid"), ("user.name", "Lexical fixture")]:
        subprocess.run(["git", "-C", str(tmp_path), "config", key, value], check=True)
    # Any accidental model access is a failure, including a bounded cold probe.
    no_service = Mock(side_effect=AssertionError("lexical retrieval contacted a model"))
    monkeypatch.setattr(embedder, "embed", no_service)
    monkeypatch.setattr(embedder, "embed_many", no_service)
    monkeypatch.setattr(reconcile, "get_ollama_client", no_service)
    server._rate_counters.clear()
    with TestClient(server.app, raise_server_exceptions=False) as client:
        yield client, tmp_path, no_service
    no_service.assert_not_called()


def _save(client, content="Use SQLite for the orionledger decision.", **kw):
    response = client.post("/save", json={"content": content, "type": "Decision", **kw})
    assert response.status_code == 200, response.text
    return response.json()


def _search(client, query="orionledger", **kw):
    response = client.post("/search", json={"query": query, "receipt": True, **kw})
    assert response.status_code == 200, response.text
    return response.json()


def test_save_read_search_inspector_without_any_model(lexical):
    client, root, _ = lexical
    before = _search(client)
    assert before["receipt"]["retrieval"]["outcome"] == "not_indexed"
    saved = _save(client)
    assert saved["indexed"] is True
    assert saved["indexed_fts"] is True
    assert saved["embedded"] is False
    assert saved["indexed_vec"] is False
    assert saved["retrieval_mode"] == "lexical"
    assert saved["git_committed"] is True
    assert "index_error" not in saved
    assert "description_pending" not in saved
    assert "summary_pending" not in saved
    read = client.get("/read", params={"file_path": saved["rel_path"]})
    assert read.status_code == 200
    assert "orionledger" in read.text
    found = _search(client, threshold=1.0, hybrid=False)
    hit = found["results"][0]
    assert hit["raw_score"] is None
    assert hit["retrieval_mode"] == "lexical"
    assert hit["freshness"] == "valid"
    assert found["receipt"]["retrieval"]["outcome"] == "matched"
    assert _search(client, "xylophonemissing")["receipt"]["retrieval"]["outcome"] == "no_match"
    ui = client.get("/ui/memory", params={"q": "orionledger"})
    assert ui.status_code == 200, ui.text
    assert "Retrieval: lexical" in ui.text
    assert "keyword match, rank" in ui.text
    assert "index matches source" in ui.text
    history = subprocess.check_output(["git", "-C", str(root), "log", "--oneline"], text=True)
    assert history.strip()


def test_lexical_noop_and_metadata_reconciliation(lexical):
    client, _, _ = lexical
    saved = _save(client)
    path = saved["file_path"]
    outcome = index_file(path)
    assert outcome["indexed"] is True
    assert outcome["chunks_unchanged"] > 0
    assert outcome["chunks_reembedded"] == 0
    text = Path(path).read_text()
    Path(path).write_text(text.replace("type: Decision", "type: Insight"))
    assert index_file(path)["indexed"]
    assert not _search(client, types=["Decision"])["results"]
    assert _search(client, types=["Insight"])["results"]


def test_filters_and_live_visibility(lexical):
    client, _, _ = lexical
    saved = _save(client, priority=2)
    assert not _search(client, min_priority=3)["results"]
    assert not _search(client, date_after="2099-01-01")["results"]
    assert not _search(client, date_before="2000-01-01")["results"]
    assert not _search(client, type_deny=["Decision"])["results"]
    assert not _search(client, category="people")["results"]
    # Authoritative source visibility must hide even before the index catches up.
    p = Path(saved["file_path"])
    p.write_text(p.read_text().replace("---\n", "---\nvisibility: private\n", 1))
    assert not _search(client)["results"]


def test_date_bounds_compare_instants_on_every_path(lexical):
    """Offset, space-separated and date-only bounds are compared as instants
    on the query path (ranker) and the empty-query path (list_recent)."""
    from datetime import UTC, datetime, timedelta, timezone

    client, _, _ = lexical
    _save(client)
    stamp = _search(client)["results"][0]["metadata"]["last_updated"]
    saved_at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone(UTC)
    minus5 = timezone(timedelta(hours=-5))
    plus2 = timezone(timedelta(hours=2))
    after_it = (saved_at + timedelta(hours=1)).astimezone(minus5).isoformat()
    before_it = (saved_at - timedelta(hours=1)).astimezone(plus2).isoformat()
    for query in ("orionledger", ""):
        assert not _search(client, query, date_after=after_it)["results"], query
        assert _search(client, query, date_after=before_it)["results"], query
        assert _search(client, query, date_before=after_it)["results"], query
        assert _search(client, query, date_before=saved_at.date().isoformat())["results"], query
    bad = client.post("/search", json={"query": "orionledger", "date_after": "yesterday"})
    assert bad.status_code == 422


def test_lexical_correction_evidence_and_lifecycle(lexical):
    client, _, _ = lexical
    old = _save(client, "Orionledger uses PostgreSQL.", slug="old")
    new = _save(client, "Orionledger uses SQLite.", slug="new")
    p = Path(old["file_path"])
    p.write_text(p.read_text().replace("---\n", f"---\nsuperseded_by: {new['rel_path']}\n", 1))
    assert index_file(str(p))["indexed"]
    found = _search(client, "PostgreSQL", resolve="linked")
    hit = found["results"][0]
    assert hit["currency"] == "retired"
    assert hit["evidence"]["replacements"]
    assert hit["resolution"]["current"]["ref"] == new["rel_path"].removesuffix(".md")
    p.write_text(p.read_text().replace("---\n", "---\nstatus: archived\n", 1))
    assert index_file(str(p))["indexed"]
    assert not _search(client, "PostgreSQL")["results"]


def test_fts_errors_are_backend_failures_not_no_match(lexical, monkeypatch):
    client, _, _ = lexical
    _save(client)
    def fail(*args, **kwargs):
        raise RuntimeError("unexpected FTS defect")
    monkeypatch.setattr(store, "search_fts", fail)
    response = client.post("/search", json={"query": "orionledger", "receipt": True})
    assert response.status_code == 500
    assert "Search failed" in response.text
    assert "no_match" not in response.text


def test_no_watcher_chat_scheduling(lexical, monkeypatch):
    client, _, _ = lexical
    saved = _save(client, core=True)
    handler = PalinodeHandler()
    summary = Mock()
    description = Mock()
    monkeypatch.setattr(handler, "_schedule_summary_generation", summary)
    monkeypatch.setattr(handler, "_schedule_description_fill", description)
    handler._process_file(saved["file_path"])
    summary.assert_not_called()
    description.assert_not_called()


def test_reindex_enables_embeddings_without_changing_files_or_history(lexical, monkeypatch):
    client, root, no_service = lexical
    saved = _save(client)
    path = Path(saved["file_path"])
    before = path.read_bytes()
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"])
    monkeypatch.setattr(config.search, "retrieval_mode", "hybrid")
    monkeypatch.setattr(config.auto_summary, "enabled", False)
    monkeypatch.setattr(reconcile, "get_ollama_client", lambda: object())
    monkeypatch.setattr(embedder, "embed", lambda text: [0.01] * 1024)
    monkeypatch.setattr(embedder, "embed_many", lambda texts: [[0.01] * 1024 for _ in texts])
    assert diagnostics()["index_state"] == "embeddings_pending"
    result = client.post("/reindex")
    assert result.status_code == 200, result.text
    assert result.json()["errors"] == 0
    assert diagnostics()["index_state"] == "ready"
    assert diagnostics()["vector_chunks"] > 0
    assert path.read_bytes() == before
    assert subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"]) == head
    assert _search(client)["results"][0]["raw_score"] is not None
    no_service.assert_not_called()


@pytest.mark.parametrize("mode", ["lexical", "hybrid"])
def test_yaml_and_env_mode(mode, tmp_path, monkeypatch):
    monkeypatch.setenv("PALINODE_DIR", str(tmp_path))
    monkeypatch.delenv("PALINODE_RETRIEVAL_MODE", raising=False)
    (tmp_path / "palinode.config.yaml").write_text(yaml.safe_dump({"search": {"retrieval_mode": mode}}))
    assert load_config().search.retrieval_mode == mode
    monkeypatch.setenv("PALINODE_RETRIEVAL_MODE", "lexical" if mode == "hybrid" else "hybrid")
    assert load_config().search.retrieval_mode != mode


def test_invalid_mode_rejected(tmp_path, monkeypatch):
    with pytest.raises(ValidationError):
        SearchConfig(retrieval_mode="typo")
    monkeypatch.setenv("PALINODE_DIR", str(tmp_path))
    monkeypatch.setenv("PALINODE_RETRIEVAL_MODE", "typo")
    with pytest.raises(ValueError, match="PALINODE_RETRIEVAL_MODE"):
        load_config()


@pytest.mark.parametrize("visibility", ["private", "restricted"])
def test_hidden_only_receipt_equals_empty_visible_corpus(lexical, visibility):
    client, _, _ = lexical
    empty = _search(client)["receipt"]["retrieval"]
    saved = _save(client)
    path = Path(saved["file_path"])
    # Deliberately leave public metadata cached in SQLite.
    path.write_text(path.read_text().replace("---\n", f"---\nvisibility: {visibility}\n", 1))
    response = _search(client)
    assert response["results"] == []
    assert response["receipt"]["retrieval"] == empty
    assert "indexed_chunks" not in empty and "vector_chunks" not in empty


def test_other_scope_does_not_change_search_readiness(lexical, monkeypatch):
    client, _, _ = lexical
    monkeypatch.setattr(config.scope, "agent", "fixture-agent")
    empty = _search(client, context=["project/allowed"])["receipt"]["retrieval"]
    saved = _save(client)
    path = Path(saved["file_path"])
    path.write_text(path.read_text().replace("---\n", "---\nscope: project/hidden\n", 1))
    response = _search(client, context=["project/allowed"])
    assert response["results"] == []
    assert response["receipt"]["retrieval"] == empty


def test_hidden_vectorless_rows_do_not_mark_visible_hybrid_pending(lexical, monkeypatch):
    client, _, _ = lexical
    saved = _save(client)
    path = Path(saved["file_path"])
    path.write_text(path.read_text().replace("---\n", "---\nvisibility: private\n", 1))
    monkeypatch.setattr(config.search, "retrieval_mode", "hybrid")
    monkeypatch.setattr(embedder, "embed", lambda text: [0.01] * 1024)
    response = _search(client)
    assert response["results"] == []
    assert response["receipt"]["retrieval"]["index_state"] == "not_indexed"


def test_current_projection_excludes_retired_terms(lexical):
    from palinode.consolidation.executor import apply_operations
    client, root, _ = lexical
    path = root / "decisions" / "projection.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("---\ntype: Decision\n---\n\n- Use retiredendpoint. <!-- fact:endpoint -->\n")
    assert index_file(str(path))["indexed"]
    assert _search(client, "retiredendpoint")["results"]
    stats = apply_operations(str(path), [{
        "op": "SUPERSEDE", "id": "endpoint", "new_text": "Use currentendpoint.",
        "rationale": "Fixture replacement",
    }])
    assert stats["superseded"] == 1
    assert "retiredendpoint" in path.read_text()  # source history is retained
    assert index_file(str(path))["indexed"]
    assert not _search(client, "retiredendpoint")["results"]
    assert _search(client, "currentendpoint")["results"][0]["freshness"] == "valid"


def test_hybrid_readiness_has_no_per_file_queries_or_full_visibility_scan(lexical, monkeypatch):
    from palinode.core import visibility
    from palinode.core.retrieval import search_diagnostics
    client, _, _ = lexical
    monkeypatch.setattr(config.search, "retrieval_mode", "hybrid")
    monkeypatch.setattr(config.auto_summary, "enabled", False)
    monkeypatch.setattr(reconcile, "get_ollama_client", lambda: object())
    monkeypatch.setattr(embedder, "embed", lambda text: [0.01] * 1024)
    monkeypatch.setattr(embedder, "embed_many", lambda texts: [[0.01] * 1024 for _ in texts])
    for i in range(8):
        _save(client, f"Orionledger decision number {i}.", slug=f"item-{i}")
    statements = []
    real_db = store.get_db
    def traced_db():
        db = real_db()
        db.set_trace_callback(statements.append)
        return db
    live_gate = Mock(wraps=visibility.is_visible)
    monkeypatch.setattr(store, "get_db", traced_db)
    monkeypatch.setattr(visibility, "is_visible", live_gate)
    value = search_diagnostics("hybrid", matched=True, chain=None)
    assert value["index_state"] == "ready"
    live_gate.assert_not_called()
    assert len([s for s in statements if s.startswith("SELECT")]) == 1
    statements.clear()
    value = search_diagnostics("hybrid", matched=False, chain=None)
    assert value["outcome"] == "no_match"
    assert live_gate.call_count == 1
    assert len([s for s in statements if s.startswith("SELECT")]) == 2
