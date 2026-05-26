"""#387 regression: runner must log and count YAML parse failures instead of silently skipping.

Previously, both ``_collect_daily_notes`` (line ~98) and
``_get_decisions_for_project`` (line ~66) used bare ``except Exception: pass``
/ ``continue`` for YAML parse errors. A corrupt frontmatter block silently
dropped the file from consolidation with no operator signal.

The fix:
  1. Log WARNING with file path, parse exception, and recovery hint
     ("run `palinode lint`").
  2. ``_collect_daily_notes`` returns (notes, skipped_count) so callers
     can surface the count in the consolidation run summary.
  3. ``run_consolidation`` and ``run_nightly`` include ``yaml_parse_errors``
     in the return dict when non-zero.
  4. Body text is still collected even when frontmatter fails — better than
     silently discarding the note entirely.

Tests use real file I/O in tmp_path; no mocking of the DB (per CLAUDE.md),
no mocking of file reads (real files are more reliable for frontmatter logic).
"""
from __future__ import annotations

import logging
import os

import pytest

from palinode.consolidation.runner import (
    _collect_daily_notes,
    _get_decisions_for_project,
)
from palinode.core.config import config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def memory_dir(tmp_path, monkeypatch):
    """Isolated memory_dir in tmp_path. Creates standard subdirs."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    (tmp_path / "daily").mkdir()
    (tmp_path / "decisions").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# _collect_daily_notes: corrupt frontmatter — warn, skip count, keep body
# ---------------------------------------------------------------------------


class TestCollectDailyNotesYamlParseWarning:

    def test_corrupt_frontmatter_logs_warning(self, memory_dir, caplog):
        """A daily note with invalid YAML frontmatter must emit a WARNING."""
        daily_dir = memory_dir / "daily"
        bad_file = daily_dir / "2026-05-24.md"
        bad_file.write_text(
            "---\n"
            "id: [unclosed bracket\n"  # invalid YAML
            "---\n\n"
            "Body text that should still be collected.\n"
        )

        with caplog.at_level(logging.WARNING, logger="palinode.consolidation"):
            notes, skipped = _collect_daily_notes(lookback_days=30)

        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning_records, (
            "_collect_daily_notes must emit WARNING when YAML parse fails (#387)"
        )
        combined = " ".join(r.getMessage() for r in warning_records)
        assert "2026-05-24.md" in combined or "yaml" in combined.lower() or "parse" in combined.lower(), (
            f"Warning must mention the bad file or 'parse'. Got: {combined!r}"
        )
        assert "palinode lint" in combined, (
            f"Warning must include recovery hint 'palinode lint'. Got: {combined!r}"
        )

    def test_corrupt_frontmatter_increments_skip_count(self, memory_dir, caplog):
        """Each file with unparseable YAML increments the returned skip count."""
        daily_dir = memory_dir / "daily"
        (daily_dir / "2026-05-23.md").write_text(
            "---\ncorrupt: [bad\n---\n\nBody A.\n"
        )
        (daily_dir / "2026-05-22.md").write_text(
            "---\nid: good\n---\n\nBody B.\n"
        )

        with caplog.at_level(logging.WARNING, logger="palinode.consolidation"):
            notes, skipped = _collect_daily_notes(lookback_days=30)

        assert skipped == 1, (
            f"Exactly 1 file had bad YAML — skip count should be 1, got {skipped} (#387)"
        )

    def test_two_corrupt_files_count_two(self, memory_dir, caplog):
        """Two bad files → skip count 2."""
        daily_dir = memory_dir / "daily"
        (daily_dir / "2026-05-23.md").write_text("---\nbad: [open\n---\n\nBody A.\n")
        (daily_dir / "2026-05-22.md").write_text("---\nalso: {bad\n---\n\nBody B.\n")

        with caplog.at_level(logging.WARNING, logger="palinode.consolidation"):
            _notes, skipped = _collect_daily_notes(lookback_days=30)

        assert skipped == 2, (
            f"Two bad files → skip count must be 2, got {skipped} (#387)"
        )

    def test_body_text_still_collected_after_parse_failure(self, memory_dir, caplog):
        """Body content must be collected even when frontmatter is invalid.

        A corrupt frontmatter block should not cause the entire note to be
        dropped — the body text may still contain useful project/person refs.
        """
        daily_dir = memory_dir / "daily"
        (daily_dir / "2026-05-24.md").write_text(
            "---\nbad: [frontmatter\n---\n\n"
            "This note mentions project/palinode and has useful content.\n"
        )

        with caplog.at_level(logging.WARNING, logger="palinode.consolidation"):
            notes, skipped = _collect_daily_notes(lookback_days=30)

        assert len(notes) == 1, (
            "Note with bad YAML frontmatter must still produce a notes entry "
            "(body text collected) (#387)"
        )
        assert "project/palinode" in notes[0]["mentions"] or "useful content" in notes[0]["content"], (
            "Body text must be present in the collected note even with bad frontmatter"
        )

    def test_good_file_returns_zero_skip_count(self, memory_dir):
        """A well-formed file produces skip count 0."""
        daily_dir = memory_dir / "daily"
        (daily_dir / "2026-05-24.md").write_text(
            "---\nid: good-note\ncategory: daily\n---\n\nGood body.\n"
        )

        notes, skipped = _collect_daily_notes(lookback_days=30)

        assert skipped == 0
        assert len(notes) == 1

    def test_empty_daily_dir_returns_zero_skip_count(self, memory_dir):
        """Empty directory → ([], 0)."""
        notes, skipped = _collect_daily_notes(lookback_days=30)
        assert notes == []
        assert skipped == 0


# ---------------------------------------------------------------------------
# _get_decisions_for_project: corrupt frontmatter — warn and continue
# ---------------------------------------------------------------------------


class TestGetDecisionsYamlParseWarning:

    def test_corrupt_decision_frontmatter_logs_warning(self, memory_dir, caplog):
        """A decision file with invalid YAML must emit a WARNING (#387)."""
        decisions_dir = memory_dir / "decisions"
        bad_file = decisions_dir / "decision-bad.md"
        bad_file.write_text(
            "---\n"
            "id: [unclosed\n"  # invalid YAML
            "entities:\n  - project/testproject\n"
            "---\n\n"
            "Decision body.\n"
        )

        with caplog.at_level(logging.WARNING, logger="palinode.consolidation"):
            result = _get_decisions_for_project("testproject")

        # Bad file is skipped — not in results
        assert result == [], (
            "Corrupt decision file must be skipped, not added to results"
        )

        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning_records, (
            "_get_decisions_for_project must emit WARNING on YAML parse failure (#387)"
        )
        combined = " ".join(r.getMessage() for r in warning_records)
        assert "decision-bad.md" in combined or "parse" in combined.lower(), (
            f"Warning must mention the bad file or 'parse'. Got: {combined!r}"
        )
        assert "palinode lint" in combined, (
            f"Warning must include recovery hint 'palinode lint'. Got: {combined!r}"
        )

    def test_good_decision_file_returned_despite_bad_peer(self, memory_dir, caplog):
        """A valid decision file must be returned even when a peer file has bad YAML."""
        decisions_dir = memory_dir / "decisions"
        (decisions_dir / "decision-bad.md").write_text(
            "---\nbad: [open\n---\n\nBody.\n"
        )
        (decisions_dir / "decision-good.md").write_text(
            "---\n"
            "id: decision-good\n"
            "name: Good Decision\n"
            "entities:\n  - project/testproject\n"
            "status: active\n"
            "---\n\n"
            "Good decision body.\n"
        )

        with caplog.at_level(logging.WARNING, logger="palinode.consolidation"):
            result = _get_decisions_for_project("testproject")

        assert len(result) == 1, (
            "Valid decision must still be returned when peer file has bad YAML"
        )
        assert result[0]["id"] == "decision-good"
