"""Deterministic accounting for text removed by a document correction."""
from __future__ import annotations

from collections import Counter
import re


def _units(body: str) -> list[str]:
    """Keep exact wording, splitting paragraphs, lists, sentences and semicolons.

    Headings and generated navigation are not claims. Fact markers identify
    lines but are not part of their wording. This is structural accounting,
    not a semantic claim matcher; abbreviations can conservatively over-split.
    """
    body = body.split("<!-- palinode-auto-footer -->", 1)[0]
    blocks: list[str] = []
    pending: list[str] = []
    for line in body.splitlines():
        boundary = not line.strip() or re.match(r"^\s*#{1,6}\s", line)
        bullet = re.match(r"^\s*(?:[-*+] |\d+[.)] )", line)
        marked = "<!-- fact:" in line
        if boundary or bullet or marked:
            if pending:
                blocks.append("\n".join(pending))
                pending = []
        if boundary:
            continue
        line = re.sub(r"<!-- fact:\S+ -->", "", line)
        line = re.sub(r"^\s*(?:[-*+] |\d+[.)] )", "", line).strip()
        pending.append(line)
        if marked:
            blocks.append("\n".join(pending))
            pending = []
    if pending:
        blocks.append("\n".join(pending))
    return [
        part.strip() for block in blocks
        for part in re.split(r"(?<=[.;!?])\s+", block) if part.strip()
    ]


def _key(text: str) -> str:
    return " ".join(text.split()).casefold().rstrip(".;!?")


def content_loss(old: str, new: str) -> dict:
    """Report omitted units; changing multiple units requires an explicit choice.

    A single omitted unit is the ordinary one-claim correction. With multiple
    omissions we cannot know which was intended, so report all of them.
    """
    original = _units(old)
    remaining = Counter(_key(unit) for unit in _units(new))
    removed = []
    for unit in original:
        key = _key(unit)
        if remaining[key]:
            remaining[key] -= 1
        else:
            removed.append(unit)
    return {
        "removed_text": removed,
        "requires_confirmation": len(original) > 1 and len(removed) > 1,
    }
