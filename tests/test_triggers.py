"""Direct unit tests for palinode.core.triggers (#324).

Tests import from ``palinode.core.triggers`` directly, not through
``palinode.core.store``, to validate the extracted module as a real seam.
Uses a real SQLite DB in tmp_path — no mocks for the store layer.
"""
from __future__ import annotations

import math
import os

import pytest

from palinode.core import store
from palinode.core.config import config
import palinode.core.db as _db_mod
from palinode.core.triggers import (
    add_trigger,
    check_triggers,
    delete_trigger,
    list_triggers,
    update_trigger_fired,
)


EMBED_DIM = 1024


def _normalize(vec: list[float]) -> list[float]:
    """L2-normalize a vector."""
    n = math.sqrt(sum(x * x for x in vec))
    if n == 0:
        return vec
    return [x / n for x in vec]


def _basis_embedding(index: int) -> list[float]:
    """Deterministic near-orthogonal unit vector keyed by *index*."""
    vec = [0.0] * EMBED_DIM
    vec[index % EMBED_DIM] = 1.0
    return vec  # already unit-length


def _similar_embedding(base_index: int, noise_index: int, noise_weight: float = 0.05) -> list[float]:
    """Embedding close to _basis_embedding(base_index) but slightly off-axis."""
    vec = [0.0] * EMBED_DIM
    vec[base_index % EMBED_DIM] = 1.0
    vec[noise_index % EMBED_DIM] = noise_weight
    return _normalize(vec)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Fresh SQLite DB for each test, no network, no git."""
    memory_dir = str(tmp_path)
    db_path = os.path.join(memory_dir, ".palinode.db")
    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config, "db_path", db_path)
    monkeypatch.setattr(config.git, "auto_commit", False)
    _db_mod._db_checked = False
    store.init_db()
    yield
    _db_mod._db_checked = False


# ── Basic CRUD ───────────────────────────────────────────────────────────────


def test_add_trigger_persists_and_lists_back():
    """add_trigger() stores a trigger; list_triggers() retrieves it intact."""
    emb = _basis_embedding(0)
    add_trigger(
        trigger_id="t1",
        description="LoRA training discussion",
        memory_file="projects/lora.md",
        embedding=emb,
        threshold=0.8,
        cooldown_hours=12,
    )
    triggers = list_triggers()
    assert len(triggers) == 1
    t = triggers[0]
    assert t["id"] == "t1"
    assert t["description"] == "LoRA training discussion"
    assert t["memory_file"] == "projects/lora.md"
    assert t["threshold"] == 0.8
    assert t["cooldown_hours"] == 12
    assert t["enabled"] == 1
    assert t["fire_count"] == 0


def test_delete_trigger_removes_both_tables():
    """delete_trigger() clears rows from both triggers and triggers_vec."""
    emb = _basis_embedding(1)
    add_trigger("t-del", "test delete", "f.md", emb)
    assert len(list_triggers()) == 1

    delete_trigger("t-del")
    assert len(list_triggers()) == 0

    # Also verify the vec table row is gone by attempting a check_triggers
    # query that would have matched — should return empty, not error.
    results = check_triggers(query_embedding=emb, cooldown_bypass=True)
    assert results == []


# ── Precondition validation ──────────────────────────────────────────────────


def test_check_triggers_empty_embedding_raises():
    """Passing [] to check_triggers must raise ValueError, not silently return []."""
    with pytest.raises(ValueError, match="query_embedding must be a non-empty"):
        check_triggers(query_embedding=[])


def test_add_trigger_empty_embedding_raises():
    """Passing [] to add_trigger must raise ValueError."""
    with pytest.raises(ValueError, match="embedding must be a non-empty"):
        add_trigger("t-bad", "desc", "f.md", embedding=[])


# ── Similarity matching ─────────────────────────────────────────────────────


def test_check_triggers_below_threshold_no_match():
    """An orthogonal embedding should score ~0 and not fire the trigger."""
    add_trigger("t-far", "topic A", "a.md", _basis_embedding(10), threshold=0.7)

    # Query with an orthogonal vector — cosine similarity ~0
    results = check_triggers(
        query_embedding=_basis_embedding(500),
        cooldown_bypass=True,
    )
    assert results == []

    # fire_count should remain 0
    t = list_triggers()[0]
    assert t["fire_count"] == 0


def test_check_triggers_above_threshold_fires():
    """A near-identical embedding should fire and increment fire_count."""
    base = _basis_embedding(42)
    add_trigger("t-near", "topic B", "b.md", base, threshold=0.7)

    # Query with a very similar vector
    query = _similar_embedding(42, 43, noise_weight=0.02)
    results = check_triggers(query_embedding=query, cooldown_bypass=True)

    assert len(results) == 1
    assert results[0]["id"] == "t-near"
    assert results[0]["memory_file"] == "b.md"
    assert results[0]["score"] >= 0.7

    # fire_count should be 1 now
    t = list_triggers()[0]
    assert t["fire_count"] == 1


# ── Cooldown behaviour ───────────────────────────────────────────────────────


def test_cooldown_blocks_refire():
    """After firing, the trigger should not fire again within cooldown_hours."""
    base = _basis_embedding(99)
    add_trigger("t-cool", "topic C", "c.md", base, threshold=0.5, cooldown_hours=24)

    query = _similar_embedding(99, 100, noise_weight=0.01)

    # First fire — should succeed
    r1 = check_triggers(query_embedding=query, cooldown_bypass=False)
    assert len(r1) == 1

    # Second fire — within cooldown, should be suppressed
    r2 = check_triggers(query_embedding=query, cooldown_bypass=False)
    assert len(r2) == 0

    # fire_count should still be 1 (second check didn't fire)
    t = list_triggers()[0]
    assert t["fire_count"] == 1


def test_cooldown_bypass():
    """cooldown_bypass=True should allow refiring within the cooldown window."""
    base = _basis_embedding(200)
    add_trigger("t-bypass", "topic D", "d.md", base, threshold=0.5, cooldown_hours=24)

    query = _similar_embedding(200, 201, noise_weight=0.01)

    # First fire
    r1 = check_triggers(query_embedding=query, cooldown_bypass=True)
    assert len(r1) == 1

    # Second fire with bypass — should fire again
    r2 = check_triggers(query_embedding=query, cooldown_bypass=True)
    assert len(r2) == 1

    # fire_count should be 2
    t = list_triggers()[0]
    assert t["fire_count"] == 2
