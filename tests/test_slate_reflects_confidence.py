"""A delivered slate reflects the evidence, not the caller's limit.

Two bounds, both applied in the one ranker every surface reaches through
``/search``:

``search.lexical_fts_threshold``
    The keyword arm's relative floor when it is the ONLY arm (explicit lexical
    retrieval, or the per-input keyword fallback). That path used to hard-set
    the FTS floor to ``0.0``, so a one-answer question still came back with
    ``top_k`` results — four of them padding. ``0.0`` restores that.

``search.max_chunks_per_file``
    How many chunks of one file may occupy the slate ahead of other files'
    competitive chunks. Overflow is deferred, never dropped: it still fills a
    slate that would otherwise come back short, so the cap changes which
    results are delivered and never how many. ``0`` restores the old
    unlimited behaviour.

Neither is an abstention floor. Both are relative to the best candidate in
the same result set, so the top result always survives and no query is
answered with an empty slate that would have had a top hit — abstention is a
separate decision and is deliberately not taken here.

Real SQLite under ``tmp_path`` throughout; the vectors are deterministic
stand-ins and are never consulted on the lexical path under test.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import palinode.mcp as mcp
from palinode.api import server
from palinode.api.routers import search as search_router
from palinode.core import ranker, store
from palinode.core.config import config
from palinode.core.retrieval_log import RetrievalLogger
from tests._store_helpers import upsert_chunks

_DIM = 1024


def _vec(axis: int) -> list[float]:
    v = [0.0] * _DIM
    v[axis % _DIM] = 1.0
    return v


def _fts(file_id: str, score: float, section: str = "root") -> dict:
    return {
        "id": f"{file_id}-{section}",
        "file_path": f"insights/{file_id}.md",
        "section_id": section,
        "content": f"{file_id} {section}",
        "category": "insights",
        "metadata": {},
        "score": score,
    }


def _rank(rows: list[dict], top_k: int = 5, priority_weight: float = 0.0) -> list[str]:
    merged = ranker.rank_hybrid(
        [], rows, top_k=top_k, threshold=0.0, hybrid_weight=1.0,
        priority_weight=priority_weight,
        fts_threshold=config.search.lexical_fts_threshold,
    )
    return [r["id"] for r in merged]


# ── the trailing edge: one answer does not become five ───────────────────────


class TestTrailingCutoff:
    def test_one_strong_answer_plus_only_competitive_results(self):
        # A dominant match, one genuine contender, and three trailing results
        # that share a word with the query and nothing else.
        rows = [
            _fts("answer", 0.44), _fts("contender", 0.30),
            _fts("trail-a", 0.12), _fts("trail-b", 0.06), _fts("trail-c", 0.01),
        ]
        assert _rank(rows) == ["answer-root", "contender-root"]

    def test_a_many_answer_question_still_fills_the_limit(self):
        rows = [_fts(f"ans-{i}", score) for i, score in
                enumerate([0.44, 0.43, 0.41, 0.40, 0.38, 0.05])]
        assert _rank(rows) == [f"ans-{i}-root" for i in range(5)]

    def test_the_top_result_always_survives(self):
        # The floor is relative, so a weak-but-best candidate is still
        # delivered: this is not abstention and must not become it.
        assert _rank([_fts("weak", 0.004)]) == ["weak-root"]

    def test_zero_restores_the_unconditional_fill(self, monkeypatch):
        monkeypatch.setattr(config.search, "lexical_fts_threshold", 0.0)
        rows = [
            _fts("answer", 0.44), _fts("contender", 0.30),
            _fts("trail-a", 0.12), _fts("trail-b", 0.06), _fts("trail-c", 0.01),
        ]
        assert len(_rank(rows)) == 5


# ── the same-file cap ────────────────────────────────────────────────────────


class TestPerFileCap:
    def test_one_file_cannot_take_the_slate_from_competitive_files(self):
        # Uncapped, `big` would take the first three of five slots. Capped, it
        # takes the first — and its next chunk appears only behind every other
        # file, as backfill for a slate that would otherwise be short.
        rows = [
            _fts("big", 0.44, "s1"), _fts("big", 0.43, "s2"), _fts("big", 0.42, "s3"),
            _fts("other-a", 0.41), _fts("other-b", 0.40), _fts("other-c", 0.39),
        ]
        assert _rank(rows) == [
            "big-s1", "other-a-root", "other-b-root", "other-c-root", "big-s2",
        ]

    def test_the_cap_does_not_shrink_what_is_delivered(self, monkeypatch):
        rows = [
            _fts("big", 0.44, "s1"), _fts("big", 0.43, "s2"), _fts("big", 0.42, "s3"),
            _fts("other-a", 0.41), _fts("other-b", 0.40), _fts("other-c", 0.39),
        ]
        capped = _rank(rows)
        monkeypatch.setattr(config.search, "max_chunks_per_file", 0)
        assert len(capped) == len(_rank(rows))

    def test_capped_chunks_still_fill_a_slate_that_would_be_short(self):
        # Nothing else competitive: the cap defers, it does not discard, so the
        # caller is not handed a shorter slate than the evidence supports.
        rows = [_fts("big", 0.44, "s1"), _fts("big", 0.43, "s2"), _fts("big", 0.42, "s3")]
        assert _rank(rows) == ["big-s1", "big-s2", "big-s3"]

    def test_backfill_respects_the_limit(self):
        rows = [_fts("big", 0.44 - i * 0.01, f"s{i}") for i in range(8)]
        assert len(_rank(rows, top_k=5)) == 5

    def test_zero_restores_the_uncapped_slate(self, monkeypatch):
        monkeypatch.setattr(config.search, "max_chunks_per_file", 0)
        rows = [
            _fts("big", 0.44, "s1"), _fts("big", 0.43, "s2"), _fts("big", 0.42, "s3"),
            _fts("other-a", 0.41), _fts("other-b", 0.40), _fts("other-c", 0.39),
        ]
        assert _rank(rows) == ["big-s1", "big-s2", "big-s3", "other-a-root", "other-b-root"]

    def test_a_higher_cap_keeps_that_many(self, monkeypatch):
        monkeypatch.setattr(config.search, "max_chunks_per_file", 2)
        rows = [
            _fts("big", 0.44, "s1"), _fts("big", 0.43, "s2"), _fts("big", 0.42, "s3"),
            _fts("other-a", 0.41), _fts("other-b", 0.40),
        ]
        assert _rank(rows) == ["big-s1", "big-s2", "other-a-root", "other-b-root", "big-s3"]

    def test_the_score_gap_still_drops_a_far_below_chunk(self, monkeypatch):
        # dedup_score_gap discards rather than defers; the cap does not change
        # that, so a chunk it dropped must not reappear through the backfill.
        # The gap compares post-fusion scores, which are rank-derived and
        # therefore nearly equal — the priority nudge is what opens a gap wide
        # enough for it to fire at all.
        monkeypatch.setattr(config.search, "dedup_score_gap", 0.05)
        rows = [_fts("big", 0.44, "s1"), _fts("big", 0.43, "s2")]
        rows[0]["metadata"] = {"priority": 5}
        rows[1]["metadata"] = {"priority": 1}
        assert _rank(rows, priority_weight=0.025) == ["big-s1"]


# ── the same two bounds, through a real store ────────────────────────────────

_ANSWER = (
    "The orionledger retention window for audit exports is ninety days, "
    "set by the orionledger retention review."
)
_CONTENDER = "Retention window sizing for orionledger is revisited each quarter."
#: Each shares exactly one query word, in a context that answers nothing.
_TRAILING = [
    "The window manager crash on the build host was traced to a driver.",
    "Data retention for chat transcripts is handled by the vendor.",
    "A maintenance window is scheduled for the atrium lighting.",
    "Retention of build artifacts follows a rolling schedule.",
    "The window seat request for the offsite was declined.",
]


def _chunk(chunk_id: str, content: str, *, file_id: str | None = None,
           section: str = "root", axis: int = 0) -> dict:
    return {
        "id": chunk_id,
        "file_path": f"insights/{file_id or chunk_id}.md",
        "section_id": section,
        "category": "insights",
        "content": content,
        "metadata": {},
        "created_at": "2026-09-22T00:00:00+00:00",
        "last_updated": "2026-09-22T00:00:00+00:00",
        "embedding": _vec(axis),
    }


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    store.init_db()
    chunks = [_chunk("answer", _ANSWER), _chunk("contender", _CONTENDER)]
    chunks += [_chunk(f"trail-{i}", text, axis=i + 1)
               for i, text in enumerate(_TRAILING)]
    upsert_chunks(chunks, skip_unchanged=False)
    return tmp_path


QUERY = "orionledger retention window"


class TestStoreDeliversTheSlateItHasEvidenceFor:
    def test_a_one_answer_query_does_not_fill_top_k(self, seeded):
        hits = store.search_hybrid(QUERY, None, top_k=5, record_access=False)
        assert [h["id"] for h in hits] == ["answer", "contender"]

    def test_every_delivered_hit_is_competitive_with_the_best(self, seeded):
        candidates = store.search_fts(QUERY, top_k=10)
        best = max(c["score"] for c in candidates)
        delivered = {h["id"] for h in
                     store.search_hybrid(QUERY, None, top_k=5, record_access=False)}
        floor = config.search.lexical_fts_threshold * best
        assert delivered == {c["id"] for c in candidates if c["score"] >= floor}

    def test_config_restores_the_padded_slate(self, seeded, monkeypatch):
        monkeypatch.setattr(config.search, "lexical_fts_threshold", 0.0)
        hits = store.search_hybrid(QUERY, None, top_k=5, record_access=False)
        assert len(hits) == 5
        assert hits[0]["id"] == "answer"

    def test_an_explicit_floor_still_wins_over_the_default(self, seeded):
        hits = store.search_hybrid(QUERY, None, top_k=5, record_access=False,
                                   fts_threshold=0.0)
        assert len(hits) == 5

    def test_recall_of_every_relevant_record_survives_the_floor(self, seeded):
        # The three records that genuinely answer the query are all delivered;
        # the cutoff removes padding, not answers.
        wide = store.search_hybrid(QUERY, None, top_k=10, record_access=False)
        assert {"answer", "contender"} <= {h["id"] for h in wide}


@pytest.fixture()
def sectioned(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    store.init_db()
    upsert_chunks([
        _chunk("weekly-1", _ANSWER, file_id="weekly", section="s1"),
        _chunk("weekly-2", _CONTENDER, file_id="weekly", section="s2"),
        _chunk("weekly-3", "Retention window follow-ups for orionledger stay open.",
               file_id="weekly", section="s3"),
        _chunk("other", "The orionledger retention window is confirmed at ninety days.",
               axis=2),
    ], skip_unchanged=False)
    return tmp_path


class TestStoreCapsOneFile:
    def test_a_sectioned_file_does_not_crowd_out_another_record(self, sectioned):
        hits = store.search_hybrid(QUERY, None, top_k=5, record_access=False)
        paths = [h["file_path"] for h in hits]
        assert paths[:2] == ["insights/weekly.md", "insights/other.md"]

    def test_the_cap_never_shortens_the_slate(self, sectioned, monkeypatch):
        capped = store.search_hybrid(QUERY, None, top_k=5, record_access=False)
        monkeypatch.setattr(config.search, "max_chunks_per_file", 0)
        uncapped = store.search_hybrid(QUERY, None, top_k=5, record_access=False)
        assert len(capped) == len(uncapped)
        assert {h["id"] for h in capped} == {h["id"] for h in uncapped}

    def test_config_restores_the_uncapped_order(self, sectioned, monkeypatch):
        monkeypatch.setattr(config.search, "max_chunks_per_file", 0)
        hits = store.search_hybrid(QUERY, None, top_k=5, record_access=False)
        assert [h["file_path"] for h in hits][:2] == ["insights/weekly.md"] * 2


# ── every surface, and the rows that describe the delivery ───────────────────


@pytest.fixture()
def lexical_api(tmp_path, monkeypatch):
    """A lexical-mode API over a real store, with its own retrieval log."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.search, "retrieval_mode", "lexical")
    store.init_db()
    chunks = [_chunk("answer", _ANSWER), _chunk("contender", _CONTENDER)]
    chunks += [_chunk(f"trail-{i}", text, axis=i + 1)
               for i, text in enumerate(_TRAILING)]
    upsert_chunks(chunks, skip_unchanged=False)
    for name in ("answer", "contender", *[f"trail-{i}" for i in range(len(_TRAILING))]):
        (tmp_path / "insights").mkdir(exist_ok=True)
        (tmp_path / "insights" / f"{name}.md").write_text("placeholder\n", encoding="utf-8")
    logger = RetrievalLogger(str(tmp_path), enabled=True)
    monkeypatch.setattr(search_router, "_retrieval_logger", logger)
    server._rate_counters.clear()
    with TestClient(server.app, raise_server_exceptions=False) as client:
        yield client, tmp_path, logger


