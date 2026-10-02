"""Tests for retrieval-event instrumentation — the retrieval-event instrumentation.

Covers:
- RetrievalLogger emits JSONL events on record() / record_search_results() / record_file_read()
- mode field is set correctly
- disable flag (config enabled=False) suppresses all writes
- PALINODE_INSTRUMENTATION_DISABLED=1 env var suppresses all writes
- palinode retrieval-stats CLI command returns sensible output
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from palinode.core.retrieval_log import RetrievalEvent, RetrievalLogger
from palinode.cli.retrieval_stats import retrieval_stats


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    """A temporary memory directory with the .audit sub-dir ready."""
    (tmp_path / ".audit").mkdir()
    return tmp_path


@pytest.fixture
def rl(memory_dir: Path) -> RetrievalLogger:
    """A RetrievalLogger writing to tmp memory_dir."""
    return RetrievalLogger(str(memory_dir), enabled=True)


def _read_log(rl: RetrievalLogger) -> list[dict]:
    path = rl.log_path
    assert path is not None
    if not path.exists():
        return []
    entries = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


# ── RetrievalEvent schema ─────────────────────────────────────────────────────


class TestRetrievalEventSchema:
    def test_all_fields_present(self):
        evt = RetrievalEvent(
            timestamp="2026-04-28T00:00:00+00:00",
            file_path="people/alice.md",
            chunk_id="chunk-001",
            mode="explicit",
            source="palinode_search",
            query="alice contact",
            rank=0,
            score=0.92,
            session_id=None,
        )
        assert evt.mode == "explicit"
        assert evt.file_path == "people/alice.md"

    def test_passive_mode_accepted(self):
        evt = RetrievalEvent(
            timestamp="2026-04-28T00:00:00+00:00",
            file_path="projects/foo.md",
            chunk_id=None,
            mode="passive",
            source="auto_inject",
            query=None,
            rank=None,
            score=None,
            session_id=None,
        )
        assert evt.mode == "passive"


# ── RetrievalLogger — basic write behaviour ───────────────────────────────────


class TestRetrievalLogger:
    def test_creates_log_directory(self, memory_dir: Path):
        audit_dir = memory_dir / ".audit"
        audit_dir.rmdir()  # remove it so we can test auto-creation
        _rl = RetrievalLogger(str(memory_dir), enabled=True)
        assert audit_dir.is_dir()

    def test_record_writes_entry(self, rl: RetrievalLogger, memory_dir: Path):
        rl.record(RetrievalEvent(
            timestamp="2026-04-28T10:00:00+00:00",
            file_path="decisions/arch.md",
            chunk_id="chunk-123",
            mode="explicit",
            source="palinode_search",
            query="architecture",
            rank=0,
            score=0.85,
            session_id=None,
        ))
        entries = _read_log(rl)
        assert len(entries) == 1
        e = entries[0]
        assert e["file_path"] == "decisions/arch.md"
        assert e["mode"] == "explicit"
        assert e["source"] == "palinode_search"
        assert e["score"] == 0.85
        assert e["rank"] == 0
        assert e["query"] == "architecture"

    def test_record_search_results_emits_one_per_result(self, rl: RetrievalLogger):
        results = [
            {"file_path": "people/alice.md", "section_id": "s1", "score": 0.9},
            {"file_path": "projects/palinode.md", "section_id": "s2", "score": 0.8},
        ]
        rl.record_search_results(
            results,
            query="palinode alice",
            source="palinode_search",
            mode="explicit",
        )
        entries = _read_log(rl)
        assert len(entries) == 2
        assert entries[0]["file_path"] == "people/alice.md"
        assert entries[0]["rank"] == 0
        assert entries[1]["file_path"] == "projects/palinode.md"
        assert entries[1]["rank"] == 1

    def test_empty_search_writes_one_call_level_row(self, rl: RetrievalLogger):
        rl.record_search_results(
            [],
            query="a question the store cannot answer",
            source="palinode_search",
            mode="explicit",
            session_id="s-1",
        )
        entries = _read_log(rl)
        assert len(entries) == 1
        e = entries[0]
        assert e["file_path"] == ""
        assert e["chunk_id"] is None and e["rank"] is None and e["score"] is None
        assert e["disposition"] == "none_delivered"
        assert e["query"] == "a question the store cannot answer"
        assert e["source"] == "palinode_search"
        assert e["session_id"] == "s-1"

    def test_record_file_read_emits_entry(self, rl: RetrievalLogger):
        rl.record_file_read("people/bob.md", source="palinode_read", mode="explicit")
        entries = _read_log(rl)
        assert len(entries) == 1
        e = entries[0]
        assert e["file_path"] == "people/bob.md"
        assert e["source"] == "palinode_read"
        assert e["mode"] == "explicit"
        assert e["chunk_id"] is None
        assert e["query"] is None
        assert e["rank"] is None
        assert e["score"] is None

    def test_mode_explicit_for_tool_calls(self, rl: RetrievalLogger):
        rl.record_file_read("decisions/foo.md", source="palinode_history", mode="explicit")
        entries = _read_log(rl)
        assert entries[0]["mode"] == "explicit"

    def test_mode_passive_for_auto_inject(self, rl: RetrievalLogger):
        rl.record_file_read("projects/core.md", source="auto_inject", mode="passive")
        entries = _read_log(rl)
        assert entries[0]["mode"] == "passive"

    def test_multiple_entries_appended(self, rl: RetrievalLogger):
        for i in range(5):
            rl.record_file_read(f"people/person{i}.md", source="palinode_read", mode="explicit")
        entries = _read_log(rl)
        assert len(entries) == 5

    def test_jsonl_each_line_valid_json(self, rl: RetrievalLogger):
        rl.record_file_read("foo.md", source="palinode_read", mode="explicit")
        rl.record_file_read("bar.md", source="palinode_read", mode="explicit")
        with open(rl.log_path, encoding="utf-8") as fh:
            lines = fh.readlines()
        assert len(lines) == 2
        for line in lines:
            json.loads(line)  # must not raise

    def test_timestamp_is_iso8601(self, rl: RetrievalLogger):
        rl.record_file_read("foo.md", source="palinode_read", mode="explicit")
        entries = _read_log(rl)
        from datetime import datetime
        dt = datetime.fromisoformat(entries[0]["timestamp"])
        assert dt.year >= 2026


# ── Disable flag — config ─────────────────────────────────────────────────────


class TestDisableFlag:
    def test_disabled_by_config_no_writes(self, memory_dir: Path):
        rl = RetrievalLogger(str(memory_dir), enabled=False)
        assert not rl.enabled
        assert rl.log_path is None
        rl.record_file_read("foo.md", source="palinode_read", mode="explicit")
        # No log file created
        assert not (memory_dir / ".audit" / "retrievals.jsonl").exists()

    def test_disabled_no_writes_record(self, memory_dir: Path):
        rl = RetrievalLogger(str(memory_dir), enabled=False)
        rl.record(RetrievalEvent(
            timestamp="2026-04-28T00:00:00+00:00",
            file_path="foo.md",
            chunk_id=None,
            mode="explicit",
            source="palinode_search",
            query="test",
            rank=0,
            score=0.9,
            session_id=None,
        ))
        assert not (memory_dir / ".audit" / "retrievals.jsonl").exists()

    def test_disabled_no_writes_search_results(self, memory_dir: Path):
        rl = RetrievalLogger(str(memory_dir), enabled=False)
        rl.record_search_results(
            [{"file_path": "foo.md", "score": 0.9}],
            query="test",
            source="palinode_search",
            mode="explicit",
        )
        assert not (memory_dir / ".audit" / "retrievals.jsonl").exists()

    def test_disabled_by_env_var(self, memory_dir: Path, monkeypatch):
        monkeypatch.setenv("PALINODE_INSTRUMENTATION_DISABLED", "1")
        rl = RetrievalLogger(str(memory_dir), enabled=True)
        assert not rl.enabled
        rl.record_file_read("foo.md", source="palinode_read", mode="explicit")
        assert not (memory_dir / ".audit" / "retrievals.jsonl").exists()

    def test_env_var_true_string(self, memory_dir: Path, monkeypatch):
        monkeypatch.setenv("PALINODE_INSTRUMENTATION_DISABLED", "true")
        rl = RetrievalLogger(str(memory_dir), enabled=True)
        assert not rl.enabled

    def test_env_var_absent_enabled(self, memory_dir: Path, monkeypatch):
        monkeypatch.delenv("PALINODE_INSTRUMENTATION_DISABLED", raising=False)
        rl = RetrievalLogger(str(memory_dir), enabled=True)
        assert rl.enabled


# ── CLI retrieval-stats command ───────────────────────────────────────────────


class TestRetrievalStatsCLI:
    def _make_log(self, memory_dir: Path, entries: list[dict]) -> None:
        log_path = memory_dir / ".audit" / "retrievals.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")

    def _sample_events(self) -> list[dict]:
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        events = []
        for i in range(3):
            events.append({
                "timestamp": (now - timedelta(days=1)).isoformat(),
                "file_path": "people/alice.md",
                "chunk_id": f"chunk-{i}",
                "mode": "explicit",
                "source": "palinode_search",
                "query": "alice",
                "rank": i,
                "score": 0.9 - i * 0.1,
                "session_id": None,
            })
        events.append({
            "timestamp": (now - timedelta(days=2)).isoformat(),
            "file_path": "projects/foo.md",
            "chunk_id": None,
            "mode": "explicit",
            "source": "palinode_read",
            "query": None,
            "rank": None,
            "score": None,
            "session_id": None,
        })
        return events

    def test_no_log_shows_helpful_message(self, memory_dir: Path, monkeypatch):
        monkeypatch.setenv("PALINODE_DIR", str(memory_dir))
        # Re-patch config.memory_dir on the module level since config is a singleton
        import palinode.core.config as cfg_mod
        original_dir = cfg_mod.config.memory_dir
        cfg_mod.config.memory_dir = str(memory_dir)
        try:
            runner = CliRunner()
            result = runner.invoke(retrieval_stats, ["--days", "7"])
            assert result.exit_code == 0
            assert "No retrieval log found" in result.output
        finally:
            cfg_mod.config.memory_dir = original_dir

    def test_text_output_shows_totals(self, memory_dir: Path):
        self._make_log(memory_dir, self._sample_events())
        import palinode.core.config as cfg_mod
        original_dir = cfg_mod.config.memory_dir
        cfg_mod.config.memory_dir = str(memory_dir)
        try:
            runner = CliRunner()
            result = runner.invoke(retrieval_stats, ["--days", "7"])
            assert result.exit_code == 0
            assert "Total events" in result.output
            assert "Explicit" in result.output
        finally:
            cfg_mod.config.memory_dir = original_dir

    def test_json_output_has_expected_keys(self, memory_dir: Path):
        self._make_log(memory_dir, self._sample_events())
        import palinode.core.config as cfg_mod
        original_dir = cfg_mod.config.memory_dir
        cfg_mod.config.memory_dir = str(memory_dir)
        try:
            runner = CliRunner()
            result = runner.invoke(retrieval_stats, ["--days", "7", "--format", "json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert "total_events" in data
            assert "explicit" in data
            assert "passive" in data
            assert "top_files" in data
            assert "distribution" in data
            assert "age_days" in data
        finally:
            cfg_mod.config.memory_dir = original_dir

    def test_empty_search_rows_are_counted_apart_from_retrievals(self, memory_dir: Path):
        from datetime import datetime, timezone
        events = self._sample_events()
        events.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "file_path": "",
            "chunk_id": None,
            "mode": "explicit",
            "source": "palinode_search",
            "query": "nothing matches this",
            "rank": None,
            "score": None,
            "session_id": None,
            "disposition": "none_delivered",
        })
        self._make_log(memory_dir, events)
        import palinode.core.config as cfg_mod
        original_dir = cfg_mod.config.memory_dir
        cfg_mod.config.memory_dir = str(memory_dir)
        try:
            runner = CliRunner()
            result = runner.invoke(retrieval_stats, ["--days", "7", "--format", "json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["total_events"] == 4
            assert data["explicit"] == 4
            assert data["empty_searches"] == 1
            assert data["unique_files_retrieved"] == 2
        finally:
            cfg_mod.config.memory_dir = original_dir

    def test_explicit_vs_passive_counts(self, memory_dir: Path):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        events = [
            {"timestamp": (now - timedelta(hours=1)).isoformat(),
             "file_path": "foo.md", "chunk_id": None, "mode": "explicit",
             "source": "palinode_read", "query": None, "rank": None, "score": None, "session_id": None},
            {"timestamp": (now - timedelta(hours=2)).isoformat(),
             "file_path": "bar.md", "chunk_id": None, "mode": "passive",
             "source": "auto_inject", "query": None, "rank": None, "score": None, "session_id": None},
        ]
        self._make_log(memory_dir, events)
        import palinode.core.config as cfg_mod
        original_dir = cfg_mod.config.memory_dir
        cfg_mod.config.memory_dir = str(memory_dir)
        try:
            runner = CliRunner()
            result = runner.invoke(retrieval_stats, ["--days", "7", "--format", "json"])
            data = json.loads(result.output)
            assert data["explicit"] == 1
            assert data["passive"] == 1
            assert data["total_events"] == 2
        finally:
            cfg_mod.config.memory_dir = original_dir

    def test_no_log_json_output(self, memory_dir: Path):
        import palinode.core.config as cfg_mod
        original_dir = cfg_mod.config.memory_dir
        cfg_mod.config.memory_dir = str(memory_dir)
        try:
            runner = CliRunner()
            result = runner.invoke(retrieval_stats, ["--format", "json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert "error" in data
        finally:
            cfg_mod.config.memory_dir = original_dir
