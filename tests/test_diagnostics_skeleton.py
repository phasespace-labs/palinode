"""
Tests for the Phase 1 palinode doctor diagnostics skeleton.

Covers:
  - memory_dir_exists check: passing case (dir exists) and failing case (dir absent)
  - format_text output contains ✓ or ✗ as appropriate
  - format_json output is valid JSON with the expected schema
  - run_one(ctx, "memory_dir_exists") via the --check filter path
  - ValueError raised by run_one for unknown check names

All fixtures use real tmp_path directories.  No SQLite mocking — project standard.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from palinode.core.config import Config
from palinode.diagnostics.types import DoctorContext, CheckResult
from palinode.diagnostics.runner import run_all, run_one
from palinode.diagnostics.formatters import format_text, format_json


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctx(memory_dir: Path) -> DoctorContext:
    """Build a synthetic DoctorContext pointing at *memory_dir*."""
    cfg = Config(
        memory_dir=str(memory_dir),
        db_path=str(memory_dir / ".palinode.db"),
    )
    return DoctorContext(config=cfg)


# ---------------------------------------------------------------------------
# memory_dir_exists — passing case
# ---------------------------------------------------------------------------

class TestMemoryDirExistsPass:
    def test_passes_when_dir_exists(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()

        ctx = _ctx(memory_dir)
        result = run_one(ctx, "memory_dir_exists")

        assert result.passed is True
        assert result.name == "memory_dir_exists"
        assert result.severity == "critical"
        assert str(memory_dir.resolve()) in result.message

    def test_remediation_is_none_on_pass(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()

        ctx = _ctx(memory_dir)
        result = run_one(ctx, "memory_dir_exists")

        assert result.remediation is None

    def test_linked_issue_present_on_pass(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()

        ctx = _ctx(memory_dir)
        result = run_one(ctx, "memory_dir_exists")

        assert result.linked_issue == "#190"


# ---------------------------------------------------------------------------
# memory_dir_exists — failing case
# ---------------------------------------------------------------------------

class TestMemoryDirExistsFail:
    def test_fails_when_dir_absent(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "does-not-exist"
        # Do NOT create it.

        ctx = _ctx(memory_dir)
        result = run_one(ctx, "memory_dir_exists")

        assert result.passed is False
        assert result.severity == "critical"

    def test_remediation_provided_on_fail(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "does-not-exist"

        ctx = _ctx(memory_dir)
        result = run_one(ctx, "memory_dir_exists")

        assert result.remediation is not None
        assert len(result.remediation) > 0

    def test_message_contains_path_on_fail(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "does-not-exist"

        ctx = _ctx(memory_dir)
        result = run_one(ctx, "memory_dir_exists")

        assert str(memory_dir.resolve()) in result.message


# ---------------------------------------------------------------------------
# format_text
# ---------------------------------------------------------------------------

class TestFormatText:
    def test_contains_checkmark_on_pass(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()

        ctx = _ctx(memory_dir)
        results = run_all(ctx)

        output = format_text(results)
        assert "✓" in output

    def test_contains_cross_on_fail(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "does-not-exist"

        ctx = _ctx(memory_dir)
        results = run_all(ctx)

        output = format_text(results)
        assert "✗" in output

    def test_verbose_includes_remediation(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "does-not-exist"

        ctx = _ctx(memory_dir)
        results = run_all(ctx)

        output = format_text(results, verbose=True)
        # Remediation text should be present
        assert "mkdir" in output

    def test_non_verbose_fail_includes_remediation(self, tmp_path: Path) -> None:
        """Remediation should appear even without --verbose when check fails."""
        memory_dir = tmp_path / "does-not-exist"

        ctx = _ctx(memory_dir)
        results = run_all(ctx)

        output = format_text(results, verbose=False)
        assert "mkdir" in output

    def test_non_verbose_pass_no_remediation(self, tmp_path: Path) -> None:
        """No remediation block when check passes and verbose is off."""
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()

        ctx = _ctx(memory_dir)
        results = run_all(ctx)

        output = format_text(results, verbose=False)
        assert "mkdir" not in output


# ---------------------------------------------------------------------------
# format_json
# ---------------------------------------------------------------------------

class TestFormatJson:
    def test_output_is_valid_json(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()

        ctx = _ctx(memory_dir)
        results = run_all(ctx)

        raw = format_json(results)
        parsed = json.loads(raw)
        assert isinstance(parsed, list)

    def test_json_schema_fields_present(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()

        ctx = _ctx(memory_dir)
        results = run_all(ctx)

        parsed = json.loads(format_json(results))
        assert len(parsed) >= 1
        entry = parsed[0]

        for field in ("name", "severity", "passed", "message", "remediation", "linked_issue"):
            assert field in entry, f"Missing field: {field}"

    def test_json_passed_is_bool(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()

        ctx = _ctx(memory_dir)
        results = run_all(ctx)

        parsed = json.loads(format_json(results))
        assert isinstance(parsed[0]["passed"], bool)

    def test_json_severity_value(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()

        ctx = _ctx(memory_dir)
        results = run_all(ctx)

        parsed = json.loads(format_json(results))
        assert parsed[0]["severity"] in ("info", "warn", "error", "critical")


# ---------------------------------------------------------------------------
# run_one filter
# ---------------------------------------------------------------------------

class TestRunOne:
    def test_run_one_returns_single_result(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()

        ctx = _ctx(memory_dir)
        result = run_one(ctx, "memory_dir_exists")

        assert isinstance(result, CheckResult)
        assert result.name == "memory_dir_exists"

    def test_run_one_unknown_name_raises(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()

        ctx = _ctx(memory_dir)
        with pytest.raises(ValueError, match="No check named"):
            run_one(ctx, "no_such_check")

    def test_run_one_result_matches_run_all(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()

        ctx = _ctx(memory_dir)
        single = run_one(ctx, "memory_dir_exists")
        all_results = run_all(ctx)

        matched = [r for r in all_results if r.name == "memory_dir_exists"]
        assert len(matched) == 1
        assert single.passed == matched[0].passed
        assert single.message == matched[0].message
