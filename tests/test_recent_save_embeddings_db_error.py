"""#384 regression: recent_save_embeddings must log a warning when the DB
open fails, rather than swallowing the sqlite3.Error silently.

The dedup mechanism is best-effort (it returns [] on failure so callers
degrade gracefully), but a silent return makes it invisible when the DB is
misconfigured or temporarily unavailable.  The warning gives operators a
log line to correlate with degraded dedup behaviour.
"""
from __future__ import annotations

import sqlite3

import pytest

from palinode.core import store


def test_recent_save_embeddings_warns_on_db_error(monkeypatch, caplog):
    """sqlite3.Error during DB open must produce a warning log, not silence."""
    import logging

    def _raise_db_error(*_args, **_kwargs):
        raise sqlite3.Error("mocked DB open failure for #384")

    monkeypatch.setattr(store, "get_db", _raise_db_error)

    with caplog.at_level(logging.WARNING, logger="palinode.store"):
        result = store.recent_save_embeddings(window_minutes=60)

    # Return contract preserved: callers get [] on failure
    assert result == []

    # The failure must not be silent
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning_records, (
        "recent_save_embeddings swallowed a sqlite3.Error with no log (#384)"
    )
    combined = " ".join(r.getMessage() for r in warning_records)
    assert "dedup" in combined.lower() or "recent_save_embeddings" in combined.lower(), (
        f"Log message did not mention dedup context. Got: {combined!r}"
    )
    assert "mocked DB open failure" in combined, (
        f"Log message did not include the original exception text. Got: {combined!r}"
    )


def test_recent_save_embeddings_nonpositive_window_returns_early(monkeypatch, caplog):
    """window_minutes <= 0 is a caller-controlled early-exit, not a DB failure."""
    import logging

    call_count = 0

    def _count_get_db(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        raise sqlite3.Error("should not be called")

    monkeypatch.setattr(store, "get_db", _count_get_db)

    with caplog.at_level(logging.WARNING, logger="palinode.store"):
        result = store.recent_save_embeddings(window_minutes=0)

    assert result == []
    assert call_count == 0, "get_db must not be called for non-positive window"
    assert not caplog.records, "no log should be emitted for the non-positive early-exit path"
