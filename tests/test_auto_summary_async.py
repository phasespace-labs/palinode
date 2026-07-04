"""Tests for async auto_summary — issue #403.

Covers:
- /save no longer calls _generate_summary inline (no LLM call in the hot path).
- /save returns summary_pending=True for eligible files (core=true, no summary,
  content >= min_content_chars) and omits it otherwise.
- /generate-summaries populates _auto_summary_state (last_run_at, count, errors,
  duration_ms) on every run.
- /status surfaces an "auto_summary" block.
- /health/auto-summary returns "ok" / "degraded" / "down" per the documented
  decision tree and is auth-exempt.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

import palinode.api.server as _server_mod
from palinode.api.server import app
from palinode.core.config import config


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    # Reset auto_summary state to a known shape per-test.
    _server_mod._auto_summary_state.update({
        "last_run_at": None,
        "last_run_duration_ms": None,
        "last_run_count": 0,
        "last_run_errors": 0,
        "last_error": None,
        "total_runs": 0,
        "total_errors": 0,
    })
    from palinode.api import server as srv
    srv._rate_counters.clear()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    srv._rate_counters.clear()


def _patch_embed():
    return patch("palinode.core.embedder.embed", return_value=[0.1] * 1024)


def _patch_scan():
    return patch("palinode.core.store.scan_memory_content", return_value=(True, "OK"))


def _patch_desc_success(text: str = "A clear description."):
    return patch("palinode.api.server._generate_description", return_value=text)


# ---------------------------------------------------------------------------
# /save no longer calls _generate_summary inline
# ---------------------------------------------------------------------------


class TestSaveDoesNotCallSummary:

    def test_save_does_not_invoke_generate_summary(self, client):
        """Eligible save (core=true, no summary, big enough content) must NOT
        call _generate_summary — that work has moved to the watcher path."""
        big_content = "x" * (config.auto_summary.min_content_chars + 50)
        with _patch_scan(), _patch_embed(), _patch_desc_success(), \
                patch("palinode.api.server._generate_summary") as mock_summary:
            res = client.post(
                "/save",
                json={"content": big_content, "type": "Decision",
                      "slug": "no-inline-sum", "core": True},
            )
        assert res.status_code == 200, res.text
        mock_summary.assert_not_called()

    def test_save_returns_summary_pending_true_for_eligible(self, client):
        big_content = "x" * (config.auto_summary.min_content_chars + 50)
        with _patch_scan(), _patch_embed(), _patch_desc_success():
            res = client.post(
                "/save",
                json={"content": big_content, "type": "Decision",
                      "slug": "pending-true", "core": True},
            )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body.get("summary_pending") is True, body

    def test_save_omits_summary_pending_when_not_core(self, client):
        big_content = "x" * (config.auto_summary.min_content_chars + 50)
        with _patch_scan(), _patch_embed(), _patch_desc_success():
            res = client.post(
                "/save",
                json={"content": big_content, "type": "Note",
                      "slug": "not-core", "core": False},
            )
        assert res.status_code == 200, res.text
        body = res.json()
        assert "summary_pending" not in body, body

    def test_save_omits_summary_pending_when_content_too_short(self, client):
        with _patch_scan(), _patch_embed(), _patch_desc_success():
            res = client.post(
                "/save",
                json={"content": "too short", "type": "Decision",
                      "slug": "short", "core": True},
            )
        assert res.status_code == 200, res.text
        body = res.json()
        assert "summary_pending" not in body, body


# ---------------------------------------------------------------------------
# /generate-summaries populates _auto_summary_state
# ---------------------------------------------------------------------------


class TestGenerateSummariesState:

    def test_state_populated_on_run(self, client, tmp_path):
        # Empty memory dir → 0 summaries, 0 errors, but state still updates.
        with _patch_scan(), _patch_embed():
            res = client.post("/generate-summaries")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["summaries_generated"] == 0
        assert body["errors"] == 0
        assert "duration_ms" in body

        state = _server_mod._auto_summary_state
        assert state["last_run_at"] is not None
        assert state["last_run_duration_ms"] is not None
        assert state["last_run_count"] == 0
        assert state["last_run_errors"] == 0
        assert state["total_runs"] == 1

    def test_state_counts_errors_without_raising(self, client, tmp_path):
        # Plant a core file missing summary; force _generate_summary to fail.
        memory_dir = tmp_path
        sub = memory_dir / "decisions"
        sub.mkdir()
        fp = sub / "needs-summary.md"
        fp.write_text(
            "---\nid: needs-summary\ncore: true\ntype: Decision\n---\n"
            "Body content here.\n"
        )

        def _explode(_content):
            raise RuntimeError("simulated LLM failure")

        with patch("palinode.api.server._generate_summary", side_effect=_explode):
            res = client.post("/generate-summaries")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["summaries_generated"] == 0
        assert body["errors"] == 1
        state = _server_mod._auto_summary_state
        assert state["last_run_errors"] == 1
        assert state["last_error"] is not None
        assert "RuntimeError" in state["last_error"]


# ---------------------------------------------------------------------------
# /generate-summaries also backfills descriptions
# ---------------------------------------------------------------------------


class TestGenerateDescriptionsBackfill:
    """#405: the /generate-summaries walk fills missing descriptions too —
    the watcher's description-fill route depends on this. Descriptions are not
    core-gated; every file missing one gets it."""

    def test_backfill_injects_missing_description(self, client, tmp_path):
        sub = tmp_path / "insights"
        sub.mkdir()
        fp = sub / "needs-desc.md"
        # Non-core so the summary path is a no-op — isolates description backfill.
        fp.write_text(
            "---\nid: needs-desc\ntype: Insight\n---\nBody content here.\n"
        )
        with patch("palinode.api.server._generate_description",
                   return_value="A generated description."):
            res = client.post("/generate-summaries")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["descriptions_generated"] == 1, body
        assert body["description_errors"] == 0, body
        # Summary path untouched (file is not core).
        assert body["summaries_generated"] == 0, body

        text = fp.read_text()
        assert 'description: "A generated description."' in text

        state = _server_mod._auto_summary_state
        assert state["last_run_descriptions"] == 1
        assert state["last_run_description_errors"] == 0

    def test_backfill_skips_file_with_existing_description(self, client, tmp_path):
        sub = tmp_path / "insights"
        sub.mkdir()
        fp = sub / "has-desc.md"
        fp.write_text(
            "---\nid: has-desc\ntype: Insight\ndescription: already here\n---\nBody.\n"
        )
        with patch("palinode.api.server._generate_description") as mock_desc:
            res = client.post("/generate-summaries")
        assert res.status_code == 200, res.text
        assert res.json()["descriptions_generated"] == 0
        mock_desc.assert_not_called()

    def test_backfill_counts_deferred_description_as_error(self, client, tmp_path):
        """When _generate_description defers (Ollama slow/circuit-open), the
        backfill counts it as a transient error and does not write the sentinel."""
        sub = tmp_path / "insights"
        sub.mkdir()
        fp = sub / "deferred-desc.md"
        fp.write_text("---\nid: deferred-desc\ntype: Insight\n---\nBody.\n")
        with patch("palinode.api.server._generate_description",
                   return_value=_server_mod._DESCRIPTION_DEFERRED):
            res = client.post("/generate-summaries")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["descriptions_generated"] == 0
        assert body["description_errors"] == 1
        # The sentinel object must never be written into frontmatter.
        assert "description:" not in fp.read_text()

    def test_backfill_skips_descriptions_when_disabled(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(config.auto_summary, "enabled", False)
        sub = tmp_path / "insights"
        sub.mkdir()
        fp = sub / "disabled-desc.md"
        fp.write_text("---\nid: disabled-desc\ntype: Insight\n---\nBody.\n")
        with patch("palinode.api.server._generate_description") as mock_desc:
            res = client.post("/generate-summaries")
        assert res.status_code == 200, res.text
        assert res.json()["descriptions_generated"] == 0
        mock_desc.assert_not_called()


# ---------------------------------------------------------------------------
# /status surfaces auto_summary block
# ---------------------------------------------------------------------------


class TestStatusAutoSummaryBlock:

    def test_status_includes_auto_summary_block(self, client):
        res = client.get("/status")
        assert res.status_code == 200, res.text
        body = res.json()
        assert "auto_summary" in body, body
        block = body["auto_summary"]
        for key in ("enabled", "last_run_at", "last_run_count",
                    "last_run_errors", "last_run_descriptions",
                    "last_run_description_errors", "last_error", "total_runs"):
            assert key in block, f"missing {key} in {block}"


# ---------------------------------------------------------------------------
# /health/auto-summary — status decision tree
# ---------------------------------------------------------------------------


class TestHealthAutoSummary:

    def test_ok_when_disabled(self, client, monkeypatch):
        monkeypatch.setattr(config.auto_summary, "enabled", False)
        res = client.get("/health/auto-summary")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["status"] == "ok"
        assert body["enabled"] is False

    def test_down_when_ollama_unreachable(self, client, monkeypatch):
        monkeypatch.setattr(config.auto_summary, "enabled", True)
        # Phase 5: liveness now goes through OllamaClient.ping, not httpx.get.
        fake = MagicMock(name="OllamaClient")
        fake.ping.return_value = False
        with patch("palinode.api.server.get_ollama_client", return_value=fake):
            res = client.get("/health/auto-summary")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["status"] == "down"
        assert body["ollama_reachable"] is False

    def test_ok_when_reachable_no_backlog(self, client, monkeypatch):
        monkeypatch.setattr(config.auto_summary, "enabled", True)
        fake = MagicMock(name="OllamaClient")
        fake.ping.return_value = True
        with patch("palinode.api.server.get_ollama_client", return_value=fake):
            res = client.get("/health/auto-summary")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["status"] == "ok", body
        assert body["ollama_reachable"] is True
        assert body["pending_count"] == 0
        # description backlog surfaced alongside the summary backlog.
        assert body["pending_descriptions"] == 0
        assert "last_run_descriptions" in body

    def test_auth_exempt(self, client):
        # /health/auto-summary must not require bearer auth — monitor agents
        # should be able to probe without managing a token.
        from palinode.api import server as srv
        assert "/health/auto-summary" in srv._API_EXEMPT_PATHS


# ---------------------------------------------------------------------------
# description eligibility: structural / non-memory files are excluded
# from both the pending_descriptions count and the /generate-summaries worklist
# so the backfill drains to a stable floor instead of regenerating throwaway
# descriptions forever.
# ---------------------------------------------------------------------------


class TestDescriptionEligibility:
    # Structural / non-memory locations that must never count or be backfilled.
    # (relpath under PALINODE_DIR, written without a `description:` field.)
    _STRUCTURAL = [
        ("daily", "2026-06-07.md"),
        ("archive", "old-note.md"),
        ("specs", "spec.md"),
        ("specs/prompts", "consolidation.md"),  # the live offender
    ]

    def _write(self, tmp_path, relparts, name):
        d = tmp_path
        for p in str(relparts).split("/"):
            d = d / p
        d.mkdir(parents=True, exist_ok=True)
        fp = d / name
        # Has frontmatter but no `description:` — the shape that pinned the count.
        fp.write_text("---\nid: struct\ntype: Insight\n---\nBody content.\n")
        return fp

    def test_predicate_excludes_structural_and_toplevel(self):
        # Unit-level guard on the shared predicate itself.
        is_eligible = _server_mod._is_description_eligible
        assert is_eligible("insights/foo.md") is True
        assert is_eligible("decisions/bar.md") is True
        assert is_eligible("inbox/baz.md") is True
        assert is_eligible("daily/2026-06-07.md") is False
        assert is_eligible("archive/old.md") is False
        assert is_eligible("specs/spec.md") is False
        assert is_eligible("specs/prompts/consolidation.md") is False
        assert is_eligible("README.md") is False   # top-level doc
        assert is_eligible("PROGRAM.md") is False

    def test_structural_files_not_selected_by_backfill(self, client, tmp_path):
        for relparts, name in self._STRUCTURAL:
            self._write(tmp_path, relparts, name)
        # A top-level doc too.
        (tmp_path / "README.md").write_text(
            "---\nid: readme\n---\nProject readme.\n"
        )
        with patch("palinode.api.server._generate_description") as mock_desc:
            res = client.post("/generate-summaries")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["descriptions_generated"] == 0, body
        assert body["description_errors"] == 0, body
        # The generator must never even be invoked for ineligible files —
        # that's the GPU burn
        mock_desc.assert_not_called()

    def test_structural_files_not_counted_in_pending(self, client, tmp_path, monkeypatch):
        for relparts, name in self._STRUCTURAL:
            self._write(tmp_path, relparts, name)
        (tmp_path / "README.md").write_text(
            "---\nid: readme\n---\nProject readme.\n"
        )
        monkeypatch.setattr(config.auto_summary, "enabled", True)
        fake = MagicMock(name="OllamaClient")
        fake.ping.return_value = True
        with patch("palinode.api.server.get_ollama_client", return_value=fake):
            res = client.get("/health/auto-summary")
        assert res.status_code == 200, res.text
        # A tree of only structural files missing `description` → count is 0.
        assert res.json()["pending_descriptions"] == 0, res.json()

    def test_eligible_memory_file_still_counted_and_backfilled(self, client, tmp_path, monkeypatch):
        # Regression guard: the eligibility gate must not suppress real work.
        sub = tmp_path / "insights"
        sub.mkdir()
        (sub / "real-memory.md").write_text(
            "---\nid: real-memory\ntype: Insight\n---\nA genuine memory.\n"
        )
        # Mixed in with structural noise that must be ignored.
        self._write(tmp_path, "daily", "2026-06-07.md")

        monkeypatch.setattr(config.auto_summary, "enabled", True)
        fake = MagicMock(name="OllamaClient")
        fake.ping.return_value = True
        with patch("palinode.api.server.get_ollama_client", return_value=fake):
            res = client.get("/health/auto-summary")
        assert res.json()["pending_descriptions"] == 1, res.json()

        with patch("palinode.api.server._generate_description",
                   return_value="A real description."):
            res = client.post("/generate-summaries")
        body = res.json()
        assert body["descriptions_generated"] == 1, body
        text = (sub / "real-memory.md").read_text()
        assert 'description: "A real description."' in text
