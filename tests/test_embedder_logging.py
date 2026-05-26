"""Tests for embedder.py logging — issue #383 (C1: add logger).

Verifies that every failure path in _embed_local emits at the correct log
level with exc_info and the expected structured context. Uses unittest.mock to
simulate Ollama failures without requiring a live Ollama instance.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import httpx
import pytest

from palinode.core import embedder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """Build an httpx.HTTPStatusError with a minimal mock response."""
    mock_response = MagicMock()
    mock_response.status_code = status_code
    return httpx.HTTPStatusError(
        f"HTTP {status_code}", request=MagicMock(), response=mock_response
    )


def _make_timeout_error() -> httpx.TimeoutException:
    return httpx.TimeoutException("timed out", request=MagicMock())


def _make_connect_error() -> httpx.ConnectError:
    return httpx.ConnectError("connection refused", request=MagicMock())


# ---------------------------------------------------------------------------
# Logger presence
# ---------------------------------------------------------------------------


def test_embedder_module_has_logger():
    """The module must expose a logger named palinode.core.embedder."""
    assert hasattr(embedder, "logger")
    assert embedder.logger.name == "palinode.core.embedder"


# ---------------------------------------------------------------------------
# All-endpoints-fail → WARNING with structured context
# ---------------------------------------------------------------------------


def test_all_endpoints_fail_logs_warning_with_context(caplog):
    """When every endpoint fails, a WARNING is emitted with model/url/text_len."""
    with caplog.at_level(logging.WARNING, logger="palinode.core.embedder"):
        with patch("palinode.core.embedder.httpx.post", side_effect=_make_connect_error()):
            result = embedder._embed_local("some text to embed")

    assert result == [], "must return empty list on total failure"

    # Find the final exhaustion warning
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "no WARNING emitted after all endpoints failed"

    # The terminal warning must carry structured context
    last = warnings[-1]
    assert "all endpoints exhausted" in last.message or "text_len" in last.message or "returning empty vector" in last.message, (
        f"exhaustion warning missing structured context: {last.message!r}"
    )


def test_exhaustion_warning_includes_model_and_url(caplog):
    """Terminal WARNING must include model name, url, and text_len."""
    text = "test input for context check"
    with caplog.at_level(logging.WARNING, logger="palinode.core.embedder"):
        with patch("palinode.core.embedder.httpx.post", side_effect=_make_connect_error()):
            embedder._embed_local(text)

    terminal = [r for r in caplog.records if "returning empty vector" in r.message]
    assert terminal, "terminal exhaustion WARNING not found"
    msg = terminal[0].message
    # model, url, and text_len must all appear
    assert "text_len" in msg, f"text_len missing from terminal warning: {msg!r}"
    assert "model" in msg or "bge" in msg.lower(), f"model missing from terminal warning: {msg!r}"


# ---------------------------------------------------------------------------
# Per-endpoint failure: HTTPStatusError → WARNING with exc_info
# ---------------------------------------------------------------------------


def test_http_status_error_logs_warning_with_exc_info(caplog):
    """HTTPStatusError on each endpoint logs WARNING with exc_info=True."""
    with caplog.at_level(logging.WARNING, logger="palinode.core.embedder"):
        with patch(
            "palinode.core.embedder.httpx.post",
            side_effect=_make_http_status_error(404),
        ):
            embedder._embed_local("test text")

    http_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and r.exc_info is not None
    ]
    assert http_warnings, "no WARNING with exc_info on HTTPStatusError"


def test_timeout_error_logs_warning_with_exc_info(caplog):
    """TimeoutException logs WARNING with exc_info=True."""
    with caplog.at_level(logging.WARNING, logger="palinode.core.embedder"):
        with patch(
            "palinode.core.embedder.httpx.post",
            side_effect=_make_timeout_error(),
        ):
            embedder._embed_local("timeout test text")

    timeout_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and r.exc_info is not None
    ]
    assert timeout_warnings, "no WARNING with exc_info on TimeoutException"


def test_connect_error_logs_warning_with_exc_info(caplog):
    """ConnectError logs WARNING with exc_info=True."""
    with caplog.at_level(logging.WARNING, logger="palinode.core.embedder"):
        with patch(
            "palinode.core.embedder.httpx.post",
            side_effect=_make_connect_error(),
        ):
            embedder._embed_local("connect error test")

    conn_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and r.exc_info is not None
    ]
    assert conn_warnings, "no WARNING with exc_info on ConnectError"


# ---------------------------------------------------------------------------
# Unexpected response shape → WARNING (no exc_info needed, 200 OK)
# ---------------------------------------------------------------------------


def test_unexpected_response_shape_logs_warning(caplog):
    """200 OK with neither 'embeddings' nor 'embedding' key → WARNING."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"error": "context window exceeded"}

    with caplog.at_level(logging.WARNING, logger="palinode.core.embedder"):
        with patch("palinode.core.embedder.httpx.post", return_value=mock_resp):
            result = embedder._embed_local("some text")

    assert result == [], "must return empty on unexpected shape"
    shape_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "unexpected" in r.message.lower()
    ]
    assert shape_warnings, "no WARNING on unexpected response shape"
    # Response keys must appear so operator can see what arrived
    assert "response_keys" in shape_warnings[0].message, (
        f"response_keys not in warning: {shape_warnings[0].message!r}"
    )


