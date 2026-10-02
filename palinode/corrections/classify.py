"""The model's only job: answer one closed question about one bounded window.

The classifier never sees a transcript, never sees a file and never decides
where a candidate came from — the detector already fixed all of that
deterministically. It sees a few hundred characters either side of a matched
span and picks from four answers:

``DECISION_CHANGE`` · ``REJECTED_APPROACH`` · ``REMEMBER_THIS`` · ``NONE``

``NONE`` is the whole reason the step exists: hypothetical advice, a proposal
nobody accepted, an observation made after the fact, sarcasm, and someone
else's words quoted back all read like corrections to a regex. Anything that is
not confidently one of the first three comes back ``needs_review`` — it is never
an operation, and in this release nothing is an operation anyway.

**This step sends conversation text to a model.** The deterministic detector
sends nothing; this module is the only place transcript text leaves the process,
and the configured consolidation endpoint may be a remote host. So the window is
built by refusal, not by truncation: at most
``WINDOW_TURNS_BEFORE + WINDOW_TURNS_AFTER + 1`` turns, at most
``WINDOW_TURN_CHARS`` characters each, and a turn that is not the user speaking
or an ordinary assistant reply contributes only a placeholder naming its kind —
never its text. Quoted, fenced and harness-injected regions are stripped from
the turns that do contribute, so a document pasted into the user's own turn does
not travel either.

The endpoint, model and temperature come from the configured consolidation role,
the same plumbing the lint contradiction check uses. There is no model literal
in this module.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from palinode.core.config import config
from palinode.core.ollama_client import OllamaError, OllamaRole, get_ollama_client
from palinode.corrections.detect import DetectedSpan, strip_non_user_regions
from palinode.corrections.readers import TranscriptTurn

logger = logging.getLogger("palinode.corrections.classify")

#: Turns of context either side of the matched span. Two is enough to tell an
#: accepted decision from a floated proposal ("what if we…" / "sure, do that")
#: and small enough that the window stays a window.
WINDOW_TURNS_BEFORE = 2
WINDOW_TURNS_AFTER = 2

#: Per-turn ceiling inside the window. The window is sent to the model and then
#: discarded; only the detector's bounded span is ever written down.
WINDOW_TURN_CHARS = 400

#: The classifier is advisory and bounded: one short answer, no retries. A slow
#: or cold endpoint yields ``needs_review``, which is the safe answer.
CLASSIFY_TIMEOUT_SECONDS = 45.0
CLASSIFY_MAX_TOKENS = 120

#: What a neighbouring turn contributes when it is not the user speaking and not
#: an ordinary assistant reply — its KIND, and no text at all.
#:
#: Detection already refuses to read these turns, but the window used to render
#: them verbatim, and tool output is precisely where file contents, environment
#: dumps and credentials live. "Excluded from capture" has to mean excluded from
#: the prompt too, or the exclusion only protects the queue while the sensitive
#: bytes go to a model that may not even be on this machine.
#:
#: A placeholder rather than a deletion: "the user said X, then a tool ran, then
#: the user said Y" is the shape the classifier needs to tell an accepted
#: decision from a floated one, and silently closing the gap would make two
#: distant turns look adjacent.
_OMITTED_KINDS: dict[str, str] = {
    "tool_output": "[tool output omitted]",
    "sidechain": "[subagent turn omitted]",
    "compact_summary": "[earlier-session summary omitted]",
    "meta": "[harness metadata omitted]",
    "prompt_source_system": "[harness-generated prompt omitted]",
    "prompt_source_sdk": "[programmatic prompt omitted]",
    "empty": "[empty turn omitted]",
}

#: The reason an ordinary assistant reply carries. Assistant prose is the
#: accept/reject signal ("sure, do that" / "want me to cost it?"), so it stays —
#: bounded, and with quoted and fenced regions stripped like everything else, so
#: a file the assistant pasted back does not ride along.
_ORDINARY_ASSISTANT_REASON = "not_a_user_turn"

#: Label used for a redacted entry, in place of a speaker.
OMITTED_LABEL = "omitted"

#: The ``classifier.reason`` recorded when classification was never attempted,
#: because ``capture.transcripts.classify`` is off. It is deliberately a
#: different value from the two "we asked and it did not work" reasons below —
#: a reviewer looking at a queue of ``needs_review`` candidates has to be able
#: to tell "nobody asked a model" from "a model was asked and was unusable"
#: from "a model could not be reached", because the three call for different
#: actions (turn classification on / re-run later / fix the endpoint).
NOT_RUN_REASON = "classification_not_run"

#: Recorded when the endpoint could not be reached or timed out.
UNAVAILABLE_REASON = "classifier_unavailable"

#: Recorded when a model answered but the answer was not one usable label.
AMBIGUOUS_REASON = "unparseable_classifier_answer"

#: The three states above, for surfaces and tests that enumerate them.
NOT_DECIDED_REASONS: tuple[str, ...] = (
    NOT_RUN_REASON,
    UNAVAILABLE_REASON,
    AMBIGUOUS_REASON,
)

_SYSTEM_PROMPT = (
    "You label one moment from a software session transcript. The user's turn is "
    "quoted as SPAN; the surrounding turns are context. Answer with exactly one "
    "of these tokens on the first line, and nothing else on that line:\n"
    "DECISION_CHANGE - the user explicitly overturns or reverses a decision.\n"
    "REJECTED_APPROACH - the user rejects an approach and gives a reason.\n"
    "REMEMBER_THIS - the user asks for a durable rule or fact to be remembered.\n"
    "NONE - anything else, including hypothetical or conditional advice, a "
    "proposal or question nobody accepted, an observation made after the fact, "
    "sarcasm or a joke, and words the user is quoting from someone else.\n"
    "On the second line give one short sentence of justification. If you are not "
    "confident, answer NONE."
)

_TOKEN_TO_CLASS: dict[str, str] = {
    "DECISION_CHANGE": "explicit_decision_change",
    "REJECTED_APPROACH": "rejected_approach",
    "REMEMBER_THIS": "remember_this",
    "NONE": "none_of_these",
}


@dataclass(frozen=True)
class Classification:
    """One closed-set answer, plus who answered and why it may be unusable."""

    label: str
    decided: bool
    model: str | None
    config_role: str | None
    reason: str | None = None
    justification: str | None = None

    def as_classifier_record(self) -> dict[str, object]:
        """The provenance block stored on a candidate."""
        return {
            "decided": self.decided,
            "model": self.model,
            "config_role": self.config_role,
            "reason": self.reason,
        }


def classifier_identity() -> tuple[str, str]:
    """The model and config role a classification would use, without calling it.

    Lives here because this module is the only one that decides which role the
    classifier binds to; a caller that wants to *report* the destination (the
    scan report, the disclosure) must not have to restate the role and risk
    describing an endpoint other than the one used.
    """
    return config.consolidation.llm_model, OllamaRole.CONSOLIDATION.value


def window_entry(turn: TranscriptTurn) -> tuple[str, str]:
    """Return what this turn may contribute to a prompt: its words, or its kind.

    Three cases, and only the first two carry text:

    * the user's own turn — the quoted and injected regions stripped, exactly as
      the detector sees it, so a pasted document inside the user's own turn does
      not reach the model either;
    * an ordinary assistant reply — same stripping, bounded;
    * anything else — a placeholder naming the kind of turn, and nothing more.
    """
    if turn.is_user_evidence or (
        turn.role == "assistant" and turn.ineligible_reason == _ORDINARY_ASSISTANT_REASON
    ):
        text = strip_non_user_regions(turn.text).strip()
        return turn.role, text[:WINDOW_TURN_CHARS]
    return OMITTED_LABEL, _OMITTED_KINDS.get(turn.ineligible_reason or "", "[turn omitted]")


def build_window(
    turns: Sequence[TranscriptTurn], turn_index: int
) -> tuple[list[tuple[str, str]], tuple[int, int]]:
    """Return the bounded window around *turn_index* and the turn range it covers."""
    positions = {turn.turn_index: position for position, turn in enumerate(turns)}
    center = positions.get(turn_index)
    if center is None:
        return [], (turn_index, turn_index)
    start = max(0, center - WINDOW_TURNS_BEFORE)
    end = min(len(turns) - 1, center + WINDOW_TURNS_AFTER)
    window = [window_entry(turns[position]) for position in range(start, end + 1)]
    return window, (turns[start].turn_index, turns[end].turn_index)


def _render_window(window: Sequence[tuple[str, str]], span: str) -> str:
    lines = [
        text if label == OMITTED_LABEL else f"{label.upper()}: {text}"
        for label, text in window
        if text
    ]
    context = "\n".join(lines) if lines else "(no surrounding turns)"
    return f"CONTEXT:\n{context}\n\nSPAN (the user's own words):\n{span}"


def _parse(raw: str) -> tuple[str | None, str | None]:
    """Return ``(class, justification)``; class is ``None`` when unparseable."""
    lines = [line.strip() for line in (raw or "").splitlines() if line.strip()]
    if not lines:
        return None, None
    token = lines[0].strip().strip(".:*# ").upper()
    justification = lines[1] if len(lines) > 1 else None
    if token in _TOKEN_TO_CLASS:
        return _TOKEN_TO_CLASS[token], justification
    # A model that wrapped the token in a sentence is still usable if exactly
    # one token appears; two tokens is ambiguity, not an answer.
    present = [name for name in _TOKEN_TO_CLASS if name in raw.upper()]
    if len(present) == 1:
        return _TOKEN_TO_CLASS[present[0]], justification
    return None, None


def classify_span(
    span: DetectedSpan, turns: Sequence[TranscriptTurn]
) -> tuple[Classification, tuple[int, int]]:
    """Ask the configured model to classify *span*; never raise, never guess."""
    window, turn_range = build_window(turns, span.turn_index)
    model = config.consolidation.llm_model
    role = OllamaRole.CONSOLIDATION
    try:
        content = get_ollama_client().chat_completions(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _render_window(window, span.span)},
            ],
            model=model,
            base_url=config.consolidation.llm_url,
            temperature=0.0,
            max_tokens=CLASSIFY_MAX_TOKENS,
            timeout=CLASSIFY_TIMEOUT_SECONDS,
            retries=0,
            role=role,
        )
    except OllamaError as error:
        logger.warning(
            "correction classifier unavailable; candidate left for review "
            "session=%s turn=%d error=%r",
            span.session_id, span.turn_index, error,
        )
        return (
            Classification(
                label="needs_review",
                decided=False,
                model=model,
                config_role=role.value,
                reason=UNAVAILABLE_REASON,
            ),
            turn_range,
        )

    label, justification = _parse(str(content))
    if label is None:
        logger.warning(
            "correction classifier returned no usable label; candidate left for review "
            "session=%s turn=%d", span.session_id, span.turn_index,
        )
        return (
            Classification(
                label="needs_review",
                decided=False,
                model=model,
                config_role=role.value,
                reason=AMBIGUOUS_REASON,
            ),
            turn_range,
        )
    return (
        Classification(
            label=label,
            decided=True,
            model=model,
            config_role=role.value,
            reason=None,
            justification=justification,
        ),
        turn_range,
    )


__all__ = [
    "CLASSIFY_MAX_TOKENS",
    "CLASSIFY_TIMEOUT_SECONDS",
    "AMBIGUOUS_REASON",
    "Classification",
    "NOT_DECIDED_REASONS",
    "NOT_RUN_REASON",
    "UNAVAILABLE_REASON",
    "OMITTED_LABEL",
    "classifier_identity",
    "WINDOW_TURNS_AFTER",
    "WINDOW_TURNS_BEFORE",
    "WINDOW_TURN_CHARS",
    "build_window",
    "classify_span",
    "window_entry",
]
