"""``check_triggers`` cooldown on a trigger that has already fired (the aware-now
vs naive-``last_fired`` fix).

``check_triggers`` compares an aware ``_utc_now()`` against ``last_fired``,
which ``update_trigger_fired`` writes with a ``Z`` suffix. The cooldown branch
used to parse it with ``fromisoformat(value[:19])`` — stripping the offset and
producing a naive datetime — so the subtraction raised ``TypeError`` on every
trigger that had fired once and was checked again without ``cooldown_bypass``.
No test exercised the fired-then-rechecked path.

Covers the real path (fire → recheck inside the window → suppressed; fire with
``cooldown_hours=0`` → fires again) and, by writing the column directly, a
``last_fired`` that is aware-and-old, legacy-naive-and-recent, and
legacy-naive-and-old. Real SQLite under ``tmp_path``; the DB is never mocked.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from palinode.core import store
from palinode.core.config import config

EMBED_DIM = 1024
_VEC = [0.05] * EMBED_DIM


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", os.path.join(str(tmp_path), ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    store.init_db()
    return str(tmp_path)


def _row(trigger_id: str) -> dict:
    return next(t for t in store.list_triggers() if t["id"] == trigger_id)


def _set_last_fired(value: str) -> None:
    """Write ``last_fired`` directly, bypassing ``update_trigger_fired``'s format."""
    conn = sqlite3.connect(config.db_path)
    conn.execute("UPDATE triggers SET last_fired = ?", (value,))
    conn.commit()
    conn.close()


class TestFiredTriggerCooldown:
    def test_recheck_inside_cooldown_is_suppressed_without_raising(self, isolated_store):
        store.add_trigger("t-cool", "deploying to prod", "projects/deploy.md", _VEC,
                          cooldown_hours=24)
        first = store.check_triggers(_VEC)
        assert [f["id"] for f in first] == ["t-cool"]
        row = _row("t-cool")
        assert row["fire_count"] == 1
        assert row["last_fired"].endswith("Z"), "precondition: writer emits the Z format"

        # The path that raised TypeError: aware now minus the stored timestamp.
        second = store.check_triggers(_VEC)
        assert second == []
        assert _row("t-cool")["fire_count"] == 1, "a suppressed check must not record a firing"

    def test_zero_cooldown_fires_again(self, isolated_store):
        store.add_trigger("t-hot", "deploying to prod", "projects/deploy.md", _VEC,
                          cooldown_hours=0)
        assert [f["id"] for f in store.check_triggers(_VEC)] == ["t-hot"]
        assert [f["id"] for f in store.check_triggers(_VEC)] == ["t-hot"]
        assert _row("t-hot")["fire_count"] == 2

    def test_aware_last_fired_past_cooldown_fires(self, isolated_store):
        store.add_trigger("t-old", "deploying to prod", "projects/deploy.md", _VEC,
                          cooldown_hours=24)
        _set_last_fired((datetime.now(UTC) - timedelta(hours=48)).isoformat())
        assert [f["id"] for f in store.check_triggers(_VEC)] == ["t-old"]

    def test_legacy_naive_last_fired_inside_cooldown_is_suppressed(self, isolated_store):
        store.add_trigger("t-naive", "deploying to prod", "projects/deploy.md", _VEC,
                          cooldown_hours=24)
        naive_recent = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None)
        _set_last_fired(naive_recent.isoformat())
        assert store.check_triggers(_VEC) == []
        assert _row("t-naive")["fire_count"] == 0

    def test_legacy_naive_last_fired_past_cooldown_fires(self, isolated_store):
        store.add_trigger("t-naive-old", "deploying to prod", "projects/deploy.md", _VEC,
                          cooldown_hours=24)
        naive_old = (datetime.now(UTC) - timedelta(hours=48)).replace(tzinfo=None)
        _set_last_fired(naive_old.isoformat())
        assert [f["id"] for f in store.check_triggers(_VEC)] == ["t-naive-old"]
        # The firing rewrites the column in the current format.
        assert _row("t-naive-old")["last_fired"].endswith("Z")
