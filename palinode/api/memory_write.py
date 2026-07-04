"""Save-path normalization: entity refs, wiki footers, source attribution (#556).

Extracted from the former ``routers/_shared.py`` junk drawer. The helpers the
``/save`` path runs over a write before it lands on disk: infer category
prefixes for bare entity refs, emit a safe ``## See also`` wikilink footer,
resolve the source-surface attribution, and the category↔type maps plus the
description-eligibility predicate those share.
"""

from __future__ import annotations

import logging
import os
import re

from fastapi import Request

from palinode.core.defaults import (
    SAVE_SOURCE_API_DEFAULT,
    SAVE_SOURCE_HEADER,
)

logger = logging.getLogger("palinode.api")

# Maps memory category dirs to singular entity-ref prefixes.
_CATEGORY_TO_ENTITY_PREFIX: dict[str, str] = {
    "people": "person",
    "decisions": "decision",
    "projects": "project",
    "insights": "insight",
    "research": "research",
    "inbox": "action",
}


_WIKI_FOOTER_MARKER = "<!-- palinode-auto-footer -->"

# Slugs are validated before being emitted as ``[[slug]]`` markdown wikilinks.
# Allow alphanumerics, underscore, hyphen, and dot (some legacy slugs include
# version-style dots, e.g. ``palinode-0.5.0``). Forbid ``[``, ``]``, ``|``,
# whitespace, and any other markdown-special character that could break
# wikilink syntax — see Tier B finding #4.
_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_wiki_slug(slug: str) -> bool:
    """Return True if `slug` is safe to embed inside `[[...]]` markdown.

    Used by `_apply_wiki_footer` to drop hostile entity slugs that would
    inject markdown structure (`]]bar[[`, embedded pipes, newlines, etc.).
    """
    if not slug or len(slug) > 200:
        return False
    return bool(_SAFE_SLUG_RE.fullmatch(slug))


def _apply_wiki_footer(content: str, entities: list[str]) -> str:
    """Append or update a ``## See also`` auto-footer for un-linked entities.

    When ``entities`` are provided but some of them are not already referenced
    as ``[[wikilinks]]`` in *content*, this function appends a detectable
    auto-generated footer so that Obsidian graph view picks up the links.

    Canonicalization: entity refs use the slash form ``category/slug``; the
    wikilink target is only the *slug* part (everything after the last ``/``).
    This matches the existing ``_normalize_entities`` convention — entity refs
    are stored as ``project/palinode``, the corresponding wikilink is
    ``[[palinode]]``.

    Rules:
    - If *content* is empty / None, or *entities* is empty, return unchanged.
    - Extract existing ``[[target]]`` wikilinks from body; skip entities whose
      slug already appears as an inline link.
    - If a ``## See also`` block with ``_WIKI_FOOTER_MARKER`` exists, **replace**
      it (idempotent re-save).
    - If a ``## See also`` block exists **without** the marker it is user-authored
      — leave it alone and append a new auto-footer block after it.
    - If all entities are already linked inline, remove any stale auto-footer.
    """
    if not content or not entities:
        return content

    # Pattern that matches an existing auto-footer block up to end-of-string or
    # the next level-2 heading.  Compiled once; used twice below.
    auto_footer_re = re.compile(
        r"## See also\s*\n" + re.escape(_WIKI_FOOTER_MARKER) + r".*?(?=\n## |\Z)",
        re.DOTALL,
    )

    # Scan for existing inline wikilinks OUTSIDE the auto-footer block so that
    # links inside the footer itself are not mistaken for user-authored inline
    # links.  This is the key to idempotency: on re-save the footer's own
    # [[slug]] entries do not satisfy the "already linked inline" check.
    body_for_scan = auto_footer_re.sub("", content)
    existing_links: set[str] = set(re.findall(r"\[\[([^\]]+)\]\]", body_for_scan))

    # Derive the wikilink slug for each entity (part after the last '/').
    # Tier B #4: validate every slug against _SAFE_SLUG_RE before emitting it
    # inside `[[...]]`. A slug like ``foo]]bar[[`` would otherwise let the
    # entity-list inject arbitrary markdown structure into the auto-footer.
    missing: list[str] = []
    for entity in entities:
        slug = entity.split("/")[-1]
        if not _safe_wiki_slug(slug):
            logger.warning(
                "Dropping unsafe entity slug from wiki footer: %r (entity=%r)",
                slug,
                entity,
            )
            continue
        if slug not in existing_links:
            missing.append(slug)

    # Build the new auto-footer block.  Always ends with a newline so that the
    # substitution path and the append path produce identical output (idempotent).
    if missing:
        footer_lines = ["## See also", _WIKI_FOOTER_MARKER]
        footer_lines.extend(f"- [[{slug}]]" for slug in missing)
        new_footer = "\n".join(footer_lines) + "\n"
    else:
        new_footer = ""

    if auto_footer_re.search(content):
        if new_footer:
            content = auto_footer_re.sub(new_footer, content)
        else:
            # All links are now inline — strip the stale auto-footer.
            content = auto_footer_re.sub("", content).rstrip("\n") + "\n"
    elif new_footer:
        # No existing auto-footer; append after a blank-line separator.
        content = content.rstrip("\n") + "\n\n" + new_footer

    return content


def _normalize_entities(entities: list[str], category: str) -> list[str]:
    """Ensure every entity ref has a category/ prefix.

    Bare strings (no '/') get a prefix inferred from the memory's own
    category.  Falls back to 'project/' when the category is unknown
    (matches MCP context-resolution convention).
    """
    prefix = _CATEGORY_TO_ENTITY_PREFIX.get(category, "project")
    normalized = []
    for e in entities:
        if "/" in e:
            normalized.append(e)
        else:
            logger.info("Entity normalized: %r → %r", e, f"{prefix}/{e}")
            normalized.append(f"{prefix}/{e}")
    return normalized


def _resolve_source(req_source: str | None, request: Request | None) -> str:
    """Resolve the source-surface attribution for a write.

    Precedence (ADR-010 / #167):
      1. Explicit ``source`` field in the request body — caller's intent wins.
      2. ``X-Palinode-Source`` HTTP header — set automatically by CLI/MCP.
      3. ``PALINODE_SOURCE`` environment variable — operator override.
      4. ``"api"`` default — used when nothing above is set.
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
    return os.environ.get("PALINODE_SOURCE", SAVE_SOURCE_API_DEFAULT)


_TYPE_TO_CATEGORY: dict[str, str] = {
    "PersonMemory": "people",
    "Decision": "decisions",
    "ProjectSnapshot": "projects",
    "Insight": "insights",
    "ResearchRef": "research",
    "ActionItem": "inbox",
}


#: The memory-category directories `save_api` writes to. A file outside these
#: (a `daily/` journal, `archive/`, `specs/` incl. `specs/prompts/`, or a
#: top-level doc like README.md / PROGRAM.md) is structural / non-memory: the
#: description backfill regenerates a description for it every run but
#: `_inject_description` never persists one (no memory frontmatter to land it
#: in), so counting it as "pending" loops the backfill forever.
_MEMORY_CATEGORY_DIRS: frozenset[str] = frozenset(_TYPE_TO_CATEGORY.values())


def _is_description_eligible(relpath: str) -> bool:
    """Whether ``relpath`` is a memory file that can persist an auto-description.

    The eligibility contract for both the ``pending_descriptions`` count and the
    ``/generate-summaries`` description worklist (#472). A file is eligible iff
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
