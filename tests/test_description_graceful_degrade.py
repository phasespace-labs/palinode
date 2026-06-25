"""Tests for palinode_save auto-description graceful degradation — issue #336.

Covers:
- _DESCRIPTION_DEFERRED sentinel is returned from _generate_description on timeout
  (now an OllamaTimeout from the centralized client) AND on circuit-open (#338).
- On non-timeout failure, _generate_description returns the first-line fallback.
- /save API returns description_pending: True when description was deferred.
- /save API does NOT return description_pending when description succeeded.
- /save API does NOT return description_pending on non-timeout failure (fallback used).
- config.auto_summary.describe_timeout_seconds controls the timeout passed to the client.
- PALINODE_DESCRIBE_TIMEOUT_SECONDS env var overrides the config.
- The INFO→WARNING level fix for Ollama description failures (audit Q2).
- _clean_llm_oneliner preamble-strip + clean length-clip (#338 Phase 2 auto_summary UX).

As of #338 Phase 2, _generate_description / _generate_summary route through
palinode.core.ollama_client.get_ollama_client() rather than calling httpx.post
directly, so these tests patch the client seam (enrichment.get_ollama_client).

NOTE: test_api_bearer_auth.py calls importlib.reload(palinode.api.server), which
rebinds _DESCRIPTION_DEFERRED to a new object(). All sentinel access here goes
through _server_sentinel() to read the current module binding at assertion time.
"""
from __future__ import annotations

import logging
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import palinode.api.server as _server_mod
from palinode.api.server import _clean_llm_oneliner, app
from palinode.core.config import config
from palinode.core.ollama_client import (
    OllamaCircuitOpen,
    OllamaError,
    OllamaTimeout,
    OllamaUnreachable,
)


def _server_sentinel() -> object:
    """Return the current _DESCRIPTION_DEFERRED from the server module."""
    return _server_mod._DESCRIPTION_DEFERRED


def _patch_client(*, side_effect=None, response: str | None = None):
    """Patch enrichment.get_ollama_client to return a fake whose .generate is configured.

    ``side_effect`` raises (e.g. OllamaTimeout); ``response`` returns
    ``{"response": response}`` from generate().
    """
    fake = MagicMock(name="OllamaClient")
    if side_effect is not None:
        fake.generate.side_effect = side_effect
    else:
        fake.generate.return_value = {"response": response}
    return patch("palinode.api.enrichment.get_ollama_client", return_value=fake), fake


# ---------------------------------------------------------------------------
# _generate_description — sentinel on timeout / circuit-open
# ---------------------------------------------------------------------------


def test_timeout_returns_deferred_sentinel():
    """OllamaTimeout → _DESCRIPTION_DEFERRED (not a string, not None)."""
    p, _ = _patch_client(side_effect=OllamaTimeout("timed out", role="chat"))
    with p:
        result = _server_mod._generate_description("some content to describe")
    assert result is _server_sentinel(), f"Expected _DESCRIPTION_DEFERRED, got {result!r}"


def test_circuit_open_returns_deferred_sentinel():
    """#338: a known-bad host (circuit open) also defers, like a timeout."""
    p, _ = _patch_client(side_effect=OllamaCircuitOpen("circuit open", role="chat"))
    with p:
        result = _server_mod._generate_description("some content to describe")
    assert result is _server_sentinel(), f"Expected _DESCRIPTION_DEFERRED, got {result!r}"


def test_connect_error_falls_back_to_first_line():
    """Non-timeout failure (unreachable) → first-line fallback, not sentinel."""
    p, _ = _patch_client(side_effect=OllamaUnreachable("offline", role="chat"))
    with p:
        result = _server_mod._generate_description("# My Memory Title\nDetails below.")
    assert isinstance(result, str), f"Expected str fallback, got {result!r}"
    assert result == "My Memory Title"
    assert result is not _server_sentinel()


def test_success_returns_llm_string():
    """Successful LLM call returns the (cleaned) description as a string."""
    p, _ = _patch_client(response="A decision about storage.")
    with p:
        result = _server_mod._generate_description("Decision to use SQLite.")
    assert result == "A decision about storage."
    assert result is not _server_sentinel()


# ---------------------------------------------------------------------------
# _generate_description — timeout + single-shot are passed to the client
# ---------------------------------------------------------------------------