def _log_rows(logger: RetrievalLogger) -> list[dict]:
    path: Path | None = logger.log_path
    if path is None or not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestSurfacesAndReceipts:
    def test_the_api_returns_the_short_slate(self, lexical_api):
        client, _, _ = lexical_api
        body = client.post("/search", json={"query": QUERY, "limit": 5, "receipt": True})
        assert body.status_code == 200, body.text
        results = body.json()["results"]
        assert [r["id"] for r in results] == ["answer", "contender"]

    def test_every_surface_delivers_the_same_slate(self, lexical_api, monkeypatch):
        client, _, _ = lexical_api
        direct = [h["id"] for h in
                  store.search_hybrid(QUERY, None, top_k=5, record_access=False)]

        api = [r["id"] for r in
               client.post("/search", json={"query": QUERY, "limit": 5}).json()]

        from palinode.cli._api import PalinodeAPI

        # The TestClient is an httpx.Client bound to the in-process app, so the
        # CLI's own adapter drives the real surface rather than a stand-in.
        cli = [r["id"] for r in PalinodeAPI(client=client).search(QUERY, limit=5)]

        async def _post(path, json=None, timeout=30.0):  # noqa: A002 - mirrors mcp._post
            return client.post(path, json=json)

        monkeypatch.setattr(mcp, "_post", _post)
        rendered = asyncio.run(mcp._dispatch_tool("palinode_search", {"query": QUERY}))[0].text

        assert direct == ["answer", "contender"]
        assert api == direct
        assert cli == direct
        assert "orionledger retention window for audit exports" in rendered
        assert "window manager crash" not in rendered

    def test_receipt_and_log_rows_match_what_was_delivered(self, lexical_api):
        client, _, logger = lexical_api
        payload = client.post(
            "/search", json={"query": QUERY, "limit": 5, "receipt": True, "resolve": "linked"},
        ).json()
        results = payload["results"]
        supplied = payload["receipt"]["supplied"]
        rows = [row for row in _log_rows(logger) if row.get("file_path")]
        assert len(results) == 2
        assert len(supplied) == len(results)
        assert len(rows) == len(results)
        assert [row["file_path"] for row in rows] == [r["file_path"] for r in results]

    def test_the_padded_slate_logs_the_rows_it_delivered(self, lexical_api, monkeypatch):
        monkeypatch.setattr(config.search, "lexical_fts_threshold", 0.0)
        client, _, logger = lexical_api
        results = client.post("/search", json={"query": QUERY, "limit": 5}).json()
        rows = [row for row in _log_rows(logger) if row.get("file_path")]
        assert len(results) == 5
        assert len(rows) == 5
