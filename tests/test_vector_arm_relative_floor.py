"""The vector arm is bounded against its own best match, not just the floor.

``search.mcp_threshold`` / ``search.api_threshold`` are ABSOLUTE cosine floors:
they decide whether a candidate is plausible at all, and say nothing about
whether it is plausible beside the match the query actually found. With only
those, the arm hands fusion every one of its ``top_k * 2`` candidates above the
floor, and weak neighbours fill whatever the strong match leaves.

``search.vector_relative_floor`` (default 0.85) is the vector arm's
counterpart to ``fts_threshold``: a candidate survives when its cosine is at
least that fraction of the best cosine in the same candidate set. Relative to
the best match, so the top candidate always clears it — this bounds a slate and
can never empty one. ``0.0`` restores the unbounded arm.

Real SQLite under ``tmp_path`` throughout. The vectors are deterministic
stand-ins built at an exact cosine to the query vector, so every cutoff below
is arithmetic rather than a property of some embedding model.
"""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import palinode.mcp as mcp
from palinode.api import server
from palinode.api.routers import search as search_router
from palinode.core import embedder, ranker, store
from palinode.core.config import config
from palinode.core.retrieval_log import RetrievalLogger
from tests._store_helpers import upsert_chunks

_DIM = 1024


def _query_vec() -> list[float]:
    v = [0.0] * _DIM
    v[0] = 1.0
    return v


def _at_cosine(cosine: float, axis: int) -> list[float]:
    """A unit vector whose cosine with the query vector is exactly *cosine*."""
    v = [0.0] * _DIM
    v[0] = cosine
    v[axis] = math.sqrt(max(0.0, 1.0 - cosine * cosine))
    return v


def _vec_row(id_: str, cosine: float) -> dict:
    """A vector-arm candidate as ``store.search`` returns one."""
    return {
        "id": id_,
        "file_path": f"insights/{id_}.md",
        "section_id": "root",
        "content": id_,
        "category": "insights",
        "metadata": {},
        "score": cosine,
        "raw_score": cosine,
    }


def _fts_row(id_: str, score: float) -> dict:
    return {
        "id": id_,
        "file_path": f"insights/{id_}.md",
        "section_id": "root",
        "content": id_,
        "category": "insights",
        "metadata": {},
        "score": score,
    }


def _rank(vec_rows: list[dict], fts_rows: list[dict] | None = None,
          *, top_k: int = 5, threshold: float = 0.4) -> list[str]:
    merged = ranker.rank_hybrid(
        vec_rows, fts_rows or [], top_k=top_k, threshold=threshold,
        hybrid_weight=0.5, priority_weight=0.0,
    )
    return [r["id"] for r in merged]


# ── the arm, on its own scale ────────────────────────────────────────────────


class TestTheVectorArmsRelativeFloor:
    def test_weak_neighbours_do_not_ride_in_behind_a_strong_match(self):
        # Every one of these clears the absolute floor (0.4); only the first is
        # competitive with the match this query found.
        rows = [_vec_row("answer", 0.92), _vec_row("near-a", 0.70),
                _vec_row("near-b", 0.62), _vec_row("near-c", 0.45)]
        assert _rank(rows) == ["answer"]

    def test_near_equal_matches_all_survive(self):
        rows = [_vec_row(f"ans-{i}", cosine) for i, cosine in
                enumerate([0.92, 0.90, 0.88, 0.85, 0.80])]
        assert _rank(rows) == [f"ans-{i}" for i in range(5)]

    def test_the_floor_is_a_fraction_of_the_best_cosine(self, monkeypatch):
        monkeypatch.setattr(config.search, "vector_relative_floor", 0.9)
        rows = [_vec_row("top", 0.80), _vec_row("keep", 0.74), _vec_row("drop", 0.71)]
        assert _rank(rows) == ["top", "keep"]

    def test_the_top_candidate_always_survives(self):
        # The only candidate is weak in absolute terms but is the best there
        # is: a relative floor cannot abstain, and must not start here.
        assert _rank([_vec_row("weak", 0.41)]) == ["weak"]

    def test_an_explicit_value_wins_over_the_configured_one(self):
        rows = [_vec_row("answer", 0.92), _vec_row("near-a", 0.70)]
        merged = ranker.rank_hybrid(
            rows, [], top_k=5, threshold=0.4, hybrid_weight=0.5,
            priority_weight=0.0, vector_relative_floor=0.0,
        )
        assert [r["id"] for r in merged] == ["answer", "near-a"]

    def test_zero_restores_the_unbounded_arm(self, monkeypatch):
        monkeypatch.setattr(config.search, "vector_relative_floor", 0.0)
        rows = [_vec_row("answer", 0.92), _vec_row("near-a", 0.70),
                _vec_row("near-b", 0.62), _vec_row("near-c", 0.45)]
        assert _rank(rows) == ["answer", "near-a", "near-b", "near-c"]

    def test_the_absolute_floor_still_decides_on_its_own(self):
        # 0.50 is 0.91 of the best cosine here — competitive — and still below
        # the caller's absolute floor. The two floors answer different
        # questions and neither substitutes for the other.
        rows = [_vec_row("top", 0.55), _vec_row("implausible", 0.50)]
        assert _rank(rows, threshold=0.52) == ["top"]


