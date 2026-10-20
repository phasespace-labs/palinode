"""One definition of "this path is not a memory file".

Every walker of the store asks :func:`is_skipped_path`. A path is never memory
when a directory segment:

- is in :data:`ALWAYS_SKIP` (``specs``, ``prompts``, ``logs`` and tool folders);
- starts with ``.``;
- is build noise: ``venv``, ``node_modules``, ``__pycache__`` or ``*.egg-info``;
- is ``processed`` directly under ``inbox``.

Only the path relative to the store counts, so a store that lives under a dot or
build folder still indexes. The index skips nothing beyond that. A surface that
shows less passes one of the ``SKIP_FOR_*`` constants as ``extra``; ``extra``
only adds skips.

``core.cross_refs`` has one further exception, ``_is_spec_document``: a
hand-written ``specs/<name>.md`` can be linked.
"""
from __future__ import annotations

from collections.abc import Iterable

#: Directories that never hold memory files, on any surface.
ALWAYS_SKIP: frozenset[str] = frozenset(
    {"specs", "prompts", "logs", ".palinode", ".obsidian", ".git"}
)

#: ``/list``, the browse UI, project review, propagation and revalidation show curated current memory.
SKIP_FOR_LISTING: frozenset[str] = frozenset({"daily", "archive", "inbox"})

#: The session digest reads ``inbox/`` for open ActionItems.
SKIP_FOR_DIGEST: frozenset[str] = frozenset({"daily", "archive"})

#: Lint checks ``daily`` and ``inbox`` on purpose; Obsidian sync mirrors lint.
SKIP_FOR_LINT: frozenset[str] = frozenset({"archive"})

#: Daily notes are episodic session logs, exempt from TTL.
SKIP_FOR_TTL: frozenset[str] = frozenset({"daily"})

#: Cross-refs link curated current memory, as the listing shows it.
SKIP_FOR_CROSS_REFS: frozenset[str] = frozenset({"daily", "archive", "inbox"})

_BUILD_NOISE: frozenset[str] = frozenset({"venv", "node_modules", "__pycache__"})


def _is_never_memory_segment(segment: str, previous: str | None) -> bool:
    return (
        segment.startswith(".")
        or segment in ALWAYS_SKIP
        or segment in _BUILD_NOISE
        or segment.endswith(".egg-info")
        or (segment == "processed" and previous == "inbox")
    )


def is_skipped_path(rel_path: str, extra: Iterable[str] = frozenset()) -> bool:
    """True when any *directory* segment of ``rel_path`` is not memory.

    ``rel_path`` is relative to the memory dir, with either separator. ``extra``
    adds a surface's own folders and never removes a skip. A file name never
    counts: ``logs.md`` is still a memory.
    """
    segments = [s for s in rel_path.replace("\\", "/").split("/")[:-1] if s not in ("", ".")]
    skip = frozenset(extra)
    previous: str | None = None
    for segment in segments:
        if segment in skip or _is_never_memory_segment(segment, previous):
            return True
        previous = segment
    return False
