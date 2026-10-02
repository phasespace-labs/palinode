"""Every registered trigger is a candidate, however many non-candidates are nearer.

``check_triggers`` asks vec0 for the nearest trigger embeddings and only then
filters on ``enabled``, expiry, threshold, cooldown and deliverability. vec0
cannot pre-filter on a joined column, so a fixed KNN window (it was ``k = 10``)
is really an eligibility cap: ten disabled, expired, cooling-down or
undeliverable near-neighbours fill the window and a trigger that would have
cleared its threshold is never scored at all. That is a second, silent way for
the trigger channel to be dead in a store holding more than ten triggers.

Real SQLite under ``tmp_path``; no embedder. Vectors are hand-made unit
vectors, so the store's own ``1 - d^2 / 2`` L2-to-cosine conversion makes the
score exactly the cosine written into the fixture.
"""
from __future__ import annotations

import math

import pytest

from palinode.core import store
from palinode.core.config import config

EMBED_DIM = 1024


def _unit_vector_at(cosine: float, axis: int) -> list[float]:
    """A unit vector whose cosine with ``e0`` is *cosine*, in plane (0, axis)."""
    if axis == 0:
        raise ValueError("axis 0 is the query direction")
    vector = [0.0] * EMBED_DIM
    vector[0] = cosine
    vector[axis] = math.sqrt(max(0.0, 1.0 - cosine * cosine))
    return vector


QUERY = [1.0] + [0.0] * (EMBED_DIM - 1)


@pytest.fixture()
def trigger_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    store.init_db()
    return tmp_path


def _register_crowd(count: int, *, enabled: bool) -> None:
    """*count* triggers nearer to the query than any real match, disabled."""
    for index in range(count):
        trigger_id = f"crowd-{index:02d}"
        store.add_trigger(
            trigger_id,
            f"crowd description {index}",
            f"decisions/crowd-{index:02d}.md",
            _unit_vector_at(0.99, axis=index + 1),
            threshold=0.75,
        )
        if not enabled:
            db = store.get_db()
            db.execute("UPDATE triggers SET enabled = 0 WHERE id = ?", (trigger_id,))
            db.commit()
            db.close()


def test_enabled_trigger_fires_behind_twelve_disabled_near_neighbours(trigger_store):
    _register_crowd(12, enabled=False)
    store.add_trigger(
        "real",
        "the trigger the prompt is actually about",
        "decisions/real.md",
        _unit_vector_at(0.80, axis=900),
        threshold=0.75,
    )

    fired = store.check_triggers(QUERY, cooldown_bypass=True)

    assert [row["id"] for row in fired] == ["real"], (
        "a trigger that clears its threshold must fire even when more than "
        f"k disabled neighbours are nearer; got {fired}"
    )
    assert fired[0]["score"] == pytest.approx(0.80, abs=1e-4)


def test_expired_near_neighbours_do_not_crowd_out_a_live_trigger(trigger_store):
    for index in range(12):
        store.add_trigger(
            f"expired-{index:02d}",
            f"expired description {index}",
            f"decisions/expired-{index:02d}.md",
            _unit_vector_at(0.99, axis=index + 1),
            threshold=0.75,
            expires_at="2020-01-01T00:00:00Z",
        )
    store.add_trigger(
        "real",
        "the trigger the prompt is actually about",
        "decisions/real.md",
        _unit_vector_at(0.80, axis=900),
        threshold=0.75,
    )

    fired = store.check_triggers(QUERY, cooldown_bypass=True)

    assert [row["id"] for row in fired] == ["real"]


def test_undeliverable_near_neighbours_do_not_crowd_out_a_live_trigger(trigger_store):
    _register_crowd(12, enabled=True)
    store.add_trigger(
        "real",
        "the trigger the prompt is actually about",
        "decisions/real.md",
        _unit_vector_at(0.80, axis=900),
        threshold=0.75,
    )

    fired = store.check_triggers(
        QUERY,
        cooldown_bypass=True,
        deliverable=lambda path: path == "decisions/real.md",
    )

    assert [row["id"] for row in fired] == ["real"]


def test_every_qualifying_trigger_is_returned_nearest_first(trigger_store):
    for index, cosine in enumerate((0.95, 0.85, 0.78), start=1):
        store.add_trigger(
            f"match-{index}",
            f"match description {index}",
            f"decisions/match-{index}.md",
            _unit_vector_at(cosine, axis=index),
            threshold=0.75,
        )
    _register_crowd(12, enabled=False)

    fired = store.check_triggers(QUERY, cooldown_bypass=True)

    assert [row["id"] for row in fired] == ["match-1", "match-2", "match-3"]


def test_empty_trigger_table_returns_no_matches(trigger_store):
    assert store.check_triggers(QUERY, cooldown_bypass=True) == []
