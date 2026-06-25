"""Tests for the recall-feedback loop (ADR-006/007, #371).

On every retrieval hit, access metadata must be written back to the ``chunks``
table: ``recall_count`` increments, ``last_recalled`` is stamped (tz-aware UTC
ISO-8601), and ``importance`` is nudged upward (bounded reinforcement).

The production audit (2026-05-29) showed the hook point fired (audit log) but
never wrote back — ``MAX(recall_count)=0``, ``last_recalled`` uniformly NULL.
Root cause: ``search()`` / ``search_fts()`` omitted the chunk ``id`` from their
result dicts, so the write-back in ``search_hybrid`` no-oped.

All tests use real SQLite in tmp_path (no mocked DB — repo rule). Only the
embedder is bypassed by inserting deterministic unit vectors directly.
"""
from __future__ import annotations

import math
import os
from datetime import UTC, datetime

import pytest

from palinode.core import store
from palinode.core.config import config
from palinode.diagnostics.checks.recall_write_health import recall_write_health
from palinode.diagnostics.types import DoctorContext

EMBED_DIM = 1024


def _normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n else vec


def _embedding(seed: int) -> list[float]:
    """Deterministic near-orthogonal unit vector keyed by ``seed``."""
    vec = [0.0] * EMBED_DIM
    vec[seed % EMBED_DIM] = 0.9
    vec[(seed * 7 + 3) % EMBED_DIM] = 0.4
    return _normalize(vec)


def _index_chunk(*, chunk_id: str, file_path: str, content: str, seed: int) -> None:
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    store.upsert_chunks([{
        "id": chunk_id,
        "file_path": file_path,
        "section_id": None,
        "category": "insights",
        "content": content,
        "metadata": {},
        "created_at": now_iso,
        "last_updated": now_iso,
        "embedding": _embedding(seed),
    }])


