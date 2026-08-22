"""Save-path normalization primitives: entity refs, wiki footers, type→category.

The transport-independent half of the former ``api/memory_write.py``. These run
over a memory write before it lands on disk and have nothing to do with HTTP, so
they live below the API layer where :mod:`palinode.core.save` can reach them
without ``core`` importing ``api``.

``api/memory_write.py`` re-exports every name defined here, so the historical
import paths (``palinode.api.memory_write`` and ``palinode.api.server``) keep
working unchanged — the same define-low / re-export-high shape
``core/parity.py`` uses for its enums.

The one helper that did *not* move is ``_resolve_source``: it reads an HTTP
header, so it belongs to the transport layer.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("palinode.core.memory_write")

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


_TYPE_TO_CATEGORY: dict[str, str] = {
    "PersonMemory": "people",
    "Decision": "decisions",
    "ProjectSnapshot": "projects",
    "Insight": "insights",
    "ResearchRef": "research",
    "ActionItem": "inbox",
}

#: The memory-category directories the save path writes to. Keep this next to
#: ``_TYPE_TO_CATEGORY`` so core consumers do not need to import the API layer.
_MEMORY_CATEGORY_DIRS: frozenset[str] = frozenset(_TYPE_TO_CATEGORY.values())
