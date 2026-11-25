"""Search through its core interface: ``search(SearchQuery, Caller)``.

Real SQLite and real memory files under ``tmp_path``, no FastAPI. The REST
route is transport over this; its behaviour stays covered by the route's own
tests. What is checked here is what the core interface decides: the query
against the operation spec, the caller's side effects (recall write-back, the
retrieval log and its source), the floor per surface, the capture policy,
keyword fallback, the receipt's recorded request, and ``/resolve`` taking its
seeds from the same search.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3

import pytest

from palinode.api.routers.search import SearchRequest, _search_inputs
from palinode.core import embedder, store
from palinode.core import search as core_search
from palinode.core.config import config
from palinode.core.operations import SEARCH
from palinode.core.receipt import request_scope
from palinode.core.retrieval_log import RetrievalLogger
from palinode.core.search import Caller, RecallDenied, SearchQuery, search
from tests import test_relative_floor_over_visible_hits as _floor

memory = _floor.memory
QUERY = _floor.QUERY


@pytest.fixture()
def stored(memory):
    _floor._record(memory, "kept", 0.80, 2)
    return memory


def _recall_count(memory, chunk_id: str) -> int:
    with sqlite3.connect(config.db_path) as db:
        (count,) = db.execute(
            "SELECT recall_count FROM chunks WHERE id = ?", (chunk_id,),
        ).fetchone()
    return count or 0


def _log_rows(memory) -> list[dict]:
    path = memory / ".audit" / "retrievals.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ── the query is the spec ────────────────────────────────────────────────────


def test_search_query_carries_every_spec_param_and_only_the_known_extras():
    fields = [f.name for f in dataclasses.fields(SearchQuery)]
    spec = [p.name for p in SEARCH.params]
    assert fields[:len(spec)] == spec
    assert fields[len(spec):] == ["type_deny", "hybrid", "max_chars", "prompt"]


def test_a_caller_takes_raw_scope_or_a_resolved_chain_never_both():
    with pytest.raises(ValueError, match="not both"):
        Caller(source="resolve", context=("project/x",), chain=None)
    assert Caller.from_chain(None, source="resolve").resolved
    assert not Caller().resolved


# ── what the caller may do ───────────────────────────────────────────────────


def test_a_recording_caller_writes_recall_and_a_log_row_named_for_its_surface(stored):
    log = RetrievalLogger(str(stored))
    for source, logged_as in [("mcp", "palinode_search"), ("cli", "cli_search"),
                              ("api", "api_search"), ("ui", "api_search")]:
        result = search(SearchQuery(query=QUERY, limit=5), Caller(source=source, log=log))
        assert [row["id"] for row in result.rows] == ["kept"]
        assert _log_rows(stored)[-1]["source"] == logged_as
    assert _recall_count(stored, "kept") == 4


def test_a_non_recording_caller_moves_nothing_and_logs_nothing(stored):
    result = search(SearchQuery(query=QUERY, limit=5),
                    Caller(source="api", record=False, log=RetrievalLogger(str(stored))))
    assert [row["id"] for row in result.rows] == ["kept"]
    assert _recall_count(stored, "kept") == 0
    assert _log_rows(stored) == []


@pytest.mark.parametrize(("source", "floor"), [
    ("mcp", "mcp_threshold"), ("cli", "api_threshold"), ("api", "api_threshold"),
    ("ui", "api_threshold"), ("resolve", "api_threshold"),
])
def test_the_floor_is_the_servers_for_the_callers_surface(stored, monkeypatch, source, floor):
    monkeypatch.setattr(config.search, "mcp_threshold", 0.31)
    monkeypatch.setattr(config.search, "api_threshold", 0.52)
    seen: list[float] = []
    real = store.search_hybrid

    def spy(*args, **kwargs):
        seen.append(kwargs.get("threshold"))
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "search_hybrid", spy)
    caller = (Caller.from_chain(None, source=source) if source == "resolve"
              else Caller(source=source, record=False))
    search(SearchQuery(query=QUERY), caller)
    assert set(seen) == {getattr(config.search, floor)}
    seen.clear()
    search(SearchQuery(query=QUERY, threshold=0.77), caller)
    assert set(seen) == {0.77}


def test_a_paused_recall_is_refused_before_the_query_is_embedded(stored, monkeypatch):
    from palinode.core.capture_policy import update_capture_policy

    update_capture_policy(recall_paused=True)
    embedded: list[str] = []
    monkeypatch.setattr(embedder, "embed", lambda text, *a, **kw: embedded.append(text))
    with pytest.raises(RecallDenied) as denied:
        search(SearchQuery(query=QUERY), Caller(source="mcp", automatic=True))
    assert denied.value.reason == "recall_paused"
    assert embedded == []


def test_keyword_fallback_is_the_callers_choice_when_the_embedder_is_down(stored, monkeypatch):
    def down(*args, **kwargs):
        raise embedder.EmbeddingUnavailable(backend="local", model="m", text_len=1, cause="down")

    monkeypatch.setattr(embedder, "embed", down)
    with pytest.raises(embedder.EmbeddingUnavailable):
        search(SearchQuery(query=QUERY), Caller(source="api", record=False))
    result = search(SearchQuery(query=QUERY), Caller.from_chain(None, source="resolve",
                                                                keyword_fallback=True))
    assert result.active_mode == "keyword-fallback"


# ── the receipt records the request the route always recorded ───────────────


@pytest.mark.parametrize("body", [
    {"query": QUERY},
    {"query": QUERY, "context": ["project/alpha"], "limit": 4, "types": ["Insight"],
     "include_daily": True, "since_days": 3, "mode": "passive", "session_id": "s1"},
    {"query": QUERY, "context": [], "where": ["status=open"], "resolve": "linked",
     "include_other_projects": True, "tier": "abstract", "max_chars": 50},
    {"query": "", "category": "insights", "type_deny": ["Decision"], "hybrid": False},
])
def test_the_receipts_request_is_the_rest_body_after_resolution(stored, body):
    req = SearchRequest(**body, receipt=True)
    query, caller = _search_inputs(req, None)
    caller = dataclasses.replace(caller, record=False)
    result = search(query, caller)
    expected = req.model_dump(exclude_none=True)
    if req.context is None:
        applied = result.scope.context
        if applied is None:
            expected.pop("context", None)
        else:
            expected["context"] = applied
    assert result.receipt.request == request_scope(expected)


# ── /resolve takes its seeds from the same search ────────────────────────────


def test_resolve_seeds_come_from_core_search_as_the_resolve_surface(stored, monkeypatch):
    from palinode.core.bundle import BundleRequest, build_bundle

    calls: list[tuple[SearchQuery, Caller]] = []
    real = core_search.search

    def spy(query, caller):
        calls.append((query, caller))
        return real(query, caller)

    monkeypatch.setattr(core_search, "search", spy)
    bundle = build_bundle(BundleRequest(query=QUERY), chain=None)
    assert calls, "the bundle did not call core search"
    query, caller = calls[0]
    assert query.query == QUERY
    assert caller.source == "resolve" and caller.resolved and caller.chain is None
    assert not caller.record and caller.keyword_fallback
    assert _recall_count(stored, "kept") == 0
    assert "kept" in json.dumps(bundle.to_dict())
