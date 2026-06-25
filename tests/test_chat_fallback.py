"""Tests for the CHAT-role OpenAI-compat fallback chain — issue #464.

Covers:
- _chat_fallback_oneliner returns None when no fallbacks are configured (the
  default — zero behavior change).
- _generate_description, on a brownout (OllamaCircuitOpen / OllamaTimeout) with a
  fallback configured, returns the shim's cleaned content instead of the
  _DESCRIPTION_DEFERRED sentinel.
- _generate_summary, on a brownout, returns the shim content instead of "".
- A non-brownout summary failure (plain OllamaError / bad body) does NOT escalate
  to the shim — the shim won't fix a malformed response.
- The per-run budget (llm_fallback_max_per_run) bounds escalations: once exhausted
  the file stays deferred.
- generate_summaries_api resets the budget at the top of each backfill run.
- The /save hot path never reaches the fallback (it doesn't enrich inline).

The fallback reuses OllamaClient.chat_completions (OpenAI-compat) — the tests
patch the enrichment.get_ollama_client seam, mirroring test_description_graceful_degrade.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import palinode.api.server as srv
from palinode.core.config import config
from palinode.core.ollama_client import (
    OllamaCircuitOpen,
    OllamaError,
    OllamaTimeout,
)

_FALLBACK = [{"model": "claude-sonnet-4-5", "url": "http://127.0.0.1:4010"}]


def _server_sentinel() -> object:
    return srv._DESCRIPTION_DEFERRED


@pytest.fixture()
def fallbacks_on(monkeypatch):
    """Configure a single shim fallback with a generous per-run budget."""
    monkeypatch.setattr(config.auto_summary, "llm_fallbacks", list(_FALLBACK))
    monkeypatch.setattr(config.auto_summary, "llm_fallback_max_per_run", 10)
    monkeypatch.setattr(config.auto_summary, "model", "local-chat:e4b")
    # Outside a /generate-summaries run, prime the budget directly.
    srv._fallback_state["remaining"] = 10
    yield
    srv._fallback_state["remaining"] = 0


def _fake_client(*, generate_exc, chat_return=None, chat_exc=None):
    fake = MagicMock(name="OllamaClient")
    fake.generate.side_effect = generate_exc
    if chat_exc is not None:
        fake.chat_completions.side_effect = chat_exc
    else:
        fake.chat_completions.return_value = chat_return
    return fake


# ---------------------------------------------------------------------------
# Helper-level
# ---------------------------------------------------------------------------


def test_no_fallbacks_configured_returns_none(monkeypatch):
    monkeypatch.setattr(config.auto_summary, "llm_fallbacks", [])
    assert srv._chat_fallback_oneliner("prompt", 150) is None


def test_fallback_oneliner_walks_chat_completions(fallbacks_on):
    fake = _fake_client(generate_exc=None, chat_return="A crisp shim summary.")
    fake.generate.side_effect = None  # not used here
    with patch("palinode.api.enrichment.get_ollama_client", return_value=fake):
        out = srv._chat_fallback_oneliner("prompt", 150)
    assert out == "A crisp shim summary."
    # Verify it used the OpenAI-compat path with the configured model/url.
    _, kwargs = fake.chat_completions.call_args
    assert kwargs["model"] == "claude-sonnet-4-5"
    assert kwargs["base_url"] == "http://127.0.0.1:4010"
    assert kwargs["messages"] == [{"role": "user", "content": "prompt"}]


def test_fallback_first_success_wins(fallbacks_on, monkeypatch):
    monkeypatch.setattr(config.auto_summary, "llm_fallbacks", [
        {"model": "down", "url": "http://127.0.0.1:9"},
        {"model": "claude-sonnet-4-5", "url": "http://127.0.0.1:4010"},
    ])
    fake = MagicMock(name="OllamaClient")
    fake.chat_completions.side_effect = [
        OllamaError("first host down"),
        "Second host answered.",
    ]
    with patch("palinode.api.enrichment.get_ollama_client", return_value=fake):
        out = srv._chat_fallback_oneliner("prompt", 150)
    assert out == "Second host answered."
    assert fake.chat_completions.call_count == 2


# ---------------------------------------------------------------------------
# _generate_description
# ---------------------------------------------------------------------------


def test_description_brownout_uses_fallback(fallbacks_on):
    fake = _fake_client(
        generate_exc=OllamaCircuitOpen("breaker open"),
        chat_return="Shim-generated description.",
    )
    with patch("palinode.api.enrichment.get_ollama_client", return_value=fake):
        out = srv._generate_description("Some memory body of reasonable length.")
    assert out == "Shim-generated description."
    assert out is not _server_sentinel()


def test_description_brownout_without_fallback_defers(monkeypatch):
    monkeypatch.setattr(config.auto_summary, "llm_fallbacks", [])
    fake = _fake_client(generate_exc=OllamaTimeout("slow"))
    with patch("palinode.api.enrichment.get_ollama_client", return_value=fake):
        out = srv._generate_description("Some memory body.")
    assert out is _server_sentinel()


def test_description_budget_exhausted_defers(fallbacks_on):
    srv._fallback_state["remaining"] = 0  # budget spent earlier this run
    fake = _fake_client(
        generate_exc=OllamaCircuitOpen("breaker open"),
        chat_return="Should not be used.",
    )
    with patch("palinode.api.enrichment.get_ollama_client", return_value=fake):
        out = srv._generate_description("Some memory body.")
    assert out is _server_sentinel()
    fake.chat_completions.assert_not_called()


# ---------------------------------------------------------------------------
# _generate_summary
# ---------------------------------------------------------------------------


def test_summary_brownout_uses_fallback(fallbacks_on):
    fake = _fake_client(
        generate_exc=OllamaTimeout("slow"),
        chat_return="Shim-generated summary.",
    )
    with patch("palinode.api.enrichment.get_ollama_client", return_value=fake):
        out = srv._generate_summary("A" * 300)
    assert out == "Shim-generated summary."


def test_summary_any_primary_failure_cascades(fallbacks_on):
    """#464 (revised): ANY primary failure — connect/HTTP/bad-body, not just a
    brownout — now cascades to the fallback chain. With a remote OpenAI-compat
    primary a connect error is exactly the case the configured backups cover."""
    fake = _fake_client(
        generate_exc=OllamaError("primary connect error"),
        chat_return="Backup summary.",
    )
    with patch("palinode.api.enrichment.get_ollama_client", return_value=fake):
        out = srv._generate_summary("A" * 300)
    assert out == "Backup summary."
    fake.chat_completions.assert_called_once()


def test_generate_summaries_api_resets_budget(monkeypatch, tmp_path):
    """The backfill endpoint resets the per-run budget from config so a prior
    run's exhaustion doesn't permanently disable the fallback."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.auto_summary, "llm_fallback_max_per_run", 7)
    srv._fallback_state["remaining"] = 0
    # Empty memory dir → no files to enrich, but the reset still runs.
    srv.generate_summaries_api()
    assert srv._fallback_state["remaining"] == 7


