"""Measure the correction detector against the labelled fixtures.

The issue's acceptance asks for candidate precision, yield and *missed*
corrections on versioned sanitized fixtures before the capture source is
broadened, and that is what this module computes. Two stages are scored
separately, because they fail differently:

* **Grep stage** — the deterministic detector alone. It is deliberately generous:
  a missed correction is gone forever, while a false positive costs one review.
  So the number to watch here is `missed`, and the price is precision.
* **Grep + classification** — the closed-set LLM answer applied on top. This is
  where precision is supposed to come back. It needs a reachable chat endpoint;
  without one it is reported NOT RUN rather than estimated, and the mocked tests
  below prove the plumbing only — a mock cannot tell you whether a model can
  tell sarcasm from a decision.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from palinode.corrections.detect import DetectedSpan, detect_spans, strip_non_user_regions
from palinode.corrections.readers import ClaudeCodeTranscriptReader

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "transcripts" / "claude_code" / "v1"

POSITIVE_LABELS = frozenset(
    {"explicit_decision_change", "rejected_approach", "remember_this"}
)

#: Labels whose turns the detector must exclude *structurally* — the user did
#: not write those words, or did not write them as their own statement. A
#: candidate here is not a precision problem, it is a correctness bug.
STRUCTURALLY_EXCLUDED_LABELS = frozenset(
    {"tool_output", "assistant_turn", "restated_summary", "injected_context", "pasted_document"}
)


@dataclass(frozen=True)
class Scored:
    """One stage's confusion counts against the fixture labels."""

    candidates: int
    true_positives: int
    false_positives: int
    positives: int
    missed: tuple[str, ...]

    @property
    def precision(self) -> float:
        return self.true_positives / self.candidates if self.candidates else 0.0

    @property
    def recall(self) -> float:
        return self.true_positives / self.positives if self.positives else 0.0


def _labels() -> dict[tuple[str, int], dict[str, Any]]:
    manifest = json.loads((FIXTURE_DIR / "labels.json").read_text(encoding="utf-8"))
    assert manifest["fixture_version"] == 1
    return {(row["session_id"], row["turn_index"]): row for row in manifest["turns"]}


def _detected() -> list[DetectedSpan]:
    reader = ClaudeCodeTranscriptReader()
    spans: list[DetectedSpan] = []
    for session in reader.sessions(FIXTURE_DIR):
        spans.extend(detect_spans(session.turns))
    return spans


def _score(
    predicted: dict[tuple[str, int], str | None],
    labels: dict[tuple[str, int], dict[str, Any]],
) -> Scored:
    """Score a mapping of turn → predicted class (``None`` = not a candidate)."""
    positives = [key for key, row in labels.items() if row["label"] in POSITIVE_LABELS]
    true_positives = 0
    false_positives = 0
    candidates = 0
    for key, prediction in predicted.items():
        if prediction is None:
            continue
        candidates += 1
        if labels[key]["label"] in POSITIVE_LABELS:
            true_positives += 1
        else:
            false_positives += 1
    missed = tuple(
        f"{labels[key]['transcript']}#{key[1]} ({labels[key]['label']})"
        for key in positives
        if predicted.get(key) is None
    )
    return Scored(
        candidates=candidates,
        true_positives=true_positives,
        false_positives=false_positives,
        positives=len(positives),
        missed=missed,
    )


def _report(name: str, scored: Scored, extra: str = "") -> str:
    lines = [
        f"\n{name}",
        f"  candidates:        {scored.candidates}",
        f"  true positives:    {scored.true_positives} / {scored.candidates}",
        f"  false positives:   {scored.false_positives} / {scored.candidates}",
        f"  precision:         {scored.precision:.3f}  ({scored.true_positives}/{scored.candidates})",
        f"  yield (recall):    {scored.recall:.3f}  ({scored.true_positives}/{scored.positives})",
        f"  missed:            {len(scored.missed)} / {scored.positives}",
    ]
    for item in scored.missed:
        lines.append(f"    - {item}")
    if extra:
        lines.append(f"  {extra}")
    return "\n".join(lines)


def test_fixture_label_counts_are_the_published_denominators() -> None:
    """The denominators every number below is quoted against."""
    labels = _labels()
    counts: dict[str, int] = {}
    for row in labels.values():
        counts[row["label"]] = counts.get(row["label"], 0) + 1
    positives = sum(counts[label] for label in POSITIVE_LABELS)
    assert len(labels) == 30
    assert positives == 8
    assert counts["explicit_decision_change"] == 4
    assert counts["rejected_approach"] == 2
    assert counts["remember_this"] == 2
    # Every hard case the issue names is present.
    hard_cases = {row["hard_case"] for row in labels.values() if row["hard_case"]}
    assert hard_cases == {
        "correction_text_in_tool_output",
        "correction_text_inside_pasted_document",
        "correction_text_in_injected_reminder",
        "correction_text_in_assistant_turn",
        "same_correction_restated_in_summary",
        "quoted_correction_from_another_person",
        "sarcastic_remember_this",
        "correction_reverted_later_in_same_session",
    }