class TestTheKeywordArmIsUntouched:
    def test_a_keyword_candidate_is_judged_on_its_own_scale(self):
        # Normalized BM25 sits far below any cosine; the vector arm's floor
        # must not reach across to it.
        fts = [_fts_row("cve", 0.13), _fts_row("adjacent", 0.12)]
        assert _rank([_vec_row("answer", 0.92)], fts) == ["answer", "cve", "adjacent"]

    def test_a_row_the_vector_arm_dropped_still_enters_through_keywords(self):
        # One arm is enough to admit a candidate — that is why each floor runs
        # on its own arm, before fusion.
        vec = [_vec_row("answer", 0.92), _vec_row("identifier", 0.55)]
        assert _rank(vec, [_fts_row("identifier", 0.30)]) == ["answer", "identifier"]

    def test_the_keyword_only_slate_is_unaffected(self, monkeypatch):
        fts = [_fts_row("top", 0.30), _fts_row("keep", 0.13), _fts_row("drop", 0.10)]
        with_floor = _rank([], fts)
        monkeypatch.setattr(config.search, "vector_relative_floor", 0.0)
        assert with_floor == _rank([], fts) == ["top", "keep"]


# ── the same floor, through a real store ─────────────────────────────────────

_ANSWER = (
    "The orionledger retention window for audit exports is ninety days, set by "
    "the orionledger retention review."
)
#: Vocabulary disjoint from the query, so the keyword arm never retrieves
#: them: what they are delivered on is the vector arm, and nothing else.
_NEIGHBOURS = [
    "The atrium lighting refit is scheduled around the quarterly close.",
    "Build artifacts roll off the cache host on a fixed schedule.",
    "A driver regression on the packaging host was traced and reverted.",
    "Seat allocation for the offsite is handled by the venue.",
]

QUERY = "orionledger retention window for audit exports"


def _chunk(chunk_id: str, content: str, cosine: float, axis: int,
           *, file_id: str | None = None, section: str = "root") -> dict:
    return {
        "id": chunk_id,
        "file_path": f"insights/{file_id or chunk_id}.md",
        "section_id": section,
        "category": "insights",
        "content": content,
        "metadata": {},
        "created_at": "2026-09-22T00:00:00+00:00",
        "last_updated": "2026-09-22T00:00:00+00:00",
        "embedding": _at_cosine(cosine, axis),
    }


def _memory_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)


@pytest.fixture()
def one_strong_match(tmp_path, monkeypatch):
    """One answer at cosine 0.92; four neighbours from 0.72 down to 0.52.

    Every neighbour clears both shipped absolute floors (0.40 MCP, 0.50 API),
    so what keeps them out below is the relative floor and nothing else.
    """
    _memory_dir(tmp_path, monkeypatch)
    store.init_db()
    chunks = [_chunk("answer", _ANSWER, 0.92, 1)]
    chunks += [_chunk(f"near-{i}", text, cosine, i + 2)
               for i, (text, cosine) in
               enumerate(zip(_NEIGHBOURS, [0.72, 0.66, 0.58, 0.52], strict=True))]
    upsert_chunks(chunks, skip_unchanged=False)
    return tmp_path


