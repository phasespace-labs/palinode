"""Tests for embedder.py logging — issue #383 (C1) + #338 Phase 3.

As of #338 Phase 3, `_embed_local` delegates to the centralized `OllamaClient`,
which owns the per-call structured JSON logging (the `palinode.ollama.events`
logger — covered in tests/test_ollama_client.py). What remains at the embedder
level is a single summary WARNING when an embed degrades to [] — and the
privacy invariant that logs never carry raw user text, only `text_len`.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from palinode.core import embedder
from palinode.core.ollama_client import OllamaTimeout, OllamaUnreachable


def _client_with_embed(*, embed_return=None, embed_side_effect=None):
    fake = MagicMock(name="OllamaClient")
    if embed_side_effect is not None:
        fake.embed.side_effect = embed_side_effect
    else:
        fake.embed.return_value = embed_return
    return patch("palinode.core.embedder.get_ollama_client", return_value=fake)


# ---------------------------------------------------------------------------
# Logger presence
# ---------------------------------------------------------------------------


def test_embedder_module_has_logger():
    assert hasattr(embedder, "logger")
    assert embedder.logger.name == "palinode.core.embedder"


# ---------------------------------------------------------------------------
# Failure → [] + a single WARNING with structured context (model, text_len)
# ---------------------------------------------------------------------------


def test_embed_failure_returns_empty_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="palinode.core.embedder"):
        with patch("palinode.core.embedder._run_preflight_once"), \
                _client_with_embed(embed_side_effect=OllamaUnreachable("offline", role="embed")):
            result = embedder._embed_local("some text to embed")
    assert result == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "no WARNING emitted on embed failure"
    msg = warnings[-1].message
    assert "returning empty vector" in msg
    assert "text_len" in msg
    assert "model" in msg or "bge" in msg.lower()


def test_embed_failure_warning_on_timeout(caplog):
    with caplog.at_level(logging.WARNING, logger="palinode.core.embedder"):
        with patch("palinode.core.embedder._run_preflight_once"), \
                _client_with_embed(embed_side_effect=OllamaTimeout("slow", role="embed")):
            result = embedder._embed_local("timeout test text")
    assert result == []
    assert [r for r in caplog.records if r.levelno == logging.WARNING]


# ---------------------------------------------------------------------------
# Success → returns vector, no WARNING
# ---------------------------------------------------------------------------


def test_embed_success_returns_vector_no_warning(caplog):
    with caplog.at_level(logging.DEBUG, logger="palinode.core.embedder"):
        with patch("palinode.core.embedder._run_preflight_once"), \
                _client_with_embed(embed_return=[0.1] * 1024):
            result = embedder._embed_local("successful embed text")
    assert len(result) == 1024
    assert not any(r.levelno == logging.WARNING for r in caplog.records), (
        "WARNING emitted on successful embed"
    )


# ---------------------------------------------------------------------------
# Privacy — raw text never logged; text_len carries the diagnostic
# ---------------------------------------------------------------------------


def test_warning_includes_text_len_not_raw_text(caplog):
    """Logs must carry text_len, never the raw input (PII / payload safety)."""
    sensitive_text = "SECRET_CONTENT_DO_NOT_LOG " * 10
    # Capture both the embedder logger and the client event logger.
    with caplog.at_level(logging.DEBUG):
        with patch("palinode.core.embedder._run_preflight_once"), \
                _client_with_embed(embed_side_effect=OllamaUnreachable("offline", role="embed")):
            embedder._embed_local(sensitive_text)
    for record in caplog.records:
        assert "SECRET_CONTENT_DO_NOT_LOG" not in record.getMessage(), (
            "raw user text leaked into a log record"
        )
    assert any("text_len" in r.getMessage() for r in caplog.records), (
        "text_len context missing from all log records"
    )
