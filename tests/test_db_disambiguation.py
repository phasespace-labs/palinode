"""Tests for the phantom-DB guard (#188).

Verifies that ``get_db()`` / ``_ensure_db()`` correctly disambiguates
first-run from misconfiguration before SQLite auto-creates an empty DB.

Cases:
  1. Empty memory_dir + no DB          → DB created, no error, INFO log.
  2. memory_dir with .md files + no DB → RuntimeError raised.
  3. Same as (2) but PALINODE_ALLOW_FRESH_DB=1 set → DB created, no error.
  4. DB already exists                  → no file I/O checks, just connects.

All tests use real SQLite in tmp_path.  The ``_db_checked`` module flag is
reset around every test so cases are independent.
"""
from __future__ import annotations

import os

import pytest

from palinode.core import store
from palinode.core.config import config


@pytest.fixture(autouse=True)
def _reset_db_checked():
    """Reset the module-level _db_checked flag before and after each test.

    Without this, the first test that triggers ``_ensure_db`` would mark the
    flag True and all subsequent tests would skip the check entirely.
    """
    store._db_checked = False
    yield
    store._db_checked = False


@pytest.fixture()
def fresh_env(tmp_path, monkeypatch):
    """Point config at a fresh tmp directory (no DB, no .md files)."""
    memory_dir = str(tmp_path)
    db_path = os.path.join(memory_dir, ".palinode.db")
    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config, "db_path", db_path)
    monkeypatch.setattr(config.git, "auto_commit", False)
    return tmp_path


# ── Case 1: First run — empty memory_dir, no DB ──────────────────────────────

def test_first_run_creates_db(fresh_env, caplog):
    """Empty memory_dir + no DB → DB auto-created with an INFO log."""
    import logging

    db_path = config.db_path
    assert not os.path.exists(db_path), "Pre-condition: no DB yet"

    with caplog.at_level(logging.INFO, logger="palinode.store"):
        # init_db calls get_db which calls _ensure_db
        store.init_db()

    assert os.path.exists(db_path), "DB should have been created"
    # Should log a first-run INFO message, not raise
    assert any(
        "First run detected" in r.message
        for r in caplog.records
        if r.name == "palinode.store"
    ), f"Expected 'First run detected' INFO log; got: {[r.message for r in caplog.records]}"


def test_first_run_db_is_usable(fresh_env):
    """After first-run creation, the DB must have the expected schema."""
    store.init_db()
    db = store.get_db()
    tables = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    db.close()
    assert "chunks" in tables
    assert "triggers" in tables


# ── Case 2: Misconfiguration — .md files present, no DB ───────────────────────

def test_misconfig_raises_when_md_files_exist(fresh_env):
    """.md files in memory_dir but no DB → RuntimeError with actionable message."""
    memory_dir = str(fresh_env)

    # Write a sentinel .md file to simulate an existing memory corpus
    os.makedirs(os.path.join(memory_dir, "projects"), exist_ok=True)
    with open(os.path.join(memory_dir, "projects", "alpha.md"), "w") as f:
        f.write("---\ntitle: Alpha\n---\nSome content.\n")

    assert not os.path.exists(config.db_path), "Pre-condition: no DB"

    with pytest.raises(RuntimeError) as exc_info:
        store._ensure_db()

    msg = str(exc_info.value)
    assert "memory file" in msg.lower() or "memory" in msg
    assert config.memory_dir in msg
    assert config.db_path in msg
    assert "PALINODE_ALLOW_FRESH_DB" in msg


def test_misconfig_does_not_create_db(fresh_env):
    """The guard must not create the DB file when raising."""
    memory_dir = str(fresh_env)
    with open(os.path.join(memory_dir, "stale.md"), "w") as f:
        f.write("# Stale memory\n")

    with pytest.raises(RuntimeError):
        store._ensure_db()

    assert not os.path.exists(config.db_path), "DB must NOT be created on misconfig"


def test_misconfig_nested_md_files_detected(fresh_env):
    """Recursive glob must catch .md files in subdirectories."""
    memory_dir = str(fresh_env)
    nested = os.path.join(memory_dir, "decisions", "2025")
    os.makedirs(nested, exist_ok=True)
    with open(os.path.join(nested, "adr-001.md"), "w") as f:
        f.write("# Decision\n")

    with pytest.raises(RuntimeError):
        store._ensure_db()


# ── Case 3: PALINODE_ALLOW_FRESH_DB override ──────────────────────────────────

def test_allow_fresh_db_env_bypasses_guard(fresh_env, monkeypatch):
    """PALINODE_ALLOW_FRESH_DB=1 allows DB creation even with .md files present."""
    memory_dir = str(fresh_env)
    with open(os.path.join(memory_dir, "important.md"), "w") as f:
        f.write("# Important memory\n")

    monkeypatch.setenv("PALINODE_ALLOW_FRESH_DB", "1")

    # Should not raise
    store._ensure_db()

    # DB creation happens at sqlite3.connect time (called by get_db inside init_db)
    store.init_db()
    assert os.path.exists(config.db_path)


def test_allow_fresh_db_any_truthy_value(fresh_env, monkeypatch):
    """Any non-empty value for PALINODE_ALLOW_FRESH_DB bypasses the guard."""
    memory_dir = str(fresh_env)
    with open(os.path.join(memory_dir, "some.md"), "w") as f:
        f.write("# Memory\n")

    monkeypatch.setenv("PALINODE_ALLOW_FRESH_DB", "true")
    # Should not raise
    store._ensure_db()


# ── Case 4: DB already exists — no extra I/O ──────────────────────────────────

def test_existing_db_skips_check(fresh_env, monkeypatch):
    """If the DB file already exists, _ensure_db must return immediately.

    We verify this by planting .md files AND an existing DB.  If the guard
    still ran the glob it would… actually not raise (because we're checking
    "DB exists" first).  But we also verify _db_checked is set True after the
    call, confirming the fast-path was taken.
    """
    memory_dir = str(fresh_env)
    db_path = config.db_path

    # Create the DB first (simulating normal operation)
    store.init_db()
    assert os.path.exists(db_path)

    # Now also plant .md files (would trip the guard if it ran)
    with open(os.path.join(memory_dir, "existing.md"), "w") as f:
        f.write("# Existing memory\n")

    # Reset checked flag to force re-entry
    store._db_checked = False

    # Should NOT raise even though .md files exist — DB is present
    store._ensure_db()
    assert store._db_checked is True


def test_db_checked_flag_prevents_double_check(fresh_env):
    """Once _db_checked is True, subsequent _ensure_db calls are no-ops."""
    store._db_checked = True  # Simulate already-checked state

    # Plant .md files — if the guard ran, it would raise
    memory_dir = str(fresh_env)
    with open(os.path.join(memory_dir, "trap.md"), "w") as f:
        f.write("# Trap\n")

    # Must not raise — the flag short-circuits
    store._ensure_db()
