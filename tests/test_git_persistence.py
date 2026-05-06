"""Tests for palinode.core.git_persistence — the write-and-commit seam.

Uses real git repos in tmp_path. No mocking of subprocess — these are
integration tests that verify actual git behaviour.

Tracking: #332
"""
from __future__ import annotations

import logging
import os
import subprocess

import pytest

from palinode.core.config import config
from palinode.core.git_persistence import (
    GitCommitError,
    GitPersistenceError,
    GitPushError,
    GitWriteError,
    commit_existing,
    push,
    write_and_commit,
)


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """Initialize a git repo in tmp_path and point config.memory_dir at it."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.dev"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    # Initial commit so HEAD exists (needed for rev-parse).
    readme = tmp_path / "README.md"
    readme.write_text("init")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp_path, check=True)

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    # palinode_dir is a property that aliases memory_dir, but some code
    # accesses it directly — make sure it's consistent.
    return tmp_path


# ── write_and_commit ────────────────────────────────────────────────────────


def test_write_and_commit_creates_file_and_commit(git_repo):
    """Happy path: file is written, staged, committed."""
    commit_hash = write_and_commit("projects/foo.md", "hello world", "test: create foo")

    # File exists on disk.
    assert (git_repo / "projects" / "foo.md").read_text() == "hello world"

    # Git log shows the commit.
    log = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=git_repo, capture_output=True, text=True, check=True,
    )
    assert "test: create foo" in log.stdout
    assert commit_hash  # non-empty


def test_write_and_commit_returns_hash(git_repo):
    """Returned hash matches git rev-parse HEAD."""
    commit_hash = write_and_commit("notes/a.md", "content", "test: a")

    head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=git_repo, capture_output=True, text=True, check=True,
    )
    assert commit_hash == head.stdout.strip()


def test_write_and_commit_path_traversal_rejected(git_repo):
    """Paths escaping PALINODE_DIR are rejected before any file I/O."""
    with pytest.raises(GitWriteError, match="Path rejected"):
        write_and_commit("../escape.md", "bad", "nope")

    # Nothing written outside the repo.
    assert not os.path.exists(os.path.join(str(git_repo), "..", "escape.md"))


def test_write_and_commit_absolute_path_rejected(git_repo):
    """Absolute paths are rejected."""
    with pytest.raises(GitWriteError, match="Path rejected"):
        write_and_commit("/tmp/sneaky.md", "bad", "nope")


def test_write_and_commit_idempotent_no_change(git_repo):
    """Writing identical content twice doesn't error — returns HEAD hash."""
    h1 = write_and_commit("test.md", "same", "first")
    h2 = write_and_commit("test.md", "same", "second")
    # Second commit should be a no-op (content unchanged).
    assert h2 == h1


def test_write_and_commit_creates_subdirs(git_repo):
    """Parent directories are created automatically."""
    write_and_commit("deep/nested/dir/file.md", "deep", "test: nested")
    assert (git_repo / "deep" / "nested" / "dir" / "file.md").exists()


# ── commit_existing ─────────────────────────────────────────────────────────


def test_commit_existing_with_no_paths_raises(git_repo):
    """Empty paths list is rejected immediately."""
    with pytest.raises(ValueError, match="at least one path"):
        commit_existing("msg", [])


def test_commit_existing_happy_path(git_repo):
    """Stage and commit files that were already written to disk."""
    # Write files manually (simulating consolidation runner).
    (git_repo / "projects").mkdir(exist_ok=True)
    (git_repo / "projects" / "alpha.md").write_text("alpha content")
    (git_repo / "projects" / "beta.md").write_text("beta content")

    commit_hash = commit_existing(
        "compaction: alpha + beta",
        ["projects/alpha.md", "projects/beta.md"],
    )

    log = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=git_repo, capture_output=True, text=True, check=True,
    )
    assert "compaction: alpha + beta" in log.stdout
    assert commit_hash


def test_commit_existing_no_changes_is_noop(git_repo):
    """When nothing changed, commit_existing returns HEAD hash (no error)."""
    # Everything is already clean after the initial fixture commit.
    commit_hash = commit_existing("no changes", ["README.md"])
    assert commit_hash  # non-empty, equals HEAD


def test_commit_existing_glob_pattern(git_repo):
    """Git glob patterns (*.md) work as paths."""
    (git_repo / "a.md").write_text("a")
    (git_repo / "b.md").write_text("b")

    commit_hash = commit_existing("glob test", ["*.md"])
    assert commit_hash

    # Both files should be committed.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=git_repo, capture_output=True, text=True, check=True,
    )
    # a.md and b.md should no longer show as untracked/modified.
    assert "a.md" not in status.stdout
    assert "b.md" not in status.stdout


# ── push ────────────────────────────────────────────────────────────────────


def test_push_failure_raises_typed_error(git_repo):
    """Push to a non-existent remote raises GitPushError."""
    with pytest.raises(GitPushError):
        push(remote="nonexistent")


# ── logging ─────────────────────────────────────────────────────────────────


def test_write_and_commit_logs_hash(git_repo, caplog):
    """Commit hash and message appear in INFO log."""
    with caplog.at_level(logging.INFO, logger="palinode.git_persistence"):
        commit_hash = write_and_commit("log-test.md", "logged", "test: logging")

    assert commit_hash in caplog.text
    assert "test: logging" in caplog.text


def test_commit_existing_logs_noop(git_repo, caplog):
    """No-op commits are logged at INFO."""
    with caplog.at_level(logging.INFO, logger="palinode.git_persistence"):
        commit_existing("noop msg", ["README.md"])

    assert "No changes to commit" in caplog.text
