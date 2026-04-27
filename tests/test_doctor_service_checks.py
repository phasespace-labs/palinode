"""
Tests for palinode doctor service health checks.

Covers:
  - api_reachable: passes on HTTP 200, fails on connection error, fails on non-200
  - api_status_consistent: passes when /status chunks match disk and fails on obvious DB drift
    footgun (api=0, disk has data), fails when api and disk disagree
  - watcher_alive: passes when systemctl active (Linux) or ps shows process;
    fails when neither; macOS path via ps only
  - watcher_indexes_correct_db: passes when watcher's PALINODE_DIR matches
    config; fails on mismatch; returns info-skip on macOS; warns when no
    PALINODE_DIR in env

All HTTP calls are mocked via unittest.mock.  subprocess.run calls for
systemctl and ps are mocked via monkeypatch.  No real network or process
state is required.  DB access uses real SQLite with tmp_path (project
standard: no SQLite mocking).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest import mock

import pytest

from palinode.core.config import Config
from palinode.diagnostics.checks.service import api_reachable, api_status_consistent
from palinode.diagnostics.checks.watcher import (
    watcher_alive,
    watcher_indexes_correct_db,
    _WATCHER_MODULE,
    _WATCHER_SERVICE,
)
from palinode.diagnostics.types import CheckResult, DoctorContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(memory_dir: Path, db_path: Path | None = None, *, api_host: str = "127.0.0.1", api_port: int = 6340) -> DoctorContext:
    """Build a DoctorContext pointing at *memory_dir* with optional db_path override."""
    if db_path is None:
        db_path = memory_dir / ".palinode.db"
    cfg = Config(
        memory_dir=str(memory_dir),
        db_path=str(db_path),
    )
    cfg.services.api.host = api_host
    cfg.services.api.port = api_port
    return DoctorContext(config=cfg)


def _make_db(db_path: Path, chunks: int = 0) -> None:
    """Create a minimal palinode DB at *db_path* with *chunks* rows in chunks table."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chunks "
        "(id TEXT PRIMARY KEY, entity_ref TEXT, content TEXT, last_updated TEXT)"
    )
    for i in range(chunks):
        conn.execute(
            "INSERT INTO chunks (id, entity_ref, content, last_updated) VALUES (?, ?, ?, ?)",
            (f"chunk-{i}", "entity", f"content {i}", "2026-04-26T00:00:00Z"),
        )
    conn.commit()
    conn.close()


def _mock_httpx_response(status_code: int, json_body: dict) -> mock.MagicMock:
    """Return a mock that looks like an httpx.Response."""
    resp = mock.MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    return resp


# ---------------------------------------------------------------------------
# api_reachable
# ---------------------------------------------------------------------------


