"""ADR-007 demand-decay importance — the tuning PR (#86).

Grounds the #371 recall write-back in the ADR-007 §3.3 importance rule:

  - reinforce on *explicit, distinct-session* demand by exponential approach
        importance ← importance + (cap − importance) · alpha
  - decay on read (no sweeper, no write) at rank time
        eff = base + (importance − base) · exp(−Δt/τ),  floored at base.

Test map (ADR-007 §6.7):
  (a) explicit-mode recall nudges importance; auto/ambient does NOT.
  (b) same-session repeat does NOT re-nudge; a NEW session DOES.
  (c) exponential approach hits expected importance at n ∈ {1, 2, 26}.
  (d) decay-on-read: a memory not recalled for τ has eff ~halfway to base;
      eff floors at base (cold never below 0.5).
  (e) cold/unrecalled memories remain findable (bounded re-rank, not suppression).
  (f) recall_count still increments on every hit regardless of the importance gate.

Real SQLite in tmp_path (repo rule — no mocked DB). Only the embedder is
bypassed by inserting deterministic unit vectors directly.
"""
from __future__ import annotations

import math
import os
from datetime import UTC, datetime, timedelta

import pytest

from palinode.core import store
from palinode.core.config import config

EMBED_DIM = 1024


def _normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n else vec


def _embedding(seed: int) -> list[float]:
    vec = [0.0] * EMBED_DIM
    vec[seed % EMBED_DIM] = 0.9
    vec[(seed * 7 + 3) % EMBED_DIM] = 0.4
    return _normalize(vec)


def _index_chunk(
    *,
    chunk_id: str,
    file_path: str,
    content: str,
    seed: int,
    metadata: dict | None = None,
) -> None:
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    store.upsert_chunks([{
        "id": chunk_id,
        "file_path": file_path,
        "section_id": None,
        "category": "insights",
        "content": content,
        "metadata": metadata or {},
        "created_at": now_iso,
        "last_updated": now_iso,
        "embedding": _embedding(seed),
    }])


def _imp(chunk_id: str) -> float | None:
    db = store.get_db()
    try:
        row = db.execute(
            "SELECT importance FROM chunks WHERE id = ?", (chunk_id,)
        ).fetchone()
    finally:
        db.close()
    return row["importance"]


def _recall_count(chunk_id: str) -> int:
    db = store.get_db()
    try:
        row = db.execute(
            "SELECT recall_count FROM chunks WHERE id = ?", (chunk_id,)
        ).fetchone()
    finally:
        db.close()
    return row["recall_count"]


def _set_last_recalled(chunk_id: str, when: datetime) -> None:
    db = store.get_db()
    try:
        db.execute(
            "UPDATE chunks SET last_recalled = ? WHERE id = ?",
            (when.isoformat(), chunk_id),
        )
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    memory_dir = str(tmp_path)
    db_path = os.path.join(memory_dir, ".palinode.db")
    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config, "db_path", db_path)
    monkeypatch.setattr(config.git, "auto_commit", False)
    # ADR-007 defaults (assert them so a config drift surfaces here).
    monkeypatch.setattr(config.decay, "importance_base", 0.5)
    monkeypatch.setattr(config.decay, "importance_cap", 0.95)
    monkeypatch.setattr(config.decay, "importance_alpha", 0.08)
    monkeypatch.setattr(config.decay, "importance_tau_days", 14.0)
    store._db_checked = False
    for d in ("insights", "projects"):
        os.makedirs(os.path.join(memory_dir, d), exist_ok=True)
    store.init_db()
    yield memory_dir
    store._db_checked = False


# ── (a) explicit nudges; passive does NOT ────────────────────────────────────

def test_explicit_mode_nudges_importance(_isolated_env):
    _index_chunk(chunk_id="e", file_path="insights/e.md", content="alpha", seed=1)
    assert _imp("e") == pytest.approx(0.5)

    store.search(query_embedding=_embedding(1), threshold=0.5,
                 mode="explicit", session_id="sess-A")
    # 0.5 + (0.95 - 0.5) * 0.08 = 0.536
    assert _imp("e") == pytest.approx(0.536, abs=1e-3)


def test_passive_mode_does_not_nudge_importance(_isolated_env):
    """Ambient/auto-inject/session-start recall (mode='passive') leaves
    importance at base — the memory was offered, not sought (ADR-007 §3.2)."""
    _index_chunk(chunk_id="p", file_path="insights/p.md", content="alpha", seed=1)

    store.search(query_embedding=_embedding(1), threshold=0.5,
                 mode="passive", session_id="sess-A")
    assert _imp("p") == pytest.approx(0.5)  # unchanged — no nudge


