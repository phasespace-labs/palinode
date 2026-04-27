"""
Tests for palinode doctor ``--fix`` mode.

Safety-critical scope: doctor never moves user data, even with ``--fix``.
The whitelist of fixable checks is intentionally tiny:

  - memory_dir_exists       → create directory
  - audit_log_writable      → create parent dir of relative log path
  - claude_md_palinode_block → append block to existing CLAUDE.md

Anything else gets a "no automated fix available" message in --fix mode and
the original remediation is re-printed.  In particular, ``phantom_db_files``
must NEVER be auto-fixed: doctor only prints the suggested ``mv`` command.

Tests use real tmp_path directories and the real CliRunner — no stdin
mocking is needed because the ``--yes`` flag bypasses prompts and the
``--dry-run`` flag exercises the report path without state changes.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

import sys
import palinode.cli.doctor  # noqa: F401 — ensure submodule is imported
from palinode.cli.doctor import doctor as doctor_cmd
from palinode.core.config import Config

# The `palinode.cli` package's __init__ rebinds `doctor` to the click command,
# so `palinode.cli.doctor` resolves to the Command object, not the module.
# Reach the actual module via sys.modules.
doctor_module = sys.modules["palinode.cli.doctor"]
from palinode.diagnostics import fixes as fixes_module
from palinode.diagnostics.registry import all_fixes, get_fix
from palinode.diagnostics.types import CheckResult, DoctorContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sqlite_db(path: Path) -> None:
    """Create a minimal valid SQLite database at *path* (used as the configured DB)."""
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


def _patch_default_config(monkeypatch, memory_dir: Path, db_path: Path) -> Config:
    """Point the CLI's default config at *memory_dir* / *db_path*."""
    cfg = Config(memory_dir=str(memory_dir), db_path=str(db_path))
    monkeypatch.setattr(doctor_module, "_default_config", cfg)
    return cfg


# ---------------------------------------------------------------------------
# Whitelist sanity — the safety-critical contract.
# ---------------------------------------------------------------------------

class TestWhitelist:
    """Locks down the exact set of fixable checks.

    If a fix slips into the registry without an explicit decision, this test
    catches it. Any change to this set requires explicit reasoning in the PR
    description.
    """

    EXPECTED = {
        "memory_dir_exists",
        "audit_log_writable",
        "claude_md_palinode_block",
    }

    def test_fix_registry_matches_whitelist(self) -> None:
        # Ensure import side-effects ran.
        from palinode.diagnostics import runner  # noqa: F401
        registered = set(all_fixes().keys())
        assert registered == self.EXPECTED, (
            f"Fix registry drifted from whitelist. "
            f"Extra: {registered - self.EXPECTED}, "
            f"Missing: {self.EXPECTED - registered}"
        )

    def test_phantom_db_files_has_no_fix(self) -> None:
        """Critical safety property: phantom_db_files must never be auto-fixed."""
        from palinode.diagnostics import runner  # noqa: F401
        assert get_fix("phantom_db_files") is None

    def test_db_path_under_memory_dir_has_no_fix(self) -> None:
        """Moving the DB file is data motion; doctor must refuse."""
        from palinode.diagnostics import runner  # noqa: F401
        assert get_fix("db_path_under_memory_dir") is None

    def test_watcher_indexes_correct_db_has_no_fix(self) -> None:
        """Editing systemd units is a deploy concern, not a doctor concern."""
        from palinode.diagnostics import runner  # noqa: F401
        assert get_fix("watcher_indexes_correct_db") is None


# ---------------------------------------------------------------------------
# fix_memory_dir_exists — unit-level (direct call into the fix function).
# ---------------------------------------------------------------------------

class TestFixMemoryDirExists:
    def test_creates_missing_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "palinode"
        cfg = Config(memory_dir=str(target), db_path=str(target / ".palinode.db"))
        ctx = DoctorContext(config=cfg)
        result = CheckResult(
            name="memory_dir_exists",
            severity="critical",
            passed=False,
            message="missing",
        )

        fix_result = fixes_module.fix_memory_dir_exists(ctx, result)

        assert fix_result.applied is True
        assert target.is_dir()
        assert str(target.resolve()) in fix_result.message

    def test_noop_when_directory_already_exists(self, tmp_path: Path) -> None:
        target = tmp_path / "palinode"
        target.mkdir()
        cfg = Config(memory_dir=str(target), db_path=str(target / ".palinode.db"))
        ctx = DoctorContext(config=cfg)
        result = CheckResult(
            name="memory_dir_exists",
            severity="critical",
            passed=True,
            message="exists",
        )

        fix_result = fixes_module.fix_memory_dir_exists(ctx, result)

        assert fix_result.applied is False
        assert "already exists" in fix_result.message


