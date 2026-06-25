"""Memory-path resolution and traversal/symlink guards (#284, #556).

Extracted from the former ``routers/_shared.py`` junk drawer. The seam every
file-touching handler crosses before reading a caller-supplied memory path:
resolve it inside ``memory_dir`` (rejecting traversal and absolute paths) and
open it without following symlinks. Client-facing error messages are
intentionally generic so filesystem layout never leaks to an unauthenticated
caller.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import HTTPException

from palinode.core.config import config

logger = logging.getLogger("palinode.api")


def _memory_base_dir() -> str:
    """Return the canonical memory root."""
    return os.path.realpath(getattr(config, "memory_dir", config.palinode_dir))


def _resolve_memory_path(file_path: str) -> tuple[str, str]:
    """Resolve a relative memory path without allowing traversal outside memory_dir.

    Uses ``pathlib.Path.resolve(strict=False)`` plus ``Path.is_relative_to()``
    for the membership check (#284). ``strict=False`` is used because callers
    of this helper sometimes resolve paths that don't yet exist on disk
    (e.g. ``/save`` resolving the destination path before writing it); the
    strict-existence check is performed by callers via ``os.path.exists`` /
    ``open`` once the path has cleared the traversal guard.

    Error messages returned to clients are intentionally generic — they do
    NOT include the resolved path or memory_dir, to avoid leaking filesystem
    layout to an unauthenticated attacker. The original (unresolved) input is
    logged at INFO so operators can still debug.

    Note: full TOCTOU mitigation via fd-based open requires invasive changes
    to every caller and is out of scope for this PR. The pathlib-based check
    closes the cross-platform realpath gap (Windows symlinks, junction
    points) and the symlink-replacement window is significantly narrower
    than under realpath because resolve() returns a strict canonical form.
    """
    if "\x00" in file_path:
        raise HTTPException(status_code=400, detail="Invalid path")
    base_path = Path(_memory_base_dir()).resolve()
    raw_path = Path(file_path)
    if raw_path.is_absolute():
        # Don't echo the offending input back to the client.
        logger.info("Rejected absolute path on /resolve: %r", file_path)
        raise HTTPException(status_code=403, detail="Invalid path")

    try:
        resolved_path = (base_path / raw_path).resolve()
    except (OSError, RuntimeError) as exc:
        # OSError covers symlink loops / permission errors during resolution;
        # RuntimeError is raised by pathlib for infinite loops on some plats.
        logger.info("Path resolution failed for %r: %s", file_path, exc)
        raise HTTPException(status_code=403, detail="Invalid path") from exc

    if not resolved_path.is_relative_to(base_path):
        logger.info("Rejected traversal outside memory_dir: %r", file_path)
        raise HTTPException(status_code=403, detail="Invalid path")
    return str(base_path), str(resolved_path)


def _open_memory_file_text(resolved_path: str) -> str:
    """Open a resolved memory path for reading, rejecting symlinks on POSIX.

    L5 hardening: closes the TOCTOU window where a symlink swap between
    `os.path.exists()` and `open()` could redirect a memory read to a
    sensitive file outside memory_dir. Uses ``os.O_NOFOLLOW`` where
    available (POSIX) so opening a symlink raises OSError. On platforms
    without ``O_NOFOLLOW`` (Windows), falls back to a plain open which
    is the previous behaviour (memory_dir already restricts the path).

    Raises ``FileNotFoundError`` if the file does not exist (caller maps
    that to a 404), and ``OSError`` for any other I/O failure.
    """
    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is not None:
        flags |= nofollow
    # os.fdopen takes ownership of the fd; the `with` closes it on exit.
    with os.fdopen(os.open(resolved_path, flags), "r", encoding="utf-8") as f:
        return f.read()
