"""The controls that make an opt-in transcript reader safe to ship.

Every test here is about something *not* happening: not reading, not writing,
not growing without bound, not keeping more text than the bounded span. A
capture source pointed at session transcripts earns its place by being provably
inert until switched on, and provably narrow afterwards.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from palinode.core.capture_policy import update_capture_policy
from palinode.core.config import config
from palinode.core.ollama_client import OllamaError
from palinode.corrections import classify as classify_module
from palinode.corrections import readers as readers_module
from palinode.corrections.detect import MAX_SPAN_CHARS
from palinode.corrections.queue import load_candidates, queue_path
from palinode.corrections.scan import DETECTION_ONLY, corrections_report, scan_transcripts

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "transcripts" / "claude_code" / "v1"


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real store directory on disk, with transcript capture left OFF."""
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.capture.transcripts, "enabled", False)
    monkeypatch.setattr(config.capture.transcripts, "classify", False)
    monkeypatch.setattr(config.capture.transcripts, "harness_paths", {})
    monkeypatch.setattr(config.capture.transcripts, "lookback_days", 0)
    monkeypatch.setattr(config.capture.transcripts, "max_candidates", 50)
    return tmp_path


def _enable(monkeypatch: pytest.MonkeyPatch, *, paths: list[str] | None = None) -> None:
    monkeypatch.setattr(config.capture.transcripts, "enabled", True)
    monkeypatch.setattr(
        config.capture.transcripts,
        "harness_paths",
        {"claude-code": paths if paths is not None else [str(FIXTURE_DIR)]},
    )


def _forbid_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any transcript read an outright failure."""

    def _explode(self, root):  # noqa: ANN001 - test double
        raise AssertionError(f"transcripts were read when they must not be: {root}")

    monkeypatch.setattr(readers_module.ClaudeCodeTranscriptReader, "sessions", _explode)


class _ForbiddenClient:
    """A chat client that fails the test if anything asks it for a completion.

    ``call_count`` rather than a bare raise so a test can also assert the
    *absence* of calls positively; the raise is there so a leak fails loudly
    even in a test that forgets to check the count.
    """

    def __init__(self) -> None:
        self.call_count = 0

    def chat_completions(self, messages, **kwargs):  # noqa: ANN001 - test double
        self.call_count += 1
        raise AssertionError(
            "the classifier was called with transcript text while "
            "capture.transcripts.classify is off"
        )


def _forbid_model_calls(monkeypatch: pytest.MonkeyPatch) -> _ForbiddenClient:
    client = _ForbiddenClient()
    monkeypatch.setattr(classify_module, "get_ollama_client", lambda: client)
    return client


def test_classification_is_off_by_default_and_no_model_is_called(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enabling the source must not, by itself, send a byte to any model.

    Reading transcripts is local; classifying them is not. They are separate
    opt-ins precisely so that turning capture on cannot silently start
    transmitting a person's conversations to a configured endpoint that may be
    somewhere else entirely.
    """
    assert config.capture.transcripts.classify is False, "the shipped default is off"
    _enable(monkeypatch)
    client = _forbid_model_calls(monkeypatch)

    report, candidates = scan_transcripts()

    assert client.call_count == 0
    assert report.classification_ran is False
    assert report.classifier["used"] is False
    assert report.classifier["summary"] == DETECTION_ONLY
    assert report.classifier["model"] is None
    assert report.classifier["config_role"] is None
    assert report.candidates_detected == 12
    assert len(candidates) == 12
    assert report.classified == {"needs_review": 12}


