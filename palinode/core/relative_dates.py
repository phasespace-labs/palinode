"""Relative time expressions: detect them, resolve them, rewrite them.

``PROGRAM.md`` § "Resolve dates" states the rule: resolve relative times against
the session date and record the absolute one, because "last Tuesday" is
unreadable six months on and *nothing downstream can recover which Tuesday it
was*. The rule shipped as prose; this module is the machinery that enforces it,
and it is deliberately three separable pieces:

* :func:`find_relative_dates` — the detector. Deterministic, anchored so that
  ISO dates, dated bullets and code samples do not trip it. Feeds
  ``palinode lint``'s ``relative_dates`` check.
* :func:`resolve` — the arithmetic. A phrase plus an anchor date yields an
  absolute date, or a reason why it never can.
* :func:`normalize_text` — the rewriter, used at write time. Strictly narrower
  than the detector: it refuses everything the detector merely reports.

The asymmetry between the last two is the design. **Detection is generous;
rewriting is not.** A relative date inside a quotation rots exactly like any
other, so lint reports it — but rewriting it would put words in the speaker's
mouth, so normalisation leaves it alone. The same split applies to code blocks
(reported never, rewritten never), and to vague expressions like "recently",
which name no day at all: they are drift, so they are reported, and no
arithmetic can fix them, so they are never rewritten and never proposed as an
operation.

The rewriter is wired into **session-end only**, not into every save. A
session-end body is text an agent just extracted from a conversation it was
present for, so the session timestamp genuinely anchors it; ``palinode_save``
carries arbitrary caller content — a pasted document, a quote, an imported
note — whose relative phrases are anchored to something the save call does not
know. Rewriting those would date them to the import. Lint still reports them.

Two phrase classes never resolve, whatever the anchor:

* **vague** — "recently", "lately", "currently", "right now", "these days".
  There is no day to compute.
* **interval** — "last week", "next month", "three weeks ago". These name a
  *span*, and collapsing a span to a single day asserts a precision the author
  did not write. Only day-precise phrases ("yesterday", "3 days ago", "last
  Tuesday") are rewritten.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterator

#: Sentinel in the ``resolved`` field of a finding whose phrase names no day.
UNRESOLVABLE = "unresolvable"

#: Sentinel in the ``anchor`` field when nothing dated the memory.
NO_ANCHOR = "none"

_NUMBER_WORDS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}

_WEEKDAYS: dict[str, int] = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

# Phrases that name no day at any anchor.
_VAGUE = frozenset({"recently", "lately", "currently", "right now", "these days"})

# Phrases that name a span rather than a day.
_INTERVAL_UNITS = frozenset({"week", "month", "year"})

_NUMBER = r"(?:\d+|" + "|".join(_NUMBER_WORDS) + r")"
_WEEKDAY = "|".join(_WEEKDAYS)

# The detector. Every alternative is word-boundary guarded; the numeric one
# additionally refuses a number that is part of an ISO date or a path, so
# "[2026-09-10] shipped" and "2026-09-10 days into the quarter" stay quiet.
_PHRASE_RE = re.compile(
    r"(?<!\w)(?:"
    rf"(?<![\w/-]){_NUMBER} (?:days|weeks|months|years) ago"
    rf"|(?:last|next|this) (?:week|month|year|{_WEEKDAY})"
    r"|right now|these days"
    r"|yesterday|today|tomorrow|recently|lately|currently"
    r")(?!\w)",
    re.IGNORECASE,
)

# A phrase already followed by its absolute date — "yesterday (2026-09-08)" —
# is anchored prose, not drift. Also the shape a partial normalisation leaves
# behind, so without this the linter would report its own output forever.
_ALREADY_ANCHORED_RE = re.compile(r"\s*[\(\[]\s*\d{4}-\d{2}-\d{2}")

_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_QUOTED_RE = re.compile(r"\"[^\"]*\"|“[^”]*”")
_BLOCKQUOTE_RE = re.compile(r"^\s*>")
# Matched with ``.match(line, end)``, which anchors at ``end`` — a leading
# ``^`` would anchor at the start of the line instead and never fire.
_POSSESSIVE_RE = re.compile(r"['’]s(?!\w)", re.IGNORECASE)
_ISO_IN_NAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# After one of these, the bare date reads correctly ("since 2026-09-08"); after
# anything else the rewrite needs its own preposition ("shipped on 2026-09-08").
_PREPOSITIONS = frozenset({
    "on", "since", "until", "till", "by", "from", "after", "before",
    "through", "around", "about", "of", "in", "at",
})

_REASON_RESOLVED = ""
_REASON_VAGUE = "vague — the phrase names no particular day"
_REASON_INTERVAL = (
    "an interval, not a day — collapsing it to one date would assert a "
    "precision the author did not write"
)
_REASON_AMBIGUOUS = (
    "ambiguous — 'this <weekday>' can fall either side of the anchor date"
)
_REASON_NO_ANCHOR = (
    "no anchor — the memory carries no created_at and its filename is not dated"
)
_REASON_UNKNOWN = "not a recognised relative-date phrase"
_REASON_OUT_OF_RANGE = "the arithmetic lands outside the representable calendar"


@dataclass(frozen=True)
class RelativeDate:
    """One detected phrase, with its resolution if it has one."""

    line: int
    start: int
    end: int
    expression: str
    resolved: date | None
    reason: str

    def as_finding(self, anchor: date | None) -> dict[str, str]:
        """The lint-report shape: JSON-safe strings on every surface."""
        return {
            "line": str(self.line),
            "expression": self.expression,
            "resolved": self.resolved.isoformat() if self.resolved else UNRESOLVABLE,
            "anchor": anchor.isoformat() if anchor else NO_ANCHOR,
            "reason": self.reason,
        }


def _as_date(value: Any) -> date | None:
    """Best-effort date from a frontmatter value; ``None`` when unusable."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def anchor_for(metadata: dict[str, Any] | None, rel_path: str = "") -> date | None:
    """The date a memory's relative phrases are relative *to*.

    ``created_at`` first — the memory says when its text was written. Failing
    that, an ISO date in the filename, which covers the two file shapes that
    carry their date in the name rather than the frontmatter:
    ``daily/2026-09-10.md`` and the ``session-end-2026-09-10-…`` files
    session-end writes.

    Deliberately NOT ``last_updated``: a memory edited months after it was
    written would resolve its own "yesterday" against the edit, which is a
    wrong date stated confidently — worse than the relative phrase it replaces.
    """
    if metadata:
        for key in ("created_at", "date"):
            anchor = _as_date(metadata.get(key))
            if anchor is not None:
                return anchor
    match = _ISO_IN_NAME_RE.search(os.path.basename(rel_path))
    if match:
        return _as_date(match.group(1))
    return None


