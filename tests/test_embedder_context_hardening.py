"""Tests for embed-path context-window hardening — the work hardening the palinode embed
path against ollama context-window work.

As of Phase 3 of the Ollama traffic-surface hardening, `embedder._embed_local` delegates
to the centralized `OllamaClient.embed()`, which owns the dual-endpoint fallback, vector
parsing, and context-overflow detection (those mechanics are tested directly in
`tests/test_ollama_client.py`). This file covers the *embedder wrapper* contract:

- `_embed_local` re-raises `EmbeddingContextError` (does not swallow it to []).
- `_embed_local` raises `EmbeddingUnavailable` on any other `OllamaError`
  — replaces the old silent-`[]` return; see `tests/test_embedder_logging.py`
  for the accompanying WARNING log.
- `embed()` (public) propagates both `EmbeddingContextError` and
  `EmbeddingUnavailable`.
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
    EmbeddingInputError,
    EmbeddingUnavailable,
    _is_ctx_overflow_message,
    check_model_context,
)
from palinode.core.ollama_client import OllamaUnreachable


def _client_with_embed(*, embed_return=None, embed_side_effect=None):
    fake = MagicMock(name="OllamaClient")
    if embed_side_effect is not None:
        fake.embed.side_effect = embed_side_effect
    else:
        fake.embed.return_value = embed_return
    return patch("palinode.core.embedder.get_ollama_client", return_value=fake)


def _client_with_embed_many(*, embed_return=None, embed_side_effect=None):
    fake = MagicMock(name="OllamaClient")
    if embed_side_effect is not None:
        fake.embed_many.side_effect = embed_side_effect
    else:
        fake.embed_many.return_value = embed_return
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


def test_embed_local_raises_embedding_unavailable_on_ollama_error():
    """Connectivity/timeout/unexpected-shape (any OllamaError) → EmbeddingUnavailable.

    This used to return [] and let the failure travel silently into whatever
    the caller passed it to next (sqlite-vec, two modules away). Now it
    raises at the boundary instead.
    """
    with patch("palinode.core.embedder._run_preflight_once"), \
            _client_with_embed(embed_side_effect=OllamaUnreachable("offline", role="embed")):
        with pytest.raises(EmbeddingUnavailable) as ei:
            embedder._embed_local("some text")
    assert ei.value.backend == "local"
    assert ei.value.text_len == len("some text")
    assert "offline" in ei.value.cause
    # The message is the diagnostic surface an operator actually reads —
    # it must name the cause and point at a next step, not just "failed".
    assert "backend=local" in str(ei.value)
    assert "palinode doctor" in str(ei.value)


def test_embed_public_propagates_context_error():
    """embed() (public entry) also surfaces EmbeddingContextError."""
    err = EmbeddingContextError(model="bge-m3", text_len=4, ollama_message="too long for max context")
    with patch("palinode.core.embedder._run_preflight_once"), _client_with_embed(embed_side_effect=err):
        with pytest.raises(EmbeddingContextError):
            embedder.embed("test text")


def test_embed_public_propagates_embedding_unavailable():
    """embed() (public entry) also surfaces EmbeddingUnavailable."""
    with patch("palinode.core.embedder._run_preflight_once"), \
            _client_with_embed(embed_side_effect=OllamaUnreachable("offline", role="embed")):
        with pytest.raises(EmbeddingUnavailable):
            embedder.embed("test text")


def test_embed_many_public_returns_ordered_batch():
    expected = [[0.1, 0.2], [0.3, 0.4]]
    with patch("palinode.core.embedder._run_preflight_once"), \
            _client_with_embed_many(embed_return=expected):
        assert embedder.embed_many(["alpha", "beta"]) == expected


def test_embed_many_propagates_typed_input_error():
    error = EmbeddingInputError(
        model="bge-m3", text_len=9, ollama_message="unsupported value: NaN"
    )
    with patch("palinode.core.embedder._run_preflight_once"), \
            _client_with_embed_many(embed_side_effect=error):
        with pytest.raises(EmbeddingInputError):
            embedder.embed_many(["alpha", "beta"])


def test_embed_many_wraps_backend_error_with_aggregate_length():
    error = OllamaUnreachable("offline", role="embed")
    with patch("palinode.core.embedder._run_preflight_once"), \
            _client_with_embed_many(embed_side_effect=error):
        with pytest.raises(EmbeddingUnavailable) as exc_info:
            embedder.embed_many(["alpha", "beta"])
    assert exc_info.value.text_len == len("alpha") + len("beta")
    assert exc_info.value.cause == "offline"


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


# ---------------------------------------------------------------------------
# check_model_context — the live /api/show shape (parameters is a str; bge-m3
# reports bert.context_length, not llama.context_length)
# ---------------------------------------------------------------------------


_LIVE_BGE_M3_SHOW = {
    "model_info": {"bert.context_length": 8192, "llama.context_length": None},
    "parameters": "num_ctx                        4096",
}


def test_preflight_parses_parameters_string_and_runtime_wins(caplog):
    """The live shape: runtime num_ctx 4096 (a string field) beats the 8192 capability."""
    with caplog.at_level(logging.WARNING, logger="palinode.core.embedder"):
        with _client_with_show(show_return=_LIVE_BGE_M3_SHOW):
            check_model_context(min_ctx=8192)  # must not raise
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "runtime num_ctx below minimum must warn"
    assert "num_ctx=4096" in warnings[0].message
    assert "supports 8192" in warnings[0].message


def test_preflight_uses_bert_capability_when_no_parameters(caplog):
    with caplog.at_level(logging.WARNING, logger="palinode.core.embedder"):
        with _client_with_show(show_return={"model_info": {"bert.context_length": 8192}}):
            check_model_context(min_ctx=8192)
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_preflight_accepts_legacy_parameters_mapping(caplog):
    with caplog.at_level(logging.WARNING, logger="palinode.core.embedder"):
        with _client_with_show(show_return={"model_info": {}, "parameters": {"num_ctx": 2048}}):
            check_model_context(min_ctx=8192)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings and "num_ctx=2048" in warnings[0].message


def test_preflight_runtime_wins_over_llama_capability(caplog):
    with caplog.at_level(logging.WARNING, logger="palinode.core.embedder"):
        with _client_with_show(show_return={
            "model_info": {"llama.context_length": 8192},
            "parameters": "temperature 0.1\nnum_ctx 4096\n",
        }):
            check_model_context(min_ctx=8192)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings and "num_ctx=4096" in warnings[0].message


@pytest.mark.parametrize(
    "parameters",
    ["", "stop <|im_end|>", "num_ctx notanumber", 4096, ["num_ctx", "4096"]],
)
def test_parse_num_ctx_returns_none_on_unusable_input(parameters):
    import palinode.core.embedder as emb_mod

    assert emb_mod._parse_num_ctx(parameters) is None


def test_preflight_defect_cannot_take_down_an_embed(monkeypatch, caplog):
    """A raise inside the preflight costs one WARNING, never the caller's payload."""
    import palinode.core.embedder as emb_mod

    monkeypatch.setattr(emb_mod, "_preflight_done", False)

    def _boom(*a, **k):
        raise AttributeError("'str' object has no attribute 'get'")

    monkeypatch.setattr(emb_mod, "check_model_context", _boom)
    with caplog.at_level(logging.WARNING, logger="palinode.core.embedder"):
        with _client_with_embed(embed_return=[0.1] * 10):
            vec = emb_mod._embed_local("first embed of the process")
    assert vec, "the first embed must still return a vector"
    assert any("preflight" in r.message and "ignored" in r.message for r in caplog.records)