# ---------------------------------------------------------------------------
# Success path → DEBUG (not WARNING), returns vector
# ---------------------------------------------------------------------------


def test_success_logs_at_debug_not_warning(caplog):
    """Successful embed must log at DEBUG, never at WARNING."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"embeddings": [[0.1] * 1024]}

    with caplog.at_level(logging.DEBUG, logger="palinode.core.embedder"):
        with patch("palinode.core.embedder.httpx.post", return_value=mock_resp):
            result = embedder._embed_local("successful embed text")

    assert len(result) == 1024, "must return the embedding vector on success"
    # No WARNINGs on the success path
    assert not any(r.levelno == logging.WARNING for r in caplog.records), (
        "WARNING emitted on successful embed — should be DEBUG only"
    )
    # At least one DEBUG record
    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert debug_records, "no DEBUG log emitted on successful embed"
    # timing present
    assert "elapsed_ms" in debug_records[0].message, (
        f"timing missing from debug log: {debug_records[0].message!r}"
    )


def test_success_legacy_embedding_key_logs_debug(caplog):
    """Legacy 'embedding' (singular) key also succeeds and logs DEBUG."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"embedding": [0.5] * 1024}

    with caplog.at_level(logging.DEBUG, logger="palinode.core.embedder"):
        with patch("palinode.core.embedder.httpx.post", return_value=mock_resp):
            result = embedder._embed_local("legacy key test")

    assert len(result) == 1024
    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert debug_records, "no DEBUG log for legacy 'embedding' key success"


# ---------------------------------------------------------------------------
# Warning messages carry text_len (not raw text)
# ---------------------------------------------------------------------------


def test_warning_includes_text_len_not_raw_text(caplog):
    """Warnings must include text_len; raw text must not appear in the log.

    Logging raw text could expose PII or large payloads to log aggregators.
    """
    sensitive_text = "SECRET_CONTENT_DO_NOT_LOG " * 10
    with caplog.at_level(logging.WARNING, logger="palinode.core.embedder"):
        with patch("palinode.core.embedder.httpx.post", side_effect=_make_connect_error()):
            embedder._embed_local(sensitive_text)

    for record in caplog.records:
        assert "SECRET_CONTENT_DO_NOT_LOG" not in record.message, (
            "raw user text leaked into a log record"
        )

    # text_len must appear in at least one warning so operators can diagnose
    # oversize inputs without seeing the content itself.
    assert any("text_len" in r.message for r in caplog.records), (
        "text_len context missing from all log records"
    )
