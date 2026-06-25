"""Tests for auto-generated description field on save (M0)."""
from unittest import mock

from palinode.api.server import _generate_description, _extract_first_line
from palinode.core.ollama_client import OllamaUnreachable


def _patch_client(*, side_effect=None, response: str | None = None):
    """Patch enrichment.get_ollama_client; generate raises side_effect or returns response."""
    fake = mock.MagicMock(name="OllamaClient")
    if side_effect is not None:
        fake.generate.side_effect = side_effect
    else:
        fake.generate.return_value = {"response": response}
    return mock.patch("palinode.api.enrichment.get_ollama_client", return_value=fake)


def test_extract_first_line_basic():
    assert _extract_first_line("Hello world\nSecond line") == "Hello world"


def test_extract_first_line_strips_headers():
    assert _extract_first_line("# My Title\nBody text") == "My Title"
    assert _extract_first_line("## Sub heading\nBody") == "Sub heading"


def test_extract_first_line_skips_blank():
    assert _extract_first_line("\n\n  \nActual content") == "Actual content"


def test_extract_first_line_truncates():
    long = "A" * 300
    result = _extract_first_line(long, max_chars=150)
    assert len(result) == 150


def test_extract_first_line_empty():
    assert _extract_first_line("") == ""
    assert _extract_first_line("   \n  \n  ") == ""


def test_generate_description_fallback_on_connection_error():
    """When Ollama is unreachable, falls back to first-line extraction."""
    with _patch_client(side_effect=OllamaUnreachable("offline", role="chat")):
        result = _generate_description("Decision to use SQLite for storage.\nMore details here.")
    assert result == "Decision to use SQLite for storage."


def test_generate_description_uses_llm_when_available():
    """When Ollama responds, uses the LLM description."""
    with _patch_client(response="A decision about database storage."):
        result = _generate_description("Decision to use SQLite for storage.\nMore details here.")
    assert result == "A decision about database storage."


def test_generate_description_truncates_long_llm_response():
    """LLM responses longer than 150 chars get clipped to <= 150 chars."""
    with _patch_client(response="X" * 200):
        result = _generate_description("Some content")
    assert len(result) <= 150
    assert len(result) >= 140  # clipped near the cap, not mangled to nothing


def test_generate_description_empty_llm_response_falls_back():
    """Empty LLM response triggers first-line fallback."""
    with _patch_client(response=""):
        result = _generate_description("# Important Decision\nWe chose option A.")
    assert result == "Important Decision"