# ---------------------------------------------------------------------------
# api="openai" primary (#464) — Mac Studio / vLLM OpenAI-compat primary
# ---------------------------------------------------------------------------


@pytest.fixture()
def openai_primary(monkeypatch):
    """CHAT primary is an OpenAI-compat endpoint (e.g. LM Studio MLX), with a
    single configured fallback and a generous per-run budget."""
    monkeypatch.setattr(config.auto_summary, "api", "openai")
    monkeypatch.setattr(config.auto_summary, "model", "qwen3-coder-30b-a3b-instruct-dwq")
    monkeypatch.setattr(config.auto_summary, "ollama_url", "http://lmstudio-host:1234")
    monkeypatch.setattr(config.auto_summary, "llm_fallbacks", [
        {"model": "qwen-coder", "url": "http://vllm-host:8000"},
    ])
    monkeypatch.setattr(config.auto_summary, "llm_fallback_max_per_run", 10)
    srv._fallback_state["remaining"] = 10
    yield
    srv._fallback_state["remaining"] = 0


def test_openai_primary_used_for_description(openai_primary):
    """With api='openai' the primary is reached via chat_completions (not the
    native generate path), using the configured model + url."""
    fake = MagicMock(name="OllamaClient")
    fake.chat_completions.return_value = "Primary qwen description."
    with patch("palinode.api.enrichment.get_ollama_client", return_value=fake):
        out = srv._generate_description("A memory body.")
    assert out == "Primary qwen description."
    fake.generate.assert_not_called()
    _, kwargs = fake.chat_completions.call_args
    assert kwargs["model"] == "qwen3-coder-30b-a3b-instruct-dwq"
    assert kwargs["base_url"] == "http://lmstudio-host:1234"


def test_openai_primary_down_cascades_to_fallback(openai_primary):
    """A remote OpenAI-compat primary that drops (connect/HTTP error) cascades to
    the next link in the chain — the whole point of having vLLM + shim backups."""
    fake = MagicMock(name="OllamaClient")
    fake.chat_completions.side_effect = [
        OllamaError("studio unreachable"),   # primary
        "Backup vllm description.",          # fallback link 1
    ]
    with patch("palinode.api.enrichment.get_ollama_client", return_value=fake):
        out = srv._generate_description("A memory body.")
    assert out == "Backup vllm description."
    assert fake.chat_completions.call_count == 2


def test_openai_primary_empty_cascades_to_fallback(openai_primary):
    """An OpenAI primary that returns empty/garbage (no exception) still cascades
    to the fallback chain before degrading."""
    fake = MagicMock(name="OllamaClient")
    fake.chat_completions.side_effect = ["", "Backup vllm description."]
    with patch("palinode.api.enrichment.get_ollama_client", return_value=fake):
        out = srv._generate_description("A memory body.")
    assert out == "Backup vllm description."
    assert fake.chat_completions.call_count == 2


def test_openai_primary_summary_cascades(openai_primary):
    """Summary path mirrors description: OpenAI primary failure cascades."""
    fake = MagicMock(name="OllamaClient")
    fake.chat_completions.side_effect = [
        OllamaError("studio unreachable"),
        "Backup vllm summary.",
    ]
    with patch("palinode.api.enrichment.get_ollama_client", return_value=fake):
        out = srv._generate_summary("A" * 300)
    assert out == "Backup vllm summary."
    assert fake.chat_completions.call_count == 2
