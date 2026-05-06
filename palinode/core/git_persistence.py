"""Palinode Git Persistence — single seam for write-and-commit operations.

Git is palinode's persistence layer for markdown memories. This module
centralizes all git WRITE operations (add, commit, push) behind typed
functions with consistent invariants:

- All commands use ``cwd=config.memory_dir`` (same as git_tools.py reads).
- argv-list form only — never ``shell=True``.
- Typed exceptions (never raw ``CalledProcessError``).
- Path validation via ``memory_paths.resolve`` — no traversal escapes.
- INFO-level logging on every commit with hash and message.

**Split rationale (git_tools.py vs git_persistence.py):**

Reads and writes have different failure modes. A failed read degrades the
UI (no blame output, no diff). A failed write corrupts state (orphaned
files, half-committed transactions). Splitting them puts the entire
security-auditable write surface in one file and matches the existing
store.py (reads) vs triggers.py / entity_graph.py (writes) pattern.
The read module (git_tools.py) also has its own ``_run_git`` helper with
a specific security contract; the write module uses a parallel helper
with identical subprocess discipline but separate logging.

Tracking: #332
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from palinode.core.config import config
from palinode.core.memory_paths import MemoryPathTraversal, resolve as _resolve_path

__all__ = [
    "GitPersistenceError",
    "GitWriteError",
    "GitCommitError",
    "GitPushError",
    "write_and_commit",
    "commit_existing",
    "push",
]

logger = logging.getLogger("palinode.git_persistence")


# ── Exceptions ──────────────────────────────────────────────────────────────


class GitPersistenceError(Exception):
    """Base class for git persistence failures."""


class GitWriteError(GitPersistenceError):
    """Failed to write file to disk before staging."""


class GitCommitError(GitPersistenceError):
    """git add or git commit failed.

    Carries stdout/stderr from the failed command for diagnostics.
    """

    def __init__(self, message: str, *, stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


class GitPushError(GitPersistenceError):
    """git push failed."""


# ── Internal helpers ────────────────────────────────────────────────────────


def _run_git(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    """Run a git command in the memory data directory.

    Same security discipline as ``git_tools._run_git``: argv-list form,
    ``shell=False`` (default), user-supplied arguments passed as separate
    list elements.  All git write operations in palinode MUST go through
    this helper.
    """
    return subprocess.run(  # nosec B603 - argv form, no shell, validated cwd
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=config.memory_dir,
        check=check,
    )


def _get_head_hash() -> str:
    """Return the short hash of HEAD after a commit, or empty string."""
    result = _run_git("rev-parse", "--short", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else ""


def _validate_relative_path(file_path: str) -> str:
    """Validate that file_path is safe and relative to PALINODE_DIR.

    Returns the validated relative path string.

    Raises:
        GitWriteError: wraps MemoryPathTraversal for a consistent API.
    """
    try:
        _resolve_path(file_path)
    except MemoryPathTraversal as exc:
        raise GitWriteError(f"Path rejected: {file_path}") from exc
    return file_path


# ── Public API ──────────────────────────────────────────────────────────────


def write_and_commit(file_path: str, content: str, message: str) -> str:
    """Write ``content`` to ``file_path`` (relative to PALINODE_DIR), stage, commit.

    Creates parent directories as needed.

    Args:
        file_path: Relative path under PALINODE_DIR (e.g. ``"projects/foo.md"``).
        content: File content to write.
        message: Git commit message.

    Returns:
        Short commit hash (e.g. ``"a1b2c3d"``).

    Raises:
        GitWriteError: file write failed (path traversal, permissions, disk).
        GitCommitError: ``git add`` or ``git commit`` failed.
    """
    rel_path = _validate_relative_path(file_path)
    abs_path = os.path.join(config.memory_dir, rel_path)

    # Write file
    try:
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as exc:
        raise GitWriteError(f"Failed to write {file_path}: {exc}") from exc

    # Stage
    result = _run_git("add", rel_path)
    if result.returncode != 0:
        raise GitCommitError(
            f"git add failed for {file_path}",
            stdout=result.stdout,
            stderr=result.stderr,
        )

    # Commit
    result = _run_git("commit", "-m", message)
    if result.returncode != 0:
        # "nothing to commit" is not an error — the file was already staged
        # identically (content-addressed dedup). Return HEAD as-is.
        if "nothing to commit" in result.stdout or "nothing to commit" in result.stderr:
            commit_hash = _get_head_hash()
            logger.info("No changes to commit for %s (already at %s)", file_path, commit_hash)
            return commit_hash
        raise GitCommitError(
            f"git commit failed for {file_path}",
            stdout=result.stdout,
            stderr=result.stderr,
        )

    commit_hash = _get_head_hash()
    logger.info("Committed %s: %s [%s]", file_path, message, commit_hash)
    return commit_hash


def commit_existing(message: str, paths: list[str]) -> str:
    """Stage the given paths (relative to PALINODE_DIR), commit.

    For consolidation runs that have already mutated multiple files on disk
    and need a single atomic commit.

    If all paths are already clean (nothing to commit), returns HEAD hash
    as a no-op rather than raising — this is more useful for the
    consolidation pipeline where idempotent reruns are expected.

    Args:
        message: Git commit message.
        paths: Relative paths under PALINODE_DIR to stage.  Empty list
               is rejected.

    Returns:
        Short commit hash.

    Raises:
        ValueError: ``paths`` is empty.
        GitCommitError: ``git add`` or ``git commit`` failed.
    """
    if not paths:
        raise ValueError("commit_existing requires at least one path")

    # Stage each path
    for p in paths:
        result = _run_git("add", p)
        if result.returncode != 0:
            raise GitCommitError(
                f"git add failed for {p}",
                stdout=result.stdout,
                stderr=result.stderr,
            )

    # Commit
    result = _run_git("commit", "-m", message)
    if result.returncode != 0:
        if "nothing to commit" in result.stdout or "nothing to commit" in result.stderr:
            commit_hash = _get_head_hash()
            logger.info("No changes to commit (no-op): %s [%s]", message, commit_hash)
            return commit_hash
        raise GitCommitError(
            f"git commit failed: {message}",
            stdout=result.stdout,
            stderr=result.stderr,
        )

    commit_hash = _get_head_hash()
    logger.info("Committed: %s [%s]", message, commit_hash)
    return commit_hash


def push(remote: str = "origin", branch: str | None = None) -> None:
    """Push to remote.

    Args:
        remote: Remote name (default ``"origin"``).
        branch: Branch name. If ``None``, pushes the current branch.

    Raises:
        GitPushError: push failed.
    """
    cmd = ["push", remote]
    if branch:
        cmd.append(branch)

    result = _run_git(*cmd)
    if result.returncode != 0:
        raise GitPushError(
            f"git push failed: {result.stderr.strip()}"
        )

    logger.info("Pushed to %s%s", remote, f"/{branch}" if branch else "")
