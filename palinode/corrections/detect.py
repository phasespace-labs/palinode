"""The deterministic half: find candidate spans with a narrow grep.

No model runs here. A detected span carries its own session id, turn index and
timestamp, so a candidate's anchor into the transcript is reproducible from the
transcript alone — re-running the detector on the same file yields the same span
hash, which is half of what makes the dedupe key trustworthy.

Two rules shape everything below:

* **Only the user's own turns are evidence.** Turn eligibility is decided in
  :mod:`palinode.corrections.readers`; on top of that, this module removes the
  regions inside a user turn that are not the user speaking — fenced blocks and
  quoted lines (a pasted document, someone else's words), harness-injected
  reminders, slash-command envelopes and captured command output.
* **One candidate per turn.** A turn that matches three patterns is one moment,
  not three. The span is the matched sentences, bounded; the family is the
  strongest match. Splitting it per pattern would inflate every count downstream
  and give the same correction several dedupe keys.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable

from palinode.corrections.readers import TranscriptTurn

#: Hard ceiling on the quoted transcript text a candidate may carry. The queue
#: stores the span and nothing else from the transcript, so this is the actual
#: bound on how much of a session ends up on disk per candidate.
MAX_SPAN_CHARS = 320

#: Ceiling on a quoted rationale or a quoted target/replacement fragment. These
#: are cut from the span, so they are already inside the bound above; the
#: separate limits keep a single run-on sentence from filling the record.
MAX_RATIONALE_CHARS = 200
MAX_RELATION_CHARS = 80

#: Grep families, strongest first. The family is a *hint* for the classifier,
#: not a verdict: the closed-set answer comes from
#: :mod:`palinode.corrections.classify`.
FAMILIES: tuple[str, ...] = (
    "explicit_decision_change",
    "rejected_approach",
    "remember_this",
)

#: Regions inside a user turn that are not the user's own words. Removed before
#: any pattern runs, so a correction quoted from a colleague, pasted from a
#: vendor document or injected by the harness cannot become a candidate.
_NON_USER_REGIONS: tuple[re.Pattern[str], ...] = (
    re.compile(r"```.*?```", re.DOTALL),
    re.compile(r"~~~.*?~~~", re.DOTALL),
    re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<local-command-stdout>.*?</local-command-stdout>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<user-prompt-submit-hook>.*?</user-prompt-submit-hook>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<command-(?:name|message|args)>.*?</command-(?:name|message|args)>", re.DOTALL | re.IGNORECASE),
    # Markdown quotation: someone else's line, reproduced.
    re.compile(r"^[ \t]*>.*$", re.MULTILINE),
)

#: (family, rule name, pattern). Narrow on purpose: every pattern here is a
#: phrase a person types when overturning a decision, refusing an approach with
#: a reason, or asking for something to be remembered. Breadth is not a virtue —
#: an unclassifiable candidate costs a review, and a flood of them costs the
#: reviewer's attention, which is the scarce thing.
_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "explicit_decision_change", "negated_verdict",
        re.compile(r"\bno,?\s+(?:that(?:'s| is)?|this is|it'?s)\s+(?:wrong|incorrect|not right)", re.I),
    ),
    (
        "explicit_decision_change", "not_what_i_said",
        re.compile(r"\bthat'?s not (?:what|right|correct|it)\b", re.I),
    ),
    (
        "explicit_decision_change", "scratch_that",
        re.compile(r"\b(?:scratch that|forget what i (?:said|told)|ignore what i (?:said|told)|changed my mind)\b", re.I),
    ),
    (
        "explicit_decision_change", "revert_to",
        re.compile(r"\b(?:go|going|switch|switching|revert|reverting)\s+back to\b", re.I),
    ),
    (
        "explicit_decision_change", "not_doing_that",
        re.compile(r"\bwe(?:'re| are) not (?:using|doing|shipping|running|keeping)\b", re.I),
    ),
    (
        "explicit_decision_change", "use_instead",
        re.compile(r"\b(?:use|using|used|do|switch to)\b[^.!?]{0,80}\binstead\b", re.I),
    ),
    (
        "explicit_decision_change", "instead_of",
        re.compile(r"\binstead of\b", re.I),
    ),
    (
        "explicit_decision_change", "this_not_that",
        re.compile(r"\b(?:use|keep|do|run)\b[^.!?]{0,80},\s*not\b", re.I),
    ),
    (
        "rejected_approach", "dont_do_that",
        re.compile(r"\b(?:don'?t|do not|never)\s+(?:\w+\s+){0,1}(?:use|add|put|call|write|ship|run|merge|commit|push|store|hardcode|introduce)\b", re.I),
    ),
    (
        "rejected_approach", "stop_doing",
        re.compile(r"\bstop (?:using|doing|adding|running)\b", re.I),
    ),
    (
        "rejected_approach", "wont_work",
        re.compile(r"\bthat (?:won'?t|will not|does not|doesn'?t) work\b", re.I),
    ),
    (
        "remember_this", "remember_this",
        re.compile(r"\b(?:remember (?:this|that)|note this|make a note|for future reference|from now on)\b", re.I),
    ),
    (
        "remember_this", "standing_rule",
        re.compile(r"\b(?:always|never)\s+(?:\w+\s+){0,2}(?:use|do|run|write|commit|push|log|store|hardcode|assume|touch|edit|delete|ship|merge)\b", re.I),
    ),
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_WHITESPACE = re.compile(r"\s+")

_RATIONALE = re.compile(r"\bbecause\b(?P<reason>[^.!?]{3,%d})" % MAX_RATIONALE_CHARS, re.I)

#: Deterministic target/replacement extraction. Each pattern quotes text that is
#: physically present in the span — a relation is recorded only when the user
#: said both halves. Nothing here guesses at a memory to supersede; naming a
#: target memory is the review flow's job, with a human in it.
_REPLACED_RULES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bnot (?:using|use|shipping|doing|running)\s+(?P<value>[^.,;!?]{2,%d})" % MAX_RELATION_CHARS, re.I),
    re.compile(r"\binstead of\s+(?P<value>[^.,;!?]{2,%d})" % MAX_RELATION_CHARS, re.I),
    re.compile(r",\s*not\s+(?P<value>[^.,;!?]{2,%d})" % MAX_RELATION_CHARS, re.I),
)
_REPLACEMENT_RULES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\buse\s+(?P<value>[^.,;!?]{2,%d}?)\s+instead\b" % MAX_RELATION_CHARS, re.I),
    re.compile(r"\b(?:use|keep|switch to|go back to)\s+(?P<value>[^.,;!?]{2,%d})" % MAX_RELATION_CHARS, re.I),
)


@dataclass(frozen=True)
class DetectedSpan:
    """One candidate moment, anchored in the transcript, with no model involved."""

    harness: str
    session_id: str
    turn_index: int
    turn_uuid: str | None
    timestamp: str | None
    cwd: str | None
    git_branch: str | None
    span: str
    span_hash: str
    grep_family: str
    matched_rules: tuple[str, ...]
    rationale: str | None
    replaced: str | None
    replacement: str | None


def strip_non_user_regions(text: str) -> str:
    """Remove the parts of a user turn that the user did not write."""
    cleaned = text
    for pattern in _NON_USER_REGIONS:
        cleaned = pattern.sub(" ", cleaned)
    return cleaned


def span_hash(span: str) -> str:
    """Hash a span the way the dedupe key needs it.

    Normalised on whitespace and case so that a trailing newline or a re-wrapped
    line does not mint a second candidate for the same sentence.
    """
    normalized = _WHITESPACE.sub(" ", span).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()]


def _bounded(value: str, limit: int) -> str:
    collapsed = _WHITESPACE.sub(" ", value).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _first_match(patterns: Iterable[re.Pattern[str]], text: str) -> str | None:
    for pattern in patterns:
        found = pattern.search(text)
        if found:
            return _bounded(found.group("value"), MAX_RELATION_CHARS)
    return None


def detect_in_turn(turn: TranscriptTurn) -> DetectedSpan | None:
    """Return this turn's candidate span, or ``None`` when nothing matches."""
    if not turn.is_user_evidence:
        return None
    cleaned = strip_non_user_regions(turn.text)
    matched_sentences: list[str] = []
    rules: list[str] = []
    families: set[str] = set()
    for sentence in _sentences(cleaned):
        hits = [(family, rule) for family, rule, pattern in _RULES if pattern.search(sentence)]
        if not hits:
            continue
        matched_sentences.append(sentence)
        for family, rule in hits:
            families.add(family)
            if rule not in rules:
                rules.append(rule)
    if not matched_sentences:
        return None

    span = _bounded(" ".join(matched_sentences), MAX_SPAN_CHARS)
    family = next(name for name in FAMILIES if name in families)
    rationale_match = _RATIONALE.search(span)
    replaced = _first_match(_REPLACED_RULES, span)
    replacement = _first_match(_REPLACEMENT_RULES, span)
    if replaced is None:
        # A replacement with nothing it replaces is not a relation, just a
        # restatement of the instruction already quoted in the span.
        replacement = None
    return DetectedSpan(
        harness=turn.harness,
        session_id=turn.session_id,
        turn_index=turn.turn_index,
        turn_uuid=turn.turn_uuid,
        timestamp=turn.timestamp,
        cwd=turn.cwd,
        git_branch=turn.git_branch,
        span=span,
        span_hash=span_hash(span),
        grep_family=family,
        matched_rules=tuple(rules),
        rationale=(
            _bounded(rationale_match.group("reason"), MAX_RATIONALE_CHARS)
            if rationale_match
            else None
        ),
        replaced=replaced,
        replacement=replacement,
    )


def detect_spans(turns: Iterable[TranscriptTurn]) -> list[DetectedSpan]:
    """Run the detector over a session's turns, in order."""
    found = []
    for turn in turns:
        span = detect_in_turn(turn)
        if span is not None:
            found.append(span)
    return found


__all__ = [
    "DetectedSpan",
    "FAMILIES",
    "MAX_RATIONALE_CHARS",
    "MAX_RELATION_CHARS",
    "MAX_SPAN_CHARS",
    "detect_in_turn",
    "detect_spans",
    "span_hash",
    "strip_non_user_regions",
]
