"""``git_committed`` must be truthful.

The response-field fix added the field; the truthfulness fix found it lying. ``commit_memory_files`` ran
``git add`` / ``git commit`` with ``check=False`` and returned True whenever
the subprocess merely *spawned*, so a memory dir that was never ``git init``-ed
(the dev rig) logged ``git_committed=True`` on every save while
``fatal: not a git repository`` went to the bit bucket. The same swallow hid a
missing commit identity (``Author identity unknown``) and an ``index.lock``
collision.

The earlier version of this file patched ``subprocess.run`` on the API server
module — a wholesale mock that did not even reach ``git_tools._run_git``, which
is exactly why the lie went unnoticed. Per the 2026-08 test-shape rule,
these tests now drive the real save path (:func:`save_memory`) against real
``tmp_path`` git repositories in three states — not a repo, a repo with no
identity, a healthy repo — and let real git decide. Only the embedder and the
content scanner are stubbed (network / model I/O), never subprocess or the DB.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from palinode.core import git_tools, store
from palinode.core.config import config
from palinode.core.save import save_memory

_FAKE_VECTOR = [0.01] * 1024


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )


@pytest.fixture()
def hermetic_git(monkeypatch):
    """Strip every out-of-band source of git identity so a repo's own
    ``user.name`` / ``user.email`` are the only ones that can resolve."""
    for var in (
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL", "EMAIL", "GIT_DIR", "GIT_WORK_TREE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture()
def memory_dir(tmp_path: Path, monkeypatch, hermetic_git) -> Path:
    """A memory dir wired into config with auto_commit on. Not a repo yet —
    each test decides which of the three states it wants."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", True)
    monkeypatch.setattr(config.git, "auto_push", False)
    store.init_db()
    return tmp_path


def _init_repo(path: Path, identity: bool) -> None:
    assert _git(path, "init", "-q").returncode == 0
    # ``user.useConfigOnly`` makes git refuse its hostname-derived fallback
    # identity, so the no-identity state reproduces the rig's
    # ``Author identity unknown`` on every host — a real git behaviour, not a
    # stub. The healthy repo sets it too, proving the configured identity is
    # what the commit uses.
    _git(path, "config", "user.useConfigOnly", "true")
    if identity:
        _git(path, "config", "user.name", "Palinode Tests")
        _git(path, "config", "user.email", "tests@example.com")


def _save(slug: str, content: str = "git_committed truthfulness sentinel.") -> dict:
    with (
        patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")),
        patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR),
    ):
        return save_memory(content=content, type="Insight", slug=slug)


# ---------------------------------------------------------------------------
# Not a git repo (the dev-rig case): committed=False, reason surfaced
# ---------------------------------------------------------------------------


class TestNotARepo:

    def test_git_committed_false_with_reason(self, memory_dir, caplog):
        with caplog.at_level(logging.ERROR):
            result = _save("not-a-repo")

        assert Path(result["file_path"]).is_file()
        assert result["git_committed"] is False
        assert "not a git repository" in result["git_error"]
        errors = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
        assert "not a git repository" in errors

    def test_choke_point_outcome(self, memory_dir):
        target = memory_dir / "insights"
        target.mkdir()
        (target / "x.md").write_text("x\n", encoding="utf-8")
        outcome = git_tools.try_commit_memory_files([str(target / "x.md")], "m")
        assert outcome.committed is False
        assert outcome.error and "not a git repository" in outcome.error
        assert git_tools.commit_memory_files([str(target / "x.md")], "m") is False


# ---------------------------------------------------------------------------
# Repo with no identity: git commit exits 128 "Author identity unknown"
# ---------------------------------------------------------------------------


class TestRepoWithoutIdentity:

    def test_git_committed_false_with_identity_reason(self, memory_dir):
        _init_repo(memory_dir, identity=False)
        result = _save("no-identity")

        assert result["git_committed"] is False
        assert "identity" in result["git_error"].lower() or "who you are" in result["git_error"].lower()
        # The file was staged but never committed.
        assert _git(memory_dir, "rev-parse", "--verify", "HEAD").returncode != 0


# ---------------------------------------------------------------------------
# Healthy repo: committed=True, no git_error, a real commit exists
# ---------------------------------------------------------------------------


class TestHealthyRepo:

    def test_git_committed_true_and_commit_lands(self, memory_dir):
        _init_repo(memory_dir, identity=True)
        result = _save("healthy")

        assert result["git_committed"] is True
        assert "git_error" not in result
        log = _git(memory_dir, "log", "--oneline", "-1").stdout
        assert "auto-save: insights/healthy.md" in log
        assert _git(memory_dir, "status", "--porcelain", "--", "insights/healthy.md").stdout == ""

    def test_nothing_to_commit_is_still_success(self, memory_dir):
        _init_repo(memory_dir, identity=True)
        first = _save("idempotent")
        assert first["git_committed"] is True
        path = first["file_path"]
        outcome = git_tools.try_commit_memory_files([path], "again")
        assert outcome == git_tools.CommitOutcome(True)
        assert _git(memory_dir, "rev-list", "--count", "HEAD").stdout.strip() == "1"

    def test_index_lock_contention_is_reported(self, memory_dir):
        _init_repo(memory_dir, identity=True)
        (memory_dir / ".git" / "index.lock").write_text("", encoding="utf-8")
        result = _save("locked")

        assert result["git_committed"] is False
        assert "index.lock" in result["git_error"]


# ---------------------------------------------------------------------------
# git.auto_commit=False: git_committed is always False (no attempt made)
# ---------------------------------------------------------------------------


class TestAutoCommitOff:

    def test_git_committed_false_no_error(self, memory_dir, monkeypatch):
        monkeypatch.setattr(config.git, "auto_commit", False)
        result = _save("auto-commit-off")
        assert result["git_committed"] is False
        assert "git_error" not in result
        assert not (memory_dir / ".git").exists()
