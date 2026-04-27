"""
Tests for palinode doctor index, disk, and forward-looking checks.

Covers:
  - db_size_sanity
  - chunks_match_md_count
  - reindex_in_progress
  - git_remote_health
  - claude_md_palinode_block
  - audit_log_writable

All filesystem-dependent tests use tmp_path.
git_remote_health mocks subprocess.run.
claude_md_palinode_block monkeypatches Path.home().
No SQLite mocking (project standard: real DBs in tmp_path).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from palinode.core.config import Config, AuditConfig
from palinode.diagnostics.runner import run_one
from palinode.diagnostics.types import DoctorContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sqlite_db(path: Path) -> None:
    """Create a minimal SQLite DB with an empty chunks table."""
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


def _insert_chunks(db_path: Path, n: int) -> None:
    con = sqlite3.connect(str(db_path))
    try:
        for i in range(n):
            con.execute("INSERT INTO chunks (content) VALUES (?)", (f"chunk {i}",))
        con.commit()
    finally:
        con.close()


def _ctx(
    memory_dir: Path,
    db_path: Path | None = None,
    audit_enabled: bool = True,
    audit_log_path: str | None = None,
) -> DoctorContext:
    resolved_db = db_path if db_path is not None else (memory_dir / ".palinode.db")
    cfg = Config(
        memory_dir=str(memory_dir),
        db_path=str(resolved_db),
    )
    cfg.audit.enabled = audit_enabled
    if audit_log_path is not None:
        cfg.audit.log_path = audit_log_path
    return DoctorContext(config=cfg)


# ===========================================================================
# db_size_sanity
# ===========================================================================

class TestDbSizeSanity:
    def test_first_run_records_baseline_and_passes(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        db = memory_dir / ".palinode.db"
        _make_sqlite_db(db)

        log_path = memory_dir / ".palinode" / "db_size.log"
        assert not log_path.exists(), "Log should not exist before first run"

        ctx = _ctx(memory_dir, db)
        result = run_one(ctx, "db_size_sanity")

        assert result.passed is True
        assert result.severity == "warn"
        assert log_path.exists(), "Baseline log should be created"
        lines = [l for l in log_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        # Verify baseline log format: timestamp size_bytes chunks
        parts = lines[0].split()
        assert len(parts) == 3
        # Timestamp should contain T and Z (ISO-8601 UTC)
        assert "T" in parts[0] and parts[0].endswith("Z")
        assert int(parts[1]) >= 0  # size_bytes
        assert int(parts[2]) >= 0  # chunks

    def test_no_shrinkage_passes(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        db = memory_dir / ".palinode.db"
        _make_sqlite_db(db)

        # Simulate a previous run with same size.
        db_size = db.stat().st_size
        log_dir = memory_dir / ".palinode"
        log_dir.mkdir()
        log_path = log_dir / "db_size.log"
        log_path.write_text(f"2026-04-26T10:00:00Z {db_size} 100\n")

        ctx = _ctx(memory_dir, db)
        result = run_one(ctx, "db_size_sanity")

        assert result.passed is True

    def test_large_shrinkage_warns(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        db = memory_dir / ".palinode.db"
        _make_sqlite_db(db)

        # Current DB is small (just created); pretend previous was 10x larger.
        current_size = db.stat().st_size
        previous_size = current_size * 20  # 20x larger → well above 50% threshold

        log_dir = memory_dir / ".palinode"
        log_dir.mkdir()
        log_path = log_dir / "db_size.log"
        log_path.write_text(f"2026-04-26T10:00:00Z {previous_size} 500\n")

        ctx = _ctx(memory_dir, db)
        result = run_one(ctx, "db_size_sanity")

        assert result.passed is False
        assert result.severity == "warn"
        assert "shrunk" in result.message.lower() or "dropped" in result.message.lower()
        assert result.remediation is not None
        assert "phantom_db_files" in result.remediation

    def test_shrinkage_at_exactly_50pct_does_not_warn(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        db = memory_dir / ".palinode.db"
        _make_sqlite_db(db)

        current_size = db.stat().st_size
        # Previous exactly double — ratio = 0.5, threshold is < 0.5.
        previous_size = current_size * 2

        log_dir = memory_dir / ".palinode"
        log_dir.mkdir()
        log_path = log_dir / "db_size.log"
        log_path.write_text(f"2026-04-26T10:00:00Z {previous_size} 100\n")

        ctx = _ctx(memory_dir, db)
        result = run_one(ctx, "db_size_sanity")

        # ratio == 0.5 is NOT < 0.5 → should pass
        assert result.passed is True

    def test_appends_new_log_line_on_each_run(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        db = memory_dir / ".palinode.db"
        _make_sqlite_db(db)

        log_dir = memory_dir / ".palinode"
        log_dir.mkdir()
        log_path = log_dir / "db_size.log"
        current_size = db.stat().st_size
        log_path.write_text(f"2026-04-26T10:00:00Z {current_size} 50\n")

        ctx = _ctx(memory_dir, db)
        run_one(ctx, "db_size_sanity")  # first call

        lines = [l for l in log_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 2, "Should append one new line per run"

    def test_missing_db_warns(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        db = memory_dir / ".palinode.db"
        # Do NOT create the DB.

        ctx = _ctx(memory_dir, db)
        result = run_one(ctx, "db_size_sanity")

        assert result.passed is False
        assert result.severity == "warn"
        assert "does not exist" in result.message

    def test_baseline_log_format(self, tmp_path: Path) -> None:
        """Baseline log format: <ISO-8601-Z> <size_bytes> <chunks>"""
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        db = memory_dir / ".palinode.db"
        _make_sqlite_db(db)

        ctx = _ctx(memory_dir, db)
        run_one(ctx, "db_size_sanity")

        log_path = memory_dir / ".palinode" / "db_size.log"
        line = log_path.read_text().strip()
        parts = line.split()
        assert len(parts) == 3, f"Expected 3 fields, got: {parts!r}"
        ts, size_str, chunks_str = parts
        assert ts.endswith("Z"), "Timestamp must end with Z (UTC)"
        assert "T" in ts, "Timestamp must contain T (ISO-8601)"
        assert int(size_str) >= 0
        assert int(chunks_str) >= 0


# ===========================================================================
# chunks_match_md_count
# ===========================================================================

class TestChunksMatchMdCount:
    def test_no_md_files_passes(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        db = memory_dir / ".palinode.db"
        _make_sqlite_db(db)

        ctx = _ctx(memory_dir, db)
        result = run_one(ctx, "chunks_match_md_count")

        assert result.passed is True

    def test_sufficient_chunks_passes(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        db = memory_dir / ".palinode.db"
        _make_sqlite_db(db)

        # 4 md files, 4 chunks → ratio 1.0 → pass
        for i in range(4):
            (memory_dir / f"note{i}.md").write_text(f"# Note {i}")
        _insert_chunks(db, 4)

        ctx = _ctx(memory_dir, db)
        result = run_one(ctx, "chunks_match_md_count")

        assert result.passed is True
        assert "ratio" in result.message

    def test_chunks_above_50pct_passes(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        db = memory_dir / ".palinode.db"
        _make_sqlite_db(db)

        # 10 md files, 6 chunks → ratio 0.6 → pass (consolidation scenario)
        for i in range(10):
            (memory_dir / f"note{i}.md").write_text(f"# Note {i}")
        _insert_chunks(db, 6)

        ctx = _ctx(memory_dir, db)
        result = run_one(ctx, "chunks_match_md_count")

        assert result.passed is True

    def test_chunks_below_50pct_warns(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        db = memory_dir / ".palinode.db"
        _make_sqlite_db(db)

        # 10 md files, 2 chunks → ratio 0.2 → warn
        for i in range(10):
            (memory_dir / f"note{i}.md").write_text(f"# Note {i}")
        _insert_chunks(db, 2)

        ctx = _ctx(memory_dir, db)
        result = run_one(ctx, "chunks_match_md_count")

        assert result.passed is False
        assert result.severity == "warn"
        assert "10" in result.message  # md file count
        assert "2" in result.message   # chunk count
        assert result.remediation is not None
        assert "reindex" in result.remediation

    def test_zero_chunks_with_md_files_warns(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        db = memory_dir / ".palinode.db"
        _make_sqlite_db(db)

        for i in range(5):
            (memory_dir / f"note{i}.md").write_text(f"# Note {i}")
        # DB has 0 chunks.

        ctx = _ctx(memory_dir, db)
        result = run_one(ctx, "chunks_match_md_count")

        assert result.passed is False
        assert result.severity == "warn"

    def test_missing_db_warns(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        db = memory_dir / ".palinode.db"
        # DB does not exist.
        (memory_dir / "note.md").write_text("# Note")

        ctx = _ctx(memory_dir, db)
        result = run_one(ctx, "chunks_match_md_count")

        assert result.passed is False
        assert result.severity == "warn"

    def test_hidden_palinode_md_files_excluded(self, tmp_path: Path) -> None:
        """*.md files inside .palinode/ hidden dir should not count."""
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        db = memory_dir / ".palinode.db"
        _make_sqlite_db(db)

        # Real md file
        (memory_dir / "note.md").write_text("# Note")
        # Hidden md file — should NOT be counted
        palinode_dir = memory_dir / ".palinode"
        palinode_dir.mkdir()
        (palinode_dir / "meta.md").write_text("# Meta")

        _insert_chunks(db, 1)

        ctx = _ctx(memory_dir, db)
        result = run_one(ctx, "chunks_match_md_count")

        # Should see 1 md file (not 2), 1 chunk → ratio 1.0 → pass
        assert result.passed is True


# ===========================================================================
# reindex_in_progress
# ===========================================================================

class TestReindexInProgress:
    def test_api_unreachable_returns_info(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()

        # Point at a port that will refuse connections.
        cfg = Config(memory_dir=str(memory_dir), db_path=str(memory_dir / ".palinode.db"))
        cfg.services.api.host = "127.0.0.1"
        cfg.services.api.port = 19999  # unlikely to be in use
        ctx = DoctorContext(config=cfg)

        result = run_one(ctx, "reindex_in_progress")

        # Should degrade gracefully, not raise.
        assert result.severity == "info"
        assert result.passed is True

    def test_idle_reindex_passes(self, tmp_path: Path) -> None:
        """Mock /status returning running=False → idle."""
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        ctx = _ctx(memory_dir)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "reindex": {"running": False, "started_at": None,
                        "files_processed": 0, "total_files": 0}
        }

        with patch(
            "palinode.diagnostics.checks.reindex_state.httpx.get",
            return_value=mock_resp,
        ):
            result = run_one(ctx, "reindex_in_progress")

        assert result.passed is True
        assert result.severity == "info"
        assert "idle" in result.message

    def test_running_reindex_is_info(self, tmp_path: Path) -> None:
        """A running reindex that isn't stuck should be info, not warn."""
        from datetime import datetime, timezone, timedelta

        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        ctx = _ctx(memory_dir)

        recent_start = (
            datetime.now(tz=timezone.utc) - timedelta(minutes=2)
        ).isoformat().replace("+00:00", "Z")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "reindex": {
                "running": True,
                "started_at": recent_start,
                "files_processed": 50,
                "total_files": 200,
            }
        }

        with patch(
            "palinode.diagnostics.checks.reindex_state.httpx.get",
            return_value=mock_resp,
        ):
            result = run_one(ctx, "reindex_in_progress")

        assert result.severity == "info"
        # A running (non-stuck) reindex may pass or not, but must not be warn.
        assert result.severity != "warn"

    def test_stuck_reindex_warns(self, tmp_path: Path) -> None:
        """A reindex that started 60 minutes ago should warn."""
        from datetime import datetime, timezone, timedelta

        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        ctx = _ctx(memory_dir)

        old_start = (
            datetime.now(tz=timezone.utc) - timedelta(minutes=60)
        ).isoformat().replace("+00:00", "Z")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "reindex": {
                "running": True,
                "started_at": old_start,
                "files_processed": 1,
                "total_files": 500,
            }
        }

        with patch(
            "palinode.diagnostics.checks.reindex_state.httpx.get",
            return_value=mock_resp,
        ):
            result = run_one(ctx, "reindex_in_progress")

        assert result.passed is False
        assert result.severity == "warn"
        assert "stuck" in result.message.lower()

    def test_bad_status_code_is_info(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        ctx = _ctx(memory_dir)

        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch(
            "palinode.diagnostics.checks.reindex_state.httpx.get",
            return_value=mock_resp,
        ):
            result = run_one(ctx, "reindex_in_progress")

        assert result.severity == "info"


# ===========================================================================
# git_remote_health
# ===========================================================================

class TestGitRemoteHealth:
    def _patch_run(self, responses: dict[str, Any]):
        """Return a context manager that patches subprocess.run.

        ``responses`` maps the third argv element (e.g. 'rev-parse') to a
        fake CompletedProcess or an exception class to raise.
        """
        def _fake_run(cmd, **kwargs):
            key = cmd[2] if len(cmd) > 2 else ""
            val = responses.get(key)
            if val is None:
                # Default: not a git repo
                return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="")
            if isinstance(val, type) and issubclass(val, Exception):
                raise val()
            return val

        return patch(
            "palinode.diagnostics.checks.git_remote._run_git",
            side_effect=lambda args, cwd, timeout: _fake_run(["git", "-C", cwd] + args),
        )

    def test_not_a_git_repo_returns_info(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        ctx = _ctx(memory_dir)

        with patch(
            "palinode.diagnostics.checks.git_remote._run_git",
            return_value=subprocess.CompletedProcess([], returncode=128, stdout="", stderr=""),
        ):
            result = run_one(ctx, "git_remote_health")

        assert result.severity == "info"
        assert result.passed is True
        assert "not a git repository" in result.message.lower()

    def test_no_remote_returns_info(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        ctx = _ctx(memory_dir)

        def _side(args, cwd, timeout):
            if args[0] == "rev-parse":
                return subprocess.CompletedProcess([], 0, stdout=".git\n", stderr="")
            # remote get-url origin fails
            return subprocess.CompletedProcess([], 128, stdout="", stderr="No such remote")

        with patch(
            "palinode.diagnostics.checks.git_remote._run_git",
            side_effect=_side,
        ):
            result = run_one(ctx, "git_remote_health")

        assert result.severity == "info"
        assert result.passed is True

    def test_remote_reachable_passes(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        ctx = _ctx(memory_dir)

        def _side(args, cwd, timeout):
            if args[0] == "rev-parse":
                return subprocess.CompletedProcess([], 0, stdout=".git\n", stderr="")
            if args[0] == "remote":
                return subprocess.CompletedProcess([], 0, stdout="git@github.com:user/mem.git\n", stderr="")
            if args[0] == "ls-remote":
                return subprocess.CompletedProcess([], 0, stdout="abc123\tHEAD\n", stderr="")
            if args[0] == "rev-list":
                return subprocess.CompletedProcess([], 0, stdout="3\n", stderr="")
            return subprocess.CompletedProcess([], 1, stdout="", stderr="")

        with patch(
            "palinode.diagnostics.checks.git_remote._run_git",
            side_effect=_side,
        ):
            result = run_one(ctx, "git_remote_health")

        assert result.passed is True
        assert result.severity == "warn"  # ceiling severity for this check

    def test_remote_unreachable_warns(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        ctx = _ctx(memory_dir)

        def _side(args, cwd, timeout):
            if args[0] == "rev-parse":
                return subprocess.CompletedProcess([], 0, stdout=".git\n", stderr="")
            if args[0] == "remote":
                return subprocess.CompletedProcess([], 0, stdout="git@github.com:user/mem.git\n", stderr="")
            if args[0] == "ls-remote":
                return subprocess.CompletedProcess([], 128, stdout="", stderr="Connection refused")
            return subprocess.CompletedProcess([], 1, stdout="", stderr="")

        with patch(
            "palinode.diagnostics.checks.git_remote._run_git",
            side_effect=_side,
        ):
            result = run_one(ctx, "git_remote_health")

        assert result.passed is False
        assert result.severity == "warn"
        assert "Connection refused" in (result.remediation or "")

    def test_ls_remote_timeout_warns(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        ctx = _ctx(memory_dir)

        def _side(args, cwd, timeout):
            if args[0] == "rev-parse":
                return subprocess.CompletedProcess([], 0, stdout=".git\n", stderr="")
            if args[0] == "remote":
                return subprocess.CompletedProcess([], 0, stdout="git@github.com:user/mem.git\n", stderr="")
            if args[0] == "ls-remote":
                raise subprocess.TimeoutExpired(cmd="git", timeout=8)
            return subprocess.CompletedProcess([], 1, stdout="", stderr="")

        with patch(
            "palinode.diagnostics.checks.git_remote._run_git",
            side_effect=_side,
        ):
            result = run_one(ctx, "git_remote_health")

        assert result.passed is False
        assert result.severity == "warn"
        assert "timed out" in result.message.lower()

    def test_many_unpushed_commits_warns(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        ctx = _ctx(memory_dir)

        def _side(args, cwd, timeout):
            if args[0] == "rev-parse":
                return subprocess.CompletedProcess([], 0, stdout=".git\n", stderr="")
            if args[0] == "remote":
                return subprocess.CompletedProcess([], 0, stdout="git@github.com:user/mem.git\n", stderr="")
            if args[0] == "ls-remote":
                return subprocess.CompletedProcess([], 0, stdout="abc123\tHEAD\n", stderr="")
            if args[0] == "rev-list":
                return subprocess.CompletedProcess([], 0, stdout="75\n", stderr="")
            return subprocess.CompletedProcess([], 1, stdout="", stderr="")

        with patch(
            "palinode.diagnostics.checks.git_remote._run_git",
            side_effect=_side,
        ):
            result = run_one(ctx, "git_remote_health")

        assert result.passed is False
        assert result.severity == "warn"
        assert "75" in result.message

    def test_git_not_found_warns(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        ctx = _ctx(memory_dir)

        with patch(
            "palinode.diagnostics.checks.git_remote._run_git",
            side_effect=FileNotFoundError("git: command not found"),
        ):
            result = run_one(ctx, "git_remote_health")

        assert result.passed is False
        assert "git binary not found" in result.message.lower()


# ===========================================================================
# claude_md_palinode_block
# ===========================================================================

class TestClaudeMdPalinodeBlock:
    def test_global_claude_md_with_palinode_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        claude_dir = home / ".claude"
        claude_dir.mkdir()
        (claude_dir / "CLAUDE.md").write_text(
            "# Claude config\n\n## Memory (Palinode)\nUse palinode_search.\n"
        )

        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.chdir(home)

        ctx = _ctx(home)
        result = run_one(ctx, "claude_md_palinode_block")

        assert result.passed is True
        assert result.severity == "warn"

    def test_project_claude_md_with_palinode_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        project = home / "myproject"
        project.mkdir()
        (project / "CLAUDE.md").write_text("Use palinode.\n")

        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.chdir(project)

        ctx = _ctx(home)
        result = run_one(ctx, "claude_md_palinode_block")

        assert result.passed is True

    def test_no_palinode_mention_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        claude_dir = home / ".claude"
        claude_dir.mkdir()
        (claude_dir / "CLAUDE.md").write_text(
            "# Claude config\n\nDo not forget to commit.\n"
        )

        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.chdir(home)

        ctx = _ctx(home)
        result = run_one(ctx, "claude_md_palinode_block")

        assert result.passed is False
        assert result.severity == "warn"
        assert result.remediation is not None
        assert "palinode init" in result.remediation

    def test_no_claude_md_anywhere_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "empty-home"
        home.mkdir()

        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.chdir(home)

        ctx = _ctx(home)
        result = run_one(ctx, "claude_md_palinode_block")

        assert result.passed is False
        assert result.severity == "warn"

    def test_case_insensitive_palinode_match(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        claude_dir = home / ".claude"
        claude_dir.mkdir()
        # Mixed-case mention
        (claude_dir / "CLAUDE.md").write_text("PALINODE is configured here.\n")

        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.chdir(home)

        ctx = _ctx(home)
        result = run_one(ctx, "claude_md_palinode_block")

        assert result.passed is True

    def test_only_global_with_palinode_covers_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Global ~/.claude/CLAUDE.md with palinode is sufficient even if project CLAUDE.md lacks it."""
        home = tmp_path / "home"
        home.mkdir()
        claude_dir = home / ".claude"
        claude_dir.mkdir()
        (claude_dir / "CLAUDE.md").write_text("palinode_save after milestones.\n")

        project = home / "myproject"
        project.mkdir()
        (project / "CLAUDE.md").write_text("# Project — no memory instructions here\n")

        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.chdir(project)

        ctx = _ctx(home)
        result = run_one(ctx, "claude_md_palinode_block")

        assert result.passed is True


# ===========================================================================
# audit_log_writable
# ===========================================================================

class TestAuditLogWritable:
    def test_audit_disabled_passes(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        ctx = _ctx(memory_dir, audit_enabled=False)
        result = run_one(ctx, "audit_log_writable")

        assert result.passed is True
        assert "disabled" in result.message

    def test_absolute_writable_path_passes(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        audit_dir = memory_dir / ".audit"
        audit_dir.mkdir()
        audit_log = audit_dir / "mcp-calls.jsonl"

        ctx = _ctx(
            memory_dir,
            audit_log_path=str(audit_log),
        )
        result = run_one(ctx, "audit_log_writable")

        assert result.passed is True
        assert result.severity == "warn"

    def test_relative_path_warns(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()

        # Relative path (the default .audit/mcp-calls.jsonl pattern)
        ctx = _ctx(
            memory_dir,
            audit_log_path=".audit/mcp-calls.jsonl",
        )
        result = run_one(ctx, "audit_log_writable")

        assert result.passed is False
        assert result.severity == "warn"
        assert "relative" in result.message.lower()
        assert result.remediation is not None
        assert "absolute" in result.remediation.lower() or "memory_dir" in result.remediation

    def test_missing_parent_directory_warns(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        # Absolute path but parent does not exist
        audit_log = tmp_path / "nonexistent-dir" / "mcp-calls.jsonl"

        ctx = _ctx(
            memory_dir,
            audit_log_path=str(audit_log),
        )
        result = run_one(ctx, "audit_log_writable")

        assert result.passed is False
        assert result.severity == "warn"
        assert "does not exist" in result.message.lower() or "parent" in result.message.lower()

    def test_default_relative_log_path_warns(self, tmp_path: Path) -> None:
        """The default config uses '.audit/mcp-calls.jsonl' — should warn."""
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        ctx = _ctx(memory_dir)
        # Default audit.log_path is '.audit/mcp-calls.jsonl' (relative)
        # Config default should trigger the relative-path warning.
        result = run_one(ctx, "audit_log_writable")

        assert result.passed is False
        assert "relative" in result.message.lower()

    def test_existing_writable_file_passes(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        audit_dir = memory_dir / ".audit"
        audit_dir.mkdir()
        audit_log = audit_dir / "mcp-calls.jsonl"
        audit_log.write_text("")  # Create the file

        ctx = _ctx(
            memory_dir,
            audit_log_path=str(audit_log),
        )
        result = run_one(ctx, "audit_log_writable")

        assert result.passed is True
