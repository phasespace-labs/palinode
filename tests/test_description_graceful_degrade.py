"""Tests for palinode_save auto-description graceful degradation — issue #336.

Covers:
- _DESCRIPTION_DEFERRED sentinel is returned from _generate_description on timeout.
- On non-timeout failure, _generate_description returns the first-line fallback.
- /save API returns description_pending: True when description was deferred.
- /save API does NOT return description_pending when description succeeded.
- /save API does NOT return description_pending on non-timeout failure (fallback used).
- config.auto_summary.describe_timeout_seconds controls the timeout.
- PALINODE_DESCRIBE_TIMEOUT_SECONDS env var overrides the config.
- The INFO→WARNING level fix for Ollama description failures (audit Q2).

NOTE: test_api_bearer_auth.py calls importlib.reload(palinode.api.server), which
rebinds _DESCRIPTION_DEFERRED to a new object(). All sentinel access here goes
through _server_sentinel() to read the current module binding at assertion time,
not the stale pre-reload object captured at import time.
"""
from __future__ import annotations

import logging
import os
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

import palinode.api.server as _server_mod
from palinode.api.server import app
from palinode.core.config import config


def _server_sentinel() -> object:
    """Return the current _DESCRIPTION_DEFERRED from the server module.

    Reads the live binding so that post-reload sentinel identity is correct.
    """
    return _server_mod._DESCRIPTION_DEFERRED


# ---------------------------------------------------------------------------
# _generate_description — sentinel on timeout
# ---------------------------------------------------------------------------


def test_timeout_returns_deferred_sentinel():
    """TimeoutException → _DESCRIPTION_DEFERRED (not a string, not None)."""
    with patch(
        "palinode.api.server.httpx.post",
        side_effect=httpx.TimeoutException("timed out", request=MagicMock()),
    ):
        result = _server_mod._generate_description("some content to describe")

    assert result is _server_sentinel(), (
        f"Expected _DESCRIPTION_DEFERRED, got {result!r}"
    )


def test_connect_error_falls_back_to_first_line():
    """Non-timeout failure (ConnectError) → first-line fallback, not sentinel."""
    with patch(
        "palinode.api.server.httpx.post",
        side_effect=httpx.ConnectError("offline", request=MagicMock()),
    ):
        result = _server_mod._generate_description("# My Memory Title\nDetails below.")

    assert isinstance(result, str), f"Expected str fallback, got {result!r}"
    assert result == "My Memory Title"
    assert result is not _server_sentinel()