def test_unclassified_candidates_say_the_classifier_was_not_run(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not-run is its own state, distinct from unreachable and from ambiguous."""
    _enable(monkeypatch)
    _forbid_model_calls(monkeypatch)

    scan_transcripts()

    records = load_candidates()
    assert records
    for record in records:
        assert record["classification"] == "needs_review"
        assert record["classifier"] == {
            "decided": False,
            "model": None,
            "config_role": None,
            "reason": classify_module.NOT_RUN_REASON,
        }
    assert classify_module.NOT_RUN_REASON not in (
        classify_module.UNAVAILABLE_REASON,
        classify_module.AMBIGUOUS_REASON,
    )


def test_the_three_not_decided_states_are_distinguishable_in_the_queue(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reviewer must be able to tell which action each queue needs.

    "Nobody asked" means turn classification on; "unreachable" means fix the
    endpoint; "unusable answer" means look at it by hand. One shared
    ``needs_review`` label with no reason would collapse all three.
    """
    _enable(monkeypatch)

    # 1. never asked
    _forbid_model_calls(monkeypatch)
    scan_transcripts()
    not_run = {record["classifier"]["reason"] for record in load_candidates()}

    # 2. asked, endpoint down
    monkeypatch.setattr(config.capture.transcripts, "classify", True)
    monkeypatch.setattr(
        classify_module, "get_ollama_client", lambda: _Raising(OllamaError("refused"))
    )
    queue_path().unlink()
    scan_transcripts()
    unavailable = {record["classifier"]["reason"] for record in load_candidates()}

    # 3. asked, answer unusable
    monkeypatch.setattr(
        classify_module, "get_ollama_client", lambda: _Answering("who can say")
    )
    queue_path().unlink()
    scan_transcripts()
    ambiguous = {record["classifier"]["reason"] for record in load_candidates()}

    assert not_run == {classify_module.NOT_RUN_REASON}
    assert unavailable == {classify_module.UNAVAILABLE_REASON}
    assert ambiguous == {classify_module.AMBIGUOUS_REASON}
    assert len({*not_run, *unavailable, *ambiguous}) == 3


class _Raising:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def chat_completions(self, messages, **kwargs):  # noqa: ANN001 - test double
        raise self.error


class _Answering:
    def __init__(self, answer: str) -> None:
        self.answer = answer

    def chat_completions(self, messages, **kwargs):  # noqa: ANN001 - test double
        return self.answer


def test_disabled_by_default_reads_nothing_and_writes_nothing(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off is off: the default config opens no transcript and creates no queue."""
    assert config.capture.transcripts.enabled is False
    assert config.capture.transcripts.harness_paths == {}
    _forbid_reads(monkeypatch)

    report, candidates = scan_transcripts(classify=False)

    assert report.enabled is False
    assert report.blocked_reason == "disabled"
    assert candidates == []
    assert not queue_path().exists()


def test_capture_paused_blocks_the_scan_before_the_first_read(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store-wide pause stops this source like every other capture source."""
    _enable(monkeypatch)
    update_capture_policy(capture_paused=True)
    _forbid_reads(monkeypatch)

    report, candidates = scan_transcripts(classify=False)

    assert report.blocked_reason == "capture_paused"
    assert candidates == []
    assert not queue_path().exists()


def test_excluded_project_transcripts_are_skipped(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An excluded project's sessions are skipped, and the skip is counted."""
    _enable(monkeypatch)
    update_capture_policy(excluded_projects=["tidewater-sync"])

    report, candidates = scan_transcripts(classify=False)

    scopes = {candidate.project for candidate in candidates}
    assert "tidewater-sync" not in scopes
    assert scopes == {"harbor-notes", "lantern-api"}
    assert report.skipped.get("policy_excluded_project") == 1
    assert report.transcripts_read == 2


def test_excluded_path_transcripts_are_skipped(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path exclusion covering the transcript root skips every session under it."""
    _enable(monkeypatch)
    update_capture_policy(excluded_paths=[str(FIXTURE_DIR)])

    report, candidates = scan_transcripts(classify=False)

    assert candidates == []
    assert report.transcripts_read == 0
    assert report.skipped.get("policy_excluded_path") == 3


def test_candidate_cap_logs_what_it_skipped(
    store: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A bounded scan says how much it left behind; it never truncates quietly."""
    _enable(monkeypatch)
    monkeypatch.setattr(config.capture.transcripts, "max_candidates", 2)

    with caplog.at_level(logging.WARNING, logger="palinode.corrections.scan"):
        report, candidates = scan_transcripts(classify=False)

    assert report.candidates_detected == 12
    assert len(candidates) == 2
    assert report.skipped["over_max_candidates"] == 10
    assert any(
        "skipped 10 candidate(s)" in record.getMessage() for record in caplog.records
    ), caplog.text


def test_lookback_window_drops_older_turns_and_counts_them(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lookback is a filter with a counter, not a silent horizon."""
    _enable(monkeypatch)
    monkeypatch.setattr(config.capture.transcripts, "lookback_days", 1)

    report, candidates = scan_transcripts(classify=False)

    # The fixtures are dated in 2026-03; a one-day window excludes all of them.
    assert candidates == []
    assert report.skipped["turn_outside_lookback"] > 0
    assert report.cutoff is not None


def test_queue_holds_the_bounded_span_and_no_other_transcript_text(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the matched span is written down — not the turn, not the file."""
    _enable(monkeypatch)
    scan_transcripts(classify=False)

    raw = queue_path().read_text(encoding="utf-8")
    records = load_candidates()
    assert records

    for record in records:
        assert len(record["span"]) <= MAX_SPAN_CHARS
        # Quoted fragments are cut from the span, so they add no new text.
        if record.get("rationale"):
            assert record["rationale"].lower() in record["span"].lower()
        if record.get("relation"):
            for value in (record["relation"]["replaced"], record["relation"]["replacement"]):
                assert value.lower() in record["span"].lower()
        assert set(record) <= {
            "schema_version", "candidate_id", "status", "harness", "session_id",
            "turn_index", "turn_uuid", "window_turns", "occurred_at", "detected_at",
            "project", "span", "span_hash", "grep_family", "matched_rules",
            "classification", "classifier", "rationale", "relation",
        }

    # Unmatched sentences from the same transcripts must not be anywhere in the
    # file: the queue holds spans, not turns and not neighbouring context.
    for absent in (
        "The export finished in four minutes",
        "Can you open the changelog",
        "vendor integration note",
        "harbor-export: WARNING",
    ):
        assert absent not in raw

    # Nor the transcript's own path on this machine.
    assert str(FIXTURE_DIR) not in raw


def test_rescanning_the_same_transcripts_adds_no_second_candidate(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-reading a transcript is not new evidence, and mints no witness count."""
    _enable(monkeypatch)
    first_report, first = scan_transcripts(classify=False)
    second_report, second = scan_transcripts(classify=False)

    assert first_report.candidates_added == len(first) == 12
    assert second_report.candidates_added == 0
    assert second_report.duplicates == 12
    assert len(load_candidates()) == 12

    raw = queue_path().read_text(encoding="utf-8")
    for counter in ("witness", "corroborat", "occurrences", "seen_count", "times_seen"):
        assert counter not in raw.lower()


def test_report_says_nothing_was_applied_on_every_path(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller reading JSON should not have to infer the dry run."""
    _enable(monkeypatch)
    report = corrections_report(scan=True, classify=False)

    assert report["applied"].startswith("nothing applied")
    assert report["enabled"] is True
    assert report["scan"]["applied"].startswith("nothing applied")
    assert report["count"] == len(report["candidates"]) == 12
    assert all(
        candidate["classification"] == "needs_review" for candidate in report["candidates"]
    )
    # Classification did not run, and the record says so rather than implying a
    # verdict nobody made.
    assert all(
        candidate["classifier"]["reason"] == "classification_not_run"
        for candidate in report["candidates"]
    )


def test_listing_filters_by_project_and_window(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    scan_transcripts(classify=False)

    scoped = corrections_report(project="lantern-api")
    assert scoped["count"] == 5
    assert {candidate["project"] for candidate in scoped["candidates"]} == {"lantern-api"}

    typed = corrections_report(project="project/lantern-api")
    assert typed["count"] == 5

    recent = corrections_report(since_days=1)
    assert recent["count"] == 0


def test_the_queue_is_not_a_memory_file(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Operational state under ``.palinode/`` — never markdown, never committed."""
    _enable(monkeypatch)
    scan_transcripts(classify=False)

    path = queue_path()
    assert path.parent.name == ".palinode"
    assert path.suffix == ".jsonl"
    assert not list(Path(store).glob("*.md"))
    for line in path.read_text(encoding="utf-8").splitlines():
        assert json.loads(line)["status"] == "proposed"