def test_passive_recall_still_increments_recall_count(_isolated_env):
    """The importance *gate* is mode-sensitive, but recall_count is raw frequency
    and increments on every hit regardless of mode (ADR-007 §3.1)."""
    _index_chunk(chunk_id="pc", file_path="insights/pc.md", content="alpha", seed=1)

    store.search(query_embedding=_embedding(1), threshold=0.5, mode="passive")
    assert _recall_count("pc") == 1
    assert _imp("pc") == pytest.approx(0.5)


# ── (b) session dedup ────────────────────────────────────────────────────────

def test_same_session_repeat_does_not_renudge(_isolated_env):
    """Two explicit hits on the same memory in the same session reinforce once."""
    _index_chunk(chunk_id="s", file_path="insights/s.md", content="alpha", seed=1)

    store.search(query_embedding=_embedding(1), threshold=0.5, session_id="same")
    after_first = _imp("s")
    assert after_first == pytest.approx(0.536, abs=1e-3)

    store.search(query_embedding=_embedding(1), threshold=0.5, session_id="same")
    after_second = _imp("s")
    assert after_second == pytest.approx(after_first)  # no second nudge


def test_new_session_does_renudge(_isolated_env):
    """A hit in a *different* session is a fresh demand event and reinforces."""
    _index_chunk(chunk_id="ns", file_path="insights/ns.md", content="alpha", seed=1)

    store.search(query_embedding=_embedding(1), threshold=0.5, session_id="sess-1")
    one = _imp("ns")
    store.search(query_embedding=_embedding(1), threshold=0.5, session_id="sess-2")
    two = _imp("ns")
    assert two > one
    # 0.536 + (0.95 - 0.536) * 0.08 = 0.56912
    assert two == pytest.approx(0.56912, abs=1e-3)


# ── (c) exponential approach at n ∈ {1, 2, 26} ───────────────────────────────

def _expected_importance(n: int, base=0.5, cap=0.95, alpha=0.08) -> float:
    imp = base
    for _ in range(n):
        imp = imp + (cap - imp) * alpha
    return imp


def test_exponential_approach_values(_isolated_env):
    _index_chunk(chunk_id="x", file_path="insights/x.md", content="alpha", seed=1)

    for n in range(1, 27):
        store.search(query_embedding=_embedding(1), threshold=0.5, session_id=f"s-{n}")

    imp = _imp("x")
    # n=26 distinct-session demands → ≈0.90 per ADR-007 §3.3.
    assert imp == pytest.approx(_expected_importance(26), abs=1e-3)
    assert imp == pytest.approx(0.901, abs=1e-2)


@pytest.mark.parametrize("n,expected", [(1, 0.536), (2, 0.56912), (26, 0.901)])
def test_exponential_approach_milestones(_isolated_env, n, expected):
    _index_chunk(chunk_id=f"m{n}", file_path=f"insights/m{n}.md", content="alpha", seed=1)
    for i in range(n):
        store.search(query_embedding=_embedding(1), threshold=0.5, session_id=f"m{n}-s{i}")
    assert _imp(f"m{n}") == pytest.approx(expected, abs=1e-2)


# ── (d) decay-on-read ────────────────────────────────────────────────────────

def test_decay_on_read_halfway_at_tau(_isolated_env):
    """A memory whose stored importance is at cap but which has not been recalled
    for τ days has eff decayed ~e^-1 of the way from cap toward base — i.e. about
    37% of the (cap-base) gap remains. eff = base + (cap-base)*e^-1."""
    base, cap, tau = 0.5, 0.95, 14.0
    last = datetime.now(UTC) - timedelta(days=tau)
    eff = store.effective_importance(cap, last.isoformat())
    expected = base + (cap - base) * math.exp(-1.0)
    assert eff == pytest.approx(expected, abs=1e-3)
    # Sanity: it has moved a substantial fraction toward base but not reached it.
    assert base < eff < cap


def test_decay_on_read_floors_at_base(_isolated_env):
    """Cold is never demoted below base: a long-stale hot memory floors at base,
    and a never-recalled chunk at base stays at base."""
    base = 0.5
    # Very stale (10 τ) hot memory → eff floored at base, not below.
    stale = datetime.now(UTC) - timedelta(days=140)
    eff = store.effective_importance(0.95, stale.isoformat())
    assert eff == pytest.approx(base, abs=1e-3)
    assert eff >= base

    # Never recalled (no clock) at base → base.
    assert store.effective_importance(0.5, None) == pytest.approx(base)
    # NULL importance → base.
    assert store.effective_importance(None, None) == pytest.approx(base)


