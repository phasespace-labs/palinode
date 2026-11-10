"""triggers_vec must not be written with INSERT OR REPLACE.

ADR-002 ("Defensive virtual table writes") mandates explicit DELETE-then-INSERT
for any `vec0` virtual table, because `vec0` does not reliably honor SQLite's
conflict-resolution clauses — it can raise a UNIQUE constraint error on an
existing primary key instead of replacing the row. `chunks_vec` already follows
this pattern (store.py upsert_chunks). `triggers_vec` did not: `add_trigger`
used `INSERT OR REPLACE INTO triggers_vec`, the exact pattern the ADR forbids.

Failure mode demonstrated below: re-registering an existing trigger_id (the
normal path for updating a trigger's description/embedding, and the *only*
path the consolidation auto-register hook uses — it derives a deterministic
`auto-{base}` id every run) raises `sqlite3.OperationalError: UNIQUE
constraint failed on triggers_vec primary key` instead of upserting. Both known
callers (api/routers/triggers.py, consolidation/layer_split.py) catch this and
turn it into a swallowed failure — the trigger's embedding silently stays
stale forever, which is the "stale entries surviving a replace" class ADR-002
exists to prevent.

Tests use real SQLite with tmp_path (no mocking the DB per CLAUDE.md).
"""
from __future__ import annotations

import sqlite3

import pytest

from palinode.core import store
from palinode.core.config import config


_EMBEDDING_A = [0.01] * 1024
_EMBEDDING_B = [0.02] * 1024


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    """Isolated DB in tmp_path with store fully initialised."""
    db = tmp_path / ".palinode.db"
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db))
    store.init_db()
    return db


class TestAddTriggerReRegistration:

    def test_reregistering_existing_trigger_id_does_not_raise(self, db_path):
        """Re-registering a trigger_id that already exists must upsert, not
        raise. This is the normal shape of the bug: the consolidation
        auto-register path (layer_split.py) derives a deterministic
        `auto-{base}` id and calls add_trigger on every run.
        """
        store.add_trigger("t1", "release notes for v1", "file-v1.md", _EMBEDDING_A)

        # Must not raise sqlite3.OperationalError: UNIQUE constraint failed
        # on triggers_vec primary key.
        store.add_trigger("t1", "release notes for v2", "file-v2.md", _EMBEDDING_B)

    def test_reregistering_replaces_metadata_and_embedding(self, db_path):
        """The second registration must actually win — both the `triggers`
        row (description/memory_file) and the `triggers_vec` row (embedding)
        must reflect the second call, not the first.
        """
        store.add_trigger("t1", "release notes for v1", "file-v1.md", _EMBEDDING_A)
        store.add_trigger("t1", "release notes for v2", "file-v2.md", _EMBEDDING_B)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT description, memory_file FROM triggers WHERE id = ?",
            ("t1",),
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["description"] == "release notes for v2"
        assert row["memory_file"] == "file-v2.md"

    def test_reregistering_leaves_exactly_one_triggers_vec_row(self, db_path):
        """No duplicate / orphaned rows in the vec0 index after an update."""
        store.add_trigger("t1", "release notes for v1", "file-v1.md", _EMBEDDING_A)
        store.add_trigger("t1", "release notes for v2", "file-v2.md", _EMBEDDING_B)

        db = store.get_db()
        rows = db.execute("SELECT id FROM triggers_vec WHERE id = ?", ("t1",)).fetchall()
        db.close()

        assert len(rows) == 1, (
            f"expected exactly one triggers_vec row for 't1', got {len(rows)}"
        )

    def test_reregistering_updates_check_triggers_match(self, db_path):
        """The functional consequence: after an update, check_triggers must
        match against the NEW embedding, not the stale one left behind by a
        failed replace.
        """
        store.add_trigger("t1", "release notes for v1", "file-v1.md", _EMBEDDING_A)
        store.add_trigger("t1", "release notes for v2", "file-v2.md", _EMBEDDING_B)

        fired = store.check_triggers(_EMBEDDING_B, "release notes for v2", cooldown_bypass=True)
        assert any(f["id"] == "t1" and f["memory_file"] == "file-v2.md" for f in fired), (
            f"expected updated trigger to fire against its new embedding, got {fired}"
        )
