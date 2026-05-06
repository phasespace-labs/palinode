"""Typed memory-file access seam.

Callers in API, CLI, and MCP all share this module per ADR-010.  Path
validation, symlink-jail logic, and TOCTOU-safe file reading live here
instead of being embedded in the FastAPI route layer.

Raises typed exceptions (MemoryPathError hierarchy) so each surface can
map them to its own error representation (HTTPException, ClickException,
MCP error, etc.).
"""
from __future__ import annotations

import errno
import logging
import os
from pathlib import Path

from palinode.core.config import config

__all__ = [
    "MemoryPathError",
    "MemoryPathNotFound",
    "MemoryPathTraversal",
    "MemoryPathTooLarge",
    "memory_base_dir",
    "resolve",
    "read_text",
]

logger = logging.getLogger("palinode.core.memory_paths")


# ── Exceptions ──────────────────────────────────────────────────────────────


class MemoryPathError(Exception):
    """Base class for path-validation failures."""


class MemoryPathNotFound(MemoryPathError):
    """File not found under PALINODE_DIR."""


class MemoryPathTraversal(MemoryPathError):
    """Path attempts to escape PALINODE_DIR (.., absolute, symlink jailbreak)."""


class MemoryPathTooLarge(MemoryPathError):
    """File exceeds the configured maximum size."""


# ── Public API ──────────────────────────────────────────────────────────────


def memory_base_dir() -> str:
    """Return the canonical PALINODE_DIR (resolved, no trailing slash)."""
    return os.path.realpath(getattr(config, "memory_dir", config.palinode_dir))


def resolve(file_path: str) -> tuple[str, str]:
    """Validate and resolve a relative memory-file path.

    Returns ``(base_dir_str, absolute_resolved_path_str)``.

    The function does NOT check existence — only safety.  Use
    :func:`read_text` when you need to verify the file is present.

    Raises:
        MemoryPathTraversal: null bytes, absolute paths, ``..`` escape,
            symlink resolution landing outside PALINODE_DIR, or any
            OS-level resolution failure (loops, permissions).
    """
    if "\x00" in file_path:
        raise MemoryPathTraversal("Invalid path")

    base_path = Path(memory_base_dir()).resolve()
    raw_path = Path(file_path)

    if raw_path.is_absolute():
        logger.info("Rejected absolute path: %r", file_path)
        raise MemoryPathTraversal("Invalid path")

    try:
        resolved_path = (base_path / raw_path).resolve()
    except (OSError, RuntimeError) as exc:
        # OSError: symlink loops, permission errors during resolution.
        # RuntimeError: pathlib infinite-loop guard on some platforms.
        logger.info("Path resolution failed for %r: %s", file_path, exc)
        raise MemoryPathTraversal("Invalid path") from exc

    if not resolved_path.is_relative_to(base_path):
        logger.info("Rejected traversal outside memory_dir: %r", file_path)
        raise MemoryPathTraversal("Invalid path")

    return str(base_path), str(resolved_path)


def read_text(resolved_path: str) -> str:
    """Open a resolved memory path for reading, rejecting symlinks on POSIX.

    TOCTOU defence: uses ``O_NOFOLLOW`` where available (POSIX) so a
    symlink swap between :func:`resolve` and this open cannot redirect
    the read to a sensitive file outside PALINODE_DIR.  On platforms
    without ``O_NOFOLLOW`` (Windows), falls back to a plain open — the
    traversal guard in :func:`resolve` is still in effect.

    Raises:
        MemoryPathNotFound: file does not exist.
        OSError: any other I/O failure (propagated as-is).
    """
    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is not None:
        flags |= nofollow
    try:
        fd = os.open(resolved_path, flags)
    except FileNotFoundError as exc:
        raise MemoryPathNotFound(str(exc)) from exc
    # os.fdopen takes ownership of the fd; the `with` closes it on exit.
    with os.fdopen(fd, "r", encoding="utf-8") as f:
        return f.read()
