"""Tests for embed-path context-window hardening — issue #335.

Covers:
- EmbeddingContextError is raised (not silently swallowed) when Ollama returns
  an explicit context-overflow error in a 200 OK body.
- The typed exception carries model, text_len, and ollama_message attributes.
- check_model_context() warns when num_ctx < min_ctx.
- check_model_context() does not warn when num_ctx >= min_ctx.
- check_model_context() is silent on /api/show failure (best-effort preflight).
- The preflight runs at most once per embed call (guard flag).
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import httpx
import pytest

from palinode.core import embedder
from palinode.core.embedder import (
    EmbeddingContextError,
    _is_ctx_overflow_message,
    check_model_context,
)


# ---------------------------------------------------------------------------
# _is_ctx_overflow_message
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("msg, expected", [
    ("prompt is too long for max context", True),
    ("too long for max context", True),
    ("context length exceeded", True),
    ("exceeds context", True),
    ("num_ctx", True),
    ("embedding generated successfully", False),
    ("", False),
    ("connection refused", False),
])
def test_is_ctx_overflow_message(msg, expected):
    assert _is_ctx_overflow_message(msg) is expected


# ---------------------------------------------------------------------------
# EmbeddingContextError — raised on context-overflow body
# ---------------------------------------------------------------------------


def test_context_overflow_response_raises_typed_exception():
    """Ollama 200 OK with context-overflow error body → EmbeddingContextError."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"error": "prompt is too long for max context"}

    with patch("palinode.core.embedder._run_preflight_once"):
        with patch("palinode.core.embedder.httpx.post", return_value=mock_resp):
            with pytest.raises(EmbeddingContextError) as exc_info:
                embedder._embed_local("x" * 5000)

    err = exc_info.value
    assert err.text_len == 5000
    assert "prompt is too long for max context" in err.ollama_message
    # Recovery hint must be in the message
    assert "num_ctx" in str(err).lower() or "truncate" in str(err).lower()


def test_context_overflow_exception_carries_model_attribute():
    """EmbeddingContextError.model must match the configured model."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"error": "too long for max context"}

    with patch("palinode.core.embedder._run_preflight_once"):
        with patch("palinode.core.embedder.httpx.post", return_value=mock_resp):
            with pytest.raises(EmbeddingContextError) as exc_info:
                embedder._embed_local("test")

    assert exc_info.value.model == embedder.config.embeddings.primary.model


def test_context_overflow_not_retried_on_other_endpoint():
    """EmbeddingContextError must re-raise immediately, not try the next endpoint.

    The overflow applies to the model's context, not the endpoint — retrying
    a different endpoint would just fail again with the same error.
    """
    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"error": "prompt is too long for max context"}
        return mock_resp

    with patch("palinode.core.embedder._run_preflight_once"):
        with patch("palinode.core.embedder.httpx.post", side_effect=side_effect):
            with pytest.raises(EmbeddingContextError):
                embedder._embed_local("big text")

    # Should have been called once (first endpoint), not twice.
    assert call_count == 1, (
        f"httpx.post called {call_count} times — context overflow should stop at first endpoint"
    )


def test_non_overflow_error_body_does_not_raise_typed_exception():
    """A 200 OK with a non-overflow error key → warning, not EmbeddingContextError."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"error": "model not found"}

    with patch("palinode.core.embedder._run_preflight_once"):
        with patch("palinode.core.embedder.httpx.post", return_value=mock_resp):
            # Must NOT raise EmbeddingContextError — just return [].
            result = embedder._embed_local("some text")

    assert result == [], "non-overflow error body should return empty list"


def test_context_overflow_logged_before_raise(caplog):
    """EmbeddingContextError must be raise-able by callers — verify the raise propagates."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"error": "too long for max context"}

    with patch("palinode.core.embedder._run_preflight_once"):
        with patch("palinode.core.embedder.httpx.post", return_value=mock_resp):
            with pytest.raises(EmbeddingContextError):
                embedder.embed("test text")


# ---------------------------------------------------------------------------
# check_model_context — preflight ctx check
# ---------------------------------------------------------------------------


def _make_show_resp(ctx_value):
    """Build a mock /api/show response with the given num_ctx value."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "model_info": {"llama.context_length": ctx_value},
    }
    return mock_resp


def test_preflight_warns_when_ctx_below_minimum(caplog):
    """check_model_context warns at WARNING level when num_ctx < min_ctx."""
    with caplog.at_level(logging.WARNING, logger="palinode.core.embedder"):
        with patch("palinode.core.embedder.httpx.post", return_value=_make_show_resp(4096)):
            check_model_context(min_ctx=8192)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "no WARNING emitted when num_ctx < min_ctx"
    assert "4096" in warnings[0].message, "actual ctx value missing from warning"
    assert "8192" in warnings[0].message, "minimum ctx value missing from warning"
    assert "modelfile" in warnings[0].message.lower(), "recovery hint missing from warning"
    assert "num_ctx" in warnings[0].message, "modelfile parameter name missing from warning"


def test_preflight_silent_when_ctx_meets_minimum(caplog):
    """check_model_context must NOT warn when num_ctx >= min_ctx."""
    with caplog.at_level(logging.WARNING, logger="palinode.core.embedder"):
        with patch("palinode.core.embedder.httpx.post", return_value=_make_show_resp(8192)):
            check_model_context(min_ctx=8192)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not warnings, f"spurious WARNING when num_ctx is sufficient: {warnings}"


def test_preflight_silent_on_show_failure(caplog):
    """check_model_context must not raise when /api/show is unreachable."""
    with caplog.at_level(logging.WARNING, logger="palinode.core.embedder"):
        with patch(
            "palinode.core.embedder.httpx.post",
            side_effect=httpx.ConnectError("offline", request=MagicMock()),
        ):
            # Must not raise
            check_model_context()

    # Should not emit a WARNING for a connect failure — just DEBUG.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not warnings, f"spurious WARNING on /api/show connect failure: {warnings}"


def test_preflight_silent_on_missing_ctx_key(caplog):
    """If /api/show doesn't expose num_ctx, preflight skips silently."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"model_info": {}}  # no llama.context_length

    with caplog.at_level(logging.WARNING, logger="palinode.core.embedder"):
        with patch("palinode.core.embedder.httpx.post", return_value=mock_resp):
            check_model_context()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not warnings, f"spurious WARNING when num_ctx key absent: {warnings}"


# ---------------------------------------------------------------------------
# Preflight guard — once per process
# ---------------------------------------------------------------------------


def test_preflight_runs_at_most_once_per_process(monkeypatch):
    """_run_preflight_once must call check_model_context exactly once
    regardless of how many times _embed_local is called.
    """
    import palinode.core.embedder as emb_mod

    call_log: list[int] = []

    def fake_check(*args, **kwargs):
        call_log.append(1)

    # Reset the guard so we get a clean test.
    monkeypatch.setattr(emb_mod, "_preflight_done", False)
    monkeypatch.setattr(emb_mod, "check_model_context", fake_check)

    success_resp = MagicMock()
    success_resp.raise_for_status = MagicMock()
    success_resp.json.return_value = {"embeddings": [[0.1] * 10]}

    with patch("palinode.core.embedder.httpx.post", return_value=success_resp):
        emb_mod._embed_local("call 1")
        emb_mod._embed_local("call 2")
        emb_mod._embed_local("call 3")

    assert len(call_log) == 1, (
        f"check_model_context called {len(call_log)} times — should be exactly once"
    )
