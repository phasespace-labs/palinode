"""
Tiered views over a memory file's content.

Three tiers, cheapest first:

``abstract``
    One short paragraph — the ``summary:`` frontmatter when the file has one,
    else ``canonical_question:``, else the body's first paragraph. Capped at
    :attr:`~palinode.core.config.ReadConfig.abstract_max_chars`. Enough to
    decide whether a hit is worth opening.

``overview``
    The frontmatter block plus the first N characters of the body, capped at
    :attr:`~palinode.core.config.ReadConfig.overview_max_chars`. Enough to plan
    against without paying for the whole file.

``full``
    The content unchanged. The default for ``read``.

Every tier is computed deterministically at read time from content that is
already in hand. No LLM, no second content store: tiers are *views*, and the
markdown file stays the single source of truth (ADR-001).
"""
from __future__ import annotations

from typing import Any

from palinode.core.config import config
from palinode.core.parser import split_frontmatter

#: Marker appended when a tier truncates. Counted against the cap, never added
#: on top of it — a caller that asked for ``<= n`` chars gets ``<= n`` chars.
ELLIPSIS = "…"


def _truncate(text: str, max_chars: int) -> str:
    """Cut ``text`` to at most ``max_chars`` characters, ellipsis included."""
    if max_chars <= 0:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    if max_chars <= len(ELLIPSIS):
        return text[:max_chars]
    return text[: max_chars - len(ELLIPSIS)].rstrip() + ELLIPSIS


def first_paragraph(body: str) -> str:
    """The body's first non-empty paragraph, skipping markdown headings.

    Headings are skipped rather than returned because a heading is a label,
    not a description — ``## Session End`` tells a caller nothing that the
    file path did not already.
    """
    for block in body.split("\n\n"):
        stripped = block.strip()
        if not stripped:
            continue
        lines = [
            line
            for line in stripped.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if lines:
            return " ".join(line.strip() for line in lines)
    return ""


def abstract_for(
    metadata: dict[str, Any] | None,
    body: str,
    *,
    max_chars: int | None = None,
) -> str:
    """The cheapest useful view: summary, else canonical_question, else lede."""
    cap = config.read.abstract_max_chars if max_chars is None else max_chars
    metadata = metadata or {}

    for key in ("summary", "canonical_question"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return _truncate(value, cap)

    return _truncate(first_paragraph(body), cap)


def overview_for(
    content: str,
    body: str,
    *,
    max_chars: int | None = None,
) -> str:
    """Frontmatter block verbatim, plus the head of the body under one cap.

    The frontmatter is kept whole because it is the structured part — types,
    entities, epistemic markers, confidence — and truncating it mid-key would
    produce something that reads as YAML but is not. When frontmatter alone
    already exceeds the cap the body is simply omitted; the cap still holds.
    """
    cap = config.read.overview_max_chars if max_chars is None else max_chars
    if cap <= 0:
        return ""

    frontmatter_block = ""
    if content.lstrip().startswith("---"):
        _, after = split_frontmatter(content)
        fm_len = len(content) - len(after)
        frontmatter_block = content[:fm_len].rstrip("\n")

    if not frontmatter_block:
        return _truncate(body, cap)

    if len(frontmatter_block) >= cap:
        return frontmatter_block[:cap]

    remaining = cap - len(frontmatter_block) - 2  # the blank line between them
    head = _truncate(body, remaining) if remaining > 0 else ""
    return f"{frontmatter_block}\n\n{head}".rstrip() if head else frontmatter_block


def apply_tier(
    tier: str | None,
    content: str,
    metadata: dict[str, Any] | None = None,
    *,
    max_chars: int | None = None,
) -> str:
    """Render ``content`` at ``tier``.

    ``None`` and ``"full"`` both return the content unchanged, so a caller that
    passes nothing keeps the behaviour it had before tiers existed.
    """
    if tier is None or tier == "full":
        return content

    _, body = split_frontmatter(content)
    body = body.lstrip("\n")

    if tier == "abstract":
        return abstract_for(metadata, body, max_chars=max_chars)
    if tier == "overview":
        return overview_for(content, body, max_chars=max_chars)

    raise ValueError(f"unknown tier: {tier!r}")
