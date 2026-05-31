"""Tests for embed-path context-window hardening — issue #335.

As of #338 Phase 3, `embedder._embed_local` delegates to the centralized
`OllamaClient.embed()`, which owns the dual-endpoint fallback, vector parsing,
and context-overflow detection (those mechanics are tested directly in
`tests/test_ollama_client.py`). This file covers the *embedder wrapper* contract:

- `_embed_local` re-raises `EmbeddingContextError` (does not swallow it to []).
- `_embed_local` returns [] on any other `OllamaError`.
- `embed()` (public) propagates `EmbeddingContextError`.
- `_is_ctx_overflow_message` (re-exported from ollama_client) classifies correctly.
- `check_model_context()` warns / stays silent based on the client's /api/show.
- The preflight runs at most once per process.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from palinode.core import embedder
from palinode.core.embedder import (
    EmbeddingContextError,
    _is_ctx_overflow_message,
    check_model_context,
)
from palinode.core.ollama_client import OllamaError, OllamaUnreachable


def _client_with_embed(*, embed_return=None, embed_side_effect=None):
    fake = MagicMock(name="OllamaClient")
    if embed_side_effect is not None:
        fake.embed.side_effect = embed_side_effect
    else:
        fake.embed.return_value = embed_return
    return patch("palinode.core.embedder.get_ollama_client", return_value=fake)


def _client_with_show(*, show_return=None, show_side_effect=None):
    fake = MagicMock(name="OllamaClient")
    if show_side_effect is not None:
        fake.show.side_effect = show_side_effect
    else:
        fake.show.return_value = show_return
    return patch("palinode.core.embedder.get_ollama_client", return_value=fake)


# ---------------------------------------------------------------------------
# _is_ctx_overflow_message (re-exported helper)
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
# _embed_local wrapper — propagates EmbeddingContextError, [] on other errors
# ---------------------------------------------------------------------------


def test_embed_local_propagates_context_error():
    """A ctx overflow from the client must propagate, not degrade to []."""
    err = EmbeddingContextError(model="bge-m3", text_len=5000, ollama_message="prompt is too long")
    with patch("palinode.core.embedder._run_preflight_once"), _client_with_embed(embed_side_effect=err):
        with pytest.raises(EmbeddingContextError) as ei:
            embedder._embed_local("x" * 5000)
    assert ei.value.text_len == 5000
    assert "too long" in ei.value.ollama_message
    assert "num_ctx" in str(ei.value).lower() or "truncate" in str(ei.value).lower()


def test_embed_local_returns_empty_on_ollama_error():
    """Connectivity/timeout/unexpected-shape (any OllamaError) → []."""
    with patch("palinode.core.embedder._run_preflight_once"), \
            _client_with_embed(embed_side_effect=OllamaUnreachable("offline", role="embed")):
        result = embedder._embed_local("some text")
    assert result == []


def test_embed_public_propagates_context_error():
    """embed() (public entry) also surfaces EmbeddingContextError."""
    err = EmbeddingContextError(model="bge-m3", text_len=4, ollama_message="too long for max context")
    with patch("palinode.core.embedder._run_preflight_once"), _client_with_embed(embed_side_effect=err):
        with pytest.raises(EmbeddingContextError):
            embedder.embed("test text")


# ---------------------------------------------------------------------------
# check_model_context — preflight ctx check (now via client.show)
# ---------------------------------------------------------------------------


def _show_resp(ctx_value):
    return {"model_info": {"llama.context_length": ctx_value}}


def test_preflight_warns_when_ctx_below_minimum(caplog):
    with caplog.at_level(logging.WARNING, logger="palinode.core.embedder"):
        with _client_with_show(show_return=_show_resp(4096)):
            check_model_context(min_ctx=8192)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "no WARNING emitted when num_ctx < min_ctx"
    assert "4096" in warnings[0].message
    assert "8192" in warnings[0].message
    assert "modelfile" in warnings[0].message.lower()
    assert "num_ctx" in warnings[0].message


def test_preflight_silent_when_ctx_meets_minimum(caplog):
    with caplog.at_level(logging.WARNING, logger="palinode.core.embedder"):
        with _client_with_show(show_return=_show_resp(8192)):
            check_model_context(min_ctx=8192)
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_preflight_silent_on_show_failure(caplog):
    with caplog.at_level(logging.WARNING, logger="palinode.core.embedder"):
        with _client_with_show(show_side_effect=OllamaUnreachable("offline", role="embed")):
            check_model_context()  # must not raise
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_preflight_silent_on_missing_ctx_key(caplog):
    with caplog.at_level(logging.WARNING, logger="palinode.core.embedder"):
        with _client_with_show(show_return={"model_info": {}}):
            check_model_context()
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


# ---------------------------------------------------------------------------
# Preflight guard — once per process
# ---------------------------------------------------------------------------


def test_preflight_runs_at_most_once_per_process(monkeypatch):
    import palinode.core.embedder as emb_mod

    call_log: list[int] = []
    monkeypatch.setattr(emb_mod, "_preflight_done", False)
    monkeypatch.setattr(emb_mod, "check_model_context", lambda *a, **k: call_log.append(1))

    with _client_with_embed(embed_return=[0.1] * 10):
        emb_mod._embed_local("call 1")
        emb_mod._embed_local("call 2")
        emb_mod._embed_local("call 3")

    assert len(call_log) == 1
