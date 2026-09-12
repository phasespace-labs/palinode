"""The FTS arm's floor is relative to its best candidate, not the cosine threshold.

Once questions reached the BM25 arm at all (the OR-join fix), the per-arm
floor in ``rank_hybrid`` still applied the caller's cosine ``threshold``
(0.4 MCP / 0.5 API) to normalized BM25 (``|bm25| / 25``) — a different scale
whose magnitude also moves with corpus size. Measured on the 54-pair band rig:
the true chunk is the top keyword match in 51/54 pairs but only 43% of those
scores clear 0.4, and every single-identifier hit (``CVE-…``, ``SOW-…``,
``PLNC-…``) sits at 0.12–0.13. So at production defaults the arm fired and
the floor threw its hits away before fusion.

``config.search.fts_threshold`` (default 0.4) is now a fraction of the top
FTS score in the result set; ``rank_hybrid(fts_threshold=...)`` overrides it,
``0.0`` disables the FTS floor. Real SQLite + tmp_path for the store test.
"""
from __future__ import annotations

import pytest

from palinode.core import ranker, store
from palinode.core.config import config
from tests._store_helpers import upsert_chunks


def _fts(id_: str, score: float, **extra) -> dict:
    return {"id": id_, "file_path": f"insights/{id_}.md", "section_id": "root", "content": id_,
            "category": "insights", "metadata": {}, "score": score, **extra}


class TestRankHybridRelativeFloor:
    def test_top_fts_candidate_always_survives_the_cosine_threshold(self):
        # 0.13 is where a single-identifier BM25 hit lands; the cosine floor is 0.4.
        merged = ranker.rank_hybrid([], [_fts("cve", 0.13)], top_k=5, threshold=0.4,
                                    hybrid_weight=0.5, priority_weight=0.0)
        assert [r["id"] for r in merged] == ["cve"]

    def test_floor_is_a_fraction_of_the_top_score(self, monkeypatch):
        monkeypatch.setattr(config.search, "fts_threshold", 0.4)
        rows = [_fts("top", 0.30), _fts("keep", 0.13), _fts("drop", 0.10)]   # 0.13 ≥ 0.12, 0.10 < 0.12
        merged = ranker.rank_hybrid([], rows, top_k=5, threshold=0.4, hybrid_weight=0.5, priority_weight=0.0)
        assert [r["id"] for r in merged] == ["top", "keep"]

    def test_explicit_override_and_zero_disables(self):
        rows = [_fts("top", 0.30), _fts("low", 0.05)]
        strict = ranker.rank_hybrid([], rows, top_k=5, threshold=0.4, hybrid_weight=0.5,
                                    priority_weight=0.0, fts_threshold=0.9)
        assert [r["id"] for r in strict] == ["top"]
        none = ranker.rank_hybrid([], rows, top_k=5, threshold=0.4, hybrid_weight=0.5,
                                  priority_weight=0.0, fts_threshold=0.0)
        assert [r["id"] for r in none] == ["top", "low"]

    def test_fts_only_rows_stay_exempt(self, monkeypatch):
        monkeypatch.setattr(config.search, "fts_threshold", 0.4)
        rows = [_fts("top", 0.30), _fts("novec", 0.02, has_vector=False)]
        merged = ranker.rank_hybrid([], rows, top_k=5, threshold=0.4, hybrid_weight=0.5, priority_weight=0.0)
        assert {r["id"] for r in merged} == {"top", "novec"}

    def test_old_single_floor_behaviour_is_reproducible(self):
        # What production did before: the cosine threshold applied to BM25.
        merged = ranker.rank_hybrid([], [_fts("cve", 0.13)], top_k=5, threshold=0.4,
                                    hybrid_weight=0.5, priority_weight=0.0, fts_threshold=0.4 / 0.13 + 1)
        assert merged == []


# --------------------------------------------------------------------- store
_DIM = 1024


def _vec(axis: int) -> list[float]:
    v = [0.0] * _DIM
    v[axis] = 1.0
    return v


def _chunk(chunk_id: str, content: str, emb: list[float]) -> dict:
    return {"id": chunk_id, "file_path": f"insights/{chunk_id}.md", "section_id": "root",
            "category": "insights", "content": content, "metadata": {},
            "created_at": "2026-09-08T00:00:00+00:00", "last_updated": "2026-09-08T00:00:00+00:00",
            "embedding": emb}


@pytest.fixture()
def store_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    store.init_db()
    # The query embeds on axis 0. The identifier chunk sits on axis 1 (cosine 0
    # — the vector arm cannot vouch for it); the distractors share the query's
    # axis (cosine 1) and say nothing about the identifier.
    upsert_chunks([
        _chunk("cve", "Patched CVE-2026-31889 in the HTTP transport; the advisory landed on Tuesday.", _vec(1)),
        _chunk("d1", "Consolidation skips a group when none of its facts changed since the last pass.", _vec(0)),
        _chunk("d2", "The 2026 roadmap moves the signed-ledger milestone to a later release.", _vec(0)),
        _chunk("d3", "The user's golden retriever is a friendly dog breed.", _vec(0)),
    ], skip_unchanged=False)
    return tmp_path


class TestIdentifierHitSurvivesAtDefaults:
    def test_identifier_reaches_fusion_at_mcp_threshold(self, store_db):
        merged = store.search_hybrid("CVE-2026-31889", _vec(0), top_k=3, threshold=config.search.mcp_threshold)
        assert any(r["id"] == "cve" for r in merged), [r["id"] for r in merged]

    def test_the_old_single_floor_dropped_it(self, store_db):
        # Reproduce the pre-fix behaviour by flooring FTS at the cosine threshold's
        # magnitude: the 0.13 keyword hit never reached RRF.
        merged = store.search_hybrid("CVE-2026-31889", _vec(0), top_k=3,
                                     threshold=config.search.mcp_threshold, fts_threshold=10.0)
        assert not any(r["id"] == "cve" for r in merged)

    def test_vector_floor_still_applies_to_the_vector_arm(self, store_db):
        # Cosine 0 for "cve" on the vector arm: it must come in through FTS only,
        # and a chunk with neither a keyword match nor cosine ≥ threshold stays out.
        merged = store.search_hybrid("CVE-2026-31889", _vec(2), top_k=5, threshold=config.search.mcp_threshold)
        assert [r["id"] for r in merged] == ["cve"]
