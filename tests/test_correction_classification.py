"""The classification step's plumbing, with the LLM call — and only it — mocked.

What a mock can prove: the window sent is bounded, the answer is parsed into the
closed set, an unreachable or unparseable model degrades to ``needs_review``
rather than to a verdict, a confident ``NONE`` is dropped and counted, and the
model and config role are recorded on the candidate.

What a mock cannot prove: whether a real model can tell sarcasm from a decision.
That is the baseline in ``tests/test_correction_detector_scoring.py``, which is
reported NOT RUN unless a chat endpoint is actually reachable.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from palinode.core.config import config
from palinode.core.ollama_client import OllamaError
from palinode.corrections import classify as classify_module
from palinode.corrections.classify import (
    WINDOW_TURNS_AFTER,
    WINDOW_TURNS_BEFORE,
    WINDOW_TURN_CHARS,
    build_window,
    classify_span,
)
from palinode.corrections.detect import detect_spans
from palinode.corrections.queue import load_candidates, queue_path
from palinode.corrections.readers import ClaudeCodeTranscriptReader, TranscriptTurn
from palinode.corrections.scan import scan_transcripts

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "transcripts" / "claude_code" / "v1"


class _StubClient:
    """Stands in for the configured chat client, and records what it was asked."""

    def __init__(self, answer: str | Exception):
        self.answer = answer
        self.calls: list[dict] = []

    def chat_completions(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.capture.transcripts, "enabled", True)
    # This module exercises the classifier, so it opts in explicitly — the
    # shipped default is off, and `test_correction_capture_controls.py` is where
    # that default is asserted.
    monkeypatch.setattr(config.capture.transcripts, "classify", True)
    monkeypatch.setattr(config.capture.transcripts, "lookback_days", 0)
    monkeypatch.setattr(config.capture.transcripts, "max_candidates", 50)
    monkeypatch.setattr(
        config.capture.transcripts, "harness_paths", {"claude-code": [str(FIXTURE_DIR)]}
    )
    return tmp_path


def _use(monkeypatch: pytest.MonkeyPatch, answer: str | Exception) -> _StubClient:
    stub = _StubClient(answer)
    monkeypatch.setattr(classify_module, "get_ollama_client", lambda: stub)
    return stub


def _first_span():
    session = next(
        s for s in ClaudeCodeTranscriptReader().sessions(FIXTURE_DIR)
        if s.path.name == "harbor-notes-export.jsonl"
    )
    return detect_spans(session.turns)[0], session


def test_a_confident_answer_becomes_the_candidate_class(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _use(monkeypatch, "DECISION_CHANGE\nThe user overturned the scheduler choice.")
    span, session = _first_span()

    result, turn_range = classify_span(span, session.turns)

    assert result.label == "explicit_decision_change"
    assert result.decided is True
    assert result.model == config.consolidation.llm_model
    assert result.config_role == "consolidation"
    assert turn_range[0] <= span.turn_index <= turn_range[1]
    assert stub.calls[0]["temperature"] == 0.0
    assert stub.calls[0]["retries"] == 0


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("REJECTED_APPROACH\nreason given", "rejected_approach"),
        ("REMEMBER_THIS", "remember_this"),
        ("NONE\nsarcasm", "none_of_these"),
    ],
)
def test_the_closed_set_is_the_whole_vocabulary(
    monkeypatch: pytest.MonkeyPatch, answer: str, expected: str
) -> None:
    _use(monkeypatch, answer)
    span, session = _first_span()
    assert classify_span(span, session.turns)[0].label == expected


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        ("maybe? could be either DECISION_CHANGE or REMEMBER_THIS", "unparseable_classifier_answer"),
        ("", "unparseable_classifier_answer"),
        ("I am not sure what you mean.", "unparseable_classifier_answer"),
    ],
)
def test_an_unusable_answer_is_review_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch, answer: str, reason: str
) -> None:
    _use(monkeypatch, answer)
    span, session = _first_span()

    result, _ = classify_span(span, session.turns)

    assert result.label == "needs_review"
    assert result.decided is False
    assert result.reason == reason


def test_an_unreachable_model_leaves_the_candidate_for_review(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _use(monkeypatch, OllamaError("connection refused"))
    span, session = _first_span()

    result, _ = classify_span(span, session.turns)

    assert result.label == "needs_review"
    assert result.reason == "classifier_unavailable"


def test_the_window_is_bounded_in_turns_and_in_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _use(monkeypatch, "NONE")
    span, session = _first_span()
    window, turn_range = build_window(session.turns, span.turn_index)

    assert len(window) <= WINDOW_TURNS_BEFORE + WINDOW_TURNS_AFTER + 1
    assert all(len(text) <= WINDOW_TURN_CHARS for _, text in window)
    assert turn_range[1] - turn_range[0] <= WINDOW_TURNS_BEFORE + WINDOW_TURNS_AFTER

    classify_span(span, session.turns)
    prompt = stub.calls[0]["messages"][1]["content"]
    assert span.span in prompt
    assert prompt.count("USER:") + prompt.count("ASSISTANT:") <= len(window)


def test_a_classified_negative_is_dropped_from_the_queue_and_counted(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The classifier's whole job is removing candidates; the count stays visible."""
    _use(monkeypatch, "NONE\nnot a correction")

    report, candidates = scan_transcripts()

    assert report.candidates_detected == 12
    assert report.classified["none_of_these"] == 12
    assert report.skipped["classified_none_of_these"] == 12
    assert candidates == []
    assert not queue_path().exists()