def test_grep_stage_precision_and_yield(capsys: pytest.CaptureFixture[str]) -> None:
    """Report the deterministic stage's numbers, and hold its floors."""
    labels = _labels()
    predicted: dict[tuple[str, int], str | None] = {key: None for key in labels}
    for span in _detected():
        predicted[(span.session_id, span.turn_index)] = span.grep_family
    scored = _score(predicted, labels)

    with capsys.disabled():
        print(_report("grep stage (deterministic, no model)", scored))

    assert not scored.missed, (
        "the detector lost a labelled correction — a miss is unrecoverable, "
        "a false positive is one review"
    )
    assert scored.recall == 1.0
    # The four survivors are the intended hard cases: two unaccepted proposals,
    # one sarcastic remember-this and one hypothetical. They are what the
    # classification stage exists to remove.
    assert scored.false_positives == 4
    assert scored.precision >= 0.6


def test_structurally_excluded_turns_never_produce_candidates() -> None:
    """Not-the-user's-words is a correctness rule, not a precision tradeoff."""
    labels = _labels()
    offenders = [
        labels[(span.session_id, span.turn_index)]
        for span in _detected()
        if labels[(span.session_id, span.turn_index)]["label"] in STRUCTURALLY_EXCLUDED_LABELS
    ]
    assert not offenders, (
        "a candidate came from text the user did not write: "
        f"{[row['label'] for row in offenders]}"
    )


def test_quoted_correction_from_another_person_is_excluded() -> None:
    """A colleague's correction, quoted, is not this user deciding anything."""
    labels = _labels()
    quoted = [
        key for key, row in labels.items()
        if row["hard_case"] == "quoted_correction_from_another_person"
    ]
    detected_keys = {(span.session_id, span.turn_index) for span in _detected()}
    assert quoted and not (set(quoted) & detected_keys)


def test_both_halves_of_a_reverted_correction_are_captured() -> None:
    """A correction reverted later is two decisions, and both are evidence.

    Ordering them is the review flow's job. Dropping either one would lose the
    fact that the user changed their mind twice, which is exactly the history a
    memory system is for.
    """
    labels = _labels()
    detected_keys = {(span.session_id, span.turn_index) for span in _detected()}
    reverted = [
        key for key, row in labels.items()
        if row["hard_case"] == "correction_reverted_later_in_same_session"
    ]
    assert len(reverted) == 1
    assert set(reverted) <= detected_keys


def test_a_span_is_only_its_own_turns_words() -> None:
    """A span never borrows a neighbouring turn's text.

    The window shown to the classifier spans several turns; the *record* must
    not. Each span is asserted to be a subsequence of its own turn's text with
    the non-user regions already removed — so neither the previous turn nor a
    pasted block inside this one can end up quoted in the queue.
    """
    reader = ClaudeCodeTranscriptReader()
    for session in reader.sessions(FIXTURE_DIR):
        by_index = {turn.turn_index: turn for turn in session.turns}
        for span in detect_spans(session.turns):
            source = " ".join(
                strip_non_user_regions(by_index[span.turn_index].text).split()
            )
            for sentence in span.span.split(". "):
                fragment = sentence.strip(" .…")
                if fragment:
                    assert fragment in source, (
                        f"span text is not from turn {span.turn_index}: {fragment!r}"
                    )


def test_detected_relations_are_quoted_from_the_span() -> None:
    """A target/replacement is recorded only when the span says both halves."""
    for span in _detected():
        if span.replaced is not None:
            assert span.replaced.lower() in span.span.lower()
        if span.replacement is not None:
            assert span.replacement.lower() in span.span.lower()
            assert span.replaced is not None, "a replacement with nothing replaced is a guess"
        if span.rationale is not None:
            assert span.rationale.lower() in span.span.lower()
    # At least one fixture exercises each half, or the assertions above are vacuous.
    assert any(span.replaced and span.replacement for span in _detected())
    assert any(span.rationale for span in _detected())


# ── the classification stage ────────────────────────────────────────────────


def _chat_endpoint_reachable() -> bool:
    """True when the configured consolidation endpoint answers at all."""
    if os.environ.get("PALINODE_CORRECTION_BASELINE") != "1":
        return False
    import httpx

    from palinode.core.config import config

    try:
        httpx.get(config.consolidation.llm_url, timeout=2.0)
        return True
    except Exception:
        return False


@pytest.mark.slow
@pytest.mark.skipif(
    not _chat_endpoint_reachable(),
    reason=(
        "classification baseline NOT RUN: set PALINODE_CORRECTION_BASELINE=1 with a "
        "reachable configured chat endpoint. The numbers are never estimated."
    ),
)
def test_classification_stage_baseline(capsys: pytest.CaptureFixture[str]) -> None:
    """Record a real grep+classification baseline against the fixtures."""
    from palinode.core.config import config
    from palinode.corrections.classify import classify_span

    labels = _labels()
    reader = ClaudeCodeTranscriptReader()
    predicted: dict[tuple[str, int], str | None] = {key: None for key in labels}
    needs_review = 0
    exact = 0
    for session in reader.sessions(FIXTURE_DIR):
        for span in detect_spans(session.turns):
            result, _ = classify_span(span, session.turns)
            key = (span.session_id, span.turn_index)
            if result.label == "none_of_these":
                continue
            if result.label == "needs_review":
                needs_review += 1
                continue
            predicted[key] = result.label
            if labels[key]["label"] == result.label:
                exact += 1
    scored = _score(predicted, labels)
    with capsys.disabled():
        print(
            _report(
                "grep + classification",
                scored,
                extra=(
                    f"exact class matches: {exact}/{scored.true_positives} · "
                    f"needs_review (not counted either way): {needs_review} · "
                    f"model={config.consolidation.llm_model} role=consolidation"
                ),
            )
        )
    assert scored.candidates > 0
