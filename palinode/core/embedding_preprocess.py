"""Embedding text preprocessing for the Obsidian wiki-maintenance tools.

Background — design doc `artifacts/obsidian-integration/design.md`, section
"Embedding text preprocessing — known issue, captured":

Embedding models are sensitive to formatting noise.  When palinode auto-appends
`## See also` footers materializing entity wikilinks (Deliverable C), every note
that links the same entities gains the same trailing `[[alice]]`/`[[bob]]`
tokens.  Without preprocessing, the dedup and orphan-repair tools fire false
positives on every note that mentions the same names — "linked to the same
entities" gets conflated with "semantically similar content".

This module is the strip-at-query side of the fix.  See ``preprocess_for_similarity``
for the canonical pipeline.  Use the same pipeline on both sides of the
comparison (query content AND corpus chunks being matched against) — that's how
we keep the cosine similarity apples-to-apples.

The auto-footer marker is the HTML comment ``<!-- palinode-auto-footer -->``;
Deliverable C will emit it as the very first line of the auto-generated
``## See also`` section so the boundary is unambiguous.
"""
from __future__ import annotations

import re

import frontmatter


# HTML-comment delimiter Deliverable C will emit as the first line of any
# auto-generated `## See also` block.  The footer extends from the comment
# (inclusive) to end-of-string.  Stable contract — keep in sync with the save
# path's footer writer in `palinode/api/server.py`.
AUTO_FOOTER_MARKER = "<!-- palinode-auto-footer -->"

# `[[Alice Smith]]` and `[[Alice Smith|Alice]]` — keep the entity word, drop the
# brackets.  For aliased links (`[[target|display]]`) we keep the displayed
# text since that's what reads as semantic content; the underlying entity
# slug appears elsewhere via frontmatter ``entities:``.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|]+?)(?:\|([^\[\]]+?))?\]\]")


def strip_wikilinks(text: str) -> str:
    """Replace ``[[link]]`` syntax with the entity word.

    ``[[Alice Smith]]`` → ``Alice Smith``.
    ``[[meeting-2026-04-26|yesterday]]`` → ``yesterday`` (display text wins).

    The entity word itself is preserved because it is real semantic content of
    the note — what we are stripping is the bracket *decoration*, not the noun.
    """
    def _replace(match: re.Match[str]) -> str:
        target, display = match.group(1), match.group(2)
        return (display or target).strip()

    return _WIKILINK_RE.sub(_replace, text)


def strip_auto_footer(text: str) -> str:
    """Drop everything from the `<!-- palinode-auto-footer -->` marker onward.

    The marker is emitted by the Layer-2 save-time footer writer (Deliverable
    C, parallel PR).  If Deliverable C has not landed at index time, the
    marker simply never appears and this is a no-op.
    """
    idx = text.find(AUTO_FOOTER_MARKER)
    if idx == -1:
        return text
    # Trim back to the line break before the marker (so we don't leave an
    # orphan ``## See also`` heading hanging without its body).
    head = text[:idx].rstrip()
    # Walk back over a trailing ``## See also`` heading if one is present.
    lines = head.splitlines()
    while lines and re.match(r"^\s*##\s+See also\s*$", lines[-1], re.IGNORECASE):
        lines.pop()
    return "\n".join(lines).rstrip()


def strip_frontmatter(text: str) -> str:
    """Return the markdown body with YAML frontmatter removed.

    Mirrors what the indexer's content-hash pipeline already does (see
    ``palinode/core/store.py`` freshness check) — keeps the embedding-time and
    similarity-time text views consistent.
    """
    try:
        post = frontmatter.loads(text)
        return post.content or ""
    except Exception:
        return text


def preprocess_for_similarity(text: str) -> str:
    """Canonical preprocessing pipeline for the embedding tools.

    Order matters:
      1. Strip frontmatter (it's bookkeeping, not content)
      2. Strip the auto-generated ``## See also`` footer if present (otherwise
         every note linking the same entities looks like a duplicate)
      3. Strip ``[[wikilink]]`` bracket decoration (keep the entity word so the
         note still reads coherently to the embedding model)

    The result is the text that should be embedded for `dedup_suggest` and
    `orphan_repair`-style similarity comparisons.

    Apply this on BOTH sides of the comparison — query content and corpus
    chunks — for the cosine similarity to remain apples-to-apples.
    """
    body = strip_frontmatter(text or "")
    body = strip_auto_footer(body)
    body = strip_wikilinks(body)
    # Collapse runs of whitespace introduced by the strips.
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()