def resolve(expression: str, anchor: date | None) -> tuple[date | None, str]:
    """``(absolute date, reason)`` for one phrase. ``None`` ⇒ unresolvable.

    The anchor-independent refusals come first, so the reason an operator reads
    is the durable one: "recently" names no day whether or not the memory is
    dated, and saying "no anchor" there would send them off to fix the wrong
    thing.
    """
    phrase = " ".join(expression.lower().split())

    if phrase in _VAGUE:
        return None, _REASON_VAGUE

    words = phrase.split()

    if len(words) == 3 and words[2] == "ago":
        unit = words[1].rstrip("s")
        if unit != "day":
            return None, _REASON_INTERVAL
        if anchor is None:
            return None, _REASON_NO_ANCHOR
        count = _NUMBER_WORDS.get(words[0], 0) or int(words[0])
        try:
            return anchor - timedelta(days=count), _REASON_RESOLVED
        except OverflowError:
            return None, _REASON_OUT_OF_RANGE

    if len(words) == 2 and words[0] in {"last", "next", "this"}:
        qualifier, unit = words
        if unit in _INTERVAL_UNITS:
            return None, _REASON_INTERVAL
        if qualifier == "this":
            return None, _REASON_AMBIGUOUS
        if anchor is None:
            return None, _REASON_NO_ANCHOR
        target = _WEEKDAYS[unit]
        if qualifier == "last":
            delta = (anchor.weekday() - target) % 7 or 7
            return anchor - timedelta(days=delta), _REASON_RESOLVED
        delta = (target - anchor.weekday()) % 7 or 7
        return anchor + timedelta(days=delta), _REASON_RESOLVED

    offsets = {"yesterday": -1, "today": 0, "tomorrow": 1}
    if phrase not in offsets:
        return None, _REASON_UNKNOWN
    if anchor is None:
        return None, _REASON_NO_ANCHOR
    return anchor + timedelta(days=offsets[phrase]), _REASON_RESOLVED


