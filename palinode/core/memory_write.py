"""Save-path normalization primitives: entity refs, wiki footers, type→category.

The transport-independent half of the former ``api/memory_write.py``. These run
over a memory write before it lands on disk and have nothing to do with HTTP, so
they live below the API layer where :mod:`palinode.core.save` can reach them
without ``core`` importing ``api``.

``api/memory_write.py`` re-exports every name defined here, so the historical
import paths (``palinode.api.memory_write`` and ``palinode.api.server``) keep
working unchanged — the same define-low / re-export-high shape
``core/parity.py`` uses to re-export the enums from ``core/operations.py``.

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

#: Matches an auto-footer block up to end-of-string or the next level-2
#: heading. Module-level so ``_apply_wiki_footer`` and ``strip_wiki_footer``
#: share one pattern — an append composes a new body out of an existing one,
#: and the two must agree on what a footer block *is* or a stale footer gets
#: stranded mid-document.
_AUTO_FOOTER_RE = re.compile(
    r"## See also\s*\n" + re.escape(_WIKI_FOOTER_MARKER) + r".*?(?=\n## |\Z)",
    re.DOTALL,
)

#: Opening of the marker that tags a block ``update_policy: append`` added to
#: an existing document; the full marker closes with the append's timestamp
#: (see :func:`append_block_marker`). An HTML comment, so it is invisible in
#: rendered markdown but greppable, and it sits *under* an H2 heading rather
#: than replacing it: the indexer splits bodies on H2/H3
#: (``core/parser.py::parse_markdown``), so the heading is what gives each
#: appended block its own chunk instead of growing the last one without bound.
#: Same heading-plus-marker shape the wiki footer already uses.
APPEND_BLOCK_MARKER_PREFIX = "<!-- palinode-append"

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

    # Scan for existing inline wikilinks OUTSIDE the auto-footer block so that
    # links inside the footer itself are not mistaken for user-authored inline
    # links.  This is the key to idempotency: on re-save the footer's own
    # [[slug]] entries do not satisfy the "already linked inline" check.
    body_for_scan = _AUTO_FOOTER_RE.sub("", content)
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

    if _AUTO_FOOTER_RE.search(content):
        if new_footer:
            content = _AUTO_FOOTER_RE.sub(new_footer, content)
        else:
            # All links are now inline — strip the stale auto-footer.
            content = _AUTO_FOOTER_RE.sub("", content).rstrip("\n") + "\n"
    elif new_footer:
        # No existing auto-footer; append after a blank-line separator.
        content = content.rstrip("\n") + "\n\n" + new_footer

    return content


def strip_wiki_footer(content: str) -> str:
    """Remove the auto-generated ``## See also`` block from *content*.

    Only the marked, auto-generated footer is removed; a user-authored
    ``## See also`` section is left alone (it carries no marker, so the pattern
    does not match it). Used when composing an append: the prior body's footer
    belongs at the end of the *composed* document, not stranded in the middle
    of it, and ``_apply_wiki_footer`` re-emits it there from the entities the
    composed front matter carries, the prior ones included.
    """
    if not content:
        return content
    return _AUTO_FOOTER_RE.sub("", content).rstrip("\n")


def append_block_marker(now_iso: str) -> str:
    """The hidden marker line that opens an appended block."""
    return f"{APPEND_BLOCK_MARKER_PREFIX} {now_iso} -->"


def compose_appended_body(existing_body: str, new_content: str, now_iso: str) -> str:
    """Join *new_content* onto *existing_body* under a dated append heading.

    The separator is an H2 heading plus :func:`append_block_marker`::

        <existing body>

        ## Update — 2026-09-13
        <!-- palinode-append 2026-09-13T10:11:12.345678+00:00 -->

        <new content>

    Three properties the shape is chosen for: the H2 is a chunk boundary for
    ``parse_markdown``, so each appended block is independently retrievable
    instead of swelling the tail chunk; the heading is ordinary markdown, so
    the compaction prompt and the fact-id tagger (both of which work over
    list-item lines, not headings) need no new rules; and the hidden marker
    carries the exact ``last_updated`` stamp for anything that later wants to
    tell an appended block from author-written prose.

    *existing_body* is never truncated or rewritten — that is the whole point.
    Returns *new_content* alone when there is no prior body to append to.
    """
    prior = strip_wiki_footer(existing_body or "").rstrip("\n")
    if not prior.strip():
        return new_content
    heading_date = now_iso[:10]
    return (
        f"{prior}\n\n"
        f"## Update — {heading_date}\n"
        f"{append_block_marker(now_iso)}\n\n"
        f"{new_content.lstrip()}"
    )


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
