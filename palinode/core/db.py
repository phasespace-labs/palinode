"""
Palinode DB utilities — connection management and guards.

Extracted from ``store.py`` to break circular imports: both ``store``,
``triggers``, and ``entity_graph`` need ``get_db`` / ``_utc_now`` without
depending on each other.
"""
from __future__ import annotations

import glob as _glob
import os
import sqlite3
from datetime import UTC, datetime

import sqlite_vec

from palinode.core.config import config

__all__ = ["get_db", "utc_now", "_utc_now", "_ensure_db", "_glob_md_files"]

_store_logger = __import__("logging").getLogger("palinode.store")

# Module-level flag: once we've verified the DB state on first connect, skip
# the check on subsequent calls (it's expensive — recursive glob of memory_dir).
_db_checked: bool = False


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


# Backward-compat alias — existing internal callers use the private name.
_utc_now = utc_now


def _glob_md_files(directory: str):
    """Yield .md file paths under *directory* (recursive)."""
    yield from _glob.iglob(os.path.join(directory, "**", "*.md"), recursive=True)


def _ensure_db() -> None:
    """Disambiguate first-run from misconfiguration before SQLite auto-creates.

    Called once per process on the first ``get_db()`` invocation.  Sets the
    module-level ``_db_checked`` flag so subsequent calls are free.

    Three cases:
    - DB already exists -> nothing to do; just connect normally.
    - DB missing + memory_dir has 0 .md files (or PALINODE_ALLOW_FRESH_DB set)
      -> legitimate first run; log clearly and allow creation.
    - DB missing + memory_dir has .md files -> misconfiguration; raise
      RuntimeError with actionable guidance for the operator.
    """
    global _db_checked
    if _db_checked:
        return

    _db_checked = True

    db_path = config.db_path
    if os.path.exists(db_path):
        # Normal operation -- DB is present, nothing to check.
        return

    memory_dir = config.memory_dir
    allow_fresh = os.environ.get("PALINODE_ALLOW_FRESH_DB")

    # Count .md files in memory_dir (recursive).  If the directory doesn't
    # exist yet we treat that as 0 files (brand-new install).
    try:
        md_count = sum(1 for _ in _glob_md_files(memory_dir))
    except (OSError, ValueError):
        md_count = 0

    if md_count == 0 or allow_fresh:
        _store_logger.info(
            "palinode.store: First run detected -- creating fresh database at %s "
            "(memory_dir has %d .md file(s))",
            db_path,
            md_count,
        )
        return

    # Memory files exist but no DB -- almost certainly a misconfiguration.
    raise RuntimeError(
        f"palinode found {md_count} memory file(s) at {memory_dir} "
        f"but no database at {db_path}.\n"
        "This usually means PALINODE_DIR or db_path is misconfigured.\n\n"
        f"  - To verify:  ls {memory_dir}/*.md\n"
        "  - If you intended to start fresh, set PALINODE_ALLOW_FRESH_DB=1\n"
        "  - Otherwise, check that db_path in palinode.config.yaml matches "
        "your memory_dir"
    )


def get_db() -> sqlite3.Connection:
    """Gets an active connection to the SQLite database with vec extension active.

    On the first call per process, verifies that the DB state is consistent
    with the memory_dir contents -- raises RuntimeError on detected
    misconfiguration (DB missing but .md files present).

    Returns:
        sqlite3.Connection: Database connection featuring vec.

    Raises:
        RuntimeError: If the database is missing but memory_dir contains .md
            files, indicating a likely misconfiguration rather than first run.
    """
    _ensure_db()
    db = sqlite3.connect(config.db_path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.row_factory = sqlite3.Row
    return db