def test_a_classified_positive_is_queued_with_its_classifier_provenance(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, "REMEMBER_THIS\nstanding rule")

    report, candidates = scan_transcripts()

    assert report.classified == {"remember_this": 12}
    assert len(candidates) == 12
    records = load_candidates()
    assert all(record["classification"] == "remember_this" for record in records)
    assert all(record["classifier"]["decided"] is True for record in records)
    assert all(
        record["classifier"]["config_role"] == "consolidation" for record in records
    )
    assert report.classifier["model"] == config.consolidation.llm_model


def test_the_window_is_never_written_to_the_queue(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Context is shown to the model and thrown away; only the span is stored."""
    _use(monkeypatch, "DECISION_CHANGE\nyes")

    scan_transcripts()

    raw = queue_path().read_text(encoding="utf-8")
    assert "CONTEXT:" not in raw
    assert "Understood, switching the export" not in raw  # a neighbouring turn


# ── what may cross the wire ─────────────────────────────────────────────────
#
# The classifier is the only place transcript text leaves the process, and the
# configured consolidation endpoint need not be on this machine. "Excluded from
# capture" therefore has to mean excluded from the prompt, not merely excluded
# from the queue.


def _sentinels() -> dict[str, str]:
    manifest = json.loads((FIXTURE_DIR / "labels.json").read_text(encoding="utf-8"))
    return manifest["leak_sentinels"]


def _prompts(stub: _StubClient) -> str:
    """Every byte this run handed to the model, concatenated."""
    return "\n".join(
        message["content"] for call in stub.calls for message in call["messages"]
    )


def test_pasted_document_text_never_reaches_the_classifier_prompt(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces a real leak: the fenced block used to be rendered verbatim.

    The pasted-document turn is the user's own turn, so the window included it —
    and before the regions inside it were stripped, the whole fenced vendor
    document went to the model, sentinel and all. Verified against the previous
    window implementation: this sentinel reached the prompt, and now does not.
    """
    stub = _use(monkeypatch, "NONE")
    scan_transcripts()

    prompts = _prompts(stub)
    assert prompts, "no prompt was built — every assertion below would be vacuous"
    assert _sentinels()["pasted_document"] not in prompts
    assert "always disable checksums" not in prompts
    # The turn is still the user's, so its own framing sentence may appear; only
    # the fenced region is removed.
    assert "vendor integration note" in prompts


def test_injected_and_quoted_text_never_reaches_the_classifier_prompt(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Also reproduces real leaks, from the two other in-turn exclusions.

    A harness-injected standing instruction and a correction quoted from a
    colleague both reached the model under the previous window, for the same
    reason: the turn was the user's, so its *whole* text was rendered.
    """
    stub = _use(monkeypatch, "NONE")
    scan_transcripts()

    prompts = _prompts(stub)
    assert prompts, "no prompt was built — the assertions below would be vacuous"
    assert "always use the gateway for rate limiting" not in prompts
    assert "put the rate limiter in the gateway and remember this" not in prompts


def test_tool_output_contributes_a_placeholder_and_never_its_text(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forward guard, not a reproduction — and worth having as one.

    The fixture's tool-output turn *is* selected into a real candidate's window
    (the placeholder below only appears because it was), but its text could not
    have reached the model even before the redaction: Claude Code carries tool
    output in ``tool_result`` blocks, and the reader's text extraction takes
    ``text`` blocks only. That is one sentence in ``_blocks_text`` away from
    being untrue, and "include tool results for context" is a plausible future
    change. This pins both halves: the turn is represented, and its bytes are
    not the representation.
    """
    stub = _use(monkeypatch, "NONE")
    scan_transcripts()

    prompts = _prompts(stub)
    assert prompts, "no prompt was built — the assertions below would be vacuous"
    assert "[tool output omitted]" in prompts
    assert _sentinels()["tool_output"] not in prompts
    assert "harbor-export" not in prompts


@pytest.mark.parametrize(
    ("reason", "placeholder"),
    [
        ("tool_output", "[tool output omitted]"),
        ("sidechain", "[subagent turn omitted]"),
        ("compact_summary", "[earlier-session summary omitted]"),
        ("meta", "[harness metadata omitted]"),
        ("prompt_source_system", "[harness-generated prompt omitted]"),
    ],
)
def test_an_excluded_turn_contributes_only_its_kind(reason: str, placeholder: str) -> None:
    """Every excluded kind, asserted directly on a turn that does carry text.

    The fixtures cannot position one of each kind inside a candidate's window,
    and a test that passes because the turn was never selected proves nothing.
    This drives ``window_entry`` with a turn whose text is a sentinel, for each
    reason the reader can produce.
    """
    turn = TranscriptTurn(
        harness="claude-code",
        session_id="session-x",
        turn_index=1,
        turn_uuid="uuid-1",
        role="user",
        text="EXCLUDED-TURN-TEXT-MUST-NOT-TRAVEL",
        timestamp=None,
        cwd=None,
        git_branch=None,
        is_user_evidence=False,
        ineligible_reason=reason,
    )

    label, text = classify_module.window_entry(turn)

    assert label == classify_module.OMITTED_LABEL
    assert text == placeholder
    assert "EXCLUDED-TURN-TEXT-MUST-NOT-TRAVEL" not in text


def test_an_excluded_assistant_turn_is_redacted_like_any_other() -> None:
    """A sidechain assistant turn is a subagent's, not the reply the window wants."""
    sidechain = TranscriptTurn(
        harness="claude-code",
        session_id="session-x",
        turn_index=1,
        turn_uuid="uuid-1",
        role="assistant",
        text="EXCLUDED-TURN-TEXT-MUST-NOT-TRAVEL",
        timestamp=None,
        cwd=None,
        git_branch=None,
        is_user_evidence=False,
        ineligible_reason="sidechain",
    )

    assert classify_module.window_entry(sidechain) == (
        classify_module.OMITTED_LABEL,
        "[subagent turn omitted]",
    )


def test_ordinary_assistant_replies_stay_in_the_window(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deliberate: the assistant's prose is the accept/reject signal.

    "what if we…" answered by "want me to cost it?" is how an unaccepted
    proposal is distinguishable from a decision, so assistant replies are kept —
    bounded, and stripped of quoted and fenced regions like everything else.
    """
    stub = _use(monkeypatch, "NONE")
    scan_transcripts()

    prompts = _prompts(stub)
    assert "Understood, switching the export to the queue worker." in prompts
    assert "ASSISTANT:" in prompts


def test_leak_sentinels_never_reach_the_queue(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, "DECISION_CHANGE\nyes")
    scan_transcripts()

    raw = queue_path().read_text(encoding="utf-8")
    for sentinel in _sentinels().values():
        assert sentinel not in raw


def test_the_models_own_words_never_reach_the_queue(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model picks a label from a closed set; it cannot write the record.

    Its justification line is parsed, used for nothing that is stored, and
    dropped. Everything textual in a candidate is cut from the transcript by the
    deterministic detector, so a model that answers with a paragraph — or with a
    prompt injection — cannot put a byte of its own into the queue.
    """
    _use(monkeypatch, "DECISION_CHANGE\nMODEL-AUTHORED-TEXT-SHOULD-NOT-PERSIST")
    scan_transcripts()

    raw = queue_path().read_text(encoding="utf-8")
    assert "MODEL-AUTHORED-TEXT-SHOULD-NOT-PERSIST" not in raw
    for record in load_candidates():
        assert set(record["classifier"]) == {"decided", "model", "config_role", "reason"}
        assert record["classifier"]["reason"] is None
