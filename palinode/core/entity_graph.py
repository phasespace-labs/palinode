"""
Palinode Entity Graph — entity indexing, detection, and co-occurrence queries.

Why this exists: The entity graph tracks which named entities (people,
projects, topics) appear in which memory files, enabling co-occurrence
discovery and context-aware recall without a full embedding search.

Extracted from ``store.py`` for module cohesion.  All four public functions
are re-exported by ``store`` for backward compatibility.

SQL schema contract (managed by ``palinode.core.store.init_db``):

    entities (
        entity_ref  TEXT NOT NULL,           -- e.g. "person/alice", "project/palinode"
        file_path   TEXT NOT NULL,
        category    TEXT,                    -- from file metadata
        last_seen   TEXT,                    -- ISO-8601 UTC
        PRIMARY KEY (entity_ref, file_path)
    )
    CREATE INDEX idx_entities_ref ON entities(entity_ref)

Callers are responsible for calling ``init_db()`` before using any
function in this module; the functions obtain connections via ``get_db()``.
"""
from __future__ import annotations

import logging
from typing import Any

from palinode.core.db import get_db, _utc_now

__all__ = [
    "get_entity_files",
    "get_entity_graph",
    "upsert_entities",
    "detect_entities_in_text",
]

logger = logging.getLogger(__name__)


def get_entity_files(entity_ref: str) -> list[dict[str, Any]]:
    """Get all files that reference a specific entity.

    Returns dicts with ``file_path``, ``category``, ``last_seen``,
    ordered most-recent first.
    """
    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        "SELECT file_path, category, last_seen FROM entities WHERE entity_ref = ? ORDER BY last_seen DESC",
        (entity_ref,)
    )
    results = [{"file_path": row[0], "category": row[1], "last_seen": row[2]} for row in cursor.fetchall()]
    db.close()
    return results


def get_entity_graph(entity_ref: str, depth: int = 1) -> dict[str, list[str]]:
    """Get the entity graph -- which entities co-occur with *entity_ref*.

    Returns ``{entity_ref: [co_occurring_ref, ...]}`` by finding all
    entities that share at least one file with *entity_ref*.
    """
    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT DISTINCT e2.entity_ref
        FROM entities e1
        JOIN entities e2 ON e1.file_path = e2.file_path
        WHERE e1.entity_ref = ? AND e2.entity_ref != ?
    """, (entity_ref, entity_ref))

    co_occurring = [row[0] for row in cursor.fetchall()]
    db.close()
    return {entity_ref: co_occurring}


def upsert_entities(file_path: str, metadata: dict[str, Any]) -> None:
    """Extract and index entities from file metadata.

    Reads ``metadata["entities"]`` (a list of entity-ref strings).  If the
    key is missing or the list is empty, this is a silent no-op — the file
    simply has no entities to index.  ``metadata["category"]`` defaults to
    ``""`` when absent.
    """
    entities = metadata.get("entities", [])
    if not entities:
        return

    db = get_db()
    cursor = db.cursor()
    category = metadata.get("category", "")
    now = _utc_now().isoformat().replace("+00:00", "Z")

    for entity_ref in entities:
        cursor.execute("""
            INSERT OR REPLACE INTO entities (entity_ref, file_path, category, last_seen)
            VALUES (?, ?, ?, ?)
        """, (entity_ref, file_path, category, now))

    db.commit()
    db.close()


def detect_entities_in_text(text: str) -> list[str]:
    """Detect known entity refs mentioned in text.

    Scans for entity names from the entities index.
    Returns list of entity_ref strings (e.g., ``['person/alice', 'project/alpha']``).
    Empty or whitespace-only *text* returns ``[]`` gracefully (no DB query
    needed since no name can match).
    """
    db = get_db()
    known_entities = db.execute(
        "SELECT DISTINCT entity_ref FROM entities"
    ).fetchall()
    db.close()

    text_lower = text.lower()
    detected = []

    for (entity_ref,) in known_entities:
        # Extract the name part: "person/alice" -> "alice"
        name = entity_ref.split("/")[-1].replace("-", " ")
        if name in text_lower and len(name) > 2:
            detected.append(entity_ref)

    return list(set(detected))