class TestStoreDeliversTheCompetitiveSlate:
    def test_weak_neighbours_do_not_fill_the_limit(self, one_strong_match):
        hits = store.search_hybrid(QUERY, _query_vec(), top_k=5, threshold=0.4,
                                   record_access=False)
        assert [h["id"] for h in hits] == ["answer"]

    def test_every_delivered_hit_is_competitive_with_the_best(self, one_strong_match):
        candidates = store.search(_query_vec(), top_k=10, threshold=0.0,
                                  record_access=False)
        best = max(c["raw_score"] for c in candidates)
        floor = config.search.vector_relative_floor * best
        delivered = {h["id"] for h in
                     store.search_hybrid(QUERY, _query_vec(), top_k=5, threshold=0.4,
                                         record_access=False)}
        assert delivered == {c["id"] for c in candidates if c["raw_score"] >= floor}

    def test_config_restores_the_padded_slate(self, one_strong_match, monkeypatch):
        monkeypatch.setattr(config.search, "vector_relative_floor", 0.0)
        hits = store.search_hybrid(QUERY, _query_vec(), top_k=5, threshold=0.4,
                                   record_access=False)
        assert len(hits) == 5
        assert hits[0]["id"] == "answer"

    def test_the_vector_only_path_is_bounded_the_same_way(self, one_strong_match):
        hits = store.search_hybrid(QUERY, _query_vec(), top_k=5, threshold=0.4,
                                   record_access=False, use_fts=False)
        assert [h["id"] for h in hits] == ["answer"]

    def test_a_caller_can_keep_the_unbounded_arm(self, one_strong_match):
        # The resolver callers whose top hit is a record they discard pass
        # 0.0 through the same search path rather than reading a second one.
        hits = store.search_hybrid(QUERY, _query_vec(), top_k=5, threshold=0.4,
                                   record_access=False, vector_relative_floor=0.0)
        assert len(hits) == 5

    def test_the_lexical_path_is_unchanged(self, one_strong_match, monkeypatch):
        bounded = [h["id"] for h in
                   store.search_hybrid(QUERY, None, top_k=5, record_access=False)]
        monkeypatch.setattr(config.search, "vector_relative_floor", 0.0)
        assert bounded == [h["id"] for h in
                           store.search_hybrid(QUERY, None, top_k=5, record_access=False)]
        assert bounded == ["answer"]


@pytest.fixture()
def near_equal_matches(tmp_path, monkeypatch):
    """Five records the query matches about equally well."""
    _memory_dir(tmp_path, monkeypatch)
    store.init_db()
    upsert_chunks([
        _chunk(f"ans-{i}", f"{_ANSWER} Confirmation {i}.", cosine, i + 1)
        for i, cosine in enumerate([0.92, 0.90, 0.88, 0.86, 0.84])
    ], skip_unchanged=False)
    return tmp_path


class TestStoreStillFillsTheLimitWhenItCan:
    def test_near_equal_matches_are_all_delivered(self, near_equal_matches):
        hits = store.search_hybrid(QUERY, _query_vec(), top_k=5, threshold=0.4,
                                   record_access=False)
        assert len(hits) == 5

    def test_the_limit_is_still_the_ceiling(self, near_equal_matches):
        hits = store.search_hybrid(QUERY, _query_vec(), top_k=3, threshold=0.4,
                                   record_access=False)
        assert len(hits) == 3


@pytest.fixture()
def keyword_rescued(tmp_path, monkeypatch):
    """A record the vector arm ranks far below the top, with the exact terms."""
    _memory_dir(tmp_path, monkeypatch)
    store.init_db()
    upsert_chunks([
        _chunk("answer", _ANSWER, 0.92, 1),
        _chunk("identifier",
               "PLNC-4471 sets the orionledger retention window for audit exports.",
               0.50, 2),
        _chunk("neighbour", _NEIGHBOURS[0], 0.70, 3),
    ], skip_unchanged=False)
    return tmp_path


