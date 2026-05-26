"""#386 regression: /save must surface git_committed: false when git auto-commit fails.

Previously the git auto-commit block logged at error level but the response
dict had no ``git_committed`` field — the API returned HTTP 200 with no signal
that the save was not versioned. The MCP ``palinode_save`` already has a
_save_warnings path wired to ``git_committed``, but it had nothing to read.

The fix:
  1. Track ``git_committed = True`` only when the git commit subprocess
     completes without raising (does not check subprocess exit code, only
     that the process was spawned; a "nothing to commit" exit code is fine).
  2. Add ``exc_info=True`` to the logger.error call so the stack trace
     appears in log files for debugging (#386).
  3. Return ``git_committed`` in the /save response dict so callers can act.

Tests use TestClient + monkeypatch (no real git repo required per CLAUDE.md's
"no mocking the DB" rule — the DB is real; git is mocked via subprocess.run
injection because a real git op in tmp_path is not test-hermetic).
"""
from __future__ import annotations

import logging
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from palinode.api import server as srv
from palinode.api.server import app
from palinode.core import store
from palinode.core.config import config


_FAKE_VECTOR = [0.01] * 1024


@pytest.fixture()
def client_git_on(tmp_path, monkeypatch):
    """TestClient with auto_commit=True and a fake .git dir so the
    not-a-git-repo warning in server.py startup doesn't fire.

    palinode_dir is a property that returns memory_dir — we only need to
    set memory_dir. subprocess.run is patched per-test so cwd doesn't matter.
    """
    db_path = tmp_path / ".palinode.db"
    fake_git = tmp_path / ".git"
    fake_git.mkdir()
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(config.git, "auto_commit", True)
    monkeypatch.setattr(config.git, "auto_push", False)
    srv._rate_counters.clear()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    srv._rate_counters.clear()


def _patch_scan():
    return patch("palinode.core.store.scan_memory_content", return_value=(True, "OK"))


def _patch_embed_ok():
    return patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR)


# ---------------------------------------------------------------------------
# Happy path: git succeeds → git_committed: true
# ---------------------------------------------------------------------------


class TestSaveGitCommittedHappyPath:

    def test_git_committed_true_when_subprocess_succeeds(self, client_git_on):
        """When subprocess.run does not raise, git_committed must be True."""
        noop_result = MagicMock()
        noop_result.returncode = 0
        with (
            _patch_scan(),
            _patch_embed_ok(),
            patch("palinode.api.server.subprocess.run", return_value=noop_result),
        ):
            res = client_git_on.post(
                "/save",
                json={
                    "content": "Git-committed happy-path sentinel (#386).",
                    "type": "Insight",
                    "slug": "git-committed-happy",
                },
            )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body.get("git_committed") is True, (
            "git_committed must be True when subprocess.run succeeds (#386)"
        )


# ---------------------------------------------------------------------------
# Failure path: git raises OSError → git_committed: false + error log
# ---------------------------------------------------------------------------


class TestSaveGitCommittedFailurePath:

    def test_git_committed_false_and_error_logged_on_os_error(
        self, client_git_on, caplog
    ):
        """When subprocess.run raises OSError (e.g. git not found), the
        response must carry git_committed: false and an ERROR must be logged.
        The save itself must still succeed (HTTP 200, file on disk).
        """
        def _raising_run(cmd, *args, **kwargs):
            if cmd and cmd[0] == "git":
                raise OSError("git: command not found")
            # Non-git subprocess calls get a no-op mock result
            return MagicMock(returncode=0)

        with (
            _patch_scan(),
            _patch_embed_ok(),
            patch("palinode.api.server.subprocess.run", side_effect=_raising_run),
            caplog.at_level(logging.ERROR, logger="palinode.api.server"),
        ):
            res = client_git_on.post(
                "/save",
                json={
                    "content": "Git-failure sentinel for #386 test.",
                    "type": "Insight",
                    "slug": "git-failure-386",
                },
            )

        # File must be saved — git failure is non-fatal
        assert res.status_code == 200, res.text
        body = res.json()

        # Return contract (#386)
        assert body.get("git_committed") is False, (
            "git_committed must be False when git subprocess raises (#386)"
        )

        # Error must be logged — operator must get a signal
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records, (
            "An ERROR must be logged when git auto-commit raises (#386)"
        )
        combined = " ".join(r.getMessage() for r in error_records)
        assert "git" in combined.lower() or "auto-commit" in combined.lower(), (
            f"Error log must mention git or auto-commit. Got: {combined!r}"
        )

    def test_git_committed_false_on_subprocess_error(self, client_git_on, caplog):
        """When subprocess.run raises SubprocessError (timeout, CalledProcessError),
        git_committed must still be False and the save must succeed.
        """
        def _subprocess_error(cmd, *args, **kwargs):
            if cmd and cmd[0] == "git":
                raise subprocess.SubprocessError("subprocess timed out")
            return MagicMock(returncode=0)

        with (
            _patch_scan(),
            _patch_embed_ok(),
            patch("palinode.api.server.subprocess.run", side_effect=_subprocess_error),
            caplog.at_level(logging.ERROR, logger="palinode.api.server"),
        ):
            res = client_git_on.post(
                "/save",
                json={
                    "content": "SubprocessError sentinel for #386 test.",
                    "type": "Insight",
                    "slug": "subprocess-error-386",
                },
            )

        assert res.status_code == 200, res.text
        assert res.json().get("git_committed") is False, (
            "git_committed must be False when SubprocessError is raised (#386)"
        )

    def test_save_is_200_even_when_git_fails(self, client_git_on):
        """HTTP status must be 200 regardless of git outcome — the file is
        on disk and that is the primary contract. Git failure is advisory."""
        with (
            _patch_scan(),
            _patch_embed_ok(),
            patch(
                "palinode.api.server.subprocess.run",
                side_effect=OSError("no git"),
            ),
        ):
            res = client_git_on.post(
                "/save",
                json={
                    "content": "HTTP-200-on-git-fail sentinel.",
                    "type": "Insight",
                    "slug": "http-200-git-fail",
                },
            )
        assert res.status_code == 200, (
            f"Save must return 200 even when git fails. Got {res.status_code}: {res.text}"
        )


# ---------------------------------------------------------------------------
# git.auto_commit=False: git_committed is always False (no attempt made)
# ---------------------------------------------------------------------------


class TestSaveGitCommittedWhenAutoCommitOff:

    def test_git_committed_false_when_auto_commit_disabled(self, tmp_path, monkeypatch):
        """When auto_commit=False, no git subprocess is invoked and
        git_committed must be False (no commit was made — consistent
        with 'nothing to report').
        """
        db_path = tmp_path / ".palinode.db"
        monkeypatch.setattr(config, "memory_dir", str(tmp_path))
        monkeypatch.setattr(config, "db_path", str(db_path))
        monkeypatch.setattr(config.git, "auto_commit", False)
        srv._rate_counters.clear()

        with TestClient(app, raise_server_exceptions=True) as c:
            with _patch_scan(), _patch_embed_ok():
                res = c.post(
                    "/save",
                    json={
                        "content": "auto_commit=False sentinel (#386).",
                        "type": "Insight",
                        "slug": "auto-commit-off-386",
                    },
                )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body.get("git_committed") is False, (
            "git_committed must be False when auto_commit=False — no commit was made (#386)"
        )
        srv._rate_counters.clear()