# ---------------------------------------------------------------------------
# fix_audit_log_writable — unit-level.
# ---------------------------------------------------------------------------

class TestFixAuditLogWritable:
    def test_creates_parent_dir_for_relative_path(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        cfg = Config(memory_dir=str(memory_dir), db_path=str(memory_dir / ".palinode.db"))
        # default audit.log_path is ".audit/mcp-calls.jsonl" (relative)
        ctx = DoctorContext(config=cfg)
        result = CheckResult(
            name="audit_log_writable",
            severity="warn",
            passed=False,
            message="missing",
        )

        fix_result = fixes_module.fix_audit_log_writable(ctx, result)

        assert fix_result.applied is True
        assert (memory_dir / ".audit").is_dir()

    def test_noop_when_absolute_path(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        cfg = Config(memory_dir=str(memory_dir), db_path=str(memory_dir / ".palinode.db"))
        cfg.audit.log_path = str(tmp_path / "elsewhere" / "audit.jsonl")
        ctx = DoctorContext(config=cfg)
        result = CheckResult(
            name="audit_log_writable",
            severity="warn",
            passed=False,
            message="missing",
        )

        fix_result = fixes_module.fix_audit_log_writable(ctx, result)

        assert fix_result.applied is False
        assert "absolute" in fix_result.message
        # Did NOT create the absolute path's parent
        assert not (tmp_path / "elsewhere").exists()

    def test_noop_when_disabled(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        cfg = Config(memory_dir=str(memory_dir), db_path=str(memory_dir / ".palinode.db"))
        cfg.audit.enabled = False
        ctx = DoctorContext(config=cfg)
        result = CheckResult(
            name="audit_log_writable",
            severity="warn",
            passed=False,
            message="disabled",
        )

        fix_result = fixes_module.fix_audit_log_writable(ctx, result)
        assert fix_result.applied is False


# ---------------------------------------------------------------------------
# fix_claude_md_palinode_block — unit-level.
# ---------------------------------------------------------------------------

class TestFixClaudeMdBlock:
    def test_appends_block_to_existing_file(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# My project\n\nSome existing content.\n", encoding="utf-8")

        cfg = Config(memory_dir=str(tmp_path), db_path=str(tmp_path / ".palinode.db"))
        ctx = DoctorContext(config=cfg)
        result = CheckResult(
            name="claude_md_palinode_block",
            severity="info",
            passed=False,
            message="missing block",
        )

        fix_result = fixes_module.fix_claude_md_palinode_block(ctx, result)

        assert fix_result.applied is True
        content = claude_md.read_text(encoding="utf-8")
        assert "Some existing content." in content  # original preserved
        assert "## Memory (Palinode)" in content
        assert "palinode_session_end" in content

    def test_refuses_to_create_missing_claude_md(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        # No CLAUDE.md in cwd.
        cfg = Config(memory_dir=str(tmp_path), db_path=str(tmp_path / ".palinode.db"))
        ctx = DoctorContext(config=cfg)
        result = CheckResult(
            name="claude_md_palinode_block",
            severity="info",
            passed=False,
            message="missing block",
        )

        fix_result = fixes_module.fix_claude_md_palinode_block(ctx, result)

        assert fix_result.applied is False
        assert not (tmp_path / "CLAUDE.md").exists()
        assert "user-owned" in fix_result.message.lower() or "not create" in fix_result.message.lower()

    def test_noop_when_block_already_present(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        claude_md = tmp_path / "CLAUDE.md"
        original = "# Project\n\n## Memory (Palinode)\n\nAlready set up.\n"
        claude_md.write_text(original, encoding="utf-8")

        cfg = Config(memory_dir=str(tmp_path), db_path=str(tmp_path / ".palinode.db"))
        ctx = DoctorContext(config=cfg)
        result = CheckResult(
            name="claude_md_palinode_block",
            severity="info",
            passed=False,
            message="missing block",
        )

        fix_result = fixes_module.fix_claude_md_palinode_block(ctx, result)

        assert fix_result.applied is False
        assert claude_md.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# CLI integration — `palinode doctor --fix` end-to-end.
# ---------------------------------------------------------------------------

class TestFixModeCli:
    def test_fix_with_no_failures_reports_nothing_to_fix(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        db = memory_dir / ".palinode.db"
        _make_sqlite_db(db)
        # Point at a fully-healthy state for the fast subset.  Some deep
        # checks (api_reachable, watcher_alive) may still fail, but that's
        # fine — those aren't fixable and the test only asserts the "nothing
        # to fix" path runs when there are no failed *fixable* results.
        # To get a clean "no failures" state, pin search_roots so phantom_db
        # finds nothing extra and run only the trivial check via --check.
        _patch_default_config(monkeypatch, memory_dir, db)

        runner = CliRunner()
        result = runner.invoke(
            doctor_cmd, ["--fix", "--yes", "--check", "memory_dir_exists"]
        )
        assert "Nothing to fix" in result.output
        assert result.exit_code == 0

    def test_fix_yes_creates_missing_memory_dir(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        memory_dir = tmp_path / "palinode"
        # Do NOT create memory_dir; --fix should create it.
        db = memory_dir / ".palinode.db"
        _patch_default_config(monkeypatch, memory_dir, db)

        runner = CliRunner()
        result = runner.invoke(
            doctor_cmd, ["--fix", "--yes", "--check", "memory_dir_exists"]
        )
        assert memory_dir.is_dir(), result.output
        assert "Created" in result.output or "applied" in result.output.lower()

    def test_fix_dry_run_does_not_apply(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        memory_dir = tmp_path / "palinode"
        # Missing — would normally be created.
        db = memory_dir / ".palinode.db"
        _patch_default_config(monkeypatch, memory_dir, db)

        runner = CliRunner()
        result = runner.invoke(
            doctor_cmd,
            ["--fix", "--dry-run", "--yes", "--check", "memory_dir_exists"],
        )
        # Directory must NOT have been created.
        assert not memory_dir.exists(), (
            f"--dry-run should not create memory_dir; output was:\n{result.output}"
        )
        assert "dry-run" in result.output.lower()
        assert "Would apply fix" in result.output

    def test_fix_no_automated_fix_for_phantom_db_files(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """phantom_db_files must report no automated fix and print the mv suggestion."""
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        configured_db = memory_dir / ".palinode.db"
        _make_sqlite_db(configured_db)

        # Add a phantom DB in a search root.
        phantom_root = tmp_path / "old"
        phantom_root.mkdir()
        phantom_db = phantom_root / ".palinode.db"
        _make_sqlite_db(phantom_db)

        cfg = Config(memory_dir=str(memory_dir), db_path=str(configured_db))
        cfg.doctor.search_roots = [str(phantom_root)]
        monkeypatch.setattr(doctor_module, "_default_config", cfg)

        runner = CliRunner()
        result = runner.invoke(
            doctor_cmd, ["--fix", "--yes", "--check", "phantom_db_files"]
        )
        assert "no automated fix" in result.output.lower()
        # Phantom DB files must NOT have been moved or deleted.
        assert phantom_db.exists()
        assert configured_db.exists()
        # The remediation block re-prints the suggested mv.
        assert "mv " in result.output
        # Failure not fixed → exit non-zero.
        assert result.exit_code != 0

    def test_fix_appends_palinode_block_to_existing_claude_md(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Use the fix function directly; CLI integration of phase-5 check
        comes later when that check lands.  This test guarantees the fix
        works end-to-end on real file state."""
        monkeypatch.chdir(tmp_path)
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# Notes\n", encoding="utf-8")

        cfg = Config(memory_dir=str(tmp_path), db_path=str(tmp_path / ".palinode.db"))
        ctx = DoctorContext(config=cfg)
        # Synthesize the failure result.
        result = CheckResult(
            name="claude_md_palinode_block",
            severity="info",
            passed=False,
            message="missing",
        )

        fix_fn = get_fix("claude_md_palinode_block")
        assert fix_fn is not None
        fix_result = fix_fn(ctx, result)

        assert fix_result.applied is True
        content = claude_md.read_text(encoding="utf-8")
        assert "# Notes" in content
        assert "## Memory (Palinode)" in content
        # Idempotent — second call is a no-op.
        again = fix_fn(ctx, result)
        assert again.applied is False
