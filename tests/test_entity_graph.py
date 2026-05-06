"""Direct unit tests for palinode.core.entity_graph (#324).

Tests import from ``palinode.core.entity_graph`` directly, not through
``palinode.core.store``, to validate the extracted module as a real seam.
Uses a real SQLite DB in tmp_path — no mocks for the store layer.
"""
from __future__ import annotations

import os
import time

import pytest

from palinode.core import store
from palinode.core.config import config
import palinode.core.db as _db_mod
from palinode.core.entity_graph import (
    detect_entities_in_text,
    get_entity_files,
    get_entity_graph,
    upsert_entities,
)


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


# ── upsert + retrieval ──────────────────────────────────────────────────────


def test_upsert_entities_indexes_metadata():
    """upsert_entities stores entity refs; get_entity_files retrieves them."""
    upsert_entities("projects/alpha.md", {
        "entities": ["person/alice", "project/alpha"],
        "category": "projects",
    })

    files = get_entity_files("person/alice")
    assert len(files) == 1
    assert files[0]["file_path"] == "projects/alpha.md"
    assert files[0]["category"] == "projects"
    assert files[0]["last_seen"] is not None


def test_get_entity_files_returns_recent_first():
    """Multiple files for an entity come back most-recent first."""
    # Insert two files with a small time gap so last_seen differs
    upsert_entities("old.md", {
        "entities": ["project/beta"],
        "category": "archive",
    })
    # Tiny delay to ensure distinct timestamps
    time.sleep(0.05)
    upsert_entities("new.md", {
        "entities": ["project/beta"],
        "category": "projects",
    })

    files = get_entity_files("project/beta")
    assert len(files) == 2
    assert files[0]["file_path"] == "new.md"
    assert files[1]["file_path"] == "old.md"


# ── Co-occurrence graph ──────────────────────────────────────────────────────


def test_get_entity_graph_returns_co_occurring_entities():
    """Entities that share a file appear in each other's co-occurrence list."""
    upsert_entities("shared.md", {
        "entities": ["person/alice", "person/bob", "project/gamma"],
        "category": "projects",
    })
    # Separate file with only alice
    upsert_entities("solo.md", {
        "entities": ["person/alice"],
        "category": "insights",
    })

    graph = get_entity_graph("person/alice")
    co = graph["person/alice"]
    assert "person/bob" in co
    assert "project/gamma" in co
    # alice should not appear in her own co-occurrence list
    assert "person/alice" not in co


# ── detect_entities_in_text ──────────────────────────────────────────────────


def test_detect_entities_in_text_finds_known_refs():
    """detect_entities_in_text matches indexed entity names in prose."""
    upsert_entities("f.md", {
        "entities": ["person/alice", "project/alpha"],
        "category": "projects",
    })

    detected = detect_entities_in_text("Talked with alice about alpha today")
    assert "person/alice" in detected
    assert "project/alpha" in detected


def test_detect_entities_in_text_empty_input():
    """Empty text returns [] without error."""
    # Seed the DB so there are entities to scan against
    upsert_entities("f.md", {
        "entities": ["person/alice"],
        "category": "projects",
    })
    assert detect_entities_in_text("") == []


# ── Edge cases ───────────────────────────────────────────────────────────────


def test_upsert_entities_no_entities_metadata():
    """Metadata missing the 'entities' key is a silent no-op."""
    # Should not raise
    upsert_entities("empty.md", {"category": "projects"})
    upsert_entities("also-empty.md", {"entities": []})

    # DB should have no entity rows
    files = get_entity_files("person/nobody")
    assert files == []