def test_decay_on_read_recent_is_near_peak(_isolated_env):
    """A just-recalled hot memory reads close to its stored peak."""
    now = datetime.now(UTC)
    eff = store.effective_importance(0.9, now.isoformat())
    assert eff == pytest.approx(0.9, abs=1e-3)


# ── (e) cold memories remain findable (bounded re-rank, not suppression) ──────

def test_cold_memory_remains_findable(_isolated_env, monkeypatch):
    """With the decay ranker ON, a cold (never-recalled) memory that is the only
    relevant hit must still be returned — the re-rank term is a bounded boost,
    never a suppressor (ADR-007 §3.4 / §5 watch item)."""
    monkeypatch.setattr(config.decay, "enabled", True)
    _index_chunk(chunk_id="cold", file_path="insights/cold.md", content="rare topic xyzzy", seed=2)

    results = store.search_hybrid(
        query_text="rare topic xyzzy",
        query_embedding=_embedding(2),
        top_k=5,
        threshold=0.0,
    )
    ids = {r.get("id") for r in results}
    assert "cold" in ids
    cold = next(r for r in results if r.get("id") == "cold")
    # eff floors at base ⇒ the bounded boost is >= 1.0; score is not suppressed.
    assert cold["score"] > 0.0


def test_hot_memory_ranks_above_cold_when_equally_relevant(_isolated_env, monkeypatch):
    """The bounded re-rank term breaks ties toward demonstrated demand without
    overriding relevance: given two equally-relevant hits, the hot one ranks
    first, but only by the bounded band."""
    monkeypatch.setattr(config.decay, "enabled", True)
    _index_chunk(chunk_id="hot", file_path="insights/hot.md", content="shared keyword topic", seed=3)
    _index_chunk(chunk_id="cool", file_path="insights/cool.md", content="shared keyword topic", seed=4)

    # Make "hot" demonstrably hot: importance near cap, recently recalled.
    db = store.get_db()
    try:
        db.execute(
            "UPDATE chunks SET importance = ?, last_recalled = ? WHERE id = ?",
            (0.95, datetime.now(UTC).isoformat(), "hot"),
        )
        db.commit()
    finally:
        db.close()

    results = store.search_hybrid(
        query_text="shared keyword topic",
        query_embedding=_embedding(3),
        top_k=5,
        threshold=0.0,
    )
    order = [r.get("id") for r in results if r.get("id") in ("hot", "cool")]
    assert order and order[0] == "hot"
    # Both still present — cold was not suppressed out of the result set.
    assert "cool" in order


def test_priority_nudge_does_not_override_strong_vector_match(_isolated_env, monkeypatch):
    """Human priority nudges near ties but cannot make a weak hit outrank a
    much stronger normal-priority vector match."""
    monkeypatch.setattr(config.decay, "enabled", False)
    _index_chunk(
        chunk_id="strong-normal",
        file_path="insights/strong-normal.md",
        content="exact semantic target",
        seed=10,
        metadata={"priority": 3},
    )
    _index_chunk(
        chunk_id="weak-critical",
        file_path="insights/weak-critical.md",
        content="unrelated aside",
        seed=700,
        metadata={"priority": 5},
    )

    results = store.search_hybrid(
        query_text="exact semantic target",
        query_embedding=_embedding(10),
        top_k=5,
        threshold=0.0,
        hybrid_weight=0.0,
    )
    order = [r.get("id") for r in results if r.get("id") in ("strong-normal", "weak-critical")]
    assert order[:2] == ["strong-normal", "weak-critical"]


def test_priority_metadata_does_not_repurpose_decay_importance_column(_isolated_env):
    _index_chunk(
        chunk_id="priority-meta",
        file_path="insights/priority-meta.md",
        content="priority metadata only",
        seed=11,
        metadata={"priority": 5},
    )

    assert _imp("priority-meta") == pytest.approx(0.5)
    assert store.effective_importance(_imp("priority-meta"), None) == pytest.approx(0.5)


# ── (f) recall_count increments on every hit regardless of dedup/gate ─────────

def test_recall_count_raw_across_same_session(_isolated_env):
    """recall_count is raw frequency: every explicit hit increments it even when
    the importance nudge is session-deduplicated."""
    _index_chunk(chunk_id="rc", file_path="insights/rc.md", content="alpha", seed=1)

    for _ in range(3):
        store.search(query_embedding=_embedding(1), threshold=0.5, session_id="one-session")

    assert _recall_count("rc") == 3            # raw count: every hit
    assert _imp("rc") == pytest.approx(0.536, abs=1e-3)  # nudged once
