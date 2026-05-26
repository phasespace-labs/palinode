"""Tests for `palinode mcp-smoke` CLI subcommand (#345, parent #342).

Validates:
  - --list exits 0 and lists every Tier 1+2 harness
  - unknown harness exits non-zero with a useful message
  - Tier 3 harness is refused with an explanatory message
  - --json output parses as valid JSON
  - --record writes a valid JSONL line
  - default (no flags) prints the runbook text
"""
from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner

from palinode.cli import main
from palinode.cli.mcp_smoke import _HARNESSES, _TIER3_NAMES, _HARNESS_MAP


@pytest.fixture()
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# --list
# ---------------------------------------------------------------------------

class TestListHarnesses:
    def test_list_exits_zero(self, runner):
        result = runner.invoke(main, ["mcp-smoke", "--list"])
        assert result.exit_code == 0

    def test_list_includes_all_tier1_and_tier2(self, runner):
        result = runner.invoke(main, ["mcp-smoke", "--list"])
        output = result.output
        for harness_id, _, tier in _HARNESSES:
            assert tier in (1, 2)
            assert harness_id in output, (
                f"Harness '{harness_id}' missing from --list output"
            )

    def test_list_json_when_piped(self, runner):
        """When not a TTY (CliRunner is not a TTY), output should be JSON."""
        result = runner.invoke(main, ["mcp-smoke", "--list"])
        # CliRunner is not a TTY, so output should be parseable JSON
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == len(_HARNESSES)
        ids = {item["harness"] for item in data}
        for harness_id, _, _ in _HARNESSES:
            assert harness_id in ids


# ---------------------------------------------------------------------------
# Unknown harness
# ---------------------------------------------------------------------------

class TestUnknownHarness:
    def test_unknown_exits_nonzero(self, runner):
        result = runner.invoke(main, ["mcp-smoke", "nonexistent-ide"])
        assert result.exit_code != 0

    def test_unknown_shows_useful_message(self, runner):
        result = runner.invoke(main, ["mcp-smoke", "nonexistent-ide"])
        combined = result.output.lower()
        assert "unknown harness" in combined


# ---------------------------------------------------------------------------
# Tier 3 refusal
# ---------------------------------------------------------------------------

class TestTier3Refusal:
    @pytest.mark.parametrize("harness", sorted(_TIER3_NAMES))
    def test_tier3_exits_nonzero(self, runner, harness):
        result = runner.invoke(main, ["mcp-smoke", harness])
        assert result.exit_code != 0

    @pytest.mark.parametrize("harness", sorted(_TIER3_NAMES))
    def test_tier3_shows_explanatory_message(self, runner, harness):
        result = runner.invoke(main, ["mcp-smoke", harness])
        combined = result.output.lower()
        assert "tier 3" in combined
        assert "not yet supported" in combined or "future" in combined


# ---------------------------------------------------------------------------
# --json
# ---------------------------------------------------------------------------

class TestJsonOutput:
    @pytest.mark.parametrize("harness", ["claude-code", "cursor", "zed"])
    def test_json_parses(self, runner, harness):
        result = runner.invoke(main, ["mcp-smoke", harness, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["harness"] == harness
        assert "tier" in data
        assert "calls" in data
        assert "expected" in data
        assert isinstance(data["calls"], list)
        assert len(data["calls"]) == 5

    def test_json_tier_matches_registry(self, runner):
        for harness_id, (_, tier) in _HARNESS_MAP.items():
            result = runner.invoke(main, ["mcp-smoke", harness_id, "--json"])
            data = json.loads(result.output)
            assert data["tier"] == tier


# ---------------------------------------------------------------------------
# --record
# ---------------------------------------------------------------------------

class TestRecord:
    def test_record_writes_jsonl_line(self, runner, tmp_path, monkeypatch):
        monkeypatch.setenv("PALINODE_DIR", str(tmp_path))
        # Also monkeypatch config.memory_dir so _smoke_log_path resolves
        from palinode.core.config import config as _cfg
        monkeypatch.setattr(_cfg, "memory_dir", str(tmp_path))

        result = runner.invoke(main, [
            "mcp-smoke", "claude-code", "--record",
            "--date", "2026-05-08", "--operator", "test-agent",
        ])
        assert result.exit_code == 0

        log_path = tmp_path / ".palinode" / "harness-smoke-runs.jsonl"
        assert log_path.exists()

        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 1

        entry = json.loads(lines[0])
        assert entry["harness"] == "claude-code"
        assert entry["tier"] == 1
        assert entry["date"] == "2026-05-08"
        assert entry["operator"] == "test-agent"
        assert entry["passed"] is True

    def test_record_appends_not_overwrites(self, runner, tmp_path, monkeypatch):
        monkeypatch.setenv("PALINODE_DIR", str(tmp_path))
        from palinode.core.config import config as _cfg
        monkeypatch.setattr(_cfg, "memory_dir", str(tmp_path))

        runner.invoke(main, ["mcp-smoke", "claude-code", "--record", "--date", "2026-05-01"])
        runner.invoke(main, ["mcp-smoke", "cursor", "--record", "--date", "2026-05-02"])

        log_path = tmp_path / ".palinode" / "harness-smoke-runs.jsonl"
        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 2

        first = json.loads(lines[0])
        second = json.loads(lines[1])
        assert first["harness"] == "claude-code"
        assert second["harness"] == "cursor"


# ---------------------------------------------------------------------------
# Default mode (runbook text)
# ---------------------------------------------------------------------------

class TestRunbook:
    @pytest.mark.parametrize("harness", ["claude-code", "codex", "cursor"])
    def test_runbook_prints_header(self, runner, harness):
        result = runner.invoke(main, ["mcp-smoke", harness])
        assert result.exit_code == 0
        assert "Smoke checklist" in result.output
        assert harness in result.output

    def test_runbook_contains_all_five_calls(self, runner):
        result = runner.invoke(main, ["mcp-smoke", "claude-code"])
        assert "palinode_status" in result.output
        assert "palinode_search" in result.output
        assert "palinode_save" in result.output
        assert "palinode_list" in result.output
        assert "palinode_read" in result.output

    def test_runbook_contains_record_reminder(self, runner):
        result = runner.invoke(main, ["mcp-smoke", "claude-code"])
        assert "--record" in result.output


# ---------------------------------------------------------------------------
# No argument
# ---------------------------------------------------------------------------

class TestNoArgument:
    def test_no_arg_no_list_exits_nonzero(self, runner):
        result = runner.invoke(main, ["mcp-smoke"])
        assert result.exit_code != 0