def _protected_spans(line: str, *, quotes: bool) -> list[tuple[int, int]]:
    """Character ranges on this line the detector must not look inside."""
    spans = [m.span() for m in _INLINE_CODE_RE.finditer(line)]
    if quotes:
        spans += [m.span() for m in _QUOTED_RE.finditer(line)]
    return spans


def _scan(
    text: str,
    anchor: date | None,
    *,
    protect_quotes: bool,
) -> Iterator[RelativeDate]:
    """Every phrase outside a fence, a code span and (optionally) a quotation."""
    in_fence = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if protect_quotes and _BLOCKQUOTE_RE.match(line):
            continue
        spans = _protected_spans(line, quotes=protect_quotes)
        for match in _PHRASE_RE.finditer(line):
            start, end = match.span()
            if any(a <= start < b for a, b in spans):
                continue
            if _ALREADY_ANCHORED_RE.match(line, end):
                continue
            if protect_quotes and _POSSESSIVE_RE.match(line, end):
                continue
            resolved, reason = resolve(match.group(0), anchor)
            yield RelativeDate(
                line=line_number,
                start=start,
                end=end,
                expression=match.group(0),
                resolved=resolved,
                reason=reason,
            )


def find_relative_dates(text: str, anchor: date | None = None) -> list[RelativeDate]:
    """Every relative phrase in ``text``, resolved against ``anchor`` if given.

    Quotations and blockquotes are **included**: a relative date inside a
    quotation rots like any other, and reporting it costs a line of output.
    Rewriting it is a different question — see :func:`normalize_text`.
    """
    return list(_scan(text, anchor, protect_quotes=False))


def _replacement(line: str, match: RelativeDate) -> str:
    """The absolute form, with a preposition only where the prose needs one."""
    assert match.resolved is not None
    iso = match.resolved.isoformat()
    preceding = line[: match.start].rstrip().rsplit(" ", 1)[-1].strip("([\"'")
    if preceding.lower() in _PREPOSITIONS:
        return iso
    lead = "On" if match.expression[:1].isupper() else "on"
    return f"{lead} {iso}"


def normalize_text(text: str, anchor: date | None) -> tuple[str, int]:
    """Rewrite the resolvable relative phrases in ``text`` to absolute dates.

    Returns ``(rewritten text, count)``. What it refuses to touch, and why:

    * **fenced code and inline code spans** — a code sample containing
      ``yesterday`` is a literal, and rewriting it changes what the sample does.
    * **quoted text and blockquotes** — rewriting a quotation puts words in the
      speaker's mouth. The memory keeps what was said; lint still reports it.
    * **possessives** ("yesterday's build") — the rewrite would need to
      restructure the sentence, which is wording, which is judgement.
    * **anything unresolvable** — vague and interval phrases, and every phrase
      when no anchor dates the text.
    """
    if anchor is None or not text:
        return text, 0

    lines = text.splitlines(keepends=True)
    count = 0
    # Grouped by line, applied right-to-left so earlier spans keep their offsets.
    by_line: dict[int, list[RelativeDate]] = {}
    for match in _scan(text, anchor, protect_quotes=True):
        if match.resolved is not None:
            by_line.setdefault(match.line, []).append(match)

    for line_number, matches in by_line.items():
        index = line_number - 1
        line = lines[index]
        for match in sorted(matches, key=lambda m: m.start, reverse=True):
            line = line[: match.start] + _replacement(line, match) + line[match.end:]
            count += 1
        lines[index] = line

    return "".join(lines), count


def normalize_lines(values: list[str] | None, anchor: date | None) -> tuple[list[str] | None, int]:
    """:func:`normalize_text` over a list of one-line strings (decisions, blockers)."""
    if not values:
        return values, 0
    rewritten: list[str] = []
    total = 0
    for value in values:
        text, count = normalize_text(value, anchor)
        rewritten.append(text)
        total += count
    return rewritten, total