class TestOneArmIsEnough:
    def test_the_keyword_arm_re_admits_what_the_vector_floor_dropped(self, keyword_rescued):
        hits = store.search_hybrid(QUERY, _query_vec(), top_k=5, threshold=0.4,
                                   record_access=False)
        assert {h["id"] for h in hits} == {"answer", "identifier"}


# ── every surface, and the rows that describe the delivery ───────────────────


@pytest.fixture()
def hybrid_api(one_strong_match, monkeypatch):
    """A hybrid-mode API over the real store, with its own retrieval log."""
    monkeypatch.setattr(embedder, "embed", lambda text, *a, **kw: _query_vec())
    monkeypatch.setattr(search_router.embedder, "embed",
                        lambda text, *a, **kw: _query_vec())
    for name in ("answer", *[f"near-{i}" for i in range(len(_NEIGHBOURS))]):
        (one_strong_match / "insights").mkdir(exist_ok=True)
        (one_strong_match / "insights" / f"{name}.md").write_text(
            "placeholder\n", encoding="utf-8")
    logger = RetrievalLogger(str(one_strong_match), enabled=True)
    monkeypatch.setattr(search_router, "_retrieval_logger", logger)
    server._rate_counters.clear()
    with TestClient(server.app, raise_server_exceptions=False) as client:
        yield client, one_strong_match, logger


def _log_rows(logger: RetrievalLogger) -> list[dict]:
    path: Path | None = logger.log_path
    if path is None or not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestSurfacesAndReceipts:
    def test_the_api_returns_the_bounded_slate(self, hybrid_api):
        client, _, _ = hybrid_api
        body = client.post("/search", json={"query": QUERY, "limit": 5})
        assert body.status_code == 200, body.text
        assert [r["id"] for r in body.json()] == ["answer"]

    def test_every_surface_delivers_the_same_slate(self, hybrid_api, monkeypatch):
        client, _, _ = hybrid_api
        direct = [h["id"] for h in
                  store.search_hybrid(QUERY, _query_vec(), top_k=5,
                                      threshold=config.search.api_threshold,
                                      record_access=False)]
        api = [r["id"] for r in
               client.post("/search", json={"query": QUERY, "limit": 5}).json()]

        from palinode.cli._api import PalinodeAPI

        cli = [r["id"] for r in PalinodeAPI(client=client).search(QUERY, limit=5)]

        async def _post(path, json=None, timeout=30.0):  # noqa: A002 - mirrors mcp._post
            return client.post(path, json=json)

        monkeypatch.setattr(mcp, "_post", _post)
        rendered = asyncio.run(mcp._dispatch_tool("palinode_search", {"query": QUERY}))[0].text

        assert direct == ["answer"]
        assert api == direct
        assert cli == direct
        assert "orionledger retention window for audit exports" in rendered
        assert "atrium lighting refit" not in rendered

    def test_receipt_and_log_rows_match_what_was_delivered(self, hybrid_api):
        client, _, logger = hybrid_api
        payload = client.post(
            "/search",
            json={"query": QUERY, "limit": 5, "receipt": True, "resolve": "linked"},
        ).json()
        results = payload["results"]
        rows = [row for row in _log_rows(logger) if row.get("file_path")]
        assert len(results) == 1
        assert len(payload["receipt"]["supplied"]) == len(results)
        assert len(rows) == len(results)
        assert [row["file_path"] for row in rows] == [r["file_path"] for r in results]

    def test_the_unbounded_arm_logs_the_rows_it_delivered(self, hybrid_api, monkeypatch):
        monkeypatch.setattr(config.search, "vector_relative_floor", 0.0)
        client, _, logger = hybrid_api
        results = client.post("/search", json={"query": QUERY, "limit": 5}).json()
        rows = [row for row in _log_rows(logger) if row.get("file_path")]
        assert len(results) == 5
        assert len(rows) == 5