def test_success_returns_llm_string():
    """Successful LLM call returns the LLM description as a string."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"response": "A decision about storage."}

    with patch("palinode.api.server.httpx.post", return_value=mock_resp):
        result = _server_mod._generate_description("Decision to use SQLite.")

    assert result == "A decision about storage."
    assert result is not _server_sentinel()


# ---------------------------------------------------------------------------
# _generate_description — timeout uses configurable timeout, not hardcoded 15.0
# ---------------------------------------------------------------------------


def test_describe_timeout_used_from_config():
    """The timeout passed to httpx.post must come from config.auto_summary.describe_timeout_seconds."""
    posted_timeouts: list[object] = []

    def capture_post(*args, **kwargs):
        posted_timeouts.append(kwargs.get("timeout"))
        raise httpx.TimeoutException("timed out", request=MagicMock())

    with patch("palinode.api.server.httpx.post", side_effect=capture_post):
        with patch.object(config.auto_summary, "describe_timeout_seconds", 3.0):
            _server_mod._generate_description("test content")

    assert posted_timeouts, "httpx.post was not called"
    assert posted_timeouts[0] == 3.0, (
        f"Expected timeout 3.0, got {posted_timeouts[0]!r}"
    )


def test_describe_timeout_env_override(monkeypatch):
    """PALINODE_DESCRIBE_TIMEOUT_SECONDS env var is picked up in load_config."""
    from palinode.core.config import load_config
    monkeypatch.setenv("PALINODE_DESCRIBE_TIMEOUT_SECONDS", "7.5")
    cfg = load_config()
    assert cfg.auto_summary.describe_timeout_seconds == 7.5, (
        f"Expected 7.5, got {cfg.auto_summary.describe_timeout_seconds!r}"
    )


def test_describe_timeout_env_override_invalid_value_ignored(monkeypatch):
    """Invalid PALINODE_DESCRIBE_TIMEOUT_SECONDS is silently ignored (keeps default)."""
    from palinode.core.config import load_config
    monkeypatch.setenv("PALINODE_DESCRIBE_TIMEOUT_SECONDS", "not-a-number")
    cfg = load_config()
    # Should still be the default — invalid value silently discarded.
    assert cfg.auto_summary.describe_timeout_seconds == 5.0, (
        f"Expected 5.0 default, got {cfg.auto_summary.describe_timeout_seconds!r}"
    )


# ---------------------------------------------------------------------------
# /save API — description_pending in response
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient wired to tmp_path with git, embed, and scan mocked out."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    from palinode.api import server as srv
    srv._rate_counters.clear()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    srv._rate_counters.clear()


def _patch_embed():
    return patch("palinode.core.embedder.embed", return_value=[0.1] * 1024)


def _patch_scan():
    return patch("palinode.core.store.scan_memory_content", return_value=(True, "OK"))


def _patch_desc_timeout():
    """Make _generate_description return the deferred sentinel.

    Fetches the sentinel from the module at call time so post-reload tests
    still get the correct object identity.
    """
    return patch(
        "palinode.api.server._generate_description",
        return_value=_server_sentinel(),
    )


def _patch_desc_success(text: str = "A clear description."):
    """Make _generate_description return a real string."""
    return patch(
        "palinode.api.server._generate_description",
        return_value=text,
    )


def _patch_desc_fallback():
    """Make _generate_description return a first-line fallback (string, not sentinel)."""
    return patch(
        "palinode.api.server._generate_description",
        return_value="First line fallback",
    )


class TestSaveDescriptionPending:

    def test_save_returns_description_pending_true_on_timeout(self, client):
        """When description times out, response must include description_pending: True."""
        with _patch_scan(), _patch_embed(), _patch_desc_timeout():
            res = client.post(
                "/save",
                json={
                    "content": "Important decision about architecture.",
                    "type": "Decision",
                    "slug": "arch-decision",
                },
            )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body.get("description_pending") is True, (
            f"Expected description_pending: true, got: {body}"
        )

    def test_save_does_not_include_description_pending_on_success(self, client):
        """Successful description → description_pending absent (or False) in response."""
        with _patch_scan(), _patch_embed(), _patch_desc_success():
            res = client.post(
                "/save",
                json={
                    "content": "Successful save with description.",
                    "type": "Insight",
                    "slug": "successful-save",
                },
            )
        assert res.status_code == 200, res.text
        body = res.json()
        assert not body.get("description_pending"), (
            f"description_pending should be absent or False on success: {body}"
        )

    def test_save_does_not_include_description_pending_on_fallback(self, client):
        """Non-timeout failure (fallback used) → description_pending absent in response.

        The fallback path uses first-line extraction — that IS a real description,
        just not LLM-generated. The watcher should not re-schedule a retry.
        """
        with _patch_scan(), _patch_embed(), _patch_desc_fallback():
            res = client.post(
                "/save",
                json={
                    "content": "First line fallback test content.",
                    "type": "Insight",
                    "slug": "fallback-test",
                },
            )
        assert res.status_code == 200, res.text
        body = res.json()
        assert not body.get("description_pending"), (
            f"description_pending should be absent on fallback (non-timeout): {body}"
        )

    def test_save_still_returns_200_on_description_timeout(self, client):
        """Even when description times out, /save must return 200 (file is on disk)."""
        with _patch_scan(), _patch_embed(), _patch_desc_timeout():
            res = client.post(
                "/save",
                json={
                    "content": "This will be saved even if description times out.",
                    "type": "Insight",
                    "slug": "timeout-200-test",
                },
            )
        assert res.status_code == 200, res.text
        body = res.json()
        assert "file_path" in body, "file_path must be in response even on description timeout"
        assert os.path.exists(body["file_path"]), "file must be on disk even on description timeout"


# ---------------------------------------------------------------------------
# Logging level fix — audit Q2
# ---------------------------------------------------------------------------


def test_non_timeout_failure_logged_at_warning_not_info(caplog):
    """Audit Q2: Ollama description failure (non-timeout) must log at WARNING, not INFO."""
    with caplog.at_level(logging.WARNING, logger="palinode.api.server"):
        with patch(
            "palinode.api.server.httpx.post",
            side_effect=httpx.ConnectError("offline", request=MagicMock()),
        ):
            _server_mod._generate_description("some content")

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, (
        "No WARNING emitted for non-timeout Ollama description failure — "
        "should have been upgraded from INFO (audit Q2)"
    )


def test_timeout_failure_logged_at_warning(caplog):
    """Timeout also logs at WARNING (higher priority than non-timeout fallback)."""
    with caplog.at_level(logging.WARNING, logger="palinode.api.server"):
        with patch(
            "palinode.api.server.httpx.post",
            side_effect=httpx.TimeoutException("timed out", request=MagicMock()),
        ):
            _server_mod._generate_description("some content")

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "No WARNING emitted for timeout description failure"
    assert "deferred" in warning_records[0].message.lower() or "timed out" in warning_records[0].message.lower()