def test_describe_timeout_and_retries_passed_to_client():
    """The client must be called with the configured timeout and retries=0 (#336)."""
    p, fake = _patch_client(side_effect=OllamaTimeout("t", role="chat"))
    with p:
        with patch.object(config.auto_summary, "describe_timeout_seconds", 3.0):
            _server_mod._generate_description("test content")
    assert fake.generate.called, "client.generate was not called"
    kwargs = fake.generate.call_args.kwargs
    assert kwargs.get("timeout") == 3.0, f"Expected timeout 3.0, got {kwargs.get('timeout')!r}"
    assert kwargs.get("retries") == 0, "inline description must be single-shot (retries=0)"


def test_describe_timeout_env_override(monkeypatch):
    """PALINODE_DESCRIBE_TIMEOUT_SECONDS env var is picked up in load_config."""
    from palinode.core.config import load_config
    monkeypatch.setenv("PALINODE_DESCRIBE_TIMEOUT_SECONDS", "7.5")
    cfg = load_config()
    assert cfg.auto_summary.describe_timeout_seconds == 7.5


def test_describe_timeout_env_override_invalid_value_ignored(monkeypatch):
    """Invalid PALINODE_DESCRIBE_TIMEOUT_SECONDS is silently ignored (keeps default)."""
    from palinode.core.config import load_config
    monkeypatch.setenv("PALINODE_DESCRIBE_TIMEOUT_SECONDS", "not-a-number")
    cfg = load_config()
    assert cfg.auto_summary.describe_timeout_seconds == 5.0


# ---------------------------------------------------------------------------
# _clean_llm_oneliner — preamble strip + clean clip (#338 Phase 2)
# ---------------------------------------------------------------------------


class TestCleanLlmOneliner:

    def test_passthrough_when_clean_and_short(self):
        assert _clean_llm_oneliner("A decision about storage.", 150) == "A decision about storage."

    def test_strips_the_memory_describes_preamble(self):
        out = _clean_llm_oneliner("The memory describes a centralized client.", 150)
        assert out == "A centralized client."

    def test_strips_here_is_the_summary_preamble(self):
        out = _clean_llm_oneliner("Here is the summary: adopt the client.", 150)
        assert out == "Adopt the client."

    def test_strips_label_prefix(self):
        assert _clean_llm_oneliner("Summary: use SQLite.", 150) == "Use SQLite."

    def test_leaves_legitimate_subject_alone(self):
        # "The system decided ..." is a fine summary — must NOT be stripped.
        s = "The system decided to consolidate Ollama traffic."
        assert _clean_llm_oneliner(s, 150) == s

    def test_clips_overshoot_at_word_boundary_not_midword(self):
        long = ("A centralized OllamaClient was adopted to manage all traffic, "
                "adding resilience via circuit breakers and retries everywhere now.")
        out = _clean_llm_oneliner(long, 60)
        assert len(out) <= 60
        assert out.endswith("…")
        assert "  " not in out
        # last visible word is not chopped mid-token
        assert not out[:-1].rstrip().endswith("-")

    def test_prefers_sentence_boundary_when_present(self):
        s = "Adopt the client. It also adds retries and circuit breaking for resilience."
        out = _clean_llm_oneliner(s, 40)
        assert out == "Adopt the client."

    def test_empty_input_returns_empty(self):
        assert _clean_llm_oneliner("", 150) == ""
        assert _clean_llm_oneliner("   ", 150) == ""


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
    """Make _generate_description return the deferred sentinel."""
    return patch(
        "palinode.api.server._generate_description",
        return_value=_server_sentinel(),
    )


def _patch_desc_success(text: str = "A clear description."):
    return patch("palinode.api.server._generate_description", return_value=text)


def _patch_desc_fallback():
    return patch("palinode.api.server._generate_description", return_value="First line fallback")


