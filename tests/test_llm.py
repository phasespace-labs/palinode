"""Tests for palinode.core.llm — LLMProvider protocol, OllamaProvider, FakeProvider."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import httpx
import pytest

from palinode.core.llm import (
    FakeProvider,
    LLMUnreachable,
    LLMTimeout,
    OllamaProvider,
)


# ---------------------------------------------------------------------------
# FakeProvider unit tests
# ---------------------------------------------------------------------------


class TestFakeProviderDefault:
    def test_returns_default_when_no_responses(self):
        fake = FakeProvider(default="fallback")
        assert fake.generate("anything") == "fallback"

    def test_returns_empty_string_by_default(self):
        fake = FakeProvider()
        assert fake.generate("anything") == ""


class TestFakeProviderPrefixMatch:
    def test_matches_prefix(self):
        fake = FakeProvider(responses={"Summarize:": "ok"})
        assert fake.generate("Summarize: this thing") == "ok"

    def test_no_match_returns_default(self):
        fake = FakeProvider(responses={"Summarize:": "ok"}, default="nope")
        assert fake.generate("Describe: something") == "nope"

    def test_longest_prefix_wins(self):
        fake = FakeProvider(
            responses={
                "Sum": "short",
                "Summarize:": "long",
                "Summarize: the": "longest",
            }
        )
        assert fake.generate("Summarize: the memory") == "longest"
        assert fake.generate("Summarize: a thing") == "long"
        assert fake.generate("Sum total") == "short"


class TestFakeProviderMaxChars:
    def test_truncates_when_max_chars_set(self):
        fake = FakeProvider(default="abcdefghij")
        assert fake.generate("prompt", max_chars=5) == "abcde"

    def test_no_truncation_when_within_limit(self):
        fake = FakeProvider(default="short")
        assert fake.generate("prompt", max_chars=100) == "short"

    def test_no_truncation_when_max_chars_none(self):
        fake = FakeProvider(default="a" * 500)
        assert len(fake.generate("prompt")) == 500


class TestFakeProviderAuditTrail:
    def test_calls_are_recorded(self):
        fake = FakeProvider()
        fake.generate("first")
        fake.generate("second")
        assert fake.calls == ["first", "second"]


# ---------------------------------------------------------------------------
# OllamaProvider unit tests (mocked httpx)
# ---------------------------------------------------------------------------


class TestOllamaProviderSuccess:
    def test_returns_cleaned_response(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": '  "hello world"  '}
        mock_resp.raise_for_status.return_value = None

        with patch("palinode.core.llm.httpx.post", return_value=mock_resp):
            provider = OllamaProvider(url="http://localhost:11434", model="test")
            result = provider.generate("test prompt")

        assert result == "hello world"

    def test_truncates_to_max_chars(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "a" * 200}
        mock_resp.raise_for_status.return_value = None

        with patch("palinode.core.llm.httpx.post", return_value=mock_resp):
            provider = OllamaProvider(url="http://localhost:11434", model="test")
            result = provider.generate("test", max_chars=50)

        assert len(result) == 50


class TestOllamaProviderErrors:
    def test_raises_unreachable_on_connection_error(self):
        with patch("palinode.core.llm.httpx.post", side_effect=httpx.ConnectError("refused")):
            provider = OllamaProvider(url="http://localhost:11434", model="test")
            with pytest.raises(LLMUnreachable):
                provider.generate("test")

    def test_raises_timeout_on_timeout(self):
        with patch("palinode.core.llm.httpx.post", side_effect=httpx.ReadTimeout("deadline")):
            provider = OllamaProvider(url="http://localhost:11434", model="test")
            with pytest.raises(LLMTimeout):
                provider.generate("test")

    def test_raises_unreachable_on_http_status_error(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock()
        )
        with patch("palinode.core.llm.httpx.post", return_value=mock_resp):
            provider = OllamaProvider(url="http://localhost:11434", model="test")
            with pytest.raises(LLMUnreachable):
                provider.generate("test")

    def test_raises_unreachable_on_json_decode_error(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.side_effect = ValueError("not json")

        with patch("palinode.core.llm.httpx.post", return_value=mock_resp):
            provider = OllamaProvider(url="http://localhost:11434", model="test")
            with pytest.raises(LLMUnreachable):
                provider.generate("test")
