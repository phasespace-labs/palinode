"""The reader's safety rules and the queue's storage contract.

The reader is pointed at a directory full of someone's private sessions, so its
job description is mostly refusals: absolute paths only, no traversal, no
symlink, read-only, and only the user's own turns count as evidence. The queue's
job is to hold what the detector found without becoming a memory file or a
second store.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from palinode.core.config import config
from palinode.corrections.queue import (
    CorrectionCandidate,
    SCHEMA_VERSION,
    append_candidates,
    load_candidates,
    queue_path,
)
from palinode.corrections.readers import (
    ClaudeCodeTranscriptReader,
    TranscriptPathError,
    reader_for_harness,
    supported_harnesses,
    validate_transcript_root,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "transcripts" / "claude_code" / "v1"


# ── path validation ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        "relative/projects",
        "",
        "   ",
        "/srv/../etc",
        "/srv/fixture\x00/projects",
    ],
)
def test_unsafe_transcript_roots_are_rejected(bad: str) -> None:
    with pytest.raises(TranscriptPathError):
        validate_transcript_root(bad)


def test_transcript_root_through_a_symlink_is_rejected(tmp_path: Path) -> None:
    """A symlinked component turns "read my transcripts" into "read anything"."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(TranscriptPathError):
        validate_transcript_root(str(link / "projects"))


def test_transcript_root_expands_home_and_stays_absolute(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    resolved = validate_transcript_root("~/sessions")
    assert resolved.is_absolute()
    assert resolved == tmp_path / "sessions"


def test_a_symlinked_transcript_file_is_skipped(tmp_path: Path) -> None:
    """The per-file check matters too: the root can be clean and a file not."""
    target = FIXTURE_DIR / "harbor-notes-export.jsonl"
    (tmp_path / "linked.jsonl").symlink_to(target)

    assert list(ClaudeCodeTranscriptReader().sessions(tmp_path)) == []


def test_reader_writes_nothing_to_the_transcript_directory(tmp_path: Path) -> None:
    """Read-only means the harness directory is byte-identical afterwards."""
    before = {
        path.name: (path.stat().st_mtime_ns, path.stat().st_size)
        for path in sorted(FIXTURE_DIR.iterdir())
    }
    sessions = list(ClaudeCodeTranscriptReader().sessions(FIXTURE_DIR))
    after = {
        path.name: (path.stat().st_mtime_ns, path.stat().st_size)
        for path in sorted(FIXTURE_DIR.iterdir())
    }
    assert sessions and before == after


# ── turn eligibility ────────────────────────────────────────────────────────


def _labels() -> dict[tuple[str, int], dict]:
    manifest = json.loads((FIXTURE_DIR / "labels.json").read_text(encoding="utf-8"))
    return {(row["session_id"], row["turn_index"]): row for row in manifest["turns"]}


def test_only_the_users_own_turns_are_evidence() -> None:
    """Tool output, summaries, injected meta and the assistant are not the user."""
    labels = _labels()
    reasons: dict[str, set[str | None]] = {}
    for session in ClaudeCodeTranscriptReader().sessions(FIXTURE_DIR):
        for turn in session.turns:
            label = labels[(turn.session_id, turn.turn_index)]["label"]
            reasons.setdefault(label, set()).add(
                None if turn.is_user_evidence else turn.ineligible_reason
            )

    assert reasons["tool_output"] == {"tool_output"}
    assert reasons["assistant_turn"] == {"not_a_user_turn"}
    assert reasons["restated_summary"] == {"compact_summary"}
    # An injected reminder is still the user's line; the detector strips the
    # region rather than the turn, which is checked in the scoring module.
    assert reasons["injected_context"] == {None}
    assert reasons["explicit_decision_change"] == {None}


def test_reader_skips_bookkeeping_lines_and_keeps_turn_order() -> None:
    session = next(
        s for s in ClaudeCodeTranscriptReader().sessions(FIXTURE_DIR)
        if s.path.name == "harbor-notes-export.jsonl"
    )
    assert [turn.turn_index for turn in session.turns] == list(range(len(session.turns)))
    assert all(turn.role in {"user", "assistant"} for turn in session.turns)
    assert session.session_id == "11111111-2222-4333-8444-555555555555"


def test_only_claude_code_is_readable_in_this_release() -> None:
    assert supported_harnesses() == ("claude-code",)
    assert reader_for_harness("codex-cli") is None


# ── the queue ───────────────────────────────────────────────────────────────


def _candidate(**overrides) -> CorrectionCandidate:
    base = dict(
        harness="claude-code",
        session_id="session-a",
        turn_index=3,
        turn_uuid="uuid-3",
        span="Use the queue worker instead.",
        span_hash="a" * 64,
        grep_family="explicit_decision_change",
        matched_rules=("use_instead",),
        classification="needs_review",
        classifier={"decided": False, "model": None, "config_role": None, "reason": "x"},
        window_turns=(1, 5),
        occurred_at="2026-03-02T09:03:00.000Z",
        detected_at="2026-03-05T00:00:00Z",
        project="harbor-notes",
    )
    base.update(overrides)
    return CorrectionCandidate(**base)


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    return tmp_path


def test_append_is_atomic_and_private(store: Path) -> None:
    append_candidates([_candidate()])
    path = queue_path()

    assert path.exists()
    assert not list(path.parent.glob("*.tmp")), "the temp file outlived the replace"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_dedupe_is_session_plus_span_and_never_a_counter(store: Path) -> None:
    """The same correction seen twice stays one candidate with no tally."""
    first = append_candidates([_candidate()])
    second = append_candidates([_candidate(turn_index=99, turn_uuid="later")])

    assert (first.added, first.duplicates) == (1, 0)
    assert (second.added, second.duplicates) == (0, 1)

    records = load_candidates()
    assert len(records) == 1
    assert records[0]["turn_index"] == 3
    assert not any("count" in key for key in records[0])


def test_the_same_span_in_another_session_is_a_separate_candidate(store: Path) -> None:
    append_candidates([_candidate()])
    result = append_candidates([_candidate(session_id="session-b")])

    assert result.added == 1
    assert len({record["candidate_id"] for record in load_candidates()}) == 2


def test_a_relation_is_stored_only_when_both_halves_are_evidenced(store: Path) -> None:
    append_candidates([
        _candidate(span_hash="b" * 64, replaced="nightly cron", replacement="the queue worker"),
        _candidate(span_hash="c" * 64, replaced="nightly cron", replacement=None),
        _candidate(span_hash="d" * 64, rationale="the worker already retries"),
    ])
    records = {record["span_hash"]: record for record in load_candidates()}

    assert records["b" * 64]["relation"] == {
        "replaced": "nightly cron",
        "replacement": "the queue worker",
        "evidence": "quoted from the span",
    }
    assert "relation" not in records["c" * 64]
    assert "rationale" not in records["c" * 64]
    assert records["d" * 64]["rationale"] == "the worker already retries"
    assert "relation" not in records["d" * 64]


def test_unreadable_rows_are_skipped_not_guessed_at(store: Path) -> None:
    append_candidates([_candidate()])
    path = queue_path()
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("{not json at all\n")
        handle.write(json.dumps({"schema_version": SCHEMA_VERSION + 1, "span": "future"}) + "\n")
        handle.write("\n")

    records = load_candidates()
    assert len(records) == 1
    assert records[0]["span_hash"] == "a" * 64


def test_a_missing_queue_is_an_empty_list_not_an_error(store: Path) -> None:
    assert load_candidates() == []