class TestSaveDescriptionPending:
    """#405: description generation is fully deferred off the /save hot path.

    /save never calls _generate_description inline — it sets description_pending
    for eligible files and the watcher-driven /generate-summaries backfill lands
    the description later. (Pre-#405, /save called the LLM inline with a #336
    timeout/circuit-breaker, which still blocked up to describe_timeout_seconds
    on a warm-but-slow model.)
    """

    def test_save_does_not_invoke_generate_description(self, client):
        """The /save hot path must NOT call the description LLM (#405)."""
        with _patch_scan(), _patch_embed(), \
                patch("palinode.api.server._generate_description") as mock_desc:
            res = client.post(
                "/save",
                json={
                    "content": "Important decision about architecture.",
                    "type": "Decision",
                    "slug": "arch-decision",
                },
            )
        assert res.status_code == 200, res.text
        mock_desc.assert_not_called()

    def test_save_returns_description_pending_true_for_eligible(self, client):
        """Eligible save (enabled, no description provided) → description_pending."""
        with _patch_scan(), _patch_embed():
            res = client.post(
                "/save",
                json={
                    "content": "Important decision about architecture.",
                    "type": "Decision",
                    "slug": "pending-true",
                },
            )
        assert res.status_code == 200, res.text
        assert res.json().get("description_pending") is True

    def test_save_omits_description_pending_when_description_provided(self, client):
        """A caller-supplied description is respected; no deferral."""
        with _patch_scan(), _patch_embed():
            res = client.post(
                "/save",
                json={
                    "content": "Save with a provided description.",
                    "type": "Insight",
                    "slug": "provided-desc",
                    "metadata": {"description": "Caller-supplied description."},
                },
            )
        assert res.status_code == 200, res.text
        assert not res.json().get("description_pending")

    def test_save_omits_description_pending_when_auto_summary_disabled(self, client, monkeypatch):
        """auto_summary.enabled is the master switch — disabled → no description work."""
        monkeypatch.setattr(config.auto_summary, "enabled", False)
        with _patch_scan(), _patch_embed(), \
                patch("palinode.api.server._generate_description") as mock_desc:
            res = client.post(
                "/save",
                json={
                    "content": "Save with auto_summary disabled.",
                    "type": "Insight",
                    "slug": "disabled-desc",
                },
            )
        assert res.status_code == 200, res.text
        assert not res.json().get("description_pending")
        mock_desc.assert_not_called()

    def test_save_still_returns_200_without_blocking_on_llm(self, client):
        """/save commits the file and returns 200 with no inline LLM call (#405)."""
        with _patch_scan(), _patch_embed(), \
                patch("palinode.api.server._generate_description") as mock_desc:
            res = client.post(
                "/save",
                json={
                    "content": "This is saved without waiting on any model.",
                    "type": "Insight",
                    "slug": "no-block-test",
                },
            )
        assert res.status_code == 200, res.text
        body = res.json()
        assert "file_path" in body
        assert os.path.exists(body["file_path"])
        mock_desc.assert_not_called()


# ---------------------------------------------------------------------------
# Logging level fix — audit Q2
# ---------------------------------------------------------------------------


def test_non_timeout_failure_logged_at_warning_not_info(caplog):
    """Audit Q2: Ollama description failure (non-timeout) must log at WARNING."""
    p, _ = _patch_client(side_effect=OllamaUnreachable("offline", role="chat"))
    with caplog.at_level(logging.WARNING, logger="palinode.api.server"):
        with p:
            _server_mod._generate_description("some content")
    assert [r for r in caplog.records if r.levelno == logging.WARNING], (
        "No WARNING emitted for non-timeout Ollama description failure"
    )


def test_timeout_failure_logged_at_warning(caplog):
    """Timeout also logs at WARNING."""
    p, _ = _patch_client(side_effect=OllamaTimeout("timed out", role="chat"))
    with caplog.at_level(logging.WARNING, logger="palinode.api.server"):
        with p:
            _server_mod._generate_description("some content")
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "No WARNING emitted for timeout description failure"
    msg = warning_records[0].message.lower()
    assert "deferred" in msg or "circuit" in msg or "slow" in msg


def test_generate_summary_returns_empty_on_error():
    """_generate_summary returns '' on any OllamaError (timeout/circuit/unreachable)."""
    p, _ = _patch_client(side_effect=OllamaError("boom", role="chat"))
    with p:
        assert _server_mod._generate_summary("some content to summarize") == ""


def test_generate_summary_cleans_and_clips():
    """_generate_summary routes a successful response through _clean_llm_oneliner."""
    p, _ = _patch_client(response="The memory describes a clean summary path.")
    with p:
        out = _server_mod._generate_summary("content")
    assert not out.lower().startswith("the memory")
    assert out == "A clean summary path."
