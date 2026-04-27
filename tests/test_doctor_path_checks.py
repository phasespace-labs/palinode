"""
Tests for palinode doctor path integrity checks.

Covers:
  - db_path_resolvable  (pass: valid DB, fail: missing parent, fail: not-a-DB)
  - db_path_under_memory_dir  (pass: inside, fail: outside)
  - phantom_db_files  (pass: 0 DBs, pass: exactly 1 = configured, fail: 2+)
  - multiple_palinode_dirs  (pass: no env var, pass: matching env+config, fail: mismatch)
  - config.doctor.search_roots  (YAML-configurable extra roots honored)

All fixtures use real tmp_path directories and real SQLite databases.
No mocking of SQLite — project standard.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from palinode.core.config import Config
from palinode.diagnostics.runner import run_one
from palinode.diagnostics.types import DoctorContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sqlite_db(path: Path) -> None:
    """Create a minimal valid SQLite database at *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS chunks "
            "(id INTEGER PRIMARY KEY, content TEXT)"
        )
        con.commit()
    finally:
        con.close()


def _ctx(
    memory_dir: Path,
    db_path: Path | None = None,
    search_roots: list[str] | None = None,
) -> DoctorContext:
    """Build a DoctorContext for test use."""
    resolved_db = db_path if db_path is not None else (memory_dir / ".palinode.db")
    cfg = Config(
        memory_dir=str(memory_dir),
        db_path=str(resolved_db),
    )
    if search_roots is not None:
        cfg.doctor.search_roots = search_roots
    return DoctorContext(config=cfg)


# ---------------------------------------------------------------------------
# db_path_resolvable
# ---------------------------------------------------------------------------

