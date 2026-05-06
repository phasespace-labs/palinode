"""
Palinode Triggers — prospective-memory trigger management.

Why this exists: Triggers implement prospective memory — the ability to
surface a specific memory file when the user's context semantically matches
a registered description.  Matching is cosine-similarity over pre-computed
embeddings stored in a sqlite-vec virtual table.

Extracted from ``store.py`` for module cohesion.  All five public functions
are re-exported by ``store`` for backward compatibility.

SQL schema contract (managed by ``palinode.core.store.init_db``):

    triggers (
        id              TEXT PRIMARY KEY,
        description     TEXT NOT NULL,
        memory_file     TEXT NOT NULL,
        threshold       FLOAT DEFAULT 0.75,
        cooldown_hours  INT DEFAULT 24,
        last_fired      TEXT,          -- ISO-8601 UTC or NULL
        fire_count      INT DEFAULT 0,
        created_at      TEXT,          -- ISO-8601 UTC
        enabled         INT DEFAULT 1  -- boolean flag
    )

    triggers_vec USING vec0 (         -- sqlite-vec virtual table
        id         TEXT PRIMARY KEY,
        embedding  FLOAT[{dimensions}]  -- default 1024 for BGE-M3
    )

Callers are responsible for calling ``init_db()`` before using any
function in this module; the functions obtain connections via ``get_db()``.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from palinode.core.db import get_db, _utc_now

__all__ = [
    "add_trigger",
    "check_triggers",
    "list_triggers",
    "delete_trigger",
    "update_trigger_fired",
]

logger = logging.getLogger(__name__)


def add_trigger(
    trigger_id: str,
    description: str,
    memory_file: str,
    embedding: list[float],
    threshold: float = 0.75,
    cooldown_hours: int = 24,
) -> None:
    """Register a prospective trigger.

    Args:
        trigger_id: Unique ID for this trigger.
        description: What context should fire this (e.g., "LoRA training").
        memory_file: Relative path to inject when fired.
        embedding: Pre-computed embedding of the description.
        threshold: Cosine similarity threshold to fire (0.0-1.0).
        cooldown_hours: Hours between refires.

    Raises:
        ValueError: If *embedding* is empty or not a list.
    """
    if not embedding:
        raise ValueError("embedding must be a non-empty list[float]")
    db = get_db()
    now = _utc_now().isoformat().replace("+00:00", "Z")
    db.execute("""
        INSERT OR REPLACE INTO triggers (id, description, memory_file, threshold, cooldown_hours, created_at, enabled, fire_count)
        VALUES (?, ?, ?, ?, ?, ?, 1, 0)
    """, (trigger_id, description, memory_file, threshold, cooldown_hours, now))

    emb_json = json.dumps(embedding)
    db.execute("""
        INSERT OR REPLACE INTO triggers_vec (id, embedding)
        VALUES (?, ?)
    """, (trigger_id, emb_json))

    db.commit()
    db.close()


def check_triggers(
    query_embedding: list[float],
    cooldown_bypass: bool = False,
) -> list[dict[str, Any]]:
    """Check if any triggers match the current context.

    Args:
        query_embedding: Embedding of the current user message.
        cooldown_bypass: If True, ignore cooldown (useful for testing).

    Returns:
        List of fired trigger dicts with memory_file and trigger info.

    Raises:
        ValueError: If *query_embedding* is empty or not a list.
    """
    if not query_embedding:
        raise ValueError("query_embedding must be a non-empty list[float]")
    db = get_db()
    query_vec_json = json.dumps(query_embedding)

    rows = db.execute("""
        SELECT t.*, v.distance
        FROM triggers_vec v
        JOIN triggers t ON v.id = t.id
        WHERE v.embedding MATCH ? AND k = 10
    """, (query_vec_json,)).fetchall()

    results = []
    now = _utc_now()

    for row in rows:
        if not row["enabled"]:
            continue

        dist = row["distance"] or 0
        score = 1.0 - ((dist ** 2) / 2.0)

        if score >= row["threshold"]:
            if not cooldown_bypass and row["last_fired"]:
                # last_fired is stored as "...Z"; [:19] strips the Z,
                # producing a naive datetime.  Attach UTC so subtraction
                # against the aware _utc_now() doesn't raise TypeError.
                last_fired_date = datetime.fromisoformat(
                    row["last_fired"][:19]
                ).replace(tzinfo=UTC)
                hours_since = (now - last_fired_date).total_seconds() / 3600
                if hours_since < row["cooldown_hours"]:
                    continue  # In cooldown

            results.append({
                "id": row["id"],
                "description": row["description"],
                "memory_file": row["memory_file"],
                "score": score,
            })

            # Since it fired, record it
            update_trigger_fired(row["id"])

    db.close()
    return results


def list_triggers() -> list[dict]:
    """Return all registered triggers with their stats."""
    db = get_db()
    rows = db.execute("SELECT id, description, memory_file, threshold, cooldown_hours, last_fired, fire_count, created_at, enabled FROM triggers ORDER BY created_at DESC").fetchall()
    db.close()
    return [dict(r) for r in rows]


def delete_trigger(trigger_id: str) -> None:
    """Remove a trigger."""
    db = get_db()
    db.execute("DELETE FROM triggers WHERE id = ?", (trigger_id,))
    db.execute("DELETE FROM triggers_vec WHERE id = ?", (trigger_id,))
    db.commit()
    db.close()


def update_trigger_fired(trigger_id: str) -> None:
    """Record that a trigger fired -- update last_fired and fire_count."""
    db = get_db()
    now = _utc_now().isoformat().replace("+00:00", "Z")
    db.execute("""
        UPDATE triggers
        SET last_fired = ?, fire_count = fire_count + 1
        WHERE id = ?
    """, (now, trigger_id))
    db.commit()
    db.close()