class TestApiReachable:
    def test_passes_on_http_200(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        mock_resp = _mock_httpx_response(200, {"status": "ok"})
        with mock.patch("httpx.get", return_value=mock_resp) as mock_get:
            result = api_reachable(ctx)
        assert result.passed is True
        assert result.name == "api_reachable"
        assert result.severity == "error"
        assert "/health" in mock_get.call_args.args[0]

    def test_fails_on_connection_error(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        with mock.patch("httpx.get", side_effect=Exception("connection refused")):
            result = api_reachable(ctx)
        assert result.passed is False
        assert result.severity == "error"
        assert result.remediation is not None
        assert "palinode-api" in result.remediation

    def test_fails_on_non_200_status(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        mock_resp = _mock_httpx_response(503, {})
        with mock.patch("httpx.get", return_value=mock_resp):
            result = api_reachable(ctx)
        assert result.passed is False
        assert result.severity == "error"
        assert "503" in result.message

    def test_url_contains_configured_host_and_port(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path, api_host="10.0.0.5", api_port=9999)
        mock_resp = _mock_httpx_response(200, {"status": "ok"})
        with mock.patch("httpx.get", return_value=mock_resp) as mock_get:
            api_reachable(ctx)
        url = mock_get.call_args.args[0]
        assert "10.0.0.5" in url
        assert "9999" in url

    def test_remediation_is_none_on_pass(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        mock_resp = _mock_httpx_response(200, {"status": "ok"})
        with mock.patch("httpx.get", return_value=mock_resp):
            result = api_reachable(ctx)
        assert result.remediation is None

# ---------------------------------------------------------------------------
# api_status_consistent
# ---------------------------------------------------------------------------


class TestApiStatusConsistent:
    def test_passes_when_api_and_disk_agree(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        db_path = memory_dir / ".palinode.db"
        _make_db(db_path, chunks=5)
        (memory_dir / "note.md").write_text("# Note\ncontent\n")

        ctx = _ctx(memory_dir, db_path)
        mock_status_resp = _mock_httpx_response(200, {"total_chunks": 5})
        with mock.patch("httpx.get", return_value=mock_status_resp):
            result = api_status_consistent(ctx)

        assert result.passed is True
        assert result.name == "api_status_consistent"

    def test_fails_when_api_reports_zero_but_md_files_exist(self, tmp_path: Path) -> None:
        """api says 0 chunks while disk clearly has markdown files."""
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        db_path = memory_dir / ".palinode.db"
        _make_db(db_path, chunks=10)
        (memory_dir / "note.md").write_text("# Note\ncontent\n")

        ctx = _ctx(memory_dir, db_path)
        mock_status_resp = _mock_httpx_response(200, {"total_chunks": 0})
        with mock.patch("httpx.get", return_value=mock_status_resp):
            result = api_status_consistent(ctx)

        assert result.passed is False
        assert result.severity == "error"
        assert "0 chunks" in result.message
        assert result.remediation is not None

    def test_fails_when_api_zero_and_disk_has_chunks(self, tmp_path: Path) -> None:
        """api reports 0 chunks, disk DB has data but no md files — db/api diverged."""
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        db_path = memory_dir / ".palinode.db"
        _make_db(db_path, chunks=50)
        # No .md files, so md_count=0 branch doesn't fire; use disk check

        ctx = _ctx(memory_dir, db_path)
        mock_status_resp = _mock_httpx_response(200, {"total_chunks": 0})
        with mock.patch("httpx.get", return_value=mock_status_resp):
            result = api_status_consistent(ctx)

        assert result.passed is False
        assert result.severity == "error"
        assert "disk" in result.message.lower() or "chunk" in result.message.lower()

    def test_warns_when_api_has_chunks_but_disk_empty(self, tmp_path: Path) -> None:
        """api says 5 chunks, disk DB has 0 — API likely on different DB."""
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        db_path = memory_dir / ".palinode.db"
        _make_db(db_path, chunks=0)

        ctx = _ctx(memory_dir, db_path)
        mock_status_resp = _mock_httpx_response(200, {"total_chunks": 5})
        with mock.patch("httpx.get", return_value=mock_status_resp):
            result = api_status_consistent(ctx)

        assert result.passed is False
        assert result.severity == "warn"
        assert "different database" in result.message.lower() or "different db" in result.message.lower() or "0 chunks" in result.message.lower()

    def test_degrades_gracefully_on_connection_error(self, tmp_path: Path) -> None:
        """If API is unreachable, warn but don't crash."""
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        ctx = _ctx(memory_dir)
        with mock.patch("httpx.get", side_effect=Exception("refused")):
            result = api_status_consistent(ctx)
        assert result.passed is False
        assert result.severity == "warn"
        assert result.remediation is not None

    def test_degrades_on_non_200_status(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        ctx = _ctx(memory_dir)
        mock_resp = _mock_httpx_response(500, {})
        with mock.patch("httpx.get", return_value=mock_resp):
            result = api_status_consistent(ctx)
        assert result.passed is False
        assert result.severity == "warn"

    def test_no_db_file_does_not_crash(self, tmp_path: Path) -> None:
        """If the DB doesn't exist yet, skip disk comparison but don't crash."""
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        db_path = memory_dir / ".palinode.db"  # does not exist
        ctx = _ctx(memory_dir, db_path)
        mock_status_resp = _mock_httpx_response(200, {"total_chunks": 0})
        with mock.patch("httpx.get", return_value=mock_status_resp):
            result = api_status_consistent(ctx)
        assert isinstance(result, CheckResult)


# ---------------------------------------------------------------------------
# watcher_alive
# ---------------------------------------------------------------------------


def _ps_output_with_watcher(pid: int = 12345) -> str:
    """Fake ps -ef output that includes a watcher process."""
    return (
        "UID        PID  PPID  C STIME TTY          TIME CMD\n"
        f"clawd    {pid}  1     0 10:00 ?        00:00:01 "
        f"/usr/bin/python3 -m {_WATCHER_MODULE}\n"
    )


def _ps_output_without_watcher() -> str:
    return (
        "UID        PID  PPID  C STIME TTY          TIME CMD\n"
        "clawd    9999  1     0 10:00 ?        00:00:01 /usr/bin/python3 other_module\n"
    )


class TestWatcherAlive:
    def test_passes_linux_systemctl_active(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        ctx = _ctx(tmp_path)

        def _fake_run(cmd, **kwargs):
            if "systemctl" in cmd:
                return mock.Mock(returncode=0, stdout="active\n", stderr="")
            return mock.Mock(returncode=1, stdout="", stderr="")

        with mock.patch("subprocess.run", side_effect=_fake_run):
            result = watcher_alive(ctx)

        assert result.passed is True
        assert "active" in result.message

    def test_passes_linux_ps_fallback_when_systemctl_inactive(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        ctx = _ctx(tmp_path)

        def _fake_run(cmd, **kwargs):
            if "systemctl" in cmd:
                return mock.Mock(returncode=1, stdout="inactive\n", stderr="")
            # ps -ef
            return mock.Mock(returncode=0, stdout=_ps_output_with_watcher(), stderr="")

        with mock.patch("subprocess.run", side_effect=_fake_run):
            result = watcher_alive(ctx)

        assert result.passed is True
        assert "PID" in result.message

    def test_fails_linux_when_neither_found(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        ctx = _ctx(tmp_path)

        def _fake_run(cmd, **kwargs):
            if "systemctl" in cmd:
                return mock.Mock(returncode=1, stdout="inactive\n", stderr="")
            return mock.Mock(returncode=0, stdout=_ps_output_without_watcher(), stderr="")

        with mock.patch("subprocess.run", side_effect=_fake_run):
            result = watcher_alive(ctx)

        assert result.passed is False
        assert result.severity == "error"
        assert result.remediation is not None

    def test_passes_macos_via_ps(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        ctx = _ctx(tmp_path)

        def _fake_run(cmd, **kwargs):
            return mock.Mock(returncode=0, stdout=_ps_output_with_watcher(), stderr="")

        with mock.patch("subprocess.run", side_effect=_fake_run):
            result = watcher_alive(ctx)

        assert result.passed is True

    def test_fails_macos_when_no_process(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        ctx = _ctx(tmp_path)

        def _fake_run(cmd, **kwargs):
            return mock.Mock(returncode=0, stdout=_ps_output_without_watcher(), stderr="")

        with mock.patch("subprocess.run", side_effect=_fake_run):
            result = watcher_alive(ctx)

        assert result.passed is False
        assert result.severity == "error"
        assert "macOS" in result.message

    def test_remediation_is_none_on_pass(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        ctx = _ctx(tmp_path)
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout=_ps_output_with_watcher(), stderr="")):
            result = watcher_alive(ctx)
        assert result.remediation is None


# ---------------------------------------------------------------------------
# watcher_indexes_correct_db
# ---------------------------------------------------------------------------


def _null_sep_environ(env: dict[str, str]) -> bytes:
    """Encode a dict as a /proc/<pid>/environ-style null-separated byte string."""
    parts = [f"{k}={v}".encode() for k, v in env.items()]
    return b"\x00".join(parts) + b"\x00"


class TestWatcherIndexesCorrectDb:
    def test_skipped_on_macos_with_info(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        ctx = _ctx(tmp_path)
        result = watcher_indexes_correct_db(ctx)
        assert result.passed is True
        assert result.severity == "info"
        assert "macOS" in result.message

    def test_passes_linux_when_dir_matches(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        ctx = _ctx(memory_dir)

        pid = 55555
        proc_env = _null_sep_environ({"PALINODE_DIR": str(memory_dir)})

        def _fake_run(cmd, **kwargs):
            return mock.Mock(returncode=0, stdout=_ps_output_with_watcher(pid), stderr="")

        with (
            mock.patch("subprocess.run", side_effect=_fake_run),
            mock.patch("pathlib.Path.read_bytes", return_value=proc_env),
        ):
            result = watcher_indexes_correct_db(ctx)

        assert result.passed is True
        assert result.severity == "error"
        assert str(pid) in result.message

    def test_fails_linux_when_dir_mismatches(self, tmp_path: Path, monkeypatch) -> None:
        """Watcher has an old PALINODE_DIR after a directory rename."""
        monkeypatch.setattr(sys, "platform", "linux")
        memory_dir = tmp_path / "palinode-data"
        memory_dir.mkdir()
        ctx = _ctx(memory_dir)

        stale_dir = tmp_path / "stale-data"
        stale_dir.mkdir()
        pid = 66666
        proc_env = _null_sep_environ({"PALINODE_DIR": str(stale_dir)})

        def _fake_run(cmd, **kwargs):
            return mock.Mock(returncode=0, stdout=_ps_output_with_watcher(pid), stderr="")

        with (
            mock.patch("subprocess.run", side_effect=_fake_run),
            mock.patch("pathlib.Path.read_bytes", return_value=proc_env),
        ):
            result = watcher_indexes_correct_db(ctx)

        assert result.passed is False
        assert result.severity == "error"
        assert str(stale_dir) in result.message
        assert result.remediation is not None
        assert "restart" in result.remediation.lower()

    def test_warns_when_no_watcher_process(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        ctx = _ctx(tmp_path)

        def _fake_run(cmd, **kwargs):
            return mock.Mock(returncode=0, stdout=_ps_output_without_watcher(), stderr="")

        with mock.patch("subprocess.run", side_effect=_fake_run):
            result = watcher_indexes_correct_db(ctx)

        assert result.passed is False
        assert result.severity == "warn"

    def test_warns_when_proc_environ_unreadable(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        ctx = _ctx(tmp_path)
        pid = 77777

        def _fake_run(cmd, **kwargs):
            return mock.Mock(returncode=0, stdout=_ps_output_with_watcher(pid), stderr="")

        with (
            mock.patch("subprocess.run", side_effect=_fake_run),
            mock.patch("pathlib.Path.read_bytes", side_effect=PermissionError("denied")),
        ):
            result = watcher_indexes_correct_db(ctx)

        assert result.passed is False
        assert result.severity == "warn"
        assert str(pid) in result.message

    def test_warns_when_no_palinode_dir_in_env(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        ctx = _ctx(memory_dir)
        pid = 88888

        # environ with no PALINODE_DIR key
        proc_env = _null_sep_environ({"HOME": "/home/test-user", "USER": "test-user"})

        def _fake_run(cmd, **kwargs):
            return mock.Mock(returncode=0, stdout=_ps_output_with_watcher(pid), stderr="")

        with (
            mock.patch("subprocess.run", side_effect=_fake_run),
            mock.patch("pathlib.Path.read_bytes", return_value=proc_env),
        ):
            result = watcher_indexes_correct_db(ctx)

        assert result.passed is False
        assert result.severity == "warn"
        assert "PALINODE_DIR" in result.message
