"""Tests for palinode.core.summarize — LLMProvider injection paths."""
from __future__ import annotations

import pytest

from palinode.core.llm import FakeProvider, LLMUnreachable
from palinode.core.summarize import (
    extract_first_line,
    generate_description,
    generate_summary,
)


# ---------------------------------------------------------------------------
# generate_description
# ---------------------------------------------------------------------------


class TestGenerateDescription:
    def test_uses_injected_llm(self):
        fake = FakeProvider(default="A test description")
        result = generate_description("Some memory content here.", llm=fake)
        assert result == "A test description"
        assert len(fake.calls) == 1
        # Verify the prompt contains the user_content fence
        assert "<user_content>" in fake.calls[0]

    def test_falls_back_on_unreachable(self):
        """When the LLM raises LLMUnreachable, extract_first_line fires."""

        class FailingProvider:
            def generate(self, prompt, *, max_chars=None, timeout=30.0):
                raise LLMUnreachable("connection refused")

        result = generate_description(
            "First meaningful line\n\nMore content here.",
            llm=FailingProvider(),
        )
        assert result == "First meaningful line"

    def test_falls_back_on_empty_response(self):
        """An empty LLM response triggers the first-line fallback."""
        fake = FakeProvider(default="")
        result = generate_description(
            "# Header\n\nActual first line.",
            llm=fake,
        )
        # extract_first_line strips markdown headers, so "# Header" -> "Header"
        assert result == "Header"

    def test_respects_max_chars(self):
        long_reply = "x" * 300
        fake = FakeProvider(default=long_reply)
        result = generate_description("content", llm=fake)
        # generate_description passes max_chars=150 to provider.generate
        assert len(result) <= 150


class TestGenerateSummary:
    def test_uses_injected_llm(self):
        fake = FakeProvider(default="Summary of the memory.")
        result = generate_summary("Full content of memory file.", llm=fake)
        assert result == "Summary of the memory."
        assert len(fake.calls) == 1

    def test_returns_empty_on_unreachable(self):
        """generate_summary returns '' when provider is unreachable."""

        class FailingProvider:
            def generate(self, prompt, *, max_chars=None, timeout=30.0):
                raise LLMUnreachable("timeout")

        result = generate_summary("content", llm=FailingProvider())
        assert result == ""


# ---------------------------------------------------------------------------
# extract_first_line (unchanged, but verify it still works)
# ---------------------------------------------------------------------------


class TestExtractFirstLine:
    def test_basic(self):
        assert extract_first_line("hello\nworld") == "hello"

    def test_strips_header(self):
        assert extract_first_line("## Title\n\nBody text") == "Title"

    def test_empty_content(self):
        assert extract_first_line("") == ""

    def test_max_chars(self):
        assert len(extract_first_line("a" * 200, max_chars=50)) == 50