class TestDbPathResolvable:
    def test_passes_when_db_is_valid_sqlite(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        db = memory_dir / ".palinode.db"
        _make_sqlite_db(db)

        ctx = _ctx(memory_dir, db)
        result = run_one(ctx, "db_path_resolvable")

        assert result.passed is True
        assert result.severity == "error"
        assert result.remediation is None

    def test_fails_when_parent_dir_missing(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        db = tmp_path / "nonexistent_dir" / ".palinode.db"
        # Do NOT create the parent directory.

        ctx = _ctx(memory_dir, db)
        result = run_one(ctx, "db_path_resolvable")

        assert result.passed is False
        assert result.severity == "error"
        assert result.remediation is not None
        assert "parent directory" in result.remediation

    def test_fails_when_file_is_not_sqlite(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        db = memory_dir / ".palinode.db"
        # Write garbage bytes — not a valid SQLite DB.
        db.write_bytes(b"this is not a sqlite database at all")

        ctx = _ctx(memory_dir, db)
        result = run_one(ctx, "db_path_resolvable")

        assert result.passed is False
        assert result.severity == "error"

    def test_fails_when_file_absent_but_parent_exists(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        db = memory_dir / ".palinode.db"
        # Parent exists but file does not — sqlite mode=ro refuses to create.

        ctx = _ctx(memory_dir, db)
        result = run_one(ctx, "db_path_resolvable")

        assert result.passed is False

    def test_remediation_mentions_db_path(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        db = memory_dir / ".palinode.db"
        # File absent.

        ctx = _ctx(memory_dir, db)
        result = run_one(ctx, "db_path_resolvable")

        assert result.remediation is not None
        assert "palinode.config.yaml" in result.remediation


# ---------------------------------------------------------------------------
# db_path_under_memory_dir
# ---------------------------------------------------------------------------

class TestDbPathUnderMemoryDir:
    def test_passes_when_db_inside_memory_dir(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        db = memory_dir / ".palinode.db"

        ctx = _ctx(memory_dir, db)
        result = run_one(ctx, "db_path_under_memory_dir")

        assert result.passed is True
        assert result.severity == "warn"

    def test_passes_when_db_in_subdirectory_of_memory_dir(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        subdir = memory_dir / ".palinode"
        subdir.mkdir()
        db = subdir / ".palinode.db"

        ctx = _ctx(memory_dir, db)
        result = run_one(ctx, "db_path_under_memory_dir")

        assert result.passed is True

    def test_fails_when_db_outside_memory_dir(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode-data"
        memory_dir.mkdir()
        # DB is in a sibling directory, which is the expected drift pattern.
        other_dir = tmp_path / "stale-data"
        other_dir.mkdir()
        db = other_dir / ".palinode.db"

        ctx = _ctx(memory_dir, db)
        result = run_one(ctx, "db_path_under_memory_dir")

        assert result.passed is False
        assert result.severity == "warn"

    def test_fail_remediation_contains_suggestion(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode-data"
        memory_dir.mkdir()
        other_dir = tmp_path / "stale-data"
        other_dir.mkdir()
        db = other_dir / ".palinode.db"

        ctx = _ctx(memory_dir, db)
        result = run_one(ctx, "db_path_under_memory_dir")

        assert result.remediation is not None
        # Should suggest the canonical path inside memory_dir.
        assert ".palinode.db" in result.remediation
        assert str(memory_dir.resolve()) in result.remediation

    def test_fail_message_shows_both_paths(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode-data"
        memory_dir.mkdir()
        other_dir = tmp_path / "stale-data"
        other_dir.mkdir()
        db = other_dir / ".palinode.db"

        ctx = _ctx(memory_dir, db)
        result = run_one(ctx, "db_path_under_memory_dir")

        assert str(memory_dir.resolve()) in result.message
        assert str(other_dir.resolve()) in result.message


# ---------------------------------------------------------------------------
# phantom_db_files
# ---------------------------------------------------------------------------

class TestPhantomDbFiles:
    def test_passes_when_no_dbs_found(self, tmp_path: Path) -> None:
        """Explicit isolated roots, no .palinode.db present — should pass."""
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        # DB does not exist on disk.  Pass search_roots to avoid scanning the
        # real filesystem (built-ins are bypassed when search_roots is non-empty).
        ctx = _ctx(memory_dir, search_roots=[str(memory_dir)])
        result = run_one(ctx, "phantom_db_files")

        assert result.passed is True
        assert result.severity == "info"

    def test_passes_when_exactly_one_db_at_configured_path(
        self, tmp_path: Path
    ) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        db = memory_dir / ".palinode.db"
        _make_sqlite_db(db)

        # Explicit roots: only search this isolated tmp dir.
        ctx = _ctx(memory_dir, db, search_roots=[str(memory_dir)])
        result = run_one(ctx, "phantom_db_files")

        assert result.passed is True
        assert result.severity == "info"

    def test_fails_when_two_distinct_dbs_found(self, tmp_path: Path) -> None:
        """Two separate .palinode.db files (different inodes) → critical."""
        memory_dir = tmp_path / "palinode-data"
        memory_dir.mkdir()
        db_configured = memory_dir / ".palinode.db"
        _make_sqlite_db(db_configured)

        other_dir = tmp_path / "old-palinode"
        other_dir.mkdir()
        db_phantom = other_dir / ".palinode.db"
        _make_sqlite_db(db_phantom)

        ctx = _ctx(
            memory_dir,
            db_configured,
            search_roots=[str(memory_dir), str(other_dir)],
        )
        result = run_one(ctx, "phantom_db_files")

        assert result.passed is False
        assert result.severity == "critical"
        assert str(db_phantom.resolve()) in result.message

    def test_fails_with_three_dbs(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        db_configured = memory_dir / ".palinode.db"
        _make_sqlite_db(db_configured)

        dirs = [tmp_path / f"old-{i}" for i in range(2)]
        for d in dirs:
            d.mkdir()
            _make_sqlite_db(d / ".palinode.db")

        ctx = _ctx(
            memory_dir,
            db_configured,
            search_roots=[str(memory_dir)] + [str(d) for d in dirs],
        )
        result = run_one(ctx, "phantom_db_files")

        assert result.passed is False
        assert result.severity == "critical"

    def test_non_sqlite_file_is_ignored(self, tmp_path: Path) -> None:
        """A file named .palinode.db with wrong magic bytes must be ignored."""
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        configured_db = memory_dir / ".palinode.db"
        _make_sqlite_db(configured_db)

        other_dir = tmp_path / "other"
        other_dir.mkdir()
        fake_db = other_dir / ".palinode.db"
        fake_db.write_bytes(b"not a sqlite file at all, just garbage")

        # Explicit roots: only these two isolated dirs (bypasses built-ins).
        ctx = _ctx(
            memory_dir,
            configured_db,
            search_roots=[str(memory_dir), str(other_dir)],
        )
        result = run_one(ctx, "phantom_db_files")

        # fake_db should be filtered out by magic-byte check; only the real DB remains.
        assert result.passed is True

    def test_inode_deduplication(self, tmp_path: Path) -> None:
        """Same file seen via two different roots should be counted once."""
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        db = memory_dir / ".palinode.db"
        _make_sqlite_db(db)

        # Both roots resolve to the same directory — same inode will be seen twice
        # if dedup were not in place.  Explicit roots bypass built-ins.
        ctx = _ctx(
            memory_dir,
            db,
            search_roots=[str(memory_dir), str(memory_dir)],
        )
        result = run_one(ctx, "phantom_db_files")

        # Still only one file → pass (no phantoms).
        assert result.passed is True

    def test_custom_search_roots_honored(self, tmp_path: Path) -> None:
        """YAML-configurable extra roots are actually walked."""
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        configured_db = memory_dir / ".palinode.db"
        _make_sqlite_db(configured_db)

        custom_root = tmp_path / "custom-root"
        custom_root.mkdir()
        phantom_db = custom_root / ".palinode.db"
        _make_sqlite_db(phantom_db)

        ctx = _ctx(
            memory_dir,
            configured_db,
            # custom_root is not in any built-in list; pass via search_roots.
            search_roots=[str(memory_dir), str(custom_root)],
        )
        result = run_one(ctx, "phantom_db_files")

        assert result.passed is False
        assert str(phantom_db.resolve()) in result.message

    def test_remediation_contains_mv_command(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        configured_db = memory_dir / ".palinode.db"
        _make_sqlite_db(configured_db)

        phantom_dir = tmp_path / "old"
        phantom_dir.mkdir()
        phantom = phantom_dir / ".palinode.db"
        _make_sqlite_db(phantom)

        ctx = _ctx(
            memory_dir,
            configured_db,
            search_roots=[str(memory_dir), str(phantom_dir)],
        )
        result = run_one(ctx, "phantom_db_files")

        assert result.remediation is not None
        assert "mv" in result.remediation
        assert ".bak" in result.remediation

    def test_chunk_count_reported_in_message(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        configured_db = memory_dir / ".palinode.db"
        _make_sqlite_db(configured_db)

        phantom_dir = tmp_path / "old"
        phantom_dir.mkdir()
        phantom = phantom_dir / ".palinode.db"
        _make_sqlite_db(phantom)
        # Add a chunk so the count is readable.
        con = sqlite3.connect(str(phantom))
        con.execute("INSERT INTO chunks (content) VALUES ('hi')")
        con.commit()
        con.close()

        ctx = _ctx(
            memory_dir,
            configured_db,
            search_roots=[str(memory_dir), str(phantom_dir)],
        )
        result = run_one(ctx, "phantom_db_files")

        # The chunk count ("1 chunks") should appear in the detail.
        assert "chunks" in result.message


# ---------------------------------------------------------------------------
# multiple_palinode_dirs
# ---------------------------------------------------------------------------

class TestMultiplePalinodeDirs:
    def test_passes_when_env_not_set(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("PALINODE_DIR", raising=False)
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()

        ctx = _ctx(memory_dir)
        result = run_one(ctx, "multiple_palinode_dirs")

        assert result.passed is True
        assert result.severity == "warn"

    def test_passes_when_env_matches_config(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        monkeypatch.setenv("PALINODE_DIR", str(memory_dir))

        # Build ctx manually so config.memory_dir matches env.
        cfg = Config(
            memory_dir=str(memory_dir),
            db_path=str(memory_dir / ".palinode.db"),
        )
        ctx = DoctorContext(config=cfg)
        result = run_one(ctx, "multiple_palinode_dirs")

        assert result.passed is True

    def test_fails_when_env_differs_from_config(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        memory_dir_config = tmp_path / "palinode-data"
        memory_dir_config.mkdir()
        memory_dir_env = tmp_path / "stale-data"
        memory_dir_env.mkdir()

        # Set env to a DIFFERENT path than what config.memory_dir says.
        monkeypatch.setenv("PALINODE_DIR", str(memory_dir_env))

        # Build config pointing at config-dir (simulating YAML before env override).
        cfg = Config(
            memory_dir=str(memory_dir_config),
            db_path=str(memory_dir_config / ".palinode.db"),
        )
        ctx = DoctorContext(config=cfg)
        result = run_one(ctx, "multiple_palinode_dirs")

        assert result.passed is False
        assert result.severity == "warn"

    def test_fail_message_shows_both_paths(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        memory_dir_config = tmp_path / "palinode-data"
        memory_dir_config.mkdir()
        memory_dir_env = tmp_path / "stale-data"
        memory_dir_env.mkdir()

        monkeypatch.setenv("PALINODE_DIR", str(memory_dir_env))

        cfg = Config(
            memory_dir=str(memory_dir_config),
            db_path=str(memory_dir_config / ".palinode.db"),
        )
        ctx = DoctorContext(config=cfg)
        result = run_one(ctx, "multiple_palinode_dirs")

        assert str(memory_dir_env.resolve()) in result.message
        assert str(memory_dir_config.resolve()) in result.message

    def test_fail_remediation_contains_fix_instructions(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        memory_dir_config = tmp_path / "palinode-data"
        memory_dir_config.mkdir()
        memory_dir_env = tmp_path / "stale-data"
        memory_dir_env.mkdir()

        monkeypatch.setenv("PALINODE_DIR", str(memory_dir_env))

        cfg = Config(
            memory_dir=str(memory_dir_config),
            db_path=str(memory_dir_config / ".palinode.db"),
        )
        ctx = DoctorContext(config=cfg)
        result = run_one(ctx, "multiple_palinode_dirs")

        assert result.remediation is not None
        assert "palinode.config.yaml" in result.remediation

    def test_tilde_expansion_in_env(self, tmp_path: Path, monkeypatch) -> None:
        """Env var with ~ should expand to the same resolved path as config."""
        memory_dir = Path.home() / ".palinode-test-tmp"
        # We don't actually create it; we just test that ~ expansion works
        # for matching purposes when both sides expand to the same absolute path.
        monkeypatch.setenv("PALINODE_DIR", "~/.palinode-test-tmp")

        cfg = Config(
            memory_dir=str(memory_dir),
            db_path=str(memory_dir / ".palinode.db"),
        )
        ctx = DoctorContext(config=cfg)
        result = run_one(ctx, "multiple_palinode_dirs")

        # Both sides expand to the same path → should pass.
        assert result.passed is True
