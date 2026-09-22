"""Memory-path resolution and traversal/symlink guard — the single implementation.

Every surface that resolves a caller-supplied memory path (HTTP read routes,
git provenance tools, the on-demand archive/retract ops) must cross this seam
before touching disk: resolve the path inside ``memory_dir`` — rejecting
absolute paths, ``../`` traversal, and symlinks that resolve outside the
tree — using ``pathlib.Path.resolve()`` + ``Path.is_relative_to()`` rather
than ``os.path.realpath()`` + ``str.startswith()``. The pathlib form closes
the Windows reparse-point / junction gap that ``realpath`` has, and
``resolve()`` returns a canonical form that narrows (though does not fully
close — see the module docstring in ``api/path_safety.py`` for the TOCTOU
caveat) the symlink-replacement race.

Promoted from ``api/path_safety.py``: that module and ``core/git_tools.py``
each carried their own ``_resolve_memory_path``, with materially different
security properties — the ``core`` copy used ``os.path.realpath`` with no
absolute-path rejection and echoed the caller's raw input in its
``ValueError`` message. This module is now the only implementation;
``api/path_safety.py``, ``core/git_tools.py`` and ``consolidation/archive.py``
all resolve through it.

This module raises :class:`PathTraversalError` — a plain :class:`ValueError`
subclass — and never imports FastAPI or any other transport. Each calling
surface maps the typed error to whatever status code / exit code / MCP error
shape is appropriate for its transport; the guard itself has no opinion about
that.

Error messages are intentionally generic — they do NOT include the resolved
path or memory_dir, to avoid leaking filesystem layout to an unauthenticated
attacker. The original (unresolved) input is logged at INFO so operators can
still debug, and is available on the raised exception as ``.file_path`` for a
caller that wants to log it again with local context.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from palinode.core.config import config

logger = logging.getLogger("palinode.core.path_guard")


class PathTraversalError(ValueError):
    """A caller-supplied memory path was rejected by :func:`resolve_memory_path`.

    ``str(exc)`` is the one generic, safe-to-surface message shared by every
    caller of this guard — it never echoes the offending input, so an HTTP
    handler, CLI command, or MCP tool can pass it straight through to an
    untrusted caller without leaking filesystem layout. The raw input remains
    available as ``.file_path`` for server-side logging only (this module
    already logs it at INFO — callers do not need to log it again just to
    preserve that visibility).

    ``.malformed`` distinguishes two rejection classes, matching the split
    ``api/path_safety.py`` already made before this promotion, so an HTTP
    adapter can keep choosing a status code the same way: ``True`` for input
    that is invalid on its face (currently: a null byte) and ``False`` for a
    syntactically valid path that resolves outside ``memory_dir`` (absolute
    path, ``../`` traversal, or a symlink escape).
    """

    def __init__(self, file_path: str, *, malformed: bool = False) -> None:
        self.file_path = file_path
        self.malformed = malformed
        super().__init__("Invalid path")


def memory_base_dir() -> str:
    """Return the canonical memory root."""
    return os.path.realpath(getattr(config, "memory_dir", config.palinode_dir))


def to_rel_path(
    file_path: str | os.PathLike[str],
    base_dir: str | os.PathLike[str] | None = None,
) -> str:
    """Best-effort memory-relative path for display, derived from ``memory_dir``.

    This is the read side of the relative-path story — not a security
    boundary like :func:`resolve_memory_path`. It exists so callers that
    already hold an absolute path (typically ``chunks.file_path``, which the
    store always stores absolute) can render it the way a caller would
    re-supply it to ``palinode_read``/``resolve_memory_path``.

    Uses ``os.path.relpath`` against ``memory_dir`` (or an explicit
    ``base_dir`` override) rather than string-splitting on a hardcoded
    literal such as ``/palinode/`` — the historical approach in ``mcp.py``
    silently returned the absolute path unchanged whenever the configured
    memory directory didn't happen to contain that substring (any custom
    install), and mis-split when a directory segment repeated (e.g.
    ``.../palinode/palinode/``).

    Normalizes computed and already-relative paths to POSIX forward slashes.
    Returns an empty string unchanged. Absolute paths on another Windows
    drive or outside the base directory are returned unchanged. Inputs
    outside the declared ``str | os.PathLike[str]`` contract raise
    ``TypeError``.
    """
    if not file_path:
        return os.fspath(file_path)
    path_str = os.fspath(file_path)
    if not os.path.isabs(path_str):
        return path_str.replace("\\", "/")
    base_str = os.fspath(base_dir) if base_dir is not None else config.memory_dir
    try:
        rel = os.path.relpath(path_str, base_str)
    except ValueError:
        # Windows: file_path and base on different drives.
        return path_str
    if rel == os.path.pardir or rel.startswith(os.path.pardir + os.path.sep):
        return path_str
    return rel.replace("\\", "/")


def resolve_memory_path(file_path: str) -> tuple[str, str]:
    """Resolve a relative memory path without allowing traversal outside memory_dir.

    Uses ``pathlib.Path.resolve(strict=False)`` plus ``Path.is_relative_to()``
    for the membership check. ``strict=False`` is used because callers of
    this helper sometimes resolve paths that don't yet exist on disk (e.g.
    ``/save`` resolving the destination path before writing it); the
    strict-existence check is performed by callers via ``os.path.exists`` /
    ``open`` once the path has cleared the traversal guard.

    Note: full TOCTOU mitigation via fd-based open requires invasive changes
    to every caller and is out of scope here. The pathlib-based check closes
    the cross-platform realpath gap (Windows symlinks, junction points) and
    the symlink-replacement window is significantly narrower than under
    realpath because resolve() returns a strict canonical form. Read callers
    that need the full TOCTOU close should pair this with
    :func:`palinode.api.path_safety._open_memory_file_text`
    (``O_NOFOLLOW``), which is a separate concern from resolution.

    Returns:
        ``(base_dir, resolved_path)`` — both absolute, both strings.

    Raises:
        PathTraversalError: ``file_path`` contains a null byte, is absolute,
            fails to resolve (symlink loop / permission error), or resolves
            outside ``memory_dir``.
    """
    if "\x00" in file_path:
        logger.info("Rejected null byte in memory path: %r", file_path)
        raise PathTraversalError(file_path, malformed=True)

    base_path = Path(memory_base_dir()).resolve()
    raw_path = Path(file_path)
    if raw_path.is_absolute():
        # Don't echo the offending input back to the caller.
        logger.info("Rejected absolute path: %r", file_path)
        raise PathTraversalError(file_path)

    try:
        resolved_path = (base_path / raw_path).resolve()
    except (OSError, RuntimeError) as exc:
        # OSError covers symlink loops / permission errors during resolution;
        # RuntimeError is raised by pathlib for infinite loops on some plats.
        logger.info("Path resolution failed for %r: %s", file_path, exc)
        raise PathTraversalError(file_path) from exc

    if not resolved_path.is_relative_to(base_path):
        logger.info("Rejected traversal outside memory_dir: %r", file_path)
        raise PathTraversalError(file_path)

    return str(base_path), str(resolved_path)


__all__ = ["PathTraversalError", "memory_base_dir", "resolve_memory_path", "to_rel_path"]
