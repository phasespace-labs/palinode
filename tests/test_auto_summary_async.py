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
                    "last_run_errors", "last_error", "total_runs"):
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
        with patch("palinode.api.server.httpx.get",
                   side_effect=httpx.ConnectError("offline", request=MagicMock())):
            res = client.get("/health/auto-summary")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["status"] == "down"
        assert body["ollama_reachable"] is False

    def test_ok_when_reachable_no_backlog(self, client, monkeypatch):
        monkeypatch.setattr(config.auto_summary, "enabled", True)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("palinode.api.server.httpx.get", return_value=mock_resp):
            res = client.get("/health/auto-summary")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["status"] == "ok", body
        assert body["ollama_reachable"] is True
        assert body["pending_count"] == 0

    def test_auth_exempt(self, client):
        # /health/auto-summary must not require bearer auth — monitor agents
        # should be able to probe without managing a token.
        from palinode.api import server as srv
        assert "/health/auto-summary" in srv._BearerAuthMiddleware._AUTH_EXEMPT_PATHS