def _meta(chunk_id: str) -> tuple[int, str | None, float | None]:
    """Return (recall_count, last_recalled, importance) for a chunk id."""
    db = store.get_db()
    try:
        row = db.execute(
            "SELECT recall_count, last_recalled, importance FROM chunks WHERE id = ?",
            (chunk_id,),
        ).fetchone()
    finally:
        db.close()
    return row["recall_count"], row["last_recalled"], row["importance"]


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Point config at a fresh tmp dir + real SQLite DB. No git, no Ollama."""
    memory_dir = str(tmp_path)
    db_path = os.path.join(memory_dir, ".palinode.db")
    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config, "db_path", db_path)
    monkeypatch.setattr(config.git, "auto_commit", False)
    store._db_checked = False
    for d in ("insights", "projects"):
        os.makedirs(os.path.join(memory_dir, d), exist_ok=True)
    store.init_db()
    yield memory_dir
    store._db_checked = False


# ── (a) a search stamps exactly the hit chunks ───────────────────────────────

def test_search_stamps_only_hit_chunks(_isolated_env):
    """A vector search increments recall_count + stamps last_recalled on the
    hit chunk only — an unrelated chunk stays at the schema defaults."""
    _index_chunk(chunk_id="hit", file_path="insights/hit.md", content="alpha topic", seed=1)
    _index_chunk(chunk_id="miss", file_path="insights/miss.md", content="beta topic", seed=900)

    # Query close to "hit" (seed=1); threshold excludes the orthogonal "miss".
    results = store.search(query_embedding=_embedding(1), threshold=0.5)
    hit_ids = {r["id"] for r in results}
    assert "hit" in hit_ids
    assert "miss" not in hit_ids

    rc_hit, lr_hit, _ = _meta("hit")
    assert rc_hit == 1
    assert lr_hit is not None
    # Timestamp is tz-aware UTC ISO-8601 and parses.
    datetime.fromisoformat(lr_hit.replace("Z", "+00:00"))

    rc_miss, lr_miss, _ = _meta("miss")
    assert rc_miss == 0
    assert lr_miss is None


def test_search_hybrid_stamps_hit_chunk(_isolated_env):
    """Parity: the hybrid path (vector + BM25 RRF) also stamps its merged hits,
    and does not double-count via the inner vector pass."""
    _index_chunk(chunk_id="h1", file_path="insights/h1.md", content="gamma keyword", seed=2)

    results = store.search_hybrid(
        query_text="gamma keyword",
        query_embedding=_embedding(2),
        top_k=5,
        threshold=0.0,
    )
    assert any(r.get("id") == "h1" for r in results)

    rc, lr, _ = _meta("h1")
    assert rc == 1  # exactly one increment, not two (inner search suppressed)
    assert lr is not None


# ── (b) repeated recall accumulates ──────────────────────────────────────────

def test_repeated_recall_accumulates(_isolated_env):
    _index_chunk(chunk_id="rep", file_path="insights/rep.md", content="delta topic", seed=3)

    for expected in (1, 2, 3):
        store.search(query_embedding=_embedding(3), threshold=0.5)
        rc, lr, _ = _meta("rep")
        assert rc == expected
        assert lr is not None


# ── (c) importance nudge matches ADR-007 reading ─────────────────────────────
# NOTE: ADR-007 §3.2 deduplicates the importance nudge per (chunk, session_id).
# These tests pass a fresh session_id per recall so each search is a *distinct*
# demand event (the session-dedup gate is exercised in test_adr007_demand_decay).

def test_importance_nudge_per_recall(_isolated_env):
    """Each distinct-session explicit recall reinforces importance by the
    ADR-007 exponential approach: importance += (cap - importance) * alpha.
    Defaults: base=0.5, cap=0.95, alpha=0.08."""
    _index_chunk(chunk_id="imp", file_path="insights/imp.md", content="epsilon topic", seed=4)

    _, _, imp0 = _meta("imp")
    assert imp0 == pytest.approx(0.5)  # schema default / base

    store.search(query_embedding=_embedding(4), threshold=0.5, session_id="s-1")
    _, _, imp1 = _meta("imp")
    # 0.5 + (0.95 - 0.5) * 0.08 = 0.536
    assert imp1 == pytest.approx(0.536, abs=1e-3)

    store.search(query_embedding=_embedding(4), threshold=0.5, session_id="s-2")
    _, _, imp2 = _meta("imp")
    # 0.536 + (0.95 - 0.536) * 0.08 = 0.56912
    assert imp2 == pytest.approx(0.56912, abs=1e-3)


def test_importance_nudge_respects_cap(_isolated_env):
    """Exponential approach is self-limiting: importance never exceeds the cap
    no matter how many distinct-session demands accumulate."""
    _index_chunk(chunk_id="cap", file_path="insights/cap.md", content="zeta topic", seed=5)

    for i in range(200):
        store.search(query_embedding=_embedding(5), threshold=0.5, session_id=f"s-{i}")

    _, _, imp = _meta("cap")
    assert imp <= 0.95 + 1e-9
    assert imp == pytest.approx(0.95, abs=1e-3)  # saturates at, never past, cap


# ── read path: recall recorded against all chunks of a file ──────────────────

def test_record_recall_for_paths_stamps_file_chunks(_isolated_env):
    _index_chunk(chunk_id="f1-a", file_path="insights/file1.md", content="section a", seed=6)
    _index_chunk(chunk_id="f1-b", file_path="insights/file1.md", content="section b", seed=7)
    _index_chunk(chunk_id="f2", file_path="insights/file2.md", content="other", seed=8)

    updated = store.record_recall_for_paths(["insights/file1.md"])
    assert updated == 2

    for cid in ("f1-a", "f1-b"):
        rc, lr, _ = _meta(cid)
        assert rc == 1 and lr is not None

    rc2, lr2, _ = _meta("f2")
    assert rc2 == 0 and lr2 is None


# ── (d) a metadata-write failure does not fail the search ────────────────────

def test_search_survives_recall_write_failure(_isolated_env, monkeypatch):
    """A forced error in the recall write must NOT propagate — the search still
    returns its results (the read path is latency-sensitive). The failure is
    injected inside the write (real record_recall's try/except must catch it),
    not by replacing record_recall, so we exercise the real resilience boundary."""
    _index_chunk(chunk_id="ok", file_path="insights/ok.md", content="eta topic", seed=9)

    real_get_db = store.get_db
    calls = {"n": 0}

    def _failing_get_db():
        # search() opens the DB for its query first (let that through), then
        # record_recall opens it again for the UPDATE (make that one fail).
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("simulated DB write failure")
        return real_get_db()

    monkeypatch.setattr(store, "get_db", _failing_get_db)

    # If record_recall leaked the failure, this would raise.
    results = store.search(query_embedding=_embedding(9), threshold=0.5)
    assert any(r["id"] == "ok" for r in results)


def test_record_recall_swallows_db_error(_isolated_env, monkeypatch):
    """record_recall itself must swallow a DB failure and return 0."""
    _index_chunk(chunk_id="swallow", file_path="insights/swallow.md", content="theta", seed=10)

    def _boom():
        raise RuntimeError("simulated get_db failure")

    monkeypatch.setattr(store, "get_db", _boom)
    assert store.record_recall(["swallow"]) == 0


def test_record_recall_empty_is_noop(_isolated_env):
    assert store.record_recall([]) == 0
    assert store.record_recall([None, ""]) == 0
    assert store.record_recall_for_paths([]) == 0


# ── (e) doctor reports recall-write health ───────────────────────────────────

def test_doctor_flags_severed_loop(_isolated_env):
    """Chunks indexed but never recalled → check fails (loop severed)."""
    _index_chunk(chunk_id="cold", file_path="insights/cold.md", content="iota", seed=11)

    result = recall_write_health(DoctorContext(config=config))
    assert result.passed is False
    assert "severed" in result.message.lower()


def test_doctor_passes_after_recall(_isolated_env):
    """After a search records recall, the check passes."""
    _index_chunk(chunk_id="warm", file_path="insights/warm.md", content="kappa", seed=12)
    store.search(query_embedding=_embedding(12), threshold=0.5)

    result = recall_write_health(DoctorContext(config=config))
    assert result.passed is True
    assert "being written" in result.message.lower()


def test_doctor_passes_on_empty_db(_isolated_env):
    """No chunks indexed → nothing to recall → pass."""
    result = recall_write_health(DoctorContext(config=config))
    assert result.passed is True


# ── (H1) record_access=False suppresses recall recording ─────────────────────

def test_search_record_access_false_does_not_record(_isolated_env):
    """H1: ``store.search(record_access=False)`` returns hits but records no
    recall — the contract the internal callers (consolidation dedup,
    ``_embedding_candidates`` feeding dedup/orphan/cluster/topic-coverage) rely
    on so maintenance scans don't inflate recall_count / nudge importance."""
    _index_chunk(chunk_id="int", file_path="insights/int.md", content="alpha topic", seed=1)

    results = store.search(query_embedding=_embedding(1), threshold=0.5, record_access=False)
    assert any(r["id"] == "int" for r in results)

    rc, lr, imp = _meta("int")
    assert rc == 0, "internal (record_access=False) read polluted recall_count (H1)"
    assert lr is None
    assert imp == 0.5  # schema default — importance not nudged


# ── (H2) re-index must preserve accumulated recall metadata ──────────────────

def test_reindex_preserves_recall_metadata(_isolated_env):
    """H2: re-indexing a chunk whose content changed must NOT reset its
    accumulated recall_count / last_recalled / importance. ``INSERT OR REPLACE``
    wiped them to schema defaults on every content_hash change; the
    ``ON CONFLICT(id) DO UPDATE`` write preserves them while refreshing content."""
    _index_chunk(chunk_id="dur", file_path="insights/dur.md", content="original body", seed=5)
    # Stake a recall signal via a real search (record_access defaults True).
    store.search(query_embedding=_embedding(5), threshold=0.5)
    rc0, lr0, imp0 = _meta("dur")
    assert rc0 == 1 and lr0 is not None

    # Re-index the SAME id with CHANGED content (content_hash differs → real write).
    _index_chunk(chunk_id="dur", file_path="insights/dur.md", content="EDITED body now", seed=5)

    rc1, lr1, imp1 = _meta("dur")
    assert rc1 == rc0, "recall_count reset on re-index (H2 regression)"
    assert lr1 == lr0, "last_recalled reset on re-index (H2 regression)"
    assert imp1 == imp0, "importance reset on re-index (H2 regression)"

    # The content column itself did update (the UPDATE half of the upsert works).
    db = store.get_db()
    try:
        row = db.execute("SELECT content FROM chunks WHERE id = ?", ("dur",)).fetchone()
    finally:
        db.close()
    assert row["content"] == "EDITED body now"
