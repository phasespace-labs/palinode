"""Save-path normalization: entity refs, wiki footers, source attribution.

Extracted from the former ``routers/_shared.py`` junk drawer. The helpers the
``/save`` path runs over a write before it lands on disk: infer category
prefixes for bare entity refs, emit a safe ``## See also`` wikilink footer,
resolve the source-surface attribution, and the category↔type maps plus the
description-eligibility predicate those share.

Most of that body now lives in :mod:`palinode.core.memory_write` so that
:mod:`palinode.core.save` can reach it without ``core`` importing ``api`` —
the transport-independent normalizers moved down and are re-exported here, so
``from palinode.api.memory_write import _apply_wiki_footer`` (and the
``palinode.api.server`` re-export of the same names) still resolves.

What remains defined here is what genuinely belongs to the HTTP surface:
``_resolve_source``, which reads a request header, and the description
-eligibility predicate the enrichment endpoints share.
"""

from __future__ import annotations

import logging
import os

from fastapi import Request

from palinode.core.defaults import SAVE_SOURCE_HEADER

# Re-exported for backwards compatibility: these are *defined* in
# palinode.core.memory_write (see this module's docstring). Import them from
# either path — they are the same objects.
from palinode.core.memory_write import (  # noqa: F401
    _CATEGORY_TO_ENTITY_PREFIX,
    _MEMORY_CATEGORY_DIRS,
    _SAFE_SLUG_RE,
    _TYPE_TO_CATEGORY,
    _WIKI_FOOTER_MARKER,
    _apply_wiki_footer,
    _normalize_entities,
    _safe_wiki_slug,
)

logger = logging.getLogger("palinode.api")


def _resolve_source(req_source: str | None, request: Request | None) -> str:
    """Resolve the source-surface attribution for a write.

    Precedence (ADR-010):
      1. Explicit ``source`` field in the request body — caller's intent wins.
      2. ``X-Palinode-Source`` HTTP header — set automatically by CLI/MCP.
      3. ``PALINODE_SOURCE`` environment variable — operator override.
      4. ``"api"`` default — used when nothing above is set.

    Levels 1-2 are transport-specific and resolved here; levels 3-4 are
    ``core.save.default_source()``, which the capability also applies to callers
    that reach it without going through HTTP. Imported lazily so the API's
    normalization helpers stay cheap to import (``core.save`` pulls in the
    store).
    """
    if req_source:
        return req_source
    if request is not None:
        # FastAPI normalizes header names to lowercase on read; supply both
        # spellings to be safe across stacks.
        hdr = request.headers.get(SAVE_SOURCE_HEADER) or request.headers.get(
            SAVE_SOURCE_HEADER.lower()
        )
        if hdr:
            return hdr
    from palinode.core.save import default_source

    return default_source()


def _is_description_eligible(relpath: str) -> bool:
    """Whether ``relpath`` is a memory file that can persist an auto-description.

    The eligibility contract for both the ``pending_descriptions`` count and the
    ``/generate-summaries`` description worklist. A file is eligible iff
    it lives directly under one of the memory-category directories
    (:data:`_MEMORY_CATEGORY_DIRS`) that ``save_api`` writes to. Structural /
    non-memory files — `daily/`, `archive/`, `specs/`, and top-level docs — are
    excluded, because the description write-back is a no-op for them; counting
    or regenerating their descriptions burns inference on output that is thrown
    away (the permanent-backlog bug this predicate fixes).

    Args:
        relpath (str): File path relative to ``PALINODE_DIR``.

    Returns:
        bool: True if the file may carry a persisted ``description``.
    """
    parts = relpath.split(os.sep)
    if len(parts) < 2:
        return False  # top-level file (README.md, PROGRAM.md, …) — not a memory
    return parts[0] in _MEMORY_CATEGORY_DIRS
