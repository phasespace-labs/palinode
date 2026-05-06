"""Wiki-link formatting utilities for Palinode memory files.

Handles entity normalization, wiki footer generation, and slug validation
for Obsidian-compatible ``[[wikilink]]`` syntax.
"""
from __future__ import annotations

import logging
import re

__all__ = [
    "CATEGORY_TO_ENTITY_PREFIX",
    "WIKI_FOOTER_MARKER",
    "SAFE_SLUG_RE",
    "safe_wiki_slug",
    "apply_wiki_footer",
    "normalize_entities",
]

logger = logging.getLogger(__name__)

# Maps memory category dirs to singular entity-ref prefixes.
CATEGORY_TO_ENTITY_PREFIX: dict[str, str] = {
    "people": "person",
    "decisions": "decision",
    "projects": "project",
    "insights": "insight",
    "research": "research",
    "inbox": "action",
}


WIKI_FOOTER_MARKER = "<!-- palinode-auto-footer -->"

# Slugs are validated before being emitted as ``[[slug]]`` markdown wikilinks.
# Allow alphanumerics, underscore, hyphen, and dot (some legacy slugs include
# version-style dots, e.g. ``palinode-0.5.0``). Forbid ``[``, ``]``, ``|``,
# whitespace, and any other markdown-special character that could break
# wikilink syntax — see Tier B finding #4.
SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def safe_wiki_slug(slug: str) -> bool:
    """Return True if `slug` is safe to embed inside `[[...]]` markdown.

    Used by `apply_wiki_footer` to drop hostile entity slugs that would
    inject markdown structure (`]]bar[[`, embedded pipes, newlines, etc.).
    """
    if not slug or len(slug) > 200:
        return False
    return bool(SAFE_SLUG_RE.fullmatch(slug))


def apply_wiki_footer(content: str, entities: list[str]) -> str:
    """Append or update a ``## See also`` auto-footer for un-linked entities.

    When ``entities`` are provided but some of them are not already referenced
    as ``[[wikilinks]]`` in *content*, this function appends a detectable
    auto-generated footer so that Obsidian graph view picks up the links.

    Canonicalization: entity refs use the slash form ``category/slug``; the
    wikilink target is only the *slug* part (everything after the last ``/``).
    This matches the existing ``normalize_entities`` convention — entity refs
    are stored as ``project/palinode``, the corresponding wikilink is
    ``[[palinode]]``.

    Rules:
    - If *content* is empty / None, or *entities* is empty, return unchanged.
    - Extract existing ``[[target]]`` wikilinks from body; skip entities whose
      slug already appears as an inline link.
    - If a ``## See also`` block with ``WIKI_FOOTER_MARKER`` exists, **replace**
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
        r"## See also\s*\n" + re.escape(WIKI_FOOTER_MARKER) + r".*?(?=\n## |\Z)",
        re.DOTALL,
    )

    # Scan for existing inline wikilinks OUTSIDE the auto-footer block so that
    # links inside the footer itself are not mistaken for user-authored inline
    # links.  This is the key to idempotency: on re-save the footer's own
    # [[slug]] entries do not satisfy the "already linked inline" check.
    body_for_scan = auto_footer_re.sub("", content)
    existing_links: set[str] = set(re.findall(r"\[\[([^\]]+)\]\]", body_for_scan))

    # Derive the wikilink slug for each entity (part after the last '/').
    # Tier B #4: validate every slug against SAFE_SLUG_RE before emitting it
    # inside `[[...]]`. A slug like ``foo]]bar[[`` would otherwise let the
    # entity-list inject arbitrary markdown structure into the auto-footer.
    missing: list[str] = []
    for entity in entities:
        slug = entity.split("/")[-1]
        if not safe_wiki_slug(slug):
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
        footer_lines = ["## See also", WIKI_FOOTER_MARKER]
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


def normalize_entities(entities: list[str], category: str) -> list[str]:
    """Ensure every entity ref has a category/ prefix.

    Bare strings (no '/') get a prefix inferred from the memory's own
    category.  Falls back to 'project/' when the category is unknown
    (matches MCP context-resolution convention).
    """
    prefix = CATEGORY_TO_ENTITY_PREFIX.get(category, "project")
    normalized = []
    for e in entities:
        if "/" in e:
            normalized.append(e)
        else:
            logger.info("Entity normalized: %r → %r", e, f"{prefix}/{e}")
            normalized.append(f"{prefix}/{e}")
    return normalized
