"""Unit tests for the transport-neutral memory-path access seam (#329).

Tests exercise ``palinode.core.memory_paths`` directly — no HTTP layer,
no FastAPI TestClient.  The module raises typed exceptions that each
surface (API, CLI, MCP) maps to its own error representation.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from palinode.core.memory_paths import (
    MemoryPathNotFound,
    MemoryPathTraversal,
    memory_base_dir,
    read_text,
    resolve,
)


@pytest.fixture()
def memory_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set up a temporary PALINODE_DIR with a sample file."""
    from palinode.core.config import config

    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "decisions").mkdir()
    (mem / "decisions" / "ok.md").write_text("# OK decision\nbody\n")
    (mem / "projects").mkdir()
    (mem / "projects" / "foo.md").write_text("---\nname: foo\n---\nfoo body\n")

    monkeypatch.setattr(config, "memory_dir", str(mem), raising=False)
    return mem


# ── resolve() — rejection ───────────────────────────────────────────────────


def test_resolve_rejects_dotdot_traversal(memory_dir: Path) -> None:
    """``..`` segments that escape PALINODE_DIR raise MemoryPathTraversal."""
    with pytest.raises(MemoryPathTraversal):
        resolve("decisions/../../etc/passwd")


def test_resolve_rejects_absolute_path(memory_dir: Path) -> None:
    """Absolute paths outside PALINODE_DIR raise MemoryPathTraversal."""
    with pytest.raises(MemoryPathTraversal):
        resolve("/etc/passwd")


def test_resolve_rejects_null_byte(memory_dir: Path) -> None:
    """Null bytes in path raise MemoryPathTraversal."""
    with pytest.raises(MemoryPathTraversal):
        resolve("decisions/foo\x00bar.md")


# ── resolve() — acceptance ──────────────────────────────────────────────────


def test_resolve_accepts_relative_path(memory_dir: Path) -> None:
    """A valid relative path returns (base_dir, resolved_absolute)."""
    base, resolved = resolve("projects/foo.md")
    assert base == str(memory_dir.resolve())
    assert resolved == str((memory_dir / "projects" / "foo.md").resolve())


def test_resolve_normalises_dot_segments(memory_dir: Path) -> None:
    """Single-dot segments are normalized away."""
    base, resolved = resolve("decisions/./ok.md")
    assert resolved == str((memory_dir / "decisions" / "ok.md").resolve())


def test_resolve_accepts_nonexistent_path(memory_dir: Path) -> None:
    """Non-existent but structurally-safe paths resolve without raising.

    Callers decide whether to 404 — resolve() only validates safety.
    """
    base, resolved = resolve("decisions/does-not-exist.md")
    assert resolved.startswith(str(memory_dir.resolve()))


# ── resolve() — symlink outside PALINODE_DIR ────────────────────────────────


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="Symlink semantics differ on Windows",
)
def test_resolve_rejects_symlink_escaping_base(memory_dir: Path, tmp_path: Path) -> None:
    """A symlink whose target resolves outside PALINODE_DIR is rejected."""
    outside = tmp_path / "outside-secret.md"
    outside.write_text("secret\n")

    link = memory_dir / "decisions" / "link-out.md"
    os.symlink(outside, link)

    with pytest.raises(MemoryPathTraversal):
        resolve("decisions/link-out.md")


# ── read_text() ─────────────────────────────────────────────────────────────


def test_read_text_returns_content(memory_dir: Path) -> None:
    """Reading an existing file returns its full text."""
    _, resolved = resolve("decisions/ok.md")
    content = read_text(resolved)
    assert "OK decision" in content


def test_read_text_not_found_raises(memory_dir: Path) -> None:
    """A non-existent resolved path raises MemoryPathNotFound."""
    fake = str(memory_dir / "nope.md")
    with pytest.raises(MemoryPathNotFound):
        read_text(fake)


@pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW") or sys.platform.startswith("win"),
    reason="O_NOFOLLOW is POSIX-only",
)
def test_read_text_rejects_symlink_at_open(memory_dir: Path) -> None:
    """O_NOFOLLOW causes read_text to raise OSError on a symlink.

    Even if resolve() passed (e.g. an intra-PALINODE_DIR symlink), the
    open-time O_NOFOLLOW check catches a swap to a symlink between
    resolve() and read_text().
    """
    real = memory_dir / "decisions" / "ok.md"
    link = memory_dir / "decisions" / "linked.md"
    os.symlink(str(real), str(link))

    with pytest.raises(OSError):
        read_text(str(link))


# ── memory_base_dir() ──────────────────────────────────────────────────────


def test_memory_base_dir_matches_config(memory_dir: Path) -> None:
    """memory_base_dir() returns the realpath of config.memory_dir."""
    assert memory_base_dir() == str(memory_dir.resolve())
