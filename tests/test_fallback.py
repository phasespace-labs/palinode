"""Consolidation LLM fallback-chain tests.

As of #338 Phase 4, `_call_llm_with_fallback` routes through the centralized
`OllamaClient.chat_completions` (CONSOLIDATION role) instead of `httpx.post`.
The fallback chain (primary → fallbacks) stays in the runner; each attempt
passes its own `base_url`/`model` to the client, and a failed attempt raises
`OllamaError` (which the loop catches to try the next host).
"""
from unittest.mock import MagicMock, patch

import pytest

from palinode.core.config import config
from palinode.consolidation.runner import _build_model_chain, _call_llm_with_fallback
from palinode.core.ollama_client import OllamaError, OllamaTimeout


@pytest.fixture
def run_config(monkeypatch):
    monkeypatch.setattr(config.consolidation, "llm_model", "primary-model")
    monkeypatch.setattr(config.consolidation, "llm_url", "http://primary")
    monkeypatch.setattr(config.consolidation, "llm_fallbacks", [
        {"model": "fallback-1", "url": "http://fallback-1"},
        {"model": "fallback-2", "url": "http://fallback-2"},
    ])
    return config


def _patch_client(chat_side_effect):
    fake = MagicMock(name="OllamaClient")
    fake.chat_completions.side_effect = chat_side_effect
    return patch("palinode.consolidation.runner.get_ollama_client", return_value=fake), fake


def test_build_model_chain_order(run_config):
    """Chain is always: primary first, then fallbacks in config order."""
    chain = _build_model_chain()
    assert chain == [
        {"model": "primary-model", "url": "http://primary"},
        {"model": "fallback-1", "url": "http://fallback-1"},
        {"model": "fallback-2", "url": "http://fallback-2"},
    ]


def test_primary_succeeds_no_fallback(run_config):
    p, fake = _patch_client(["success"])
    with p:
        result, model = _call_llm_with_fallback("sys", "user")
    assert result == "success"
    assert model == "primary-model"
    assert fake.chat_completions.call_count == 1
    kwargs = fake.chat_completions.call_args.kwargs
    assert kwargs["base_url"] == "http://primary"
    assert kwargs["model"] == "primary-model"
    assert kwargs["retries"] == 0


def test_primary_timeout_uses_fallback(run_config):
    p, fake = _patch_client([OllamaTimeout("timeout", role="consolidation"), "fallback_success"])
    with p:
        result, model = _call_llm_with_fallback("sys", "user")
    assert result == "fallback_success"
    assert model == "fallback-1"
    assert fake.chat_completions.call_count == 2
    assert fake.chat_completions.call_args_list[0].kwargs["base_url"] == "http://primary"
    assert fake.chat_completions.call_args_list[1].kwargs["base_url"] == "http://fallback-1"


def test_all_models_fail_raises(run_config):
    p, fake = _patch_client(OllamaTimeout("timeout", role="consolidation"))
    with p:
        with pytest.raises(RuntimeError, match="All 3 models failed"):
            _call_llm_with_fallback("sys", "user")
    assert fake.chat_completions.call_count == 3


def test_fallback_logged(run_config, caplog):
    import logging
    caplog.set_level(logging.INFO)
    p, _ = _patch_client([OllamaError("boom", role="consolidation"), "fb"])
    with p:
        _call_llm_with_fallback("sys", "user")
    assert "Model primary-model @ http://primary failed" in caplog.text
    assert "Fallback model succeeded: fallback-1 @ http://fallback-1" in caplog.text


def test_empty_fallbacks_primary_only(monkeypatch):
    monkeypatch.setattr(config.consolidation, "llm_model", "primary")
    monkeypatch.setattr(config.consolidation, "llm_url", "http://primary")
    monkeypatch.setattr(config.consolidation, "llm_fallbacks", [])
    p, fake = _patch_client(OllamaTimeout("timeout", role="consolidation"))
    with p:
        with pytest.raises(RuntimeError, match="All 1 models failed"):
            _call_llm_with_fallback("sys", "user")
    assert fake.chat_completions.call_count == 1


def test_model_used_returned(run_config):
    p, fake = _patch_client([
        OllamaTimeout("t", role="consolidation"),
        OllamaTimeout("t", role="consolidation"),
        "fb2",
    ])
    with p:
        _, model = _call_llm_with_fallback("sys", "user")
    assert model == "fallback-2"
